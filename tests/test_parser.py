"""Tests for app.parser — no network, stdlib unittest only."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.parser import (
    base_name, is_organized, lang_suffix, organized_episode,
    parse_episode, parse_folder_name, season_number_from_folder,
)

SHOW = "罗梅莉亚战记"


class TestFolderName(unittest.TestCase):
    def test_title_year(self):
        self.assertEqual(parse_folder_name("绝望主妇 (2004)"), ("绝望主妇", 2004))
        self.assertEqual(parse_folder_name("罗梅莉亚战记 (2026)"), ("罗梅莉亚战记", 2026))

    def test_no_year(self):
        self.assertEqual(parse_folder_name("Some Show"), ("Some Show", None))


class TestParser(unittest.TestCase):
    def test_bracket_group_and_resolution(self):
        # Required case: [组] ... - 02 [1080p] -> 2, not 1080, not 02-as-group
        g = parse_episode("[组] 罗梅莉亚战记 - 02 [1080p].mkv", SHOW)
        self.assertEqual((g.episode, g.season), (2, None))

    def test_sxxeyy_priority(self):
        g = parse_episode("罗梅莉亚战记 - S01E05 - 标题.mkv", SHOW)
        self.assertEqual((g.episode, g.season, g.confidence), (5, 1, "high"))

    def test_720_and_1080_are_not_episodes(self):
        g = parse_episode("Show 2024 1080p 03.mkv", "Show")
        self.assertEqual(g.episode, 3)
        g = parse_episode("[Group] Show - 720p - 07.mkv", "Show")
        self.assertEqual(g.episode, 7)

    def test_codec_and_year_stripped(self):
        g = parse_episode("Show - 04 x265 10bit WEB-DL.mkv", "Show")
        self.assertEqual(g.episode, 4)

    def test_e_alone_and_chinese(self):
        self.assertEqual(parse_episode("Show E07.mkv", "Show").episode, 7)
        self.assertEqual(parse_episode("Show 第5集.mkv", "Show").episode, 5)
        self.assertEqual(parse_episode("Show 第2季第3集.mkv", "Show").episode, 3)
        self.assertEqual(parse_episode("Show 第2季第3集.mkv", "Show").season, 2)

    def test_season_only_marker(self):
        g = parse_episode("Show S2 01.mkv", "Show")
        self.assertEqual((g.episode, g.season), (1, 2))

    def test_season_word_marker(self):
        # 青之箱实测：Season 2 - 05 里的 2 是季、05 是集，不能判 ambiguous。
        f = "[Tsukigakirei] Ao no Hako Season 2 - 05 [WebRip 1080P AVC-8bit AAC][CHS&JPN].mp4"
        g = parse_episode(f, "青之箱")
        self.assertEqual((g.episode, g.season), (5, 2))
        g = parse_episode("[Erai-raws] Ao no Hako Season 2 - 04 [1080p].mp4", "青之箱")
        self.assertEqual((g.episode, g.season), (4, 2))
        g = parse_episode("青之箱 第二季 - 05.mp4", "青之箱")
        self.assertEqual((g.episode, g.season), (5, 2))
        g = parse_episode("Show Season 2 Episode 5.mkv", "Show")
        self.assertEqual((g.episode, g.season), (5, 2))

    def test_bracketed_episode_number(self):
        # 字幕组常把集数写在方括号里：[01] 在 _clean 剥方括号时
        # 会被一起剥掉，所以必须在剥之前先提取。以前这几类全是
        # "no number found"，只能靠补位猜，单集先到就猜错。
        g = parse_episode(
            "[DBD-Raws][Medalist][01][1080P][BDRip][HEVC-10bit][FLAC].mkv",
            "Medalist")
        self.assertEqual(g.episode, 1)
        g = parse_episode(
            "[DBD-Raws][Medalist][05][1080P][BDRip][HEVC-10bit][FLAC].mkv",
            "Medalist")
        self.assertEqual(g.episode, 5)
        # 字幕：[01] 是集数，(A85E5FC2) 是 CRC 校验码，不是集数。
        g = parse_episode(
            "[POPGO][Mobile_Suit_Gundam_Unicorn][HEVC_10bit_AACx4][1080P]"
            "[BDRip][01](A85E5FC2).cht.ass", "Mobile Suit Gundam Unicorn")
        self.assertEqual(g.episode, 1)
        g = parse_episode(
            "[POPGO][Mobile_Suit_Gundam_Unicorn][HEVC_10bit_AACx4][1080P]"
            "[BDRip][07](F679FB7B).cht.ass", "Mobile Suit Gundam Unicorn")
        self.assertEqual(g.episode, 7)
        # [1080P] 不是纯数字组，不会被当成集数。
        g = parse_episode("[Group] Show [1080P] - 03.mkv", "Show")
        self.assertEqual(g.episode, 3)

    def test_sc_tc_subtitle_lang_tags(self):
        # .sc_v2 / .tc_v2 是简繁中文字幕，必须认成语言后缀：
        # 剥掉后视频和两份字幕才同组，不剥则两份字幕都撞到同一个
        # 目标名，pending。v2 本身不是语言标记。
        self.assertEqual(lang_suffix("T.sc_v2.ass"), ".sc_v2")
        self.assertEqual(lang_suffix("T.tc_v2.ass"), ".tc_v2")
        self.assertEqual(lang_suffix("T.sc.ass"), ".sc")
        self.assertEqual(base_name("T.sc_v2.ass"), "T")
        self.assertEqual(base_name("T.tc_v2.ass"), "T")
        self.assertEqual(lang_suffix("Title.v2.mkv"), "")
        self.assertEqual(lang_suffix("Title.zh.srt"), ".zh")

    def test_digit_named_show(self):
        # Show "24": the 24 is the show, 05 is the episode.
        self.assertEqual(parse_episode("24 - 05.mkv", "24").episode, 5)
        self.assertEqual(parse_episode("[G] 24 - 05 [1080p].mkv", "24").episode, 5)
        self.assertEqual(parse_episode("86 - 02.mkv", "86").episode, 2)

    def test_no_number_is_not_01(self):
        # The old fallback to 01 is gone: no number -> episode None.
        g = parse_episode("罗梅莉亚战记 - 特别篇.mkv", SHOW)
        self.assertIsNone(g.episode)
        self.assertEqual(g.confidence, "none")

    def test_ambiguous_numbers_not_guessed(self):
        g = parse_episode("Show 03 04.mkv", "Show")
        self.assertIsNone(g.episode)


class TestOrganized(unittest.TestCase):
    def test_already_organised_skip(self):
        name = "罗梅莉亚战记 - S01E05 - 标题.mkv"
        self.assertTrue(is_organized(name, SHOW))
        self.assertEqual(organized_episode(name), (1, 5))
        self.assertFalse(is_organized("罗梅莉亚战记 - 05.mkv", SHOW))

    def test_season_folder(self):
        self.assertEqual(season_number_from_folder("Season 01"), 1)
        self.assertEqual(season_number_from_folder("Specials"), 0)
        self.assertIsNone(season_number_from_folder("Extras"))

    def test_subtitle_lang_suffix(self):
        self.assertEqual(base_name("Title.zh.srt"), "Title")
        self.assertEqual(base_name("Title.en.ass"), "Title")
        self.assertEqual(base_name("Title.mkv"), "Title")
        self.assertEqual(lang_suffix("Title.zh.srt"), ".zh")
        self.assertEqual(lang_suffix("Title.srt"), "")


if __name__ == "__main__":
    unittest.main()
