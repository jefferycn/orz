"""Tests for app.cache — no network, stdlib unittest only."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import load_episodes, save_episodes

SEASONS = {1: {1: "第1集", 2: "第2集"}, 0: {1: "特别篇"}}


class TestCache(unittest.TestCase):
    def test_roundtrip_int_keys(self):
        show_dir = tempfile.mkdtemp()
        path = save_episodes(show_dir, 123, "罗梅莉亚战记", SEASONS)
        self.assertTrue(os.path.exists(path))
        data = load_episodes(show_dir)
        self.assertEqual(data["tmdbid"], 123)
        self.assertEqual(data["seasons"], SEASONS)

    def test_missing_is_none(self):
        self.assertIsNone(load_episodes(tempfile.mkdtemp()))

    def test_corrupt_is_none(self):
        show_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(show_dir, ".orz"))
        with open(os.path.join(show_dir, ".orz", "episodes.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(load_episodes(show_dir))


if __name__ == "__main__":
    unittest.main()
