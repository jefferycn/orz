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


if __name__ == "__main__":
    unittest.main()
