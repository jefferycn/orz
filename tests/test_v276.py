"""v2.7.6 tests — read Jellyfin/Kodi tvshow.nfo id forms.

The user renamed Spartacus / The Liberator and the log kept
saying "No tvshow.nfo" even though the file was clearly there:
``read_nfo`` only accepted ``<tmdbid>``, but Jellyfin/Kodi also
write the id as ``<tmdb>`` or ``<uniqueid type="tmdb">``.  The
message also fired once per season folder per pass (four times
in two seconds after a folder rename), so the unusable-nfo
warning is now throttled to once an hour per show.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.watcher as watcher_module
from app.nfo import read_nfo
from app.watcher import process_season_folder

NFO_TMDBID = ('<?xml version="1.0" encoding="utf-8"?>\n'
              '<tvshow><title>Spartacus</title>'
              '<tmdbid>12345</tmdbid></tvshow>')
NFO_TMDB = ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<tvshow><title>Spartacus</title>'
            '<tmdb>12345</tmdb></tvshow>')
NFO_UNIQUEID = ('<?xml version="1.0" encoding="utf-8"?>\n'
                '<tvshow><title>Spartacus</title>'
                '<uniqueid type="tvdb">123456</uniqueid>'
                '<uniqueid type="tmdb">12345</uniqueid></tvshow>')
NFO_TVDB_ONLY = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<tvshow><title>Spartacus</title>'
                 '<id>123456</id></tvshow>')


def _write(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)


class TestReadNfo(unittest.TestCase):
    def _show(self, tmp, nfo):
        show_dir = os.path.join(tmp, "Show (2010)")
        if nfo is not None:
            _write(os.path.join(show_dir, "tvshow.nfo"), nfo)
        return show_dir

    def test_tmdbid(self):
        self.assertEqual(
            read_nfo(self._show(tempfile.mkdtemp(), NFO_TMDBID)),
            {"tmdbid": 12345, "title": "Spartacus"})

    def test_tmdb_tag(self):
        self.assertEqual(
            read_nfo(self._show(tempfile.mkdtemp(), NFO_TMDB)),
            {"tmdbid": 12345, "title": "Spartacus"})

    def test_uniqueid_tmdb(self):
        self.assertEqual(
            read_nfo(self._show(tempfile.mkdtemp(), NFO_UNIQUEID)),
            {"tmdbid": 12345, "title": "Spartacus"})

    def test_whitespace_stripped(self):
        nfo = ('<tvshow><title>  Spartacus </title>'
               '<tmdbid> 12345 </tmdbid></tvshow>')
        self.assertEqual(
            read_nfo(self._show(tempfile.mkdtemp(), nfo)),
            {"tmdbid": 12345, "title": "Spartacus"})

    def test_no_tmdb_id_is_none(self):
        self.assertIsNone(
            read_nfo(self._show(tempfile.mkdtemp(), NFO_TVDB_ONLY)))

    def test_garbage_xml_is_none(self):
        self.assertIsNone(
            read_nfo(self._show(tempfile.mkdtemp(), "<tvshow><title>")))

    def test_missing_file_is_none(self):
        self.assertIsNone(read_nfo(self._show(tempfile.mkdtemp(), None)))


class TestBadNfoWarning(unittest.TestCase):
    def setUp(self):
        watcher_module._BAD_NFO_LAST_LOG.clear()

    def _season(self, tmp, nfo):
        show_dir = os.path.join(tmp, "Show (2010)")
        season_dir = os.path.join(show_dir, "Season 01")
        os.makedirs(season_dir)
        if nfo is not None:
            _write(os.path.join(show_dir, "tvshow.nfo"), nfo)
        return season_dir

    def test_unusable_nfo_warns_once_with_remedy(self):
        season_dir = self._season(tempfile.mkdtemp(), NFO_TVDB_ONLY)
        with self.assertLogs("orz.watcher", level="WARNING") as logs:
            self.assertIsNone(process_season_folder(season_dir))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("has no TMDB id", logs.output[0])
        self.assertIn("orz refresh", logs.output[0])
        # A second pass within the hour stays silent.
        with self.assertNoLogs("orz.watcher", level="WARNING"):
            self.assertIsNone(process_season_folder(season_dir))

    def test_never_logged_sentinel_is_none_not_zero(self):
        # Regression: the throttle used 0.0 as "never logged" with
        # time.monotonic() (seconds since boot).  On a container
        # younger than one hour that made the first warning look
        # like a recent one and suppressed it entirely.
        season_dir = self._season(tempfile.mkdtemp(), NFO_TVDB_ONLY)
        self.assertIsNone(watcher_module._BAD_NFO_LAST_LOG.get(
            os.path.dirname(season_dir)))
        with self.assertLogs("orz.watcher", level="WARNING"):
            process_season_folder(season_dir)

    def test_missing_nfo_keeps_old_message(self):
        season_dir = self._season(tempfile.mkdtemp(), None)
        with self.assertLogs("orz.watcher", level="INFO") as logs:
            self.assertIsNone(process_season_folder(season_dir))
        self.assertTrue(
            any("No tvshow.nfo" in line for line in logs.output),
            logs.output)


if __name__ == "__main__":
    unittest.main()
