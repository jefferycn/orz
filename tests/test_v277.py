"""v2.7.7 tests — manual mode + one pass per folder rename.

The user asked for two things:
(a) no automatic renames at all — the watcher must only log
    what is waiting, and renames happen only via the CLI;
(b) renaming a folder used to be processed twice (the rename
    marked the parent watch root dirty too, triggering a full
    rescan on top of the show-dir pass).
"""
import logging
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.watcher as watcher_module
from app.cache import save_episodes
from app.cli import _watcher
from app.inotify import IN_ISDIR, IN_MOVED_FROM, IN_MOVED_TO
from app.watcher import EventWatcher, Watcher


def _make_show(root, name="Show (2010)", season="Season 01"):
    show_dir = os.path.join(root, name)
    season_dir = os.path.join(show_dir, season)
    os.makedirs(season_dir)
    with open(os.path.join(show_dir, "tvshow.nfo"), "w",
              encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tvshow>'
                "<title>Show</title><tmdbid>1</tmdbid></tvshow>")
    save_episodes(show_dir, 1, "Show",
                  {1: {i: f"第{i}集" for i in range(1, 13)}})
    return show_dir, season_dir


class TestFolderRenameSinglePass(unittest.TestCase):
    def test_rename_marks_only_new_dir_dirty(self):
        tmp = tempfile.mkdtemp()
        try:
            root = os.path.join(tmp, "TV show")
            os.makedirs(root)
            old = os.path.join(root, "Old (2010)")
            os.makedirs(old)
            new = os.path.join(root, "New (2010)")
            os.rename(old, new)
            w = EventWatcher(client=None, roots=[root],
                             stable_seconds=0, rescan_interval=0)
            try:
                w._add_watch_recursive(root)
                wd = w._path_to_wd[root]
                w._handle_event(wd, IN_ISDIR | IN_MOVED_FROM, "Old (2010)")
                w._handle_event(wd, IN_ISDIR | IN_MOVED_TO, "New (2010)")
                # The root must NOT be dirty: it used to trigger a
                # full rescan on top of the show-dir pass.
                self.assertEqual(set(w._dirty), {new})
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestManualMode(unittest.TestCase):
    def setUp(self):
        watcher_module._MANUAL_WAITING.clear()
        watcher_module._MANUAL_NEW_SHOWN.clear()

    def test_waiting_logged_once_nothing_renamed(self):
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            scene = os.path.join(season, "[Group] Show - 02.mkv")
            with open(scene, "wb") as f:
                f.write(b"data-02")
            w = EventWatcher(client=None, roots=[tmp], stable_seconds=0,
                             rescan_interval=0, manual=True)
            try:
                with self.assertLogs("orz.watcher", level="INFO") as logs:
                    w._process_season_dir(season)
                self.assertEqual(len(logs.output), 1)
                self.assertIn("Manual mode: 1 file(s) waiting", logs.output[0])
                self.assertIn("orz folder", logs.output[0])
                # Nothing renamed, no snapshot recorded.
                self.assertTrue(os.path.exists(scene))
                self.assertFalse(os.path.exists(
                    os.path.join(os.path.dirname(season), ".orz",
                                 "snapshots.json")))
                # Same files again: silent.
                with self.assertNoLogs("orz.watcher", level="INFO"):
                    w._process_season_dir(season)
                # One more file: logs again, with the new count.
                with open(os.path.join(season, "[Group] Show - 03.mkv"),
                          "wb") as f:
                    f.write(b"data-03")
                with self.assertLogs("orz.watcher", level="INFO") as logs2:
                    w._process_season_dir(season)
                self.assertIn("2 file(s) waiting", logs2.output[0])
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_waiting_without_nfo_points_at_create(self):
        tmp = tempfile.mkdtemp()
        try:
            show_dir = os.path.join(tmp, "NoNfo (2020)")
            season = os.path.join(show_dir, "Season 01")
            os.makedirs(season)
            with open(os.path.join(season, "[Group] Show - 01.mkv"),
                      "wb") as f:
                f.write(b"x")
            w = EventWatcher(client=None, roots=[tmp], stable_seconds=0,
                             rescan_interval=0, manual=True)
            try:
                with self.assertLogs("orz.watcher", level="INFO") as logs:
                    w._process_season_dir(season)
                self.assertIn("orz create", logs.output[0])
                self.assertTrue(os.path.exists(
                    os.path.join(season, "[Group] Show - 01.mkv")))
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_scan_once_manual_skips_normalize_and_create(self):
        tmp = tempfile.mkdtemp()
        try:
            # Unpadded Season 1 with a waiting file: must stay
            # exactly as it is in manual mode.
            show_dir, season = _make_show(tmp, season="Season 1")
            scene = os.path.join(season, "[Group] Show - 02.mkv")
            with open(scene, "wb") as f:
                f.write(b"data-02")
            # A brand-new show folder with no nfo.
            new_show = os.path.join(tmp, "New (2026)")
            os.makedirs(new_show)
            watcher = Watcher(client=None, roots=[tmp], stable_seconds=0,
                              manual=True)
            with self.assertLogs("orz.watcher", level="INFO") as logs:
                watcher.scan_once()
            self.assertTrue(os.path.isdir(season))
            self.assertFalse(os.path.isdir(
                os.path.join(show_dir, "Season 01")))
            self.assertTrue(os.path.exists(scene))
            self.assertFalse(os.path.exists(
                os.path.join(new_show, "tvshow.nfo")))
            self.assertFalse(os.path.exists(
                os.path.join(show_dir, ".orz", "snapshots.json")))
            self.assertTrue(any("waiting in" in line for line in logs.output),
                            logs.output)
            self.assertTrue(any("new show" in line for line in logs.output),
                            logs.output)
            # Second scan: fully silent — the waiting set is
            # unchanged and the new-show line does not repeat.
            with self.assertNoLogs("orz.watcher", level="INFO"):
                watcher.scan_once()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cli_watcher_manual_needs_no_api_key(self):
        old = dict(os.environ)
        try:
            os.environ["TMDB_API_KEY"] = ""
            os.environ["ORZ_MANUAL_MODE"] = "1"
            os.environ.pop("ORZ_WATCH_MODE", None)
            watcher = _watcher()
            self.assertTrue(watcher.manual)
            self.assertIsNone(watcher.client)
            # Without manual mode the same empty key must fail.
            os.environ.pop("ORZ_MANUAL_MODE", None)
            with self.assertRaises(SystemExit):
                _watcher()
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
