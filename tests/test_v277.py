"""v2.7.7/v2.7.8 tests — one pass per folder rename; events only.

v2.7.7 fixed the folder-rename duplication: renaming a show folder
used to mark the parent watch root dirty too, triggering a full
rescan on top of the show-dir pass, so one rename was processed
twice.

v2.7.8 removed polling and the catch-up scan entirely (the user
pointed out his old PHP version had neither, and move/create
events are enough): the watcher adds watches at startup and
processes only what events mark dirty.  No startup scan, no
overflow rescan, no periodic rescan, no polling fallback — an
inotify failure now exits with an error instead of silently
polling.  The manual one-shot `orz scan` remains for files that
arrived while the container was down.
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import save_episodes
from app.inotify import IN_ISDIR, IN_MOVED_FROM, IN_MOVED_TO, IN_Q_OVERFLOW
from app.watcher import EventWatcher


def _wait_for(predicate, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _make_show(root, name="Show (2010)"):
    show_dir = os.path.join(root, name)
    season = os.path.join(show_dir, "Season 01")
    os.makedirs(season)
    with open(os.path.join(show_dir, "tvshow.nfo"), "w",
              encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tvshow>'
                "<title>Show</title><tmdbid>1</tmdbid></tvshow>")
    save_episodes(show_dir, 1, "Show",
                  {1: {i: f"第{i}集" for i in range(1, 13)}})
    return show_dir, season


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
            w = EventWatcher(client=None, roots=[root], stable_seconds=0)
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


class TestEventsOnly(unittest.TestCase):
    def _run_watcher(self, root, stable_seconds=0.3):
        w = EventWatcher(client=None, roots=[root],
                         stable_seconds=stable_seconds)
        t = threading.Thread(target=w.watch, daemon=True)
        t.start()
        self.assertTrue(_wait_for(
            lambda: root in w._path_to_wd, timeout=5))
        return w, t

    def test_startup_does_not_process_existing_files(self):
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            old = os.path.join(season, "[Group] Show - 02.mkv")
            with open(old, "wb") as f:
                f.write(b"data-02")
            w, _ = self._run_watcher(tmp)
            try:
                # The file was already there at startup: no scan,
                # so it stays exactly as it is.
                time.sleep(1.5)
                self.assertTrue(os.path.exists(old))
                self.assertFalse(os.path.exists(os.path.join(
                    season, "Show - S01E02 - 第2集.mkv")))
                # A file dropped in after startup still renames
                # on its event — the watcher is alive, just not
                # scanning.
                new = os.path.join(season, "[Group] Show - 03.mkv")
                with open(new, "wb") as f:
                    f.write(b"data-03")
                self.assertTrue(_wait_for(lambda: os.path.exists(
                    os.path.join(season, "Show - S01E03 - 第3集.mkv")),
                    timeout=10))
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_overflow_only_warns_no_rescan(self):
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            old = os.path.join(season, "[Group] Show - 02.mkv")
            with open(old, "wb") as f:
                f.write(b"data-02")
            w, _ = self._run_watcher(tmp)
            try:
                wd = w._path_to_wd[tmp]
                with self.assertLogs("orz.watcher", level="WARNING") as logs:
                    w._handle_event(wd, IN_Q_OVERFLOW, "")
                self.assertTrue(any("queue overflow" in line
                                    for line in logs.output), logs.output)
                self.assertTrue(any("orz scan" in line
                                    for line in logs.output), logs.output)
                # No rescan: the pre-existing file stays unrenamed.
                time.sleep(1.5)
                self.assertTrue(os.path.exists(old))
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
