"""v2.7.5 tests — scene release names are not "already organised".

v2.7's ``is_already_named`` counted *any* explicit episode marker
as organised, so a whole season of scene-named .ass files moved
into Season 04 was skipped silently: no rename, no log at all.
The marker must sit in the clean ``Title - SxxEyy`` form.  Two
more halves of the same bug: ``existing_episodes`` counted a
scene-named file's own marker, so it collided with itself, and
the episode-taken check blocked a subtitle arriving after its
video.  Episode slots now belong to videos only.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cache import save_episodes
from app.nfo import generate_nfo
from app.parser import is_already_named
from app.renamer import existing_episodes
from app.watcher import process_season_folder

FILES = [
    "Desperate.Housewives.S04E01.Now.You.Know.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E02.Smiles.of.a.Summer.Night.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E03.The.Game.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E04.If.There's.Anything.I.Can't.Stand.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E05.Art.Isn't.Easy.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E06.Now.I.Know,.Don't.Be.Scared.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E07.You.Can't.Judge.A.Book.By.Its.Cover.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E08.Distant.Past.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E09.Something's.Coming.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E10.Welcome.to.Kanagawa.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E11.Sunday.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR[UTR].ass",
    "Desperate.Housewives.S04E12.In.Buddy's.Eyes.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E13.Hello,.Little.Girl.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E14.Opening.Doors.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E15.Mother.Said.1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E16.The.Gun.Song.(1).1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
    "Desperate.Housewives.S04E17.Free.(2).1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.ass",
]


def _write(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _show(tmp: str, season_name: str = "Season 04"):
    """绝望主妇 (2004) with nfo + S04 episode cache (17 eps)."""
    show_dir = os.path.join(tmp, "绝望主妇 (2004)")
    season_dir = os.path.join(show_dir, season_name)
    os.makedirs(season_dir)
    generate_nfo({"name": "绝望主妇", "original_name": "Desperate Housewives",
                  "id": 1, "overview": "", "vote_average": 0,
                  "vote_count": 0, "first_air_date": "2004-10-03",
                  "seasons": []}, show_dir)
    save_episodes(show_dir, 1, "绝望主妇",
                  {4: {i: f"第{i}集" for i in range(1, 18)}})
    return show_dir, season_dir


class TestSceneNames(unittest.TestCase):
    def test_all_17_ass_files_are_not_already_named(self):
        for f in FILES:
            self.assertFalse(is_already_named(f, 4), f)

    def test_all_17_ass_files_rename(self):
        tmp = tempfile.mkdtemp()
        _show_dir, season_dir = _show(tmp)
        for f in FILES:
            _write(os.path.join(season_dir, f), b"fake ass")
        result = process_season_folder(season_dir)
        self.assertEqual(len(result.renamed), 17)
        self.assertEqual(result.pending, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(
            sorted(os.listdir(season_dir)),
            [f"绝望主妇 - S04E{i:02d} - 第{i}集.ass" for i in range(1, 18)])

    def test_scene_video_does_not_collide_with_itself(self):
        tmp = tempfile.mkdtemp()
        _show_dir, season_dir = _show(tmp)
        _write(os.path.join(
            season_dir,
            "Desperate.Housewives.S04E01.Now.You.Know.1080p.WEB-DL."
            "x265.10bit.AAC.5.1-RCVR.mkv"))
        result = process_season_folder(season_dir)
        self.assertEqual(len(result.renamed), 1)
        self.assertEqual(result.pending, [])
        self.assertTrue(os.path.exists(
            os.path.join(season_dir, "绝望主妇 - S04E01 - 第1集.mkv")))

    def test_subtitle_after_video_still_renames(self):
        tmp = tempfile.mkdtemp()
        _show_dir, season_dir = _show(tmp)
        for i in (1, 2, 3):
            _write(os.path.join(
                season_dir, f"绝望主妇 - S04E{i:02d} - 第{i}集.mkv"))
        for f in FILES[:2] + FILES[3:4]:
            _write(os.path.join(season_dir, f), b"fake ass")
        result = process_season_folder(season_dir)
        self.assertEqual(len(result.renamed), 3)
        self.assertEqual(result.pending, [])
        self.assertEqual(len(result.skipped), 3)  # the videos
        for i in (1, 2, 4):
            self.assertTrue(os.path.exists(os.path.join(
                season_dir, f"绝望主妇 - S04E{i:02d} - 第{i}集.ass")))

    def test_existing_episodes_counts_only_organised_videos(self):
        tmp = tempfile.mkdtemp()
        _show_dir, season_dir = _show(tmp)
        _write(os.path.join(
            season_dir,
            "Desperate.Housewives.S04E01.Now.You.Know.1080p.WEB-DL."
            "x265.10bit.AAC.5.1-RCVR.ass"))
        _write(os.path.join(
            season_dir,
            "Desperate.Housewives.S04E02.Smiles.of.a.Summer.Night."
            "1080p.WEB-DL.x265.10bit.AAC.5.1-RCVR.mkv"))
        _write(os.path.join(season_dir, "绝望主妇 - S04E03 - 第3集.mkv"))
        _write(os.path.join(season_dir, "绝望主妇 - S04E04 - 第4集.ass"))
        # Only the organised video (E03) holds a slot: the scene
        # files hold nothing, and neither does an organised subtitle.
        self.assertEqual(existing_episodes(season_dir), {3})

    def test_clean_files_still_skip_with_a_log_line(self):
        tmp = tempfile.mkdtemp()
        _show_dir, season_dir = _show(tmp)
        _write(os.path.join(season_dir, "绝望主妇 - S04E01 - 第1集.mkv"))
        _write(os.path.join(season_dir, "绝望主妇 - S04E02 - 第2集.mkv"))
        with self.assertLogs("orz.watcher", level="INFO") as logs:
            result = process_season_folder(season_dir)
        self.assertEqual(result.renamed, [])
        self.assertEqual(len(result.skipped), 2)
        self.assertTrue(
            any("already organised" in line for line in logs.output),
            logs.output)


if __name__ == "__main__":
    unittest.main()
