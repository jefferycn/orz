"""Event-driven watcher — the main function of orz (replaces ``listen.sh``).

The container's default command is ``watch``; the CLI commands
(``create`` / ``folder`` / ``scan`` / ``dry-run`` / ``refresh``) exist
only for manual testing and troubleshooting.

Why inotify, not polling: polling re-reads every directory every
30 s whether anything changed or not.  inotify (``app/inotify.py``,
ctypes on libc, no extra binary) only wakes the process when a
file or directory actually changes — one event per change, and a
whole batch dropped in together is processed together once the
files are stable.  The old PHP ``listen.sh`` used ``inotifywait``
for the same reason; this is the same kernel mechanism, called
directly.  Polling (``Watcher``) is kept as a fallback when
inotify is unavailable (``ORZ_WATCH_MODE=poll``).

How it works
------------
* Startup: one catch-up scan (the polling ``scan_once``), so
  anything that arrived while the container was down is processed
  at once — but only directories that *changed* since the last
  recorded snapshot (``<show>/.orz/snapshots.json``).  A directory
  with no changes is not opened, not refreshed and not renamed,
  however many restarts happen.  After every processed directory
  its new snapshot is recorded, so the next startup skips it.
  The first run after upgrading has no snapshots and processes
  everything once (to build the baseline); files already in the
  clean ``Title - S01E05 - ...`` form are still left alone by the
  renamer, so that baseline pass renames nothing that was
  already organised.
* A change event marks only the affected directory dirty; it is
  processed ``stable_seconds`` (default 5 s) after the *last*
  event in it, so a slow copy keeps pushing its deadline back and
  is never renamed mid-copy.  A moved-in file is already complete
  and is processed after the same short wait.
* New show folder (``Name (Year)`` without ``tvshow.nfo``) ->
  ``create`` (TMDB lookup, writes nfo + season folders +
  ``.orz/episodes.json``).
* Season folder -> rename, **offline**: only the local
  ``tvshow.nfo`` and ``.orz/episodes.json`` are read; TMDB is
  never called here — except once, lazily: if the episode cache
  is missing (an old show that only has ``tvshow.nfo``), the
  first file event fetches it from TMDB and renaming continues
  in the same pass.  No manual ``refresh``, no bulk job.
* Safety net: a full rescan only on inotify queue overflow, in
  case an event was ever missed.  The periodic rescan is off by
  default (``ORZ_RESCAN_INTERVAL=0``) — files here are moved
  within the NAS, never slowly copied in, so there is nothing to
  catch up on; set ``ORZ_RESCAN_INTERVAL=3600`` to turn the
  hourly rescan back on.  The rescan is the only periodic
  directory read; normal operation reads nothing until an event
  arrives.

De-duplication: processing is single-threaded, already-renamed
files are skipped by name, and the renamer's ``.orz/state.json``
remembers fingerprint -> episode, so a second pass never
re-guesses or re-renames the same file.
"""
from __future__ import annotations

import json
import logging
import os
import select
import threading
import time

from .cache import load_episodes, save_episodes
from .inotify import (
    IN_CLOSE_WRITE, IN_CREATE, IN_DELETE, IN_DELETE_SELF, IN_IGNORED,
    IN_ISDIR, IN_MODIFY, IN_MOVED_FROM, IN_MOVED_TO, IN_MOVE_SELF,
    IN_Q_OVERFLOW, Inotify,
)
from .mapper import EpisodeMapper, load_override
from .nfo import (generate_nfo, generate_season_folders, nfo_path,
                  normalize_season_dir, normalize_season_folders, read_nfo)
from .parser import (
    is_already_named, parse_folder_name, season_number_from_folder,
)
from .renamer import RenameResult, group_files, rename_season_folder
from .tmdb import TmdbClient

log = logging.getLogger("orz.watcher")

SNAPSHOT_FILENAME = "snapshots.json"

DEFAULT_ROOTS = ["/media/anime", "/media/tv-series"]
DEFAULT_INTERVAL = 30        # seconds between scans (polling fallback)
DEFAULT_STABLE_SECONDS = 5   # file size/mtime unchanged for this long
DEFAULT_RESCAN_INTERVAL = 0  # safety-net full rescan, seconds (0 = off, default)

# Lazy refresh: when a show has no episode cache, the first file
# event fetches it from TMDB once.  A failed fetch is not retried
# for this long, so a folder whose name does not match TMDB does
# not hit the API on every file event.
_LAZY_REFRESH_COOLDOWN = 600  # seconds
_LAST_LAZY_REFRESH: dict[str, float] = {}

# A tvshow.nfo that exists but has no usable TMDB id: warn at most
# once an hour per show, not once per season folder per pass.
_BAD_NFO_THROTTLE = 3600  # seconds
_BAD_NFO_LAST_LOG: dict[str, float] = {}


def _snapshot(path: str) -> tuple | None:
    """(mtime, sorted entry names) — cheap change detection."""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, tuple(sorted(os.listdir(path))))
    except OSError:
        return None


def _snapshot_json(snap: tuple | None) -> list | None:
    """JSON form of a snapshot: ``[mtime_ns, [names...]]``."""
    return [snap[0], list(snap[1])] if snap is not None else None


def _snapshots_path(show_dir: str) -> str:
    return os.path.join(show_dir, ".orz", SNAPSHOT_FILENAME)


def load_snapshots(show_dir: str) -> dict[str, list]:
    """Per-season snapshots for one show (``{}`` when none yet).

    Keys are season folder names (``Season 01``, ``Season 1``,
    ``Specials``).  The file lives in ``<show>/.orz/`` next to the
    episode cache, so it survives container restarts and rebuilds.
    """
    path = _snapshots_path(show_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_snapshots(show_dir: str, snaps: dict[str, list]) -> None:
    path = _snapshots_path(show_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snaps, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _record_snapshot(show_dir: str, season_dir: str) -> None:
    """Record a season dir's current snapshot (after processing it)."""
    snap = _snapshot_json(_snapshot(season_dir))
    if snap is None:
        return
    snaps = load_snapshots(show_dir)
    snaps[os.path.basename(season_dir.rstrip("/"))] = snap
    save_snapshots(show_dir, snaps)


def _snapshot_unchanged(show_dir: str, season_dir: str) -> bool:
    """True when this season dir matches its recorded snapshot."""
    snap = _snapshot_json(_snapshot(season_dir))
    if snap is None:
        return False
    stored = load_snapshots(show_dir).get(
        os.path.basename(season_dir.rstrip("/")))
    return stored is not None and stored == snap


def _has_still_copying(result) -> bool:
    return result is not None and any(
        "still copying" in p.get("reason", "") for p in result.pending)


# A folder name that can never be a show: Jellyfin artwork
# (landscape.jpg / backdrop.jpg / logo.png), episode thumbnails
# (*-thumb.jpg), Synology's @eaDir/<file>/ thumbnail directories,
# subtitles, nfo files.  The event watcher treats any unknown
# directory as a possible show, so without this guard every one of
# these gets a "No TMDB match" warning — and a wasted TMDB query —
# every few seconds while Synology regenerates thumbnails.
_SHOW_NAME_SKIP_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff",
    "mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m2ts", "ts",
    "srt", "ass", "ssa", "sub", "vtt",
    "nfo", "txt", "md", "json", "xml",
}


def _looks_like_show_dir(name: str) -> bool:
    """Could this folder name be a show folder (``Title (year)``)?"""
    base = name.strip()
    if not base or base.startswith((".", "@")):
        return False
    if "." in base:
        ext = base.rsplit(".", 1)[-1].lower()
        if ext in _SHOW_NAME_SKIP_EXTS:
            return False
    return True


def create_show(show_dir: str, client: TmdbClient, dry_run: bool = False) -> bool:
    """TMDB lookup for a new show folder.

    Writes ``tvshow.nfo``, the season folders and the offline episode
    cache (``.orz/episodes.json``).  This is one of the only two places
    that talks to TMDB (the other is ``refresh_show``).
    """
    name = os.path.basename(show_dir.rstrip("/"))
    if not _looks_like_show_dir(name):
        # Not a show (Jellyfin artwork, a thumbnail, @eaDir, ...):
        # say so at debug level and never call TMDB for it.
        log.debug("Ignoring %s: not a show folder", show_dir)
        return False
    title, year = parse_folder_name(name)
    if not title or title.startswith("."):
        log.info("Ignoring %s: no title", show_dir)
        return False
    show = client.find_show(title, year)
    if not show:
        log.warning("No TMDB match for %s (%s)", title, year)
        return False
    log.info("Matched %s (%s) -> TMDB %s (id %s)", title, year,
             show.get("name"), show.get("id"))
    if dry_run:
        return True
    show_data = client.get_tvshow(show["id"])
    generate_nfo(show_data, show_dir)
    created = generate_season_folders(show_data, show_dir)
    for p in created:
        log.info("Created %s", p)
    seasons = client.all_seasons(show["id"])
    air_dates = client.all_air_dates(show["id"])
    cache = save_episodes(show_dir, show["id"],
                          show_data.get("name") or title, seasons,
                          air_dates=air_dates)
    log.info("Saved episode cache %s (%d seasons)", cache, len(seasons))
    return True


def refresh_show(show_dir: str, client: TmdbClient, dry_run: bool = False) -> bool:
    """Re-fetch a show from TMDB; rewrite nfo + episode cache + folders.

    Run this when a show gains a new season, TMDB corrects episode
    titles, or the local cache is missing/corrupt.  Renames never do
    this on their own.
    """
    title, year = parse_folder_name(os.path.basename(show_dir.rstrip("/")))
    show = client.find_show(title, year)
    if not show:
        log.warning("No TMDB match for %s (%s)", title, year)
        return False
    if dry_run:
        log.info("Would refresh %s -> TMDB %s (id %s)", title,
                 show.get("name"), show.get("id"))
        return True
    show_data = client.get_tvshow(show["id"])
    generate_nfo(show_data, show_dir)
    normalize_season_folders(show_dir)
    generate_season_folders(show_data, show_dir)
    seasons = client.all_seasons(show["id"])
    air_dates = client.all_air_dates(show["id"])
    save_episodes(show_dir, show["id"],
                  show_data.get("name") or title, seasons,
                  air_dates=air_dates)
    log.info("Refreshed %s (TMDB id %s, %d seasons)", title,
             show["id"], len(seasons))
    return True


def process_season_folder(season_dir: str, dry_run: bool = False,
                          stable_files: set[str] | None = None,
                          client: TmdbClient | None = None):
    """Rename files in one Season folder.

    Reads ``tvshow.nfo`` + ``.orz/episodes.json``.  When the episode
    cache is missing (or for a different TMDB id) *and* a TMDB
    client was supplied, the show is refreshed from TMDB once,
    right here, and renaming continues in the same pass — no
    manual ``refresh`` needed.  A failed refresh is not retried
    for ``_LAZY_REFRESH_COOLDOWN`` seconds, so a folder whose
    name does not match TMDB does not spam the API on every
    file event.  Without a client (the ``folder`` / ``dry-run``
    commands) the files are left untouched and the log says to
    run ``refresh`` once.
    """
    if not dry_run:
        # Standard name is padded (``Season 01``): an unpadded
        # ``Season 1`` is renamed — or merged into an existing
        # ``Season 01`` — before anything else touches it.
        season_dir = normalize_season_dir(season_dir)
    show_dir = os.path.dirname(season_dir.rstrip("/"))
    nfo = read_nfo(show_dir)
    if not nfo:
        if os.path.exists(nfo_path(show_dir)):
            # The file is there but unusable (no TMDB id, or not
            # valid XML): say exactly that, with the remedy, and
            # throttle it — otherwise every processing pass logs
            # one line per season folder (v2.7.6: a folder rename
            # produced the same lines four times in two seconds).
            # Throttled per show (not per season): the CLI is a
            # fresh process each run, so it always logs.
            now = time.monotonic()
            last = _BAD_NFO_LAST_LOG.get(show_dir)
            if last is None or now - last >= _BAD_NFO_THROTTLE:
                _BAD_NFO_LAST_LOG[show_dir] = now
                log.warning(
                    "tvshow.nfo in %s has no TMDB id (or is not valid "
                    "XML), skipping %s — run `orz refresh %s` once to "
                    "rebuild it from TMDB", show_dir, season_dir, show_dir)
        else:
            log.info("No tvshow.nfo in %s, skipping %s",
                     show_dir, season_dir)
        return None
    # Nothing to rename here: do not fetch the episode cache for an
    # empty (or already organised) season folder.  This is what
    # keeps lazy refresh lazy — the startup catch-up scan must not
    # turn into a refresh of every old show on disk, only the shows
    # that actually have files waiting.
    folder_season = season_number_from_folder(
        os.path.basename(season_dir.rstrip("/")))
    groups = group_files(season_dir)
    unorganised = [files for files in groups.values()
                   if not all(is_already_named(f, folder_season)
                              for f in files)]
    if not unorganised:
        result = RenameResult()
        result.skipped = [f for files in groups.values() for f in files]
        if result.skipped:
            # Say so at INFO, not just DEBUG: a folder full of
            # already-organised files is the normal "nothing to do"
            # case, and silence here once looked like a dead
            # watcher (v2.7.5: 17 .ass files, no rename, no log).
            log.info("Skipping %s: all %d file(s) already organised",
                     season_dir, len(result.skipped))
        return result
    cached = load_episodes(show_dir)
    if cached is None or cached.get("tmdbid") != nfo["tmdbid"]:
        if client is None:
            reason = ("no episode cache" if cached is None
                      else f"episode cache is for TMDB id "
                           f"{cached.get('tmdbid')} but nfo says "
                           f"{nfo['tmdbid']}")
            log.warning(
                "No episode cache for %s (%s, %s): run `orz refresh %s` "
                "once, then renames work offline. Leaving files "
                "untouched.", nfo["title"], show_dir, reason, show_dir)
            return None
        now = time.monotonic()
        # NB: the "never attempted" sentinel is None, not 0.0:
        # time.monotonic() is seconds since boot, so on a freshly
        # started container it can be smaller than the cooldown and
        # 0.0 would wrongly look like a recent attempt.
        last = _LAST_LAZY_REFRESH.get(show_dir)
        if last is not None and now - last < _LAZY_REFRESH_COOLDOWN:
            log.info("Episode cache still missing for %s, lazy refresh "
                     "cooling down, leaving files untouched",
                     nfo["title"])
            return None
        _LAST_LAZY_REFRESH[show_dir] = now
        log.info("No episode cache for %s, fetching from TMDB once",
                 nfo["title"])
        if not refresh_show(show_dir, client, dry_run=dry_run):
            return None
        cached = load_episodes(show_dir)
        if cached is None:
            log.warning("Refresh of %s did not write an episode cache",
                        show_dir)
            return None
    # Caches written before v2.7 have no air dates, and without
    # them the mapper cannot tell a merged TMDB season apart
    # (青之箱 S1 = 37 on TMDB is really 25 + 12).  Backfill the
    # dates once, only for a show that actually has files waiting
    # (the unorganised check above already returned for the rest),
    # and only the dates — no nfo rewrite, no season folders.
    if client is not None and not cached.get("air_dates"):
        try:
            air_dates = client.all_air_dates(nfo["tmdbid"])
        except Exception as e:  # noqa: BLE001 - keep renaming offline
            log.warning("Could not backfill air dates for %s: %s",
                        nfo["title"], e)
        else:
            if air_dates:
                save_episodes(show_dir, nfo["tmdbid"],
                              cached.get("title") or nfo["title"],
                              cached["seasons"], air_dates=air_dates)
                cached["air_dates"] = air_dates
                log.info("Backfilled air dates for %s", nfo["title"])
    mapper = EpisodeMapper(cached["seasons"], load_override(show_dir),
                           cached.get("air_dates"))
    result = rename_season_folder(season_dir, nfo["title"], show_dir,
                                  mapper, dry_run=dry_run,
                                  stable_files=stable_files)
    for old, new in result.renamed:
        log.info("Renamed %s -> %s", os.path.join(season_dir, old), new)
    for p in result.pending:
        log.warning("Pending %s: %s", p["file"], p["reason"])
    if result.skipped:
        log.debug("Skipped %d already-organised file(s) in %s",
                  len(result.skipped), season_dir)
    return result


class Watcher:
    """Stateful polling loop: directory snapshots + per-file stability."""

    def __init__(self, client: TmdbClient, roots: list[str] | None = None,
                 interval: int = DEFAULT_INTERVAL,
                 stable_seconds: float = DEFAULT_STABLE_SECONDS,
                 dry_run: bool = False):
        self.client = client
        self.roots = roots or DEFAULT_ROOTS
        self.interval = interval
        self.stable_seconds = stable_seconds
        self.dry_run = dry_run
        self.dir_snaps: dict[str, tuple] = {}
        # path -> (size, mtime_ns, unchanged_since_monotonic)
        self.file_state: dict[str, tuple[int, int, float]] = {}
        # Show dirs already warned about a missing episode cache.
        self._warned_no_cache: set[str] = set()

    # -- file stability -------------------------------------------------
    def _file_ready(self, path: str) -> bool:
        """True when size + mtime have been unchanged for stable_seconds.

        First sighting: a file whose mtime is already older than
        ``stable_seconds`` counts as stable at once (startup catch-up);
        a freshly-written file waits out the window.
        """
        try:
            st = os.stat(path)
        except OSError:
            self.file_state.pop(path, None)
            return False
        now_mono = time.monotonic()
        now_wall = time.time()
        key = (st.st_size, st.st_mtime_ns)
        prev = self.file_state.get(path)
        if prev is None or prev[:2] != key:
            age = now_wall - st.st_mtime
            since = (now_mono - self.stable_seconds if age >= self.stable_seconds
                     else now_mono)
            self.file_state[path] = (st.st_size, st.st_mtime_ns, since)
            return age >= self.stable_seconds
        return now_mono - prev[2] >= self.stable_seconds

    def _stable_files_in(self, season_dir: str) -> set[str]:
        ready: set[str] = set()
        try:
            names = os.listdir(season_dir)
        except OSError:
            return ready
        for name in names:
            path = os.path.join(season_dir, name)
            if os.path.isfile(path) and self._file_ready(path):
                ready.add(name)
        return ready

    # -- one scan --------------------------------------------------------
    def scan_once(self) -> None:
        """One pass over the roots (new shows + season renames)."""
        for root in self.roots:
            if not os.path.isdir(root):
                log.warning("Watch root missing: %s", root)
                continue
            for show_name in sorted(os.listdir(root)):
                if show_name.startswith((".", "@")):
                    continue
                show_dir = os.path.join(root, show_name)
                if not os.path.isdir(show_dir):
                    continue
                snap = _snapshot(show_dir)
                was_stable = (snap is not None
                              and self.dir_snaps.get(show_dir) == snap)
                self.dir_snaps[show_dir] = snap
                if snap is None:
                    continue
                nfo_path = os.path.join(show_dir, "tvshow.nfo")
                if not os.path.exists(nfo_path):
                    # New show: only create once the folder listing
                    # has been stable across two scans — or the
                    # folder is old (it arrived while the container
                    # was down, so its mtime is already past the
                    # stability window and no second scan will come
                    # from an event that never fired).
                    old_enough = (time.time() - snap[0] / 1e9
                                  > self.stable_seconds)
                    if was_stable or old_enough:
                        create_show(show_dir, self.client,
                                    dry_run=self.dry_run)
                    continue
                # Standardise season folder names first (cheap, and
                # snapshot gating must not skip it: a folder renamed
                # by hand still needs standardising).
                normalize_season_folders(show_dir)
                for entry in sorted(os.listdir(show_dir)):
                    season_dir = os.path.join(show_dir, entry)
                    if not os.path.isdir(season_dir):
                        continue
                    if season_number_from_folder(entry) is None:
                        continue
                    s_snap = _snapshot(season_dir)
                    self.dir_snaps[season_dir] = s_snap
                    if _snapshot_unchanged(show_dir, season_dir):
                        # No change since the last run: do not open
                        # it, do not refresh it, do not rename in it.
                        continue
                    stable_files = self._stable_files_in(season_dir)
                    result = process_season_folder(
                        season_dir, dry_run=self.dry_run,
                        stable_files=stable_files, client=self.client)
                    if result is None and show_dir not in self._warned_no_cache:
                        self._warned_no_cache.add(show_dir)
                    # Record the new snapshot — unless a file is
                    # still being copied (its size will change, and
                    # the next scan must see it again).
                    if result is not None and not _has_still_copying(result):
                        _record_snapshot(show_dir, season_dir)

    def watch(self) -> None:
        """Poll forever.  The first scan is a catch-up scan (see module docstring)."""
        log.info("Watching %s every %ss (files stable after %ss)",
                 ", ".join(self.roots), self.interval, self.stable_seconds)
        while True:
            try:
                self.scan_once()
            except Exception:
                log.exception("Scan failed, retrying next round")
            time.sleep(self.interval)


# -- backwards-compatible module functions (used by the CLI) -----------

class EventWatcher:
    """Event-driven watcher: inotify + debounce, polling only as safety net.

    ``client`` is only used to create a new show (TMDB lookup); pass
    None when the watched tree already has ``tvshow.nfo`` files and
    renames are all that is needed (tests do this).
    """

    def __init__(self, client: TmdbClient | None = None,
                 roots: list[str] | None = None,
                 stable_seconds: float = DEFAULT_STABLE_SECONDS,
                 rescan_interval: float = DEFAULT_RESCAN_INTERVAL,
                 dry_run: bool = False):
        self.client = client
        self.roots = roots or DEFAULT_ROOTS
        self.stable_seconds = stable_seconds
        self.rescan_interval = rescan_interval
        self.dry_run = dry_run
        self._stop = threading.Event()
        self._inotify = Inotify()  # raises OSError when unavailable
        self._wd_to_path: dict[int, str] = {}
        self._path_to_wd: dict[str, int] = {}
        # path -> monotonic time when it may be processed
        self._dirty: dict[str, float] = {}
        self._next_rescan = (time.monotonic() + rescan_interval
                             if rescan_interval > 0 else None)
        # Reuse the polling Watcher for catch-up / rescan scans and
        # for per-file stability checks.
        self._poller = Watcher(client, roots, stable_seconds=stable_seconds,
                               dry_run=dry_run)

    # -- watches --------------------------------------------------------
    def _add_watch(self, path: str) -> None:
        if path in self._path_to_wd or not os.path.isdir(path):
            return
        try:
            wd = self._inotify.add_watch(path)
        except OSError as e:
            log.warning("Cannot watch %s: %s", path, e)
            return
        self._wd_to_path[wd] = path
        self._path_to_wd[path] = wd

    def _add_watch_recursive(self, path: str) -> None:
        base = os.path.basename(path.rstrip("/"))
        if base == ".orz" or base.startswith((".", "@")):
            return
        for dirpath, dirnames, _filenames in os.walk(path):
            # .orz holds state.json, written by the renamer itself;
            # watching it would only re-trigger on our own writes.
            # @eaDir is Synology's thumbnail cache (it even holds
            # directories named after files, e.g. @eaDir/landscape.jpg/):
            # watching it turns every thumbnail write into a
            # "No TMDB match" warning for a jpg name.
            dirnames[:] = [d for d in dirnames
                           if d != ".orz" and not d.startswith((".", "@"))]
            self._add_watch(dirpath)

    def _remove_watch(self, path: str) -> None:
        wd = self._path_to_wd.pop(path, None)
        if wd is not None:
            self._wd_to_path.pop(wd, None)
            try:
                self._inotify.rm_watch(wd)
            except OSError:
                pass

    def _mark_dirty(self, path: str) -> None:
        self._dirty[path] = time.monotonic() + self.stable_seconds

    # -- events ---------------------------------------------------------
    def _handle_event(self, wd: int, mask: int, name: str) -> None:
        path = self._wd_to_path.get(wd)
        if path is None:
            return
        # .orz is orz's own state directory (episodes.json /
        # state.json, written by the renamer itself) — never a
        # show, never watched, never processed.
        if os.path.basename(path.rstrip("/")) == ".orz" or name == ".orz":
            return
        if mask & IN_Q_OVERFLOW:
            log.warning("inotify queue overflow, doing a full rescan")
            self._rescan()
            return
        if mask & (IN_IGNORED | IN_DELETE_SELF | IN_MOVE_SELF):
            self._remove_watch(path)
            if os.path.isdir(path):
                self._add_watch_recursive(path)
                self._mark_dirty(path)
            return
        full = os.path.join(path, name) if name else path
        if mask & IN_ISDIR:
            if mask & (IN_CREATE | IN_MOVED_TO):
                # A whole show folder (with Season 01 inside) moved
                # in at once: watch every directory in it, not just
                # the top one.
                self._add_watch_recursive(full)
                self._mark_dirty(full)
                self._mark_dirty(path)
            elif mask & (IN_DELETE | IN_MOVED_FROM):
                self._remove_watch(full)
                self._mark_dirty(path)
            return
        # File event: only the directory containing it is processed,
        # and only after it has been quiet for stable_seconds.
        self._mark_dirty(path)

    # -- processing -----------------------------------------------------
    def _process_show_dir(self, show_dir: str) -> None:
        if not os.path.isdir(show_dir):
            return
        nfo_path = os.path.join(show_dir, "tvshow.nfo")
        if not os.path.exists(nfo_path):
            if self.client is None:
                log.info("No tvshow.nfo in %s and no TMDB client, "
                         "skipping", show_dir)
                return
            create_show(show_dir, self.client, dry_run=self.dry_run)
            return
        normalize_season_folders(show_dir)
        for entry in sorted(os.listdir(show_dir)):
            if entry == ".orz":
                continue
            season_dir = os.path.join(show_dir, entry)
            if (os.path.isdir(season_dir)
                    and season_number_from_folder(entry) is not None):
                self._process_season_dir(season_dir)

    def _process_season_dir(self, season_dir: str) -> None:
        if not os.path.isdir(season_dir):
            return
        if not self.dry_run:
            season_dir = normalize_season_dir(season_dir)
        if _snapshot_unchanged(os.path.dirname(season_dir.rstrip("/")),
                               season_dir):
            # The event that marked this dir dirty changed nothing
            # since the last processed snapshot (e.g. orz's own
            # rename) — nothing to do.
            return
        stable = self._poller._stable_files_in(season_dir)
        result = process_season_folder(
            season_dir, dry_run=self.dry_run, stable_files=stable,
            client=self.client)
        if result is not None and not _has_still_copying(result):
            _record_snapshot(os.path.dirname(season_dir.rstrip("/")),
                             season_dir)
        if _has_still_copying(result):
            # A file in this folder is still being written: retry
            # after another stability window, without waiting for
            # a new event (there may not be one).
            self._mark_dirty(season_dir)

    def _process_dirty(self) -> None:
        now = time.monotonic()
        for path, due in list(self._dirty.items()):
            if due > now:
                continue
            del self._dirty[path]
            if os.path.basename(path.rstrip("/")) == ".orz":
                continue
            try:
                if path in self.roots:
                    self._rescan()
                elif season_number_from_folder(
                        os.path.basename(path.rstrip("/"))) is not None:
                    self._process_season_dir(path)
                elif os.path.dirname(path.rstrip("/")) in self.roots:
                    self._process_show_dir(path)
                elif os.path.isdir(path):
                    self._add_watch_recursive(path)
                    self._process_show_dir(path)
            except Exception:
                log.exception("Failed processing %s", path)

    def _rescan(self) -> None:
        """Full scan + re-sync watches (startup, overflow, safety net)."""
        try:
            self._poller.scan_once()
        except Exception:
            log.exception("Rescan failed")
        for root in self.roots:
            self._add_watch_recursive(root)
        if self.rescan_interval > 0:
            self._next_rescan = time.monotonic() + self.rescan_interval

    # -- main loop ------------------------------------------------------
    def start(self) -> None:
        log.info("Watching %s (inotify, files stable after %ss, "
                 "safety rescan %s)",
                 ", ".join(self.roots), self.stable_seconds,
                 f"every {self.rescan_interval}s"
                 if self.rescan_interval else "off")
        # Watches first, catch-up scan second: a file that arrives
        # after the watches are on generates an event, and a file
        # that was already there is picked up by the scan.  The
        # other order leaves a window where a new file is seen by
        # neither.
        for root in self.roots:
            self._add_watch_recursive(root)
        self._rescan()  # startup catch-up scan

    def run_once(self, timeout: float | None = None) -> None:
        """Wait for events (at most ``timeout``), then process due dirs."""
        now = time.monotonic()
        waits = []
        if self._dirty:
            waits.append(max(0.0, min(self._dirty.values()) - now))
        if self._next_rescan is not None:
            waits.append(max(0.0, self._next_rescan - now))
        wait = min(waits) if waits else 1.0
        if timeout is not None:
            wait = min(wait, timeout)
        try:
            readable, _, _ = select.select(
                [self._inotify.fileno()], [], [], wait)
        except (OSError, ValueError):
            readable = []
        if readable:
            try:
                for wd, mask, name in self._inotify.read_events():
                    self._handle_event(wd, mask, name)
            except OSError:
                # stop() closes the fd while this thread is in
                # select/read; that is a normal shutdown, not an error.
                if not self._stop.is_set():
                    log.exception("Failed reading inotify events")
        self._process_dirty()
        if (self._next_rescan is not None
                and time.monotonic() >= self._next_rescan):
            log.info("Safety rescan")
            self._rescan()

    def watch(self) -> None:
        self.start()
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("Event loop failed, continuing")

    def stop(self) -> None:
        self._stop.set()
        self._inotify.close()


def scan_once(roots: list[str], client: TmdbClient,
              stable: dict[str, tuple] | None = None,
              dry_run: bool = False) -> dict[str, tuple]:
    """One polling pass.  Prefer ``Watcher``; this is a thin wrapper
    kept for the one-shot ``scan`` command (it runs two passes so a
    single invocation actually processes stable folders)."""
    watcher = Watcher(client, roots, dry_run=dry_run)
    if stable is not None:
        watcher.dir_snaps = dict(stable)
    watcher.scan_once()
    return watcher.dir_snaps


def watch(roots: list[str], client: TmdbClient, interval: int = 30,
          dry_run: bool = False, mode: str = "inotify",
          rescan_interval: float = DEFAULT_RESCAN_INTERVAL) -> None:
    """Default entry point for ``orz watch``.

    inotify by default; ``mode="poll"`` (or an inotify failure at
    startup) falls back to the polling Watcher.
    """
    if mode == "poll":
        Watcher(client, roots, interval=interval,
                dry_run=dry_run).watch()
        return
    try:
        EventWatcher(client, roots, dry_run=dry_run,
                     rescan_interval=rescan_interval).watch()
    except OSError:
        log.warning("inotify unavailable, falling back to polling")
        Watcher(client, roots, interval=interval,
                dry_run=dry_run).watch()
