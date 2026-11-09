"""v2.7.2 tests — jpg / @eaDir warnings, S02 season folders, full restore.

The new log (orz.csv, 05:01-05:16 UTC) showed the jpg warnings are
real: "No TMDB match for landscape.jpg / backdrop.jpg / logo.png /
*-thumb.jpg" every ~10s.  Root cause: Synology's @eaDir holds
directories named after files (@eaDir/landscape.jpg/), orz watched
them, and the event watcher treated every unknown directory as a
possible show folder — calling create_show (and TMDB) on each one.
Banshee's S02/S03 season folders hit the same path: they are not
"Season N", so they were treated as shows too.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.parser import season_number_from_folder
from app.watcher import EventWatcher, create_show


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _load_restore_module():
    spec = importlib.util.spec_from_file_location(
        "restore_from_log",
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools", "restore_from_log.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTmdb:
    def __init__(self):
        self.find_calls = []

    def find_show(self, title, year):
        self.find_calls.append((title, year))
        return {"id": 1, "name": title}

    def get_tvshow(self, show_id):
        return {"id": 1, "name": "Show", "original_name": "Show",
                "first_air_date": "2024-01-01", "overview": "",
                "vote_average": 0, "vote_count": 0,
                "seasons": [{"season_number": 1}]}

    def all_seasons(self, show_id):
        return {1: {i: f"第{i}集" for i in range(1, 13)}}

    def all_air_dates(self, show_id):
        return {}


class TestShowDirGuard(unittest.TestCase):
    def test_junk_folder_names_never_reach_tmdb(self):
        tmp = tempfile.mkdtemp()
        client = FakeTmdb()
        junk = ["landscape.jpg", "backdrop.jpg", "logo.png", "@eaDir",
                "poster.jpg", "Show - S01E01-thumb.jpg", ".hidden",
                "episode.mkv", "subs.srt", "tvshow.nfo"]
        for name in junk:
            d = os.path.join(tmp, name)
            os.makedirs(d)
            with self.assertNoLogs("orz", level="WARNING"):
                self.assertFalse(create_show(d, client), name)
        self.assertEqual(client.find_calls, [])

    def test_real_shows_still_work(self):
        tmp = tempfile.mkdtemp()
        client = FakeTmdb()
        for name in ("Show (2024)", "Dr. Who (2005)"):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            self.assertTrue(create_show(d, client), name)
        self.assertEqual(client.find_calls,
                         [("Show", 2024), ("Dr. Who", 2005)])
        self.assertTrue(os.path.exists(
            os.path.join(tmp, "Show (2024)", "tvshow.nfo")))


class TestSeasonSNN(unittest.TestCase):
    def test_short_season_folders(self):
        self.assertEqual(season_number_from_folder("S02"), 2)
        self.assertEqual(season_number_from_folder("S03"), 3)
        self.assertEqual(season_number_from_folder("s1"), 1)
        self.assertEqual(season_number_from_folder("Season 2"), 2)
        self.assertEqual(season_number_from_folder("Season 01"), 1)
        self.assertEqual(season_number_from_folder("Specials"), 0)

    def test_not_a_season_folder(self):
        self.assertIsNone(season_number_from_folder("Show (2024)"))
        self.assertIsNone(season_number_from_folder("S123"))
        self.assertIsNone(season_number_from_folder("S02E05"))
        self.assertIsNone(season_number_from_folder("landscape.jpg"))


class TestWatchSkipsEaDir(unittest.TestCase):
    def test_add_watch_recursive_skips_eadir_and_dot_dirs(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "Anime")
        show = os.path.join(root, "Show (2024)")
        os.makedirs(os.path.join(show, "Season 01"))
        os.makedirs(os.path.join(show, "@eaDir", "landscape.jpg"))
        os.makedirs(os.path.join(show, "Season 01", "@eaDir", "x-thumb.jpg"))
        os.makedirs(os.path.join(root, "@eaDir"))
        os.makedirs(os.path.join(show, ".hidden"))
        ew = EventWatcher.__new__(EventWatcher)
        ew._path_to_wd = {}
        ew._wd_to_path = {}

        class FakeInotify:
            def __init__(self):
                self.watched = []

            def add_watch(self, path):
                self.watched.append(path)
                return len(self.watched)

            def rm_watch(self, wd):
                pass

        ew._inotify = FakeInotify()
        ew._add_watch_recursive(root)
        watched = ew._inotify.watched
        self.assertIn(show, watched)
        self.assertIn(os.path.join(show, "Season 01"), watched)
        self.assertFalse(any("@eaDir" in p or "/.hidden" in p
                             for p in watched))


class TestRestoreFullLog(unittest.TestCase):
    def test_list_is_complete_and_chain_free(self):
        mod = _load_restore_module()
        self.assertEqual(len(mod.RESTORE), 86)
        olds = [o for o, _ in mod.RESTORE]
        news = [n for _, n in mod.RESTORE]
        self.assertEqual(len(set(olds)), len(olds))
        self.assertEqual(len(set(news)), len(news))

        def show_of(old):
            return "/".join(old.split("/")[:4])

        for o1, n1 in mod.RESTORE:
            for o2, _ in mod.RESTORE:
                if (o2 != o1 and o2.rsplit("/", 1)[-1] == n1
                        and show_of(o2) == show_of(o1)):
                    self.fail(f"chain remains: {o1} -> {n1} -> {o2}")
        # The 灵笼 PV was renamed twice; the two entries are composed
        # into one (original name -> final name).
        pv = [e for e in mod.RESTORE
              if e[0].endswith("30分钟前瞻PV 4K.mp4")]
        self.assertEqual(len(pv), 1)
        self.assertEqual(pv[0][1], "灵笼 - S00E05 - 第5话 生态噩兆.mp4")
        # The new log's Dragon Ball Season 3 batch is in there too.
        s03 = [e for e in mod.RESTORE if "/Season 3/" in e[0]]
        self.assertEqual(len(s03), 31)

    def test_chained_pv_and_dragon_ball_s03_restore(self):
        mod = _load_restore_module()
        base = tempfile.mkdtemp()
        pv = [e for e in mod.RESTORE
              if e[0].endswith("30分钟前瞻PV 4K.mp4")][0]
        s03 = [e for e in mod.RESTORE
               if "S03E16" in e[0] and e[0].endswith(".mkv")][0]
        subset = [pv, s03]
        remapped = []
        for old, new in subset:
            new_old = base + old[len("/media"):]
            remapped.append((new_old, new))
            show_dir = os.path.dirname(os.path.dirname(new_old))
            folder = "Specials" if "Specials" in new_old else "Season 01"
            _write(os.path.join(show_dir, folder, new), b"v")
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
