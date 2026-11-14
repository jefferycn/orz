"""v2.8.0 tests — arc-title match for arc-relative episode numbers.

A bracket / bare episode number can be relative to the current arc,
not the whole show: ``[GM-Team][国漫][凡人修仙传 慕兰之战][...][24]``
is 慕兰之战 #24, which is TMDB S01E200 (titled ``慕兰之战24``), not
S01E24.  Before this version orz parsed 24, mapped it to S01E24, and
pended it ("episode 24 already exists") because the user already had
199 episodes.  Now a filename text segment + the parsed number is
compared against TMDB episode titles: an exact, unique full-title
match wins over the bare number.  The arc name is never hardcoded —
it comes from the filename, the titles from the TMDB cache — so the
rule is the same for every show and only fires where the data
supports it.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import save_episodes
from app.mapper import EpisodeMapper
from app.nfo import generate_nfo
from app.parser import title_candidates
from app.watcher import process_season_folder

GM_FILE = ("[GM-Team][国漫][凡人修仙传 慕兰之战][Fan Ren Xiu Xian Zhuan]"
           "[2026][24][AVC][GB][1080P].mkv")

# Small slice of the real 凡人修仙传 cache (TMDB 106449, S1 = 210 eps).
SEASONS = {1: {
    1: "风起天南1",
    2: "风起天南2",
    24: "魔道争锋3",
    177: "慕兰之战01",
    199: "慕兰之战23",
    200: "慕兰之战24",
    201: "慕兰之战25",
}}


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _show(tmp: str, seasons: dict | None = None,
          title: str = "凡人修仙传", year: int = 2020):
    show_dir = os.path.join(tmp, f"{title} ({year})")
    season_dir = os.path.join(show_dir, "Season 01")
    os.makedirs(season_dir)
    generate_nfo({"name": title, "original_name": title,
                  "id": 106449, "overview": "", "vote_average": 0,
                  "vote_count": 0, "first_air_date": f"{year}-01-01",
                  "seasons": []}, show_dir)
    save_episodes(show_dir, 106449, title, seasons or SEASONS)
    return show_dir, season_dir


class TestTitleCandidates(unittest.TestCase):
    def test_gm_team_filename_yields_arc(self):
        cands = title_candidates(GM_FILE, "凡人修仙传")
        self.assertIn("慕兰之战", cands)
        # Pure-digit groups are numbers, not arc text.
        self.assertNotIn("2026", cands)
        self.assertNotIn("24", cands)

    def test_non_bracket_form_yields_arc(self):
        cands = title_candidates("凡人修仙传 慕兰之战 24.mkv", "凡人修仙传")
        self.assertIn("慕兰之战", cands)


class TestMapByTitle(unittest.TestCase):
    def test_gm_team_24_is_e200(self):
        mapper = EpisodeMapper(SEASONS)
        self.assertEqual(
            mapper.map_by_title(GM_FILE, 24, 1, "凡人修仙传"),
            (1, 200, "慕兰之战24"))

    def test_padded_number_matches(self):
        # 慕兰之战01 is padded; a [01] file must still find it.
        mapper = EpisodeMapper(SEASONS)
        f = ("[GM-Team][国漫][凡人修仙传 慕兰之战][Fan Ren Xiu Xian Zhuan]"
             "[2026][01][AVC][GB][1080P].mkv")
        self.assertEqual(
            mapper.map_by_title(f, 1, 1, "凡人修仙传"),
            (1, 177, "慕兰之战01"))

    def test_descriptive_titles_never_match(self):
        # 青之箱-style titles carry no arc + number: no match, and
        # the caller falls back to the number mapping unchanged.
        mapper = EpisodeMapper({1: {30: "我回来啦", 24: "魔道争锋3"}})
        self.assertIsNone(
            mapper.map_by_title(GM_FILE, 24, 1, "凡人修仙传"))

    def test_ambiguous_match_is_no_match(self):
        # Both "A1" and "A01" exist: two hits for number 1, so the
        # match is ambiguous and must not fire.
        mapper = EpisodeMapper({1: {5: "A1", 6: "A01"}})
        self.assertIsNone(
            mapper.map_by_title("[Group][A][01].mkv", 1, 1, "A"))

    def test_other_season_titles_not_searched(self):
        mapper = EpisodeMapper({1: {24: "魔道争锋3"},
                                2: {200: "慕兰之战24"}})
        self.assertIsNone(
            mapper.map_by_title(GM_FILE, 24, 1, "凡人修仙传"))


class TestRenameWithTitleMatch(unittest.TestCase):
    def test_gm_file_renames_to_e200_not_e24(self):
        tmp = tempfile.mkdtemp()
        _show_dir, season_dir = _show(tmp)
        # E24 already held, as in the user's library (199 eps).
        _write(os.path.join(
            season_dir, "凡人修仙传 - S01E24 - 魔道争锋3.mkv"))
        _write(os.path.join(season_dir, GM_FILE))
        result = process_season_folder(season_dir)
        self.assertEqual(result.pending, [])
        self.assertEqual(
            result.renamed,
            [(GM_FILE, "凡人修仙传 - S01E200 - 慕兰之战24.mkv")])
        self.assertTrue(os.path.exists(os.path.join(
            season_dir, "凡人修仙传 - S01E200 - 慕兰之战24.mkv")))

    def test_sxxeyy_marker_is_not_overridden(self):
        # An explicit SxxEyy marker keeps its own path even when
        # the filename also carries an arc name: S01E24 maps by
        # number and collides with the held E24.
        tmp = tempfile.mkdtemp()
        _show_dir, season_dir = _show(tmp)
        _write(os.path.join(
            season_dir, "凡人修仙传 - S01E24 - 魔道争锋3.mkv"))
        f = "[GM-Team][凡人修仙传 慕兰之战] S01E24 [1080P].mkv"
        _write(os.path.join(season_dir, f))
        result = process_season_folder(season_dir)
        self.assertEqual(result.renamed, [])
        self.assertEqual(len(result.pending), 1)
        self.assertIn("already exists", result.pending[0]["reason"])
        self.assertFalse(os.path.exists(os.path.join(
            season_dir, "凡人修仙传 - S01E200 - 慕兰之战24.mkv")))

    def test_bracket_without_arc_unchanged(self):
        # No arc text in the name: the old number mapping applies.
        tmp = tempfile.mkdtemp()
        _show(tmp, seasons={1: {1: "第1集", 2: "第2集"}},
              title="Medalist", year=2025)
        season_dir = os.path.join(tmp, "Medalist (2025)", "Season 01")
        f = "[DBD-Raws][Medalist][01][1080P].mkv"
        _write(os.path.join(season_dir, f))
        result = process_season_folder(season_dir)
        self.assertEqual(result.pending, [])
        self.assertEqual(result.renamed,
                         [(f, "Medalist - S01E01 - 第1集.mkv")])


if __name__ == "__main__":
    unittest.main()
