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
  at once; then inotify watches are added recursively to the roots,
  show folders and season folders.
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
  never called here.  If the episode cache is missing, the files
  are left pending with a clear reason ("run ``orz refresh
  <show>`` once") instead of quietly calling TMDB.
* Safety net: a full rescan every ``rescan_interval`` (default
  1 h, ``ORZ_RESCAN_INTERVAL=0`` disables it) and on inotify
  queue overflow, in case an event was ever missed.  The rescan
  is the only periodic directory read; normal operation reads
  nothing until an event arrives.

De-duplication: processing is single-threaded, already-renamed
files are skipped by name, and the renamer's ``.orz/state.json``
remembers fingerprint -> episode, so a second pass never
re-guesses or re-renames the same file.
"""
from __future__ import annotations

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
from .nfo import generate_nfo, generate_season_folders, read_nfo
from .parser import parse_folder_name, season_number_from_folder
from .renamer import rename_season_folder
from .tmdb import TmdbClient

log = logging.getLogger("orz.watcher")

DEFAULT_ROOTS = ["/media/anime", "/media/tv-series"]
DEFAULT_INTERVAL = 30        # seconds between scans (polling fallback)
DEFAULT_STABLE_SECONDS = 5   # file size/mtime unchanged for this long
DEFAULT_RESCAN_INTERVAL = 3600  # safety-net full rescan, seconds (0 = off)


def _snapshot(path: str) -> tuple | None:
    """(mtime, sorted entry names) — cheap change detection."""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, tuple(sorted(os.listdir(path))))
    except OSError:
        return None


def create_show(show_dir: str, client: TmdbClient, dry_run: bool = False) -> bool:
    """TMDB lookup for a new show folder.

    Writes ``tvshow.nfo``, the season folders and the offline episode
    cache (``.orz/episodes.json``).  This is one of the only two places
    that talks to TMDB (the other is ``refresh_show``).
    """
    title, year = parse_folder_name(os.path.basename(show_dir.rstrip("/")))
    if not title:
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
    cache = save_episodes(show_dir, show["id"],
                          show_data.get("name") or title, seasons)
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
    generate_season_folders(show_data, show_dir)
    seasons = client.all_seasons(show["id"])
    save_episodes(show_dir, show["id"],
                  show_data.get("name") or title, seasons)
    log.info("Refreshed %s (TMDB id %s, %d seasons)", title,
             show["id"], len(seasons))
    return True


def process_season_folder(season_dir: str, dry_run: bool = False,
                          stable_files: set[str] | None = None,
                          client: TmdbClient | None = None):
    """Rename files in one Season folder, offline.

    Reads only ``tvshow.nfo`` + ``.orz/episodes.json``.  ``client`` is
    accepted for backwards compatibility and is never used here.
    Returns the RenameResult, or None when the folder cannot be
    processed (no nfo / no episode cache — both logged with what to do).
    """
    show_dir = os.path.dirname(season_dir.rstrip("/"))
    nfo = read_nfo(show_dir)
    if not nfo:
        log.info("No tvshow.nfo in %s, skipping %s", show_dir, season_dir)
        return None
    cached = load_episodes(show_dir)
    if cached is None:
        log.warning(
            "No episode cache for %s (%s): run `orz refresh %s` once, "
            "then renames work offline. Leaving files untouched.",
            nfo["title"], show_dir, show_dir)
        return None
    if cached.get("tmdbid") != nfo["tmdbid"]:
        log.warning("Episode cache in %s is for TMDB id %s but nfo says %s; "
                    "run `orz refresh %s`.", show_dir, cached.get("tmdbid"),
                    nfo["tmdbid"], show_dir)
        return None
    mapper = EpisodeMapper(cached["seasons"], load_override(show_dir))
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
                    # has been stable across two scans.
                    if was_stable:
                        create_show(show_dir, self.client,
                                    dry_run=self.dry_run)
                    continue
                for entry in sorted(os.listdir(show_dir)):
                    season_dir = os.path.join(show_dir, entry)
                    if not os.path.isdir(season_dir):
                        continue
                    if season_number_from_folder(entry) is None:
                        continue
                    s_snap = _snapshot(season_dir)
                    self.dir_snaps[season_dir] = s_snap
                    stable_files = self._stable_files_in(season_dir)
                    result = process_season_folder(
                        season_dir, dry_run=self.dry_run,
                        stable_files=stable_files)
                    if result is None and show_dir not in self._warned_no_cache:
                        self._warned_no_cache.add(show_dir)

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
        for dirpath, dirnames, _filenames in os.walk(path):
            # .orz holds state.json, written by the renamer itself;
            # watching it would only re-trigger on our own writes.
            dirnames[:] = [d for d in dirnames if d != ".orz"]
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
        for entry in sorted(os.listdir(show_dir)):
            season_dir = os.path.join(show_dir, entry)
            if (os.path.isdir(season_dir)
                    and season_number_from_folder(entry) is not None):
                self._process_season_dir(season_dir)

    def _process_season_dir(self, season_dir: str) -> None:
        if not os.path.isdir(season_dir):
            return
        stable = self._poller._stable_files_in(season_dir)
        result = process_season_folder(
            season_dir, dry_run=self.dry_run, stable_files=stable)
        if result is not None and any(
                "still copying" in p.get("reason", "")
                for p in result.pending):
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
                 "safety rescan every %ss)",
                 ", ".join(self.roots), self.stable_seconds,
                 self.rescan_interval or "off")
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
