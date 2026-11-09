"""CLI — for manual testing and troubleshooting only.

Normal operation needs no commands: the Docker image's default
command is ``watch``, which monitors ``/media/anime`` and
``/media/tv-series`` with inotify (events only, no polling) in the
background and renames offline.

Commands:
    create <show-folder>    TMDB lookup, write tvshow.nfo + season
                            folders + .orz/episodes.json (new show)
    refresh <show-folder>   Re-fetch a show from TMDB, rewrite nfo +
                            episode cache (new season / fixed titles /
                            missing cache).  The only other command
                            that calls TMDB.
    folder <season-folder>  Rename files in one Season folder, OFFLINE
                            (reads tvshow.nfo + .orz/episodes.json only;
                            no TMDB, no API key needed)
    dry-run <season-folder> Same as folder, but changes nothing
    scan                    One polling pass over the watch roots
    watch                   Poll forever (this is what Docker runs)

Environment:
    TMDB_API_KEY       required for create / refresh / scan / watch
                       (NOT needed for folder / dry-run: offline,
                       and NOT needed for watch in manual mode)
    TMDB_LANGUAGE      default zh-CN
    ORZ_WATCH_DIRS     comma-separated roots, default /media/anime,/media/tv-series
    ORZ_WATCH_MODE     inotify (default) or poll
    ORZ_RESCAN_INTERVAL safety-net full rescan seconds, default 0 (off)
    ORZ_CACHE_DIR      TMDB HTTP response cache, default ./var/cache
    ORZ_SCAN_INTERVAL  poll-mode interval seconds, default 30
    ORZ_STABLE_SECONDS file size/mtime unchanged window, default 5
    ORZ_MANUAL_MODE    1: the watcher only logs what is waiting and
                       never renames, creates or normalises anything;
                       renames happen only via the folder / refresh /
                       create commands below.  Default 0 (auto).
"""
from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tmdb import TmdbClient  # noqa: E402
from app.watcher import (  # noqa: E402
    DEFAULT_RESCAN_INTERVAL, DEFAULT_ROOTS, EventWatcher, Watcher,
    create_show, process_season_folder, refresh_show,
)

HELP = """Usage: orz <command> [arguments]

  orz create <show-folder>    New show: TMDB lookup, nfo + season folders
  orz refresh <show-folder>   Re-fetch a show from TMDB (nfo + episode cache)
  orz folder <season-folder>  Rename files in one Season folder (offline)
  orz dry-run <season-folder> Show what `folder` would rename, change nothing
  orz scan                    One polling pass over the watch roots
  orz watch                   Poll forever (the Docker default)

Normal use: no commands. Start the container and it watches
/media/anime and /media/tv-series on its own.

Manual mode (ORZ_MANUAL_MODE=1): the watcher only logs what is
waiting and never renames anything on its own.  Trigger renames
yourself:
  orz create <show-folder>    (new show, no tvshow.nfo yet)
  orz refresh <show-folder>   (fetch episode cache / rebuild nfo)
  orz folder <season-folder>  (rename one season, offline)
"""


def _client() -> TmdbClient:
    key = os.environ.get("TMDB_API_KEY")
    if not key:
        print("Error: TMDB_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)
    return TmdbClient(key, os.environ.get("TMDB_LANGUAGE", "zh-CN"),
                      os.environ.get("ORZ_CACHE_DIR"))


def _roots() -> list[str]:
    raw = os.environ.get("ORZ_WATCH_DIRS")
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else DEFAULT_ROOTS


def _folder_command(season_dir: str, dry_run: bool) -> int:
    # Offline on purpose: no TMDB client, no API key needed.
    result = process_season_folder(season_dir, dry_run=dry_run)
    if result is None:
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _watcher(dry_run: bool = False):
    mode = os.environ.get("ORZ_WATCH_MODE", "inotify").lower()
    manual = os.environ.get("ORZ_MANUAL_MODE", "").lower() in (
        "1", "true", "yes")
    # Manual mode never calls TMDB, so it needs no API key.
    client = None if manual else _client()
    if mode == "poll":
        return Watcher(
            client, _roots(),
            interval=int(os.environ.get("ORZ_SCAN_INTERVAL", "30")),
            stable_seconds=float(os.environ.get("ORZ_STABLE_SECONDS", "5")),
            dry_run=dry_run, manual=manual,
        )
    return EventWatcher(
        client, _roots(),
        stable_seconds=float(os.environ.get("ORZ_STABLE_SECONDS", "5")),
        rescan_interval=float(os.environ.get(
            "ORZ_RESCAN_INTERVAL", str(DEFAULT_RESCAN_INTERVAL))),
        dry_run=dry_run, manual=manual,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0 if argv else 1
    cmd = argv[0]
    if cmd == "create" and len(argv) == 2:
        return 0 if create_show(argv[1], _client()) else 1
    if cmd == "refresh" and len(argv) == 2:
        return 0 if refresh_show(argv[1], _client()) else 1
    if cmd == "folder" and len(argv) == 2:
        return _folder_command(argv[1], dry_run=False)
    if cmd == "dry-run" and len(argv) == 2:
        return _folder_command(argv[1], dry_run=True)
    if cmd == "scan":
        # Two passes: the first records snapshots / file states, the
        # second processes folders that are already stable (so a
        # one-shot `scan` catches up instead of only observing).
        watcher = _watcher(dry_run="--dry-run" in argv)
        watcher.scan_once()
        watcher.scan_once()
        return 0
    if cmd == "watch":
        _watcher().watch()
        return 0
    print(f"Unknown command '{cmd}'.", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
