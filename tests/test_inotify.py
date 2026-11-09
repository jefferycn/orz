"""Tests for the inotify watcher — real kernel events, no network."""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import save_episodes
from app.inotify import IN_CLOSE_WRITE, IN_CREATE, Inotify, available
from app.watcher import EventWatcher


def _wait_for(predicate, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class TestInotifyWrapper(unittest.TestCase):
    def test_create_and_close_write_events(self):
        self.assertTrue(available())
        tmp = tempfile.mkdtemp()
        try:
            ino = Inotify()
            wd = ino.add_watch(tmp)
            self.assertGreater(wd, 0)
            path = os.path.join(tmp, "a.txt")
            with open(path, "w") as f:
                f.write("hi")
            events = []

            def _drain():
                events.extend(ino.read_events())
                return bool(events)

            self.assertTrue(_wait_for(_drain, timeout=2))
            masks = [m for _, m, n in events if n == "a.txt"]
            self.assertTrue(any(m & IN_CREATE for m in masks))
            self.assertTrue(any(m & IN_CLOSE_WRITE for m in masks))
            ino.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _make_show(root, show="Show (2024)", titles=None):
    show_dir = os.path.join(root, show)
    season = os.path.join(show_dir, "Season 01")
    os.makedirs(season)
    with open(os.path.join(show_dir, "tvshow.nfo"), "w",
              encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tvshow>'
                "<title>Show</title><tmdbid>1</tmdbid></tvshow>")
    save_episodes(show_dir, 1, "Show",
                  {1: titles or {i: f"第{i}集" for i in range(1, 13)}})
    return show_dir, season


class TestEventWatcher(unittest.TestCase):
    def _run_watcher(self, root, stable_seconds=0.3):
        w = EventWatcher(client=None, roots=[root],
                         stable_seconds=stable_seconds)
        t = threading.Thread(target=w.watch, daemon=True)
        t.start()
        # Wait until the watches are on: a file written before the
        # watch exists generates no event (production order is
        # watches-first, but the thread still needs a moment).
        self.assertTrue(_wait_for(
            lambda: root in w._path_to_wd, timeout=5))
        return w, t

    def test_file_dropped_in_is_renamed(self):
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            w, _ = self._run_watcher(tmp)
            try:
                with open(os.path.join(
                        season, "[Group] Show - 02.mkv"), "wb") as f:
                    f.write(b"data-02")
                self.assertTrue(_wait_for(lambda: os.path.exists(
                    os.path.join(season, "Show - S01E02 - 第2集.mkv")),
                    timeout=10))
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_batch_dropped_in_together(self):
        # Two files at once: the batch common-text parse (v2.4)
        # still applies under the event watcher.
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            w, _ = self._run_watcher(tmp)
            try:
                for n in (1, 2):
                    with open(os.path.join(
                            season, f"[Group] Show Vol.3 - 0{n}.mkv"),
                            "wb") as f:
                        # Distinct contents: the renamer fingerprints
                        # files (size + sha256), identical bytes would
                        # be the same fingerprint and map to one episode.
                        f.write(f"vol3-{n}".encode())
                self.assertTrue(_wait_for(lambda: os.path.exists(
                    os.path.join(season, "Show - S01E01 - 第1集.mkv"))
                    and os.path.exists(os.path.join(
                        season, "Show - S01E02 - 第2集.mkv")), timeout=10))
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_whole_show_folder_moved_in(self):
        # A complete show folder (with Season 01 + file already
        # inside) moved into the root: watches must be added
        # recursively, not just for the top directory.
        tmp = tempfile.mkdtemp()
        try:
            staging = os.path.join(tmp, "staging")
            show_dir, season = _make_show(staging)
            with open(os.path.join(
                    season, "[Group] Show - 03.mkv"), "wb") as f:
                f.write(b"show-03")
            root = os.path.join(tmp, "Anime")
            os.makedirs(root)
            w, _ = self._run_watcher(root)
            try:
                shutil.move(show_dir, os.path.join(root, "Show (2024)"))
                self.assertTrue(_wait_for(lambda: os.path.exists(
                    os.path.join(root, "Show (2024)", "Season 01",
                                 "Show - S01E03 - 第3集.mkv")), timeout=10))
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_slow_copy_is_not_renamed_mid_write(self):
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            # Stable window 1 s, write pause 0.6 s: the pause is
            # shorter than the window, so the file must not be
            # renamed mid-write.  (A pause longer than the window
            # is indistinguishable from a finished copy — that is
            # the same mtime-window limit the polling design had.)
            w, _ = self._run_watcher(tmp, stable_seconds=1.0)
            try:
                path = os.path.join(season, "[Group] Show - 04.mkv")
                with open(path, "wb") as f:
                    f.write(b"part1")
                    f.flush()
                    # Still writing after the first stable window:
                    # the file must not be renamed yet.
                    time.sleep(0.6)
                    self.assertFalse(os.path.exists(os.path.join(
                        season, "Show - S01E04 - 第4集.mkv")))
                    f.write(b"part2")
                self.assertTrue(_wait_for(lambda: os.path.exists(
                    os.path.join(season, "Show - S01E04 - 第4集.mkv")),
                    timeout=10))
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
