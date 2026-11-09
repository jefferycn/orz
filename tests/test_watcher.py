"""Tests for app.watcher — offline rename, stability, catch-up.

No network: the fake TMDB client raises on *any* call, proving the
rename path never touches TMDB.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import save_episodes
from app.nfo import generate_nfo
from app.watcher import Watcher, process_season_folder

SHOW = "罗梅莉亚战记"
SEASONS = {1: {i: f"第{i}集" for i in range(1, 25)}}


class NoNetworkClient:
    """Any TMDB call is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"TMDB was called ({name}) — rename must be offline")


def _show(tmp, with_cache=True, files=None):
    show_dir = os.path.join(tmp, f"{SHOW} (2026)")
    season = os.path.join(show_dir, "Season 01")
    os.makedirs(season)
    generate_nfo({"name": SHOW, "original_name": SHOW, "id": 123,
                  "overview": "", "vote_average": 0, "vote_count": 0,
                  "first_air_date": "2026-01-01"}, show_dir)
    if with_cache:
        save_episodes(show_dir, 123, SHOW, SEASONS)
    for name, data in (files or {}).items():
        with open(os.path.join(season, name), "wb") as f:
            f.write(data)
    return show_dir, season


def _old(path, seconds=10):
    old = time.time() - seconds
    os.utime(path, (old, old))


class TestOfflineRename(unittest.TestCase):
    def test_folder_rename_makes_zero_tmdb_calls(self):
        tmp = tempfile.mkdtemp()
        _, season = _show(tmp, files={"罗梅莉亚战记 - 02.mkv": b"v"})
        # No client passed at all: the offline path needs none.
        result = process_season_folder(season)
        self.assertEqual(len(result.renamed), 1)
        self.assertEqual(result.renamed[0][1], f"{SHOW} - S01E02 - 第2集.mkv")

    def test_missing_cache_leaves_files_alone(self):
        tmp = tempfile.mkdtemp()
        _, season = _show(tmp, with_cache=False,
                          files={"罗梅莉亚战记 - 02.mkv": b"v"})
        self.assertIsNone(process_season_folder(season))
        self.assertTrue(os.path.exists(os.path.join(season, "罗梅莉亚战记 - 02.mkv")))


class TestStability(unittest.TestCase):
    def test_fresh_file_not_ready_old_file_ready(self):
        watcher = Watcher(NoNetworkClient(), stable_seconds=5)
        tmp = tempfile.mkdtemp()
        fresh = os.path.join(tmp, "fresh.mkv")
        with open(fresh, "wb") as f:
            f.write(b"x")
        self.assertFalse(watcher._file_ready(fresh))  # just written
        old = os.path.join(tmp, "old.mkv")
        with open(old, "wb") as f:
            f.write(b"x")
        _old(old)
        self.assertTrue(watcher._file_ready(old))  # catch-up: already stable

    def test_growing_file_not_ready_until_stable(self):
        watcher = Watcher(NoNetworkClient(), stable_seconds=5)
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "growing.mkv")
        with open(path, "wb") as f:
            f.write(b"a")
        _old(path)
        self.assertTrue(watcher._file_ready(path))
        with open(path, "ab") as f:  # more data arrives
            f.write(b"b")
        self.assertFalse(watcher._file_ready(path))
        _old(path)
        self.assertTrue(watcher._file_ready(path))


class TestWatcherScan(unittest.TestCase):
    def test_startup_catchup_scan_renames_and_dedups(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "anime")
        os.makedirs(root)
        show_dir, season = _show(root, files={"罗梅莉亚战记 - 02.mkv": b"v"})
        _old(os.path.join(season, "罗梅莉亚战记 - 02.mkv"))
        watcher = Watcher(NoNetworkClient(), roots=[root])
        watcher.scan_once()  # first scan: catch-up, renames at once
        self.assertTrue(os.path.exists(
            os.path.join(season, f"{SHOW} - S01E02 - 第2集.mkv")))
        # Second scan: nothing left to do (no re-rename, no duplicate).
        watcher.scan_once()
        self.assertEqual(sorted(os.listdir(season)),
                         [f"{SHOW} - S01E02 - 第2集.mkv"])

    def test_unstable_file_waits_for_next_scan(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "anime")
        os.makedirs(root)
        show_dir, season = _show(root, files={"罗梅莉亚战记 - 02.mkv": b"v"})
        # Fresh mtime: still copying -> left alone, marked pending.
        watcher = Watcher(NoNetworkClient(), roots=[root])
        watcher.scan_once()
        self.assertTrue(os.path.exists(os.path.join(season, "罗梅莉亚战记 - 02.mkv")))
        _old(os.path.join(season, "罗梅莉亚战记 - 02.mkv"))
        watcher.scan_once()
        self.assertTrue(os.path.exists(
            os.path.join(season, f"{SHOW} - S01E02 - 第2集.mkv")))


if __name__ == "__main__":
    unittest.main()
