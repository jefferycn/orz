"""v2.7.1 tests — lazy fingerprint, padded season folders, log noise.

- The fingerprint reads the first 4 MiB of every unorganised video;
  on a full-library startup scan that was ~4 GiB of page cache.  It
  is now lazy: only for a stored-mapping lookup or a gap-fill guess.
- Season folders standardise on padded ``Season 01``: an unpadded
  ``Season 1`` is renamed — or merged into an existing ``Season 01``
  — on the next scan, and new folders are always padded.
- The 922 ``episode X already exists`` warnings from the incident
  were files that were already correctly named; they are now skipped
  silently.  Non-media files (poster.jpg etc.) never warn.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import renamer as renamer_module
from app.cache import save_episodes
from app.mapper import EpisodeMapper
from app.nfo import (find_season_dir, generate_season_folders, generate_nfo,
                     normalize_season_dir, normalize_season_folders)
from app.renamer import existing_episodes, rename_season_folder
from app.watcher import Watcher, process_season_folder

SHOW = "Show"


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _show(parent: str, season_name: str = "Season 01"):
    """A show dir with nfo + episode cache; returns (show_dir, season)."""
    show_dir = os.path.join(parent, f"{SHOW} (2024)")
    season = os.path.join(show_dir, season_name)
    os.makedirs(season)
    generate_nfo({"name": SHOW, "original_name": SHOW, "id": 1,
                  "overview": "", "vote_average": 0, "vote_count": 0,
                  "first_air_date": "2024-01-01", "seasons": []}, show_dir)
    save_episodes(show_dir, 1, SHOW,
                  {1: {i: f"第{i}集" for i in range(1, 13)}})
    return show_dir, season


def _mapper():
    return EpisodeMapper({1: {i: f"第{i}集" for i in range(1, 13)}})


def _counting_fingerprint(calls):
    real = renamer_module._fingerprint

    def counting(path):
        calls.append(path)
        return real(path)

    return counting


class TestLazyFingerprint(unittest.TestCase):
    def test_parseable_file_reads_no_fingerprint(self):
        tmp = tempfile.mkdtemp()
        show_dir, season = _show(tmp)
        _write(os.path.join(season, f"[组] {SHOW} - 03 [1080p].mkv"),
               b"video-03")
        calls = []
        with mock.patch.object(renamer_module, "_fingerprint",
                               _counting_fingerprint(calls)):
            result = rename_season_folder(season, SHOW, show_dir, _mapper())
        self.assertEqual(calls, [])
        self.assertEqual(len(result.renamed), 1)
        self.assertTrue(os.path.exists(
            os.path.join(season, f"{SHOW} - S01E03 - 第3集.mkv")))

    def test_gap_fill_reads_fingerprint_once_and_records_it(self):
        tmp = tempfile.mkdtemp()
        show_dir, season = _show(tmp)
        _write(os.path.join(season, f"{SHOW} - random.mkv"), b"video-rand")
        calls = []
        with mock.patch.object(renamer_module, "_fingerprint",
                               _counting_fingerprint(calls)):
            result = rename_season_folder(season, SHOW, show_dir, _mapper())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result.renamed), 1)
        state = json.load(open(os.path.join(show_dir, ".orz", "state.json")))
        self.assertEqual(len(state["seasons"]["1"]["files"]), 1)

    def test_stored_mapping_lookup_reads_fingerprint(self):
        tmp = tempfile.mkdtemp()
        show_dir, season = _show(tmp)
        path = os.path.join(season, f"{SHOW} - random.mkv")
        _write(path, b"video-rand")
        fp = renamer_module._fingerprint(path)
        state_dir = os.path.join(show_dir, ".orz")
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "state.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"seasons": {"1": {"files": {fp: 5}, "pending": []}}}, f)
        calls = []
        with mock.patch.object(renamer_module, "_fingerprint",
                               _counting_fingerprint(calls)):
            result = rename_season_folder(season, SHOW, show_dir, _mapper())
        self.assertEqual(len(calls), 1)
        self.assertTrue(os.path.exists(
            os.path.join(season, f"{SHOW} - S01E05 - 第5集.mkv")))


class TestExistingEpisodesSeasonScoped(unittest.TestCase):
    def test_misplaced_season_file_does_not_claim_episode(self):
        tmp = tempfile.mkdtemp()
        season = os.path.join(tmp, "Season 1")
        os.makedirs(season)
        _write(os.path.join(season, "Show - S02E01 - Title.mkv"))
        _write(os.path.join(season, "Show - S01E02 - Title.mkv"))
        self.assertEqual(existing_episodes(season), {2})


class TestSeasonNormalize(unittest.TestCase):
    def test_both_exist_merge_into_padded(self):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Show (2024)")
        _write(os.path.join(show_dir, "Season 1", "a.mkv"))
        _write(os.path.join(show_dir, "Season 01", "b.mkv"))
        renamed = normalize_season_folders(show_dir)
        self.assertEqual(renamed, {"Season 1": "Season 01"})
        self.assertFalse(os.path.exists(os.path.join(show_dir, "Season 1")))
        self.assertTrue(os.path.exists(
            os.path.join(show_dir, "Season 01", "a.mkv")))
        self.assertTrue(os.path.exists(
            os.path.join(show_dir, "Season 01", "b.mkv")))

    def test_rename_only(self):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Show (2024)")
        _write(os.path.join(show_dir, "Season 2", "a.mkv"))
        renamed = normalize_season_folders(show_dir)
        self.assertEqual(renamed, {"Season 2": "Season 02"})
        self.assertTrue(os.path.exists(
            os.path.join(show_dir, "Season 02", "a.mkv")))

    def test_collision_leaves_file_behind(self):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Show (2024)")
        _write(os.path.join(show_dir, "Season 1", "same.mkv"), b"old")
        _write(os.path.join(show_dir, "Season 01", "same.mkv"), b"new")
        _write(os.path.join(show_dir, "Season 1", "other.mkv"))
        new_path = normalize_season_dir(os.path.join(show_dir, "Season 1"))
        # The colliding file stays put (never overwritten); the
        # source folder stays too, so the leftover still gets scanned.
        self.assertEqual(new_path, os.path.join(show_dir, "Season 1"))
        self.assertEqual(
            open(os.path.join(show_dir, "Season 1", "same.mkv"), "rb").read(),
            b"old")
        self.assertEqual(
            open(os.path.join(show_dir, "Season 01", "same.mkv"),
                 "rb").read(), b"new")
        self.assertTrue(os.path.exists(
            os.path.join(show_dir, "Season 01", "other.mkv")))

    def test_find_season_dir_prefers_padded(self):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Show (2024)")
        os.makedirs(os.path.join(show_dir, "Season 1"))
        os.makedirs(os.path.join(show_dir, "Season 01"))
        self.assertEqual(find_season_dir(show_dir, 1),
                         os.path.join(show_dir, "Season 01"))

    def test_generate_always_padded(self):
        tmp = tempfile.mkdtemp()
        show_dir = os.path.join(tmp, "Show (2024)")
        os.makedirs(show_dir)
        created = generate_season_folders(
            {"seasons": [{"season_number": 0}, {"season_number": 1},
                         {"season_number": 2}]}, show_dir)
        self.assertEqual(sorted(os.path.basename(p) for p in created),
                         ["Season 01", "Season 02", "Specials"])
        # A show that already has Season 1: normalize first, then
        # generate must not create a duplicate Season 01.
        show_dir2 = os.path.join(tmp, "Show2 (2024)")
        os.makedirs(os.path.join(show_dir2, "Season 1"))
        normalize_season_folders(show_dir2)
        created2 = generate_season_folders(
            {"seasons": [{"season_number": 1}]}, show_dir2)
        self.assertEqual(created2, [])
        self.assertTrue(os.path.isdir(os.path.join(show_dir2, "Season 01")))

    def test_scan_once_normalizes_unchanged_dir(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "Anime")
        show_dir, season = _show(root, season_name="Season 1")
        _write(os.path.join(season, "Show - S01E01 - 第1集.mkv"))
        w = Watcher(None, [root], stable_seconds=0)
        w.scan_once()
        self.assertFalse(os.path.exists(os.path.join(show_dir, "Season 1")))
        self.assertTrue(os.path.exists(
            os.path.join(show_dir, "Season 01", "Show - S01E01 - 第1集.mkv")))
        # Second scan (snapshot now recorded) does not resurrect it.
        w.scan_once()
        self.assertFalse(os.path.exists(os.path.join(show_dir, "Season 1")))


class TestLogNoise(unittest.TestCase):
    def test_jpg_files_produce_no_warnings(self):
        tmp = tempfile.mkdtemp()
        show_dir, season = _show(tmp)
        for name in ("poster.jpg", "season01-poster.jpg", "fanart.jpg"):
            _write(os.path.join(season, name), b"\xff\xd8\xff")
        with self.assertNoLogs("orz", level="WARNING"):
            result = process_season_folder(season)
        self.assertEqual(result.pending, [])
        self.assertEqual(result.renamed, [])

    def test_already_named_files_produce_no_warnings(self):
        # The incident's 922 "episode X already exists" warnings were
        # files that were already correctly named; they are skipped
        # silently now, not pended.
        tmp = tempfile.mkdtemp()
        show_dir, season = _show(tmp)
        _write(os.path.join(season, "Dragon Ball - S01E01 - Title.mkv"))
        _write(os.path.join(season, "Show - S01E02 - 第2集.mkv"))
        with self.assertNoLogs("orz", level="WARNING"):
            result = process_season_folder(season)
        self.assertEqual(result.pending, [])
        self.assertEqual(result.renamed, [])
        self.assertEqual(len(result.skipped), 2)


if __name__ == "__main__":
    unittest.main()
