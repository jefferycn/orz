"""Tests for lazy refresh: a show with no episode cache is fetched
from TMDB once, on the first file event, instead of needing a
manual `refresh`.  No network — a fake TMDB client."""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import watcher as watcher_mod
from app.cache import save_episodes
from app.watcher import EventWatcher, process_season_folder


class FakeTmdb:
    def __init__(self, match=True):
        self.match = match
        self.find_calls = 0
        self.seasons = {1: {i: f"第{i}集" for i in range(1, 13)}}

    def find_show(self, title, year):
        self.find_calls += 1
        # Only the real show title matches, like TMDB would;
        # ".orz" or any other folder name must not match.
        return ({"id": 1, "name": "Show"}
                if self.match and title == "Show" else None)

    def get_tvshow(self, show_id):
        return {"id": 1, "name": "Show", "original_name": "Show",
                "first_air_date": "2024-01-01", "overview": "",
                "vote_average": 0, "vote_count": 0,
                "seasons": [{"season_number": 1}]}

    def all_seasons(self, show_id):
        return self.seasons

    def all_air_dates(self, show_id):
        return {}


def _make_show(root, with_cache=False, cache_tmdbid=1):
    show_dir = os.path.join(root, "Show (2024)")
    season = os.path.join(show_dir, "Season 01")
    os.makedirs(season)
    with open(os.path.join(show_dir, "tvshow.nfo"), "w",
              encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tvshow>'
                "<title>Show</title><tmdbid>1</tmdbid></tvshow>")
    if with_cache:
        save_episodes(show_dir, cache_tmdbid, "Show",
                      {1: {i: f"第{i}集" for i in range(1, 13)}})
    return show_dir, season


def _wait_for(predicate, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class TestLazyRefresh(unittest.TestCase):
    def setUp(self):
        watcher_mod._LAST_LAZY_REFRESH.clear()

    def test_missing_cache_is_fetched_once_and_renamed(self):
        tmp = tempfile.mkdtemp()
        try:
            show_dir, season = _make_show(tmp)
            with open(os.path.join(season, "[Group] Show - 02.mkv"),
                      "wb") as f:
                f.write(b"lazy-02")
            client = FakeTmdb()
            result = process_season_folder(season, client=client)
            self.assertIsNotNone(result)
            self.assertEqual(result.pending, [])
            self.assertEqual(client.find_calls, 1)
            self.assertTrue(os.path.exists(
                os.path.join(show_dir, ".orz", "episodes.json")))
            self.assertTrue(os.path.exists(os.path.join(
                season, "Show - S01E02 - 第2集.mkv")))
            # Second call: cache now exists, no second fetch.
            result = process_season_folder(season, client=client)
            self.assertEqual(client.find_calls, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_season_does_not_refresh(self):
        # Startup catch-up scan hits every season folder: an empty
        # one must not trigger a fetch, or startup would refresh
        # every old show on disk (a refresh-all in disguise).
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            client = FakeTmdb()
            result = process_season_folder(season, client=client)
            self.assertIsNotNone(result)
            self.assertEqual(client.find_calls, 0)
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "Show (2024)", ".orz", "episodes.json")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_client_leaves_files_untouched(self):
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            with open(os.path.join(season, "[Group] Show - 02.mkv"),
                      "wb") as f:
                f.write(b"lazy-02")
            self.assertIsNone(process_season_folder(season))
            self.assertTrue(os.path.exists(os.path.join(
                season, "[Group] Show - 02.mkv")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_failed_refresh_cools_down(self):
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            with open(os.path.join(season, "[Group] Show - 02.mkv"),
                      "wb") as f:
                f.write(b"lazy-02")
            client = FakeTmdb(match=False)
            self.assertIsNone(process_season_folder(season, client=client))
            self.assertEqual(client.find_calls, 1)
            # Immediate retry: still cooling down, no second call.
            self.assertIsNone(process_season_folder(season, client=client))
            self.assertEqual(client.find_calls, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tmdbid_mismatch_is_refreshed(self):
        tmp = tempfile.mkdtemp()
        try:
            show_dir, season = _make_show(tmp, with_cache=True,
                                          cache_tmdbid=999)
            with open(os.path.join(season, "[Group] Show - 03.mkv"),
                      "wb") as f:
                f.write(b"lazy-03")
            client = FakeTmdb()
            result = process_season_folder(season, client=client)
            self.assertIsNotNone(result)
            self.assertTrue(os.path.exists(os.path.join(
                season, "Show - S01E03 - 第3集.mkv")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_event_watcher_fetches_on_file_drop(self):
        tmp = tempfile.mkdtemp()
        try:
            _, season = _make_show(tmp)
            client = FakeTmdb()
            w = EventWatcher(client=client, roots=[tmp],
                             stable_seconds=0.3)
            t = threading.Thread(target=w.watch, daemon=True)
            t.start()
            try:
                self.assertTrue(_wait_for(
                    lambda: tmp in w._path_to_wd, timeout=5))
                with open(os.path.join(season, "[Group] Show - 02.mkv"),
                          "wb") as f:
                    f.write(b"lazy-02")
                self.assertTrue(_wait_for(lambda: os.path.exists(
                    os.path.join(season, "Show - S01E02 - 第2集.mkv")),
                    timeout=10))
                self.assertEqual(client.find_calls, 1)
                # .orz must never be treated as a show folder:
                # no nested .orz/.orz from refresh writing its own
                # cache file.
                self.assertFalse(os.path.exists(os.path.join(
                    tmp, "Show (2024)", ".orz", ".orz")))
            finally:
                w.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
