"""v2.7 regression tests — the 2026-11-09 incident.

Startup scan renamed 55 files it should never have touched:
already-organised English-named files were treated as unprocessed,
refresh created ``Season 01`` next to the user's ``Season 1``,
gap-fill turned specials/previews into episodes, and absolute
mapping trusted TMDB's merged season lengths (青之箱 S2E05 ->
S01E42 instead of S01E30).  Each test here pins one fix.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import save_episodes
from app.mapper import EpisodeMapper
from app.nfo import find_season_dir, generate_season_folders, generate_nfo
from app.parser import is_already_named, looks_like_non_episode
from app.renamer import rename_season_folder
from app import watcher as watcher_module
from app.watcher import Watcher, load_snapshots

SHOW = "罗梅莉亚战记"


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class TestAlreadyNamed(unittest.TestCase):
    def test_sxxeyy_matching_folder_is_organised(self):
        self.assertTrue(is_already_named(
            "Dragon Ball - S02E15 - The Final Blow HDTV-720p.mkv", 2))
        self.assertTrue(is_already_named(
            "罗梅莉亚战记 - S01E05 - 第5集.mkv", 1))

    def test_scene_names_are_not_organised(self):
        # v2.7 counted any SxxEyy marker as organised, so a whole
        # season of scene release names was skipped silently (no
        # rename, no log).  The marker must sit in the clean
        # ``Title - SxxEyy`` form; dots and release tags mean the
        # file still needs normalising.
        self.assertFalse(is_already_named(
            "Attack on Titan S01E01.mkv", 1))
        self.assertFalse(is_already_named(
            "Desperate.Housewives.S04E01.Now.You.Know.1080p.WEB-DL."
            "x265.10bit.AAC.5.1-RCVR.ass", 4))
        self.assertFalse(is_already_named(
            "Desperate.Housewives.S04E11.Sunday.1080p.WEB-DL.x265."
            "10bit.AAC.5.1-RCVR[UTR].ass", 4))

    def test_sxxeyy_wrong_folder_is_not(self):
        self.assertFalse(is_already_named(
            "Dragon Ball - S02E15 - The Final Blow HDTV-720p.mkv", 1))
        self.assertFalse(is_already_named("Show - S01E05 - Title.mkv", 2))

    def test_eyy_alone_counts(self):
        # Only in the clean ``Title - E01`` form: the user's
        # ``A Sister's All You Need. E01 ...`` has no dash before
        # the marker, so it is a download name and gets renamed.
        self.assertTrue(is_already_named("Show - E01.mkv", 1))
        self.assertFalse(is_already_named(
            "A Sister's All You Need. E01 [A Sister is All You Need].mp4", 1))

    def test_fansub_names_still_need_renaming(self):
        self.assertFalse(is_already_named(
            "[组] 罗梅莉亚战记 - 05 [1080p].mkv", 1))
        self.assertFalse(is_already_named(
            "[Tsukigakirei] Ao no Hako Season 2 - 05 "
            "[WebRip 1080P AVC-8bit AAC][CHS&JPN].mp4", 1))
        # 第x話 is fansub naming, not an organised name (ReLIFE case).
        self.assertFalse(is_already_named(
            "[Snow-Raws] ReLIFE 完結編 第14話 (BD 1920x1080).mkv", 1))
        self.assertFalse(is_already_named(
            "Imouto Sae Ireba II Sp01.mp4", 1))


class TestNonEpisodeMarkers(unittest.TestCase):
    def test_markers(self):
        for name in (
                "Imouto Sae Ireba II Sp01.mp4",
                "30分钟前瞻PV 4K.mp4",
                "Sherlock.Season.Two.Extras.2011.Bluray.1080p.DD.5.1."
                "x264-BluHD.mkv",
                "Show - Special 01.mkv",
                "Show 预告.mp4",
                "Show 特典映像.mkv"):
            self.assertTrue(looks_like_non_episode(name), name)

    def test_normal_episodes_are_not_marked(self):
        for name in (
                "Dragon Ball - S02E15 - The Final Blow HDTV-720p.mkv",
                "[组] 罗梅莉亚战记 - 02 [1080p].mkv",
                "罗梅莉亚战记 - S01E05 - 第5集.mkv",
                "Show - 05.mkv"):
            self.assertFalse(looks_like_non_episode(name), name)


class TestSeasonDirReuse(unittest.TestCase):
    def test_find_prefers_padded(self):
        # v2.7 preferred the unpadded folder; v2.7.1 standardises on
        # padded (the unpadded one is merged away by normalize).
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Show (2020)")
        os.makedirs(os.path.join(show_dir, "Season 1"))
        os.makedirs(os.path.join(show_dir, "Season 01"))
        self.assertEqual(find_season_dir(show_dir, 1),
                         os.path.join(show_dir, "Season 01"))
        self.assertIsNone(find_season_dir(show_dir, 2))

    def test_generate_does_not_duplicate(self):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Show (2020)")
        os.makedirs(os.path.join(show_dir, "Season 1"))
        show = {"seasons": [{"season_number": 0},
                            {"season_number": 1},
                            {"season_number": 2}]}
        created = generate_season_folders(show, show_dir)
        # Season 1 exists (unpadded) -> no Season 01 created here
        # (normalize renames it instead); season 2 is new and is
        # always padded -> "Season 02".
        self.assertEqual(created, [os.path.join(show_dir, "Specials"),
                                   os.path.join(show_dir, "Season 02")])
        self.assertFalse(os.path.exists(os.path.join(show_dir, "Season 01")))
        self.assertTrue(os.path.exists(os.path.join(show_dir, "Specials")))

    def test_generate_default_is_padded(self):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Show (2020)")
        os.makedirs(show_dir)
        show = {"seasons": [{"season_number": 1}]}
        created = generate_season_folders(show, show_dir)
        self.assertEqual(created, [os.path.join(show_dir, "Season 01")])


class TestMapperAirDateSplit(unittest.TestCase):
    def _blue_box(self):
        # TMDB lists 青之箱 S1 as 37 episodes: 25 real S1
        # (weekly Oct 2024-Mar 2025) + 12 real S2 (weekly Oct-Dec 2026).
        from datetime import date, timedelta
        seasons = {1: {i: f"第{i}集" for i in range(1, 38)}}
        dates = {1: {}}
        start = date(2024, 10, 3)
        for i in range(1, 26):
            dates[1][i] = (start + timedelta(weeks=i - 1)).isoformat()
        start2 = date(2026, 10, 4)
        for i in range(26, 38):
            dates[1][i] = (start2 + timedelta(weeks=i - 26)).isoformat()
        return seasons, dates

    def test_blue_box_s2e05_is_30_not_42(self):
        seasons, dates = self._blue_box()
        mapper = EpisodeMapper(seasons, air_dates=dates)
        self.assertEqual(mapper.map(2, 5), (1, 30, "第30集"))
        self.assertEqual(mapper.map(2, 1), (1, 26, "第26集"))
        # Direct hit on the merged TMDB listing still works.
        self.assertEqual(mapper.map(1, 30), (1, 30, "第30集"))

    def test_without_air_dates_old_behaviour(self):
        # No dates in the cache (pre-v2.7): the merged count is all
        # the mapper has, so S2E05 -> 42.  A refresh fills the dates
        # and the split above fixes it.
        seasons, _ = self._blue_box()
        mapper = EpisodeMapper(seasons)
        self.assertEqual(mapper.map(2, 5), (1, 42, ""))

    def test_cour_break_does_not_split(self):
        # Two cours of one season, ~2 weeks apart over New Year:
        # no split, S2E01 -> 25.
        seasons = {1: {i: f"第{i}集" for i in range(1, 25)}}
        dates = {1: {i: "2024-12-26" for i in range(1, 13)}}
        dates[1].update({i: "2025-01-09" for i in range(13, 25)})
        mapper = EpisodeMapper(seasons, air_dates=dates)
        self.assertEqual(mapper.map(2, 1), (1, 25, ""))

    def test_dragon_ball_no_split(self):
        # All 153 episodes under TMDB S1, weekly airing, no long gap:
        # S2E01 -> S01E154 (the file's own Season 2 folder is what
        # protects these files in practice — see TestAlreadyNamed).
        seasons = {1: {i: f"第{i}集" for i in range(1, 154)}}
        mapper = EpisodeMapper(seasons)
        self.assertEqual(mapper.map(2, 1), (1, 154, ""))


class TestGapFillBlocked(unittest.TestCase):
    def _season(self, files, organised, mapper=None):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, f"{SHOW} (2026)")
        season = os.path.join(show_dir, "Season 01")
        os.makedirs(season)
        for name, data in files.items():
            _write(os.path.join(season, name), data)
        for name in organised:
            _write(os.path.join(season, name), b"old")
        return show_dir, season, mapper or EpisodeMapper(
            {1: {i: f"第{i}集" for i in range(1, 25)}})

    def test_sp_files_are_pending_not_gap_filled(self):
        organised = [f"{SHOW} - S01E{n:02d} - 第{n}集.mkv"
                     for n in range(1, 13)]
        show_dir, season, mapper = self._season(
            {"Imouto Sae Ireba II Sp01.mp4": b"v"}, organised)
        r = rename_season_folder(season, SHOW, show_dir, mapper)
        self.assertEqual(r.renamed, [])
        self.assertEqual(len(r.pending), 1)
        self.assertIn("special", r.pending[0]["reason"])

    def test_preview_pv_is_pending(self):
        show_dir = os.path.join(tempfile.mkdtemp(), "灵笼 (2019)")
        season = os.path.join(show_dir, "Specials")
        os.makedirs(season)
        organised = [f"Incarnation - S00E{n:02d}.mp4" for n in range(1, 5)]
        for name in organised:
            _write(os.path.join(season, name), b"old")
        _write(os.path.join(season, "30分钟前瞻PV 4K.mp4"), b"v")
        mapper = EpisodeMapper({0: {i: f"第{i}话" for i in range(1, 7)}})
        r = rename_season_folder(season, "灵笼", show_dir, mapper)
        self.assertEqual(r.renamed, [])
        self.assertEqual(len(r.pending), 1)

    def test_rename_moves_to_unpadded_season_folder(self):
        # File in "Season 2" mapping to S01E13 must land in the
        # existing season-1 folder, never a newly created one.  (At
        # this level the folder is still "Season 1"; in production
        # process_season_folder normalizes it to "Season 01" first.)
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, f"{SHOW} (2026)")
        s1 = os.path.join(show_dir, "Season 1")
        s2 = os.path.join(show_dir, "Season 2")
        os.makedirs(s1)
        os.makedirs(s2)
        for n in range(1, 13):
            _write(os.path.join(s1, f"{SHOW} - S01E{n:02d} - 第{n}集.mkv"),
                   b"old")
        _write(os.path.join(s2, f"[组] {SHOW} Season 2 - 01.mkv"), b"v")
        mapper = EpisodeMapper({1: {i: f"第{i}集" for i in range(1, 13)}})
        r = rename_season_folder(s2, SHOW, show_dir, mapper)
        self.assertEqual(r.renamed[0][1], f"{SHOW} - S01E13.mkv")
        self.assertTrue(os.path.exists(
            os.path.join(s1, f"{SHOW} - S01E13.mkv")))
        self.assertFalse(os.path.exists(os.path.join(show_dir, "Season 01")))


class TestSnapshotGating(unittest.TestCase):
    def test_second_scan_skips_unchanged_dir(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "Anime")
        show_dir = os.path.join(root, f"{SHOW} (2026)")
        season = os.path.join(show_dir, "Season 01")
        os.makedirs(season)
        generate_nfo({"name": SHOW, "original_name": SHOW, "id": 1,
                      "overview": "", "vote_average": 0, "vote_count": 0,
                      "first_air_date": "2026-01-01", "seasons": []},
                     show_dir)
        save_episodes(show_dir, 1, SHOW,
                      {1: {i: f"第{i}集" for i in range(1, 25)}})
        _write(os.path.join(season, f"[组] {SHOW} - 02 [1080p].mkv"), b"v")
        calls = []
        orig = watcher_module.process_season_folder

        def counting(*args, **kwargs):
            calls.append(args[0])
            return orig(*args, **kwargs)

        watcher_module.process_season_folder = counting
        try:
            w = Watcher(None, [root], stable_seconds=0)
            w.scan_once()
            self.assertEqual(len(calls), 1)
            self.assertTrue(os.path.exists(
                os.path.join(season, f"{SHOW} - S01E02 - 第2集.mkv")))
            # Nothing changed: the second scan must not process it.
            w.scan_once()
            self.assertEqual(len(calls), 1)
            # A new file arrives: the third scan processes again.
            _write(os.path.join(season, f"[组] {SHOW} - 03 [1080p].mkv"),
                   b"w")
            w.scan_once()
            self.assertEqual(len(calls), 2)
            self.assertTrue(os.path.exists(
                os.path.join(season, f"{SHOW} - S01E03 - 第3集.mkv")))
            # And the snapshot file was written.
            self.assertIn("Season 01", load_snapshots(show_dir))
        finally:
            watcher_module.process_season_folder = orig


class TestAirDateBackfill(unittest.TestCase):
    def test_v26_cache_without_dates_gets_backfilled(self):
        # The cache v2.6 wrote for 青之箱 has 37 titles and no air
        # dates.  On the first v2.7 run the dates are backfilled
        # (only the dates — no nfo rewrite, no season folders) and
        # S2E05 maps to S01E30, not S01E42.
        from datetime import date, timedelta
        from app.cache import load_episodes
        from app.nfo import generate_nfo
        from app.watcher import process_season_folder

        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "青之箱 (2024)")
        season = os.path.join(show_dir, "Season 01")
        os.makedirs(season)
        generate_nfo({"name": "青之箱", "original_name": "青之箱",
                      "id": 207347, "overview": "", "vote_average": 0,
                      "vote_count": 0, "first_air_date": "2024-10-03",
                      "seasons": []}, show_dir)
        seasons = {1: {i: f"第{i}集" for i in range(1, 38)}}
        seasons[1][30] = "戻ってきたよ"
        # v2.6-style cache: titles only, no air_dates.
        save_episodes(show_dir, 207347, "青之箱", seasons)
        self.assertEqual(load_episodes(show_dir)["air_dates"], {})
        for n in range(26, 30):
            _write(os.path.join(season, f"青之箱 - S01E{n} - 第{n}集.mp4"),
                   b"old")
        _write(os.path.join(
            season,
            "[Tsukigakirei] Ao no Hako Season 2 - 05 "
            "[WebRip 1080P AVC-8bit AAC][CHS&JPN].mp4"), b"v")

        class FakeTmdb:
            def all_air_dates(self, show_id):
                dates = {1: {}}
                for i in range(1, 26):
                    dates[1][i] = (
                        date(2024, 10, 3) + timedelta(weeks=i - 1)
                    ).isoformat()
                for i in range(26, 38):
                    dates[1][i] = (
                        date(2026, 10, 4) + timedelta(weeks=i - 26)
                    ).isoformat()
                return dates

        r = process_season_folder(season, client=FakeTmdb())
        self.assertEqual([n for _, n in r.renamed],
                         ["青之箱 - S01E30 - 戻ってきたよ.mp4"])
        self.assertTrue(load_episodes(show_dir)["air_dates"])


class TestRestoreTool(unittest.TestCase):
    def test_dry_run_then_apply(self):
        spec = importlib.util.spec_from_file_location(
            "restore_from_log",
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "tools", "restore_from_log.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        base = tempfile.mkdtemp()
        # Take 3 mappings, remap /media -> temp tree, create the NEW
        # files where orz left them.
        subset = mod.RESTORE[:3]
        remapped = []
        for old, new in subset:
            new_old = base + old[len("/media"):]
            remapped.append((new_old, new))
            show_dir = os.path.dirname(os.path.dirname(new_old))
            _write(os.path.join(show_dir, "Season 01", new), b"v")
        mod.RESTORE = remapped
        mod.ROOTS = [os.path.join(base, "Anime"),
                     os.path.join(base, "TV show")]
        mod.AFFECTED_SHOWS = sorted(
            {os.path.dirname(os.path.dirname(o)) for o, _ in remapped})
        self.assertEqual(mod.main(["--dry-run"]), 0)
        self.assertFalse(any(os.path.exists(o) for o, _ in remapped))
        self.assertEqual(mod.main([]), 0)
        self.assertTrue(all(os.path.exists(o) for o, _ in remapped))


if __name__ == "__main__":
    unittest.main()
