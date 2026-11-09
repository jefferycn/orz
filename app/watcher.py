"""Polling watcher — the main function of orz (replaces ``listen.sh``).

The container's default command is ``watch``; the CLI commands
(``create`` / ``folder`` / ``scan`` / ``dry-run`` / ``refresh``) exist
only for manual testing and troubleshooting.

Why polling, not inotify: the old script needed inotify-tools, a temp
event file, a flock lock and a sleep loop, and still raced with files
mid-copy.  Polling needs no extra binary and behaves the same on
Synology bind mounts, where inotify is unreliable.

How one scan works
------------------
* New show folder (``Name (Year)`` without ``tvshow.nfo``) -> ``create``
  (TMDB lookup, writes nfo + season folders + ``.orz/episodes.json``).
  A folder is created only once its directory listing has been stable
  across two scans, so a folder still being filled is not created early.
* Season folder -> rename, **offline**: only the local ``tvshow.nfo``
  and ``.orz/episodes.json`` are read; TMDB is never called here.
  If the episode cache is missing, the files are left pending with a
  clear reason ("run ``orz refresh <show>`` once") instead of quietly
  calling TMDB — that keeps renames working with no network at all.
* A file is renamed only when its size + mtime have been unchanged
  for ``stable_seconds`` (default 5 s): a file still being copied or
  moved onto the NAS is left pending and retried next scan.
* The first scan after start is a catch-up scan: files whose mtime is
  already older than ``stable_seconds`` count as stable immediately,
  so anything missed while the container was down is processed at
  once instead of waiting out another stability window.

De-duplication: the scan is single-threaded, a directory is only
re-processed when its snapshot changed, already-renamed files are
skipped by name, and the renamer's ``.orz/state.json`` remembers
fingerprint -> episode, so a second scan never re-guesses or
re-renames the same file.
"""
from __future__ import annotations

import logging
import os
import time

from .cache import load_episodes, save_episodes
from .mapper import EpisodeMapper, load_override
from .nfo import generate_nfo, generate_season_folders, read_nfo
from .parser import parse_folder_name, season_number_from_folder
from .renamer import rename_season_folder
from .tmdb import TmdbClient

log = logging.getLogger("orz.watcher")

DEFAULT_ROOTS = ["/media/anime", "/media/tv-series"]
DEFAULT_INTERVAL = 30        # seconds between scans
DEFAULT_STABLE_SECONDS = 5   # file size/mtime unchanged for this long


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
          dry_run: bool = False) -> None:
    Watcher(client, roots, interval=interval, dry_run=dry_run).watch()
