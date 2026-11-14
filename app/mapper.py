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

TMDB sometimes lists two real seasons under one TMDB season
number: 青之箱 (Blue Box) is 37 episodes under TMDB season 1
(25 real S1 + 12 real S2, which premiered Oct 2026), and 龙珠
(Dragon Ball) is all 153 episodes under TMDB season 1.  The
absolute offset must not come from TMDB's merged count (that
is how 青之箱 S2E05 became S01E42 instead of S01E30).  When
air dates are available (``air_dates``), a TMDB season is split
into segments at any gap of more than ``_SPLIT_GAP_DAYS`` days
between consecutive episodes' air dates — a season break, not
a cour break (cours resume within ~3 months).  Segments, in
order, are the show's real seasons for absolute counting:
青之箱 splits into 25 + 12, so S2E05 -> 25 + 5 = S01E30.
"""
from __future__ import annotations

import json
import os
from datetime import date

from .parser import title_candidates

# An air-date gap longer than this inside one TMDB season is a
# season break, not a cour break: cours resume within ~3 months,
# real seasons are typically 6+ months apart (青之箱 S1 finale
# Mar 2025 -> S2 premiere Oct 2026 is ~18 months).
_SPLIT_GAP_DAYS = 150


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


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
                 override: dict | None = None,
                 air_dates: dict[int, dict[int, str | None]] | None = None):
        self.seasons = {int(s): {int(e): t for e, t in eps.items()}
                        for s, eps in seasons.items()}
        self.override = override or {}
        # Segments: each TMDB season, split at long air-date gaps
        # (see module docstring).  A segment is {"season": tmdb
        # season, "eps": [episode numbers, sorted], "start":
        # absolute number of its first episode}.  Segments, in
        # order, are the show's real seasons for absolute counting.
        # Without air dates there is one segment per TMDB season,
        # exactly the old behaviour.
        self._segments: list[dict] = []
        total = 0
        for s in sorted(self.seasons):
            if s == 0:  # Specials are not part of the absolute count.
                continue
            eps = sorted(self.seasons[s])
            dates = (air_dates or {}).get(s, {})
            start_idx = 0
            for i in range(1, len(eps) + 1):
                split = False
                if i < len(eps):
                    d_prev = _parse_date(dates.get(eps[i - 1]))
                    d_next = _parse_date(dates.get(eps[i]))
                    if (d_prev is not None and d_next is not None
                            and (d_next - d_prev).days > _SPLIT_GAP_DAYS):
                        split = True
                if i == len(eps) or split:
                    seg_eps = eps[start_idx:i]
                    self._segments.append({
                        "season": s, "eps": seg_eps, "start": total + 1})
                    total += len(seg_eps)
                    start_idx = i
        self.total_episodes = total
        # Set by the renamer when the show already uses absolute
        # numbering in Season 01 (see renamer.py): S2 files then map
        # to S01E<absolute> instead of a direct S02E<ep> hit.
        self.absolute_to: int | None = None

    # -- helpers -------------------------------------------------------
    def _offset_before(self, file_season: int) -> int:
        """Episodes in the real seasons before ``file_season``.

        Real seasons are the segments, in order: file season 1 is
        segment 0, file season 2 is segment 1, and so on.  When the
        file season runs past every segment (TMDB merged even
        more), the offset is every episode TMDB lists.
        """
        n = min(max(file_season - 1, 0), len(self._segments))
        return sum(len(seg["eps"]) for seg in self._segments[:n])

    def _absolute_title(self, absolute: int) -> str:
        """Episode title at an absolute position across the segments."""
        mapped = self._from_absolute(absolute)
        return mapped[2] if mapped else ""

    def _absolute(self, season: int, episode: int) -> int | None:
        """Absolute episode number for a TMDB (season, episode).

        Uses the last segment of that TMDB season, so an episode
        past the season's end still continues arithmetically.
        """
        segs = [seg for seg in self._segments if seg["season"] == season]
        if not segs:
            return None
        seg = segs[-1]
        if episode in seg["eps"]:
            return seg["start"] + seg["eps"].index(episode)
        return seg["start"] + (episode - seg["eps"][0])

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
        if not self._segments or absolute < 1:
            return None
        for seg in self._segments:
            start, end = seg["start"], seg["start"] + len(seg["eps"]) - 1
            if start <= absolute <= end:
                ep = seg["eps"][absolute - start]
                return (seg["season"], ep,
                        self.seasons[seg["season"]].get(ep, ""))
        last = self._segments[-1]
        end = last["start"] + len(last["eps"]) - 1
        ep = last["eps"][-1] + (absolute - end)
        return (last["season"], ep,
                self.seasons[last["season"]].get(ep, ""))

    def map_by_title(self, filename: str, episode: int, season: int,
                     show_title: str | None = None
                     ) -> tuple[int, int, str] | None:
        """Arc-title match: ``(season, episode, title)`` or None.

        A bracket / bare episode number can be arc-relative: the
        release ``[凡人修仙传 慕兰之战][24]`` is 慕兰之战 #24,
        which is TMDB S01E200 (titled ``慕兰之战24``), not S01E24.
        When a text segment of the filename + the parsed number
        exactly equals one TMDB episode title of this season, that
        episode wins over the bare number.  The arc name is never
        hardcoded — it comes from the filename, and the titles
        from the TMDB cache, so the rule is the same for every
        show and only fires where the data supports it (Chinese
        year-long donghua titles carry arc + number; descriptive
        titles like ``我回来啦`` never match).  Exact full-title
        match only, and it must be unique — zero or two matches
        mean no match, and the caller falls back to the number
        mapping.  Only the folder season's titles are searched.
        """
        titles = self.seasons.get(season)
        if not titles:
            return None
        numbers = {str(episode), f"{episode:02d}"}
        hits: set[tuple[int, str]] = set()
        for text in title_candidates(filename, show_title):
            for num in numbers:
                want = text + num
                for ep, title in titles.items():
                    if title == want:
                        hits.add((ep, title))
        if len(hits) == 1:
            ep, title = hits.pop()
            return season, ep, title
        return None

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
        # of starting a Season 02.  The offset counts real seasons
        # (segments), not TMDB's merged season lengths, so S2E05
        # is 25 + 5 = S01E30 even though TMDB lists 青之箱 S1 as
        # 37 episodes.  Title comes from the absolute position
        # across TMDB seasons, so S2E05 still gets its S2 title
        # (e.g. 戻ってきたよ) as S01E30.
        if self.absolute_to is not None:
            absolute = self._offset_before(season) + file_episode
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
        # absolute number is (episodes in all earlier real seasons)
        # + file_episode.  Earlier seasons are the segments before
        # this file season — not TMDB's merged counts (青之箱:
        # TMDB S1 = 37, but the real S1 is 25, so S2E05 is 30).
        # For a file season past the last segment, "earlier" is
        # every episode TMDB lists.
        if season not in self.seasons:
            absolute = self._offset_before(season) + file_episode
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
