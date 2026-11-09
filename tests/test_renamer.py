"""End-to-end tests for app.renamer — temp dirs, fake TMDB data, no network."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mapper import EpisodeMapper
from app.renamer import load_state, rename_season_folder

SHOW = "罗梅莉亚战记"
# 24 TMDB episodes in S1 (so S2E01 -> S01E13 works and gap-fill has room).
SEASONS = {1: {i: f"第{i}集" for i in range(1, 25)}}


def _season_dir(files: dict[str, bytes], organised: list[str] | None = None):
    tmp = tempfile.mkdtemp()
    show_dir = os.path.join(tmp, f"{SHOW} (2026)")
    season = os.path.join(show_dir, "Season 01")
    os.makedirs(season)
    for name, data in files.items():
        with open(os.path.join(season, name), "wb") as f:
            f.write(data)
    for name in organised or []:
        with open(os.path.join(season, name), "wb") as f:
            f.write(b"old")
    return tmp, show_dir, season


class TestRenamer(unittest.TestCase):
    def _run(self, season, show_dir, mapper=None):
        return rename_season_folder(season, SHOW, show_dir,
                                    mapper or EpisodeMapper(SEASONS))

    def test_gap_fill_lowest_gap(self):
        # Folder has 1,2,3,5 -> ambiguous file (no number) becomes 4.
        organised = [f"{SHOW} - S01E{n:02d} - 第{n}集.mkv" for n in (1, 2, 3, 5)]
        _, show_dir, season = _season_dir({"特别篇.mkv": b"x"}, organised)
        r = self._run(season, show_dir)
        self.assertEqual(len(r.renamed), 1)
        self.assertEqual(r.renamed[0][1], f"{SHOW} - S01E04 - 第4集.mkv")
        self.assertTrue(os.path.exists(os.path.join(season, r.renamed[0][1])))

    def test_max_plus_one(self):
        # Folder has 1-5 -> ambiguous file becomes 6.
        organised = [f"{SHOW} - S01E{n:02d} - 第{n}集.mkv" for n in range(1, 6)]
        _, show_dir, season = _season_dir({"未命名.mkv": b"y"}, organised)
        r = self._run(season, show_dir)
        self.assertEqual(r.renamed[0][1], f"{SHOW} - S01E06 - 第6集.mkv")

    def test_subtitle_grouping(self):
        # Video + .zh.srt + .en.ass sharing a base name move together.
        _, show_dir, season = _season_dir({
            "[组] 罗梅莉亚战记 - 02 [1080p].mkv": b"v",
            "[组] 罗梅莉亚战记 - 02 [1080p].zh.srt": b"s",
            "[组] 罗梅莉亚战记 - 02 [1080p].en.ass": b"a",
        })
        r = self._run(season, show_dir)
        self.assertEqual(len(r.renamed), 3)
        names = sorted(n for _, n in r.renamed)
        self.assertEqual(names, [
            f"{SHOW} - S01E02 - 第2集.en.ass",
            f"{SHOW} - S01E02 - 第2集.mkv",
            f"{SHOW} - S01E02 - 第2集.zh.srt",
        ])

    def test_s2_file_maps_absolute(self):
        # Fixture S1 lists 24 eps, so the absolute offset is 24:
        # S2E01 -> S01E25 (title unknown to TMDB, omitted).
        # (With a 12-ep S1 the same code gives S01E13 — see next test.)
        _, show_dir, season = _season_dir(
            {"罗梅莉亚战记 S02E01.mkv": b"v"})
        r = self._run(season, show_dir)
        self.assertEqual(r.renamed[0][1], f"{SHOW} - S01E25.mkv")

    def test_absolute_past_tmdb_list_omits_title(self):
        # TMDB S1 lists only 12 eps: S2E01 -> S01E13 with no title part.
        _, show_dir, season = _season_dir(
            {"罗梅莉亚战记 S02E01.mkv": b"v"})
        mapper = EpisodeMapper({1: {i: f"第{i}集" for i in range(1, 13)}})
        r = self._run(season, show_dir, mapper)
        self.assertEqual(r.renamed[0][1], f"{SHOW} - S01E13.mkv")

    def test_collision_is_pending_not_overwrite(self):
        organised = [f"{SHOW} - S01E02 - 第2集.mkv"]
        _, show_dir, season = _season_dir(
            {"罗梅莉亚战记 - 02.mkv": b"new"}, organised)
        r = self._run(season, show_dir)
        self.assertEqual(r.renamed, [])
        self.assertEqual(len(r.pending), 1)
        self.assertIn("already exists", r.pending[0]["reason"])
        # The existing file is untouched.
        with open(os.path.join(season, organised[0]), "rb") as f:
            self.assertEqual(f.read(), b"old")

    def test_already_organised_skipped(self):
        organised = [f"{SHOW} - S01E05 - 第5集.mkv"]
        _, show_dir, season = _season_dir({}, organised)
        r = self._run(season, show_dir)
        self.assertEqual(r.renamed, [])
        self.assertEqual(r.skipped, organised)

    def test_state_reuses_gap_fill(self):
        # Gap-fill assigns 4; a re-scan must not re-guess or re-rename.
        organised = [f"{SHOW} - S01E{n:02d} - 第{n}集.mkv" for n in (1, 2, 3, 5)]
        _, show_dir, season = _season_dir({"特别篇.mkv": b"x"}, organised)
        self._run(season, show_dir)
        state = load_state(show_dir)
        self.assertIn("1", state["seasons"])
        self.assertEqual(list(state["seasons"]["1"]["files"].values()), [4])
        r2 = self._run(season, show_dir)
        self.assertEqual(r2.renamed, [])
        # All organised files (the 4 pre-existing + the gap-filled one)
        # are skipped on re-scan.
        self.assertEqual(sorted(r2.skipped), sorted(
            [f"{SHOW} - S01E{n:02d} - 第{n}集.mkv" for n in (1, 2, 3, 4, 5)]))

    def test_dry_run_changes_nothing(self):
        _, show_dir, season = _season_dir({"罗梅莉亚战记 - 02.mkv": b"v"})
        r = rename_season_folder(season, SHOW, show_dir,
                                 EpisodeMapper(SEASONS), dry_run=True)
        self.assertEqual(len(r.renamed), 1)
        self.assertTrue(os.path.exists(os.path.join(season, "罗梅莉亚战记 - 02.mkv")))
        self.assertFalse(os.path.exists(os.path.join(show_dir, ".orz")))


class TestAoNoHakoScreenshot(unittest.TestCase):
    """青之箱 (2024) 实测场景：Season 01 里已有 S01E26-29，
    新文件是 [Tsukigakirei] Ao no Hako Season 2 - 05 ..."""

    def _setup(self, existing_eps, new_name, seasons):
        tmp = tempfile.mkdtemp()
        # 挂载路径带空格：/media/TV show/青之箱 (2024)/Season 01
        show_dir = os.path.join(tmp, "TV show", "青之箱 (2024)")
        season = os.path.join(show_dir, "Season 01")
        os.makedirs(season)
        titles = {26: "第 26 集", 27: "憧憬", 28: "只有一件事情哦",
                  29: "是最强的感情"}
        for ep in existing_eps:
            name = f"青之箱 - S01E{ep} - {titles.get(ep, f'第{ep}集')}.mp4"
            with open(os.path.join(season, name), "wb") as f:
                f.write(f"content-{ep}".encode())
        with open(os.path.join(season, new_name), "wb") as f:
            f.write(b"content-new")
        return show_dir, season, EpisodeMapper(seasons)

    def test_s2e05_becomes_s01e30_with_s2_title(self):
        seasons = {1: {i: f"第{i}集" for i in range(1, 26)},
                   2: {1: "デジャブ", 2: "憧れ", 3: "1つのことだよ",
                       4: "好きは最強の感情", 5: "戻ってきたよ"}}
        show_dir, season, mapper = self._setup(
            [26, 27, 28, 29],
            "[Tsukigakirei] Ao no Hako Season 2 - 05 "
            "[WebRip 1080P AVC-8bit AAC][CHS&JPN].mp4", seasons)
        r = rename_season_folder(season, "青之箱", show_dir, mapper)
        self.assertEqual(r.pending, [])
        self.assertEqual(r.renamed[0][1], "青之箱 - S01E30 - 戻ってきたよ.mp4")
        self.assertTrue(os.path.exists(
            os.path.join(season, "青之箱 - S01E30 - 戻ってきたよ.mp4")))

    def test_gap_fill_from_26_to_30_not_1(self):
        # 只有 26-29 时，无数字文件应补 30，不是 1。
        seasons = {1: {i: f"第{i}集" for i in range(1, 26)},
                   2: {i: f"S2E{i}" for i in range(1, 13)}}
        show_dir, season, mapper = self._setup(
            [26, 27, 28, 29], "未命名.mp4", seasons)
        r = rename_season_folder(season, "青之箱", show_dir, mapper)
        self.assertEqual(r.renamed[0][1], "青之箱 - S01E30 - S2E5.mp4")


class TestFansubBatches(unittest.TestCase):
    """三批实测文件名：ReLIFE 完結編、Medalist、Gundam Unicorn 字幕。"""

    def _run_show(self, show_title, folder, seasons, files):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Anime", folder)
        season = os.path.join(show_dir, "Season 01")
        os.makedirs(season)
        for name, data in files.items():
            with open(os.path.join(season, name), "wb") as f:
                f.write(data)
        r = rename_season_folder(season, show_title, show_dir,
                                 EpisodeMapper(seasons))
        return r, season

    def test_relife_final_arc_14_to_17(self):
        # TMDB S1 共 17 集：13 集 TV + 4 集完結編（14-17）。
        titles = {**{i: f"第{i}集" for i in range(1, 14)},
                  14: "Final Arc: Part 1 - Seed",
                  15: "Final Arc: Part 2 - Need",
                  16: "Final Arc: Part 3 - Date",
                  17: "Final Arc: Part 4 - Life"}
        files = {f"ReLIFE - S01E{ep:02d} - {titles[ep]}.mkv": f"old-{ep}".encode()
                 for ep in range(1, 14)}
        for n in (14, 15, 16, 17):
            files[f"[Snow-Raws] ReLIFE 完結編 第{n}話 "
                  f"(BD 1920x1080 HEVC-YUV420P10 FLAC).mkv"] = f"new-{n}".encode()
        r, _ = self._run_show("ReLIFE", "ReLIFE (2016)", {1: titles}, files)
        self.assertEqual(r.pending, [])
        self.assertEqual([n for _, n in r.renamed], [
            f"ReLIFE - S01E{n:02d} - {titles[n]}.mkv" for n in (14, 15, 16, 17)])

    def test_medalist_bracket_episodes(self):
        titles = {i: f"第{i}集" for i in range(1, 14)}
        files = {f"[DBD-Raws][Medalist][0{n}][1080P][BDRip][HEVC-10bit][FLAC].mkv":
                 f"m{n}".encode() for n in range(1, 6)}
        r, _ = self._run_show("Medalist", "Medalist (2025)", {1: titles}, files)
        self.assertEqual(r.pending, [])
        self.assertEqual([n for _, n in r.renamed], [
            f"Medalist - S01E{n:02d} - 第{n}集.mkv" for n in range(1, 6)])

    def test_gundam_unicorn_subtitles_only(self):
        titles = {1: "Day of the Unicorn", 2: "The Red Comet",
                  3: "The Ghost of Laplace",
                  4: "At the Bottom of the Gravity Well",
                  5: "The Black Unicorn",
                  6: "Two Worlds, Two Tomorrows", 7: "Over the Rainbow"}
        crcs = ["A85E5FC2", "56E3B7D1", "38E6824E", "A8FE087F",
                "EBC87C11", "A4208CAA", "F679FB7B"]
        files = {f"[POPGO][Mobile_Suit_Gundam_Unicorn][HEVC_10bit_AACx4]"
                 f"[1080P][BDRip][0{n}]({crcs[n-1]}).cht.ass": f"g{n}".encode()
                 for n in range(1, 8)}
        r, _ = self._run_show("Mobile Suit Gundam Unicorn",
                              "Mobile Suit Gundam Unicorn (2010)",
                              {1: titles}, files)
        self.assertEqual(r.pending, [])
        self.assertEqual([n for _, n in r.renamed], [
            f"Mobile Suit Gundam Unicorn - S01E{n:02d} - {titles[n]}.cht.ass"
            for n in range(1, 8)])

    def test_giji_harem_out_of_order_and_end(self):
        # 乱序给 01-12，"12 END" 里的 END 不影响集数。
        titles = {i: f"第{i}集" for i in range(1, 13)}
        files = {}
        for n in [7, 1, 8, 2, 9, 3, 10, 4, 11, 5, 12, 6]:
            end = " END" if n == 12 else ""
            files[f"[Moozzi2] Giji Harem - {n:02d}{end} "
                  f"(BD 1920x1080 x265-10Bit Flac).mkv"] = f"g{n}".encode()
        r, _ = self._run_show("Giji Harem", "Giji Harem (2024)",
                              {1: titles}, files)
        self.assertEqual(r.pending, [])
        self.assertEqual(sorted(n for _, n in r.renamed), [
            f"Giji Harem - S01E{n:02d} - 第{n}集.mkv" for n in range(1, 13)])

    def test_seishun_video_plus_sc_and_tc_subtitles(self):
        # 每集 1 视频 + .sc_v2 + .tc_v2 两份字幕：三者同组改名，
        # sc/tc 后缀保留，不能撞到同一个 .ass 目标。
        titles = {i: f"Ep{i}" for i in range(1, 14)}
        files = {}
        for n in range(1, 10):
            base = ("[DMG&VCB-Studio] Seishun Buta Yarou wa Bunny Girl "
                    f"Senpai no Yume wo Minai [0{n}][Ma10p_1080p][x265_flac]")
            files[f"{base}.mkv"] = f"v{n}".encode()
            files[f"{base}.sc_v2.ass"] = f"sc{n}".encode()
            files[f"{base}.tc_v2.ass"] = f"tc{n}".encode()
        r, _ = self._run_show("青春猪头少年不会梦到兔女郎学姐",
                              "青春猪头少年不会梦到兔女郎学姐 (2018)",
                              {1: titles}, files)
        self.assertEqual(r.pending, [])
        self.assertEqual(len(r.renamed), 27)
        self.assertIn(
            "青春猪头少年不会梦到兔女郎学姐 - S01E01 - Ep1.sc_v2.ass",
            [n for _, n in r.renamed])
        self.assertIn(
            "青春猪头少年不会梦到兔女郎学姐 - S01E01 - Ep1.tc_v2.ass",
            [n for _, n in r.renamed])


if __name__ == "__main__":
    unittest.main()
