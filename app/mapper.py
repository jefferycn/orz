"""Season/episode mapping.

Pain point 2: TMDB lists only 1 season but the files say S2/S02 and
restart numbering (Anime especially).  The fix is absolute-episode
mapping: count episodes across TMDB seasons in order, so with a
12-episode S1, file S2E01 -> S01E13, S2E02 -> S01E14.  No Season 02
folder is ever created for this.

A per-show override file ``orz.json`` in the show folder (read-only,
never written by the app) can pin the mapping:

.. code-block:: json

    {
      "season_map": {"2": 1, "3": 1},
      "episode_offset": {"2": 12},
      "episode_map": {"2x01": "1x13"}
    }

``episode_map`` wins, then ``season_map`` + ``episode_offset``
(or the computed absolute offset), then the automatic mapping.
"""
from __future__ import annotations

import json
import os


def load_override(show_dir: str) -> dict:
    """Read ``orz.json`` if present; invalid/missing file -> {}.

    The file is read-only for orz: nothing in the app ever writes it.
    """
    path = os.path.join(show_dir, "orz.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


class EpisodeMapper:
    """Maps (file season, file episode) -> (target season, target episode, title).

    ``seasons``: ``{season_number: {episode_number: episode_title}}``
    from TMDB, as returned by ``TmdbClient.all_seasons``.
    """

    def __init__(self, seasons: dict[int, dict[int, str]],
                 override: dict | None = None):
        self.seasons = {int(s): {int(e): t for e, t in eps.items()}
                        for s, eps in seasons.items()}
        self.override = override or {}
        # Cumulative episode counts in season order, for absolute mapping:
        # _starts[season] = absolute number of the season's episode 1.
        self._starts: dict[int, int] = {}
        total = 0
        for s in sorted(self.seasons):
            if s == 0:  # Specials are not part of the absolute count.
                continue
            self._starts[s] = total + 1
            total += len(self.seasons[s])
        self.total_episodes = total
        # Set by the renamer when the show already uses absolute
        # numbering in Season 01 (see renamer.py): S2 files then map
        # to S01E<absolute> instead of a direct S02E<ep> hit.
        self.absolute_to: int | None = None

    # -- helpers -------------------------------------------------------
    def _absolute_title(self, absolute: int) -> str:
        """Episode title at an absolute position across TMDB seasons."""
        n = 0
        for s in sorted(self.seasons):
            if s == 0:
                continue
            for ep in sorted(self.seasons[s]):
                n += 1
                if n == absolute:
                    return self.seasons[s][ep]
        return ""
    def _absolute(self, season: int, episode: int) -> int | None:
        """Absolute episode number if the file season is a TMDB season."""
        if season not in self._starts:
            return None
        return self._starts[season] + episode - 1

    def _from_absolute(self, absolute: int) -> tuple[int, int, str] | None:
        """Absolute number -> (season, episode, title).

        Arithmetic continuation: if the absolute number runs past
        every episode TMDB lists, it still lands in the *last* season
        with the absolute number as its episode number and an empty
        title (the task's example: TMDB S1 lists 12 eps, file S2E01
        -> S01E13).  The renamer then writes ``Title - S01E13.mkv``
        with no episode-title part.  This is what "do not create
        Season 02" means in practice; the alternative (pending) would
        leave every split-cour Anime file unrenamed.
        """
        seasons = [s for s in sorted(self.seasons) if s != 0]
        if not seasons or absolute < 1:
            return None
        for s in seasons:
            count = len(self.seasons[s])
            start = self._starts[s]
            if start <= absolute < start + count:
                ep = absolute - start + 1
                return s, ep, self.seasons[s].get(ep, "")
        last = seasons[-1]
        ep = absolute - self._starts[last] + 1
        return last, ep, self.seasons[last].get(ep, "")

    def _episode_title(self, season: int, episode: int) -> str:
        return self.seasons.get(season, {}).get(episode, "")

    # -- main ----------------------------------------------------------
    def map(self, file_season: int | None, file_episode: int
            ) -> tuple[int, int, str] | None:
        """Return (target season, target episode, title) or None.

        None means "cannot map" — the caller marks the file pending
        instead of guessing (e.g. absolute number past the last episode).
        """
        season = file_season if file_season is not None else 1

        # 1. Explicit per-episode override wins.
        ep_map = self.override.get("episode_map") or {}
        key = f"{season}x{file_episode}"
        if key in ep_map:
            target = str(ep_map[key])
            if "x" in target:
                ts, te = target.split("x", 1)
                ts, te = int(ts), int(te)
                return ts, te, self._episode_title(ts, te)

        # 1b. Absolute-numbering show (renamer set absolute_to):
        # Season 01 already holds episodes past TMDB S1's count
        # (e.g. 青之箱 S01E26-29 are really S2E01-04), so every
        # S2+ file continues the absolute run in Season 01 instead
        # of starting a Season 02.  Title comes from the absolute
        # position across TMDB seasons, so S2E05 still gets its
        # S2 title (e.g. 戻ってきたよ) as S01E30.
        if self.absolute_to is not None:
            absolute = file_episode
            for s in sorted(self.seasons):
                if s == 0 or s >= season:
                    continue
                absolute += len(self.seasons[s])
            return self.absolute_to, absolute, self._absolute_title(absolute)

        # 2. Direct hit: the file season exists in TMDB.
        if season in self.seasons and file_episode in self.seasons[season]:
            return season, file_episode, self.seasons[season][file_episode]

        # 3. Season override (orz.json ``season_map`` / ``episode_offset``).
        season_map = {int(k): int(v)
                      for k, v in (self.override.get("season_map") or {}).items()}
        offsets = {int(k): int(v)
                   for k, v in (self.override.get("episode_offset") or {}).items()}
        if season in season_map:
            ts = season_map[season]
            offset = offsets.get(season, 0)
            te = file_episode + offset
            return ts, te, self._episode_title(ts, te)

        # 4. Absolute mapping: file season is beyond TMDB's seasons.
        # The file's numbering restarts at its own S2E01, so its
        # absolute number is (episodes in all earlier TMDB seasons)
        # + file_episode.  Earlier seasons are TMDB's own counts;
        # for a file season past the last TMDB season, "earlier" is
        # every TMDB season.
        if season not in self.seasons:
            absolute = self.total_episodes + file_episode
            # If the file season is e.g. S3 while TMDB has S1 only,
            # S2's episodes are unknown — still count from the end of
            # TMDB, which is the best available interpretation.
            mapped = self._from_absolute(absolute)
            if mapped:
                return mapped
            return None

        # 5. Season exists but the episode number is past its end:
        # treat as a continuation (absolute) rather than inventing it.
        absolute = self._absolute(season, file_episode)
        if absolute is not None:
            mapped = self._from_absolute(absolute)
            if mapped:
                return mapped
        return None
