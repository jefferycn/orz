"""v2.7.4 tests — season.nfo merge dedup.

The new log (orz-2.csv, 2026-11-09 06:22 UTC) showed the one leftover
problem after the v2.7.2 fix: dozens of ``Season 1`` folders survived
the merge into ``Season 01`` because both folders contain a
``season.nfo``.  orz never writes season.nfo (Jellyfin/Kodi do), so
the copy in the retired folder is dead data — but only when it is
provably the same season's record: identical bytes, or the same
<seasonnumber>.  Anything else is left in place, like any other
colliding file.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nfo import normalize_season_dir

NFO_1 = (b'<?xml version="1.0" encoding="utf-8"?>\n'
         b'<season><seasonnumber>1</seasonnumber>'
         b'<title>Season 1</title></season>')
NFO_1_OTHER_TITLE = (b'<?xml version="1.0" encoding="utf-8"?>\n'
                     b'<season><seasonnumber>1</seasonnumber>'
                     b'<title>Season 01</title></season>')
NFO_2 = (b'<?xml version="1.0" encoding="utf-8"?>\n'
         b'<season><seasonnumber>2</seasonnumber>'
         b'<title>Season 2</title></season>')


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class TestSeasonNfoMerge(unittest.TestCase):
    def _show(self, tmp):
        show_dir = os.path.join(tmp, "Show (2024)")
        return show_dir

    def test_identical_season_nfo_removed(self):
        tmp = tempfile.mkdtemp()
        show_dir = self._show(tmp)
        _write(os.path.join(show_dir, "Season 1", "season.nfo"), NFO_1)
        _write(os.path.join(show_dir, "Season 01", "season.nfo"), NFO_1)
        new_path = normalize_season_dir(os.path.join(show_dir, "Season 1"))
        self.assertEqual(new_path, os.path.join(show_dir, "Season 01"))
        self.assertFalse(os.path.exists(os.path.join(show_dir, "Season 1")))
        self.assertEqual(
            open(os.path.join(show_dir, "Season 01", "season.nfo"),
                 "rb").read(), NFO_1)

    def test_same_season_number_different_bytes_removed(self):
        tmp = tempfile.mkdtemp()
        show_dir = self._show(tmp)
        _write(os.path.join(show_dir, "Season 1", "season.nfo"), NFO_1)
        _write(os.path.join(show_dir, "Season 01", "season.nfo"),
               NFO_1_OTHER_TITLE)
        new_path = normalize_season_dir(os.path.join(show_dir, "Season 1"))
        self.assertEqual(new_path, os.path.join(show_dir, "Season 01"))
        self.assertFalse(os.path.exists(os.path.join(show_dir, "Season 1")))
        # The canonical folder's own copy is kept, not overwritten.
        self.assertEqual(
            open(os.path.join(show_dir, "Season 01", "season.nfo"),
                 "rb").read(), NFO_1_OTHER_TITLE)

    def test_different_season_number_left_behind(self):
        tmp = tempfile.mkdtemp()
        show_dir = self._show(tmp)
        # A season 2 record sitting in Season 1: not a duplicate.
        _write(os.path.join(show_dir, "Season 1", "season.nfo"), NFO_2)
        _write(os.path.join(show_dir, "Season 01", "season.nfo"), NFO_1)
        new_path = normalize_season_dir(os.path.join(show_dir, "Season 1"))
        self.assertEqual(new_path, os.path.join(show_dir, "Season 1"))
        self.assertTrue(os.path.exists(os.path.join(show_dir, "Season 1")))
        self.assertEqual(
            open(os.path.join(show_dir, "Season 1", "season.nfo"),
                 "rb").read(), NFO_2)

    def test_unparseable_season_nfo_left_behind(self):
        tmp = tempfile.mkdtemp()
        show_dir = self._show(tmp)
        _write(os.path.join(show_dir, "Season 1", "season.nfo"),
               b"not xml at all")
        _write(os.path.join(show_dir, "Season 01", "season.nfo"), NFO_1)
        new_path = normalize_season_dir(os.path.join(show_dir, "Season 1"))
        self.assertEqual(new_path, os.path.join(show_dir, "Season 1"))
        self.assertTrue(os.path.exists(os.path.join(show_dir, "Season 1")))

    def test_non_nfo_collision_still_left(self):
        tmp = tempfile.mkdtemp()
        show_dir = self._show(tmp)
        _write(os.path.join(show_dir, "Season 1", "same.mkv"), b"old")
        _write(os.path.join(show_dir, "Season 01", "same.mkv"), b"new")
        _write(os.path.join(show_dir, "Season 1", "season.nfo"), NFO_1)
        _write(os.path.join(show_dir, "Season 01", "season.nfo"), NFO_1)
        new_path = normalize_season_dir(os.path.join(show_dir, "Season 1"))
        # season.nfo is deduped, but the video collision still holds
        # the old folder open.
        self.assertEqual(new_path, os.path.join(show_dir, "Season 1"))
        self.assertFalse(os.path.exists(
            os.path.join(show_dir, "Season 1", "season.nfo")))
        self.assertEqual(
            open(os.path.join(show_dir, "Season 1", "same.mkv"),
                 "rb").read(), b"old")


if __name__ == "__main__":
    unittest.main()
