"""Tests for app.mapper — no network, stdlib unittest only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mapper import EpisodeMapper

# TMDB S1 with 12 episodes (the Anime case: files restart at "S2").
S1_12 = {1: {i: f"第{i}集" for i in range(1, 13)}}


class TestAbsoluteMapping(unittest.TestCase):
    def test_s2_maps_to_s1_absolute(self):
        # Task's literal example: TMDB S1 lists 12 eps, file S2E01
        # -> S01E13.  Title is "" here because TMDB lists no ep 13;
        # the renamer then omits the " - title" part.
        m = EpisodeMapper(S1_12)
        self.assertEqual(m.map(2, 1), (1, 13, ""))
        self.assertEqual(m.map(2, 2), (1, 14, ""))
        self.assertEqual(m.map(2, 12), (1, 24, ""))

    def test_absolute_with_title_when_tmdb_lists_it(self):
        # TMDB S1=12, S2=12; a file numbered continuously as S1E13
        # is absolute ep 13 -> S2E01, title found in TMDB.
        seasons = {1: {i: f"S1E{i}" for i in range(1, 13)},
                   2: {i: f"S2E{i}" for i in range(1, 13)}}
        m = EpisodeMapper(seasons)
        self.assertEqual(m.map(1, 13), (2, 1, "S2E1"))

    def test_overflow_lands_in_last_season(self):
        m = EpisodeMapper(S1_12)
        self.assertEqual(m.map(2, 13), (1, 25, ""))

    def test_direct_season_still_works(self):
        m = EpisodeMapper(S1_12)
        self.assertEqual(m.map(1, 5), (1, 5, "第5集"))
        self.assertEqual(m.map(None, 3), (1, 3, "第3集"))

    def test_multi_season_absolute(self):
        seasons = {1: {i: f"S1E{i}" for i in range(1, 13)},
                   2: {i: f"S2E{i}" for i in range(1, 13)}}
        m = EpisodeMapper(seasons)
        # S3E01: absolute 25 overflows into the last season (S2E13).
        self.assertEqual(m.map(3, 1), (2, 13, ""))

    def test_override_file(self):
        # orz.json: file S2 is really S1 with an explicit offset.
        m = EpisodeMapper(S1_12, {"season_map": {"2": 1},
                                  "episode_offset": {"2": 12}})
        self.assertEqual(m.map(2, 1), (1, 13, ""))
        # Explicit per-episode map wins over everything.
        m2 = EpisodeMapper(S1_12, {"episode_map": {"2x01": "1x13"}})
        self.assertEqual(m2.map(2, 1), (1, 13, ""))

    def test_absolute_mode_keeps_s2_in_season_01(self):
        # 青之箱：TMDB 有 S1=25、S2=5（标题在 S2 里），但 Season 01
        # 文件夹里已经有 S01E26-29（即 S2E01-04），所以这部剧按
        # 绝对编号走：S2E05 -> S01E30，集名仍取 S2E05 的标题。
        seasons = {1: {i: f"第{i}集" for i in range(1, 26)},
                   2: {1: "デジャブ", 2: "憧れ", 3: "1つのことだよ",
                       4: "好きは最強の感情", 5: "戻ってきたよ"}}
        m = EpisodeMapper(seasons)
        # Without absolute mode the direct hit would be (2, 5, ...).
        self.assertEqual(m.map(2, 5), (2, 5, "戻ってきたよ"))
        m.absolute_to = 1
        self.assertEqual(m.map(2, 5), (1, 30, "戻ってきたよ"))
        self.assertEqual(m.map(2, 1), (1, 26, "デジャブ"))


if __name__ == "__main__":
    unittest.main()
