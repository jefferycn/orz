"""Local episode-list cache: ``<show>/.orz/episodes.json``.

This is what makes renaming *offline*: once a show is created (or
refreshed), the full ``{season: {episode: title}}`` map lives on disk
next to ``tvshow.nfo``.  Renaming reads only this file + the nfo and
never touches TMDB.  TMDB is called only by ``create`` (new show) and
``refresh`` (re-fetch a show's metadata).

Format::

    {"tmdbid": 123, "title": "罗梅莉亚战记",
     "updated_at": "2026-11-08T12:00:00+08:00",
     "seasons": {"1": {"1": "第1集", ...}, "0": {...}}}

The file is written by orz (unlike ``orz.json``, which is the user's
read-only override file and is never written).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .renamer import STATE_DIRNAME


def episodes_path(show_dir: str) -> str:
    return os.path.join(show_dir, STATE_DIRNAME, "episodes.json")


def save_episodes(show_dir: str, tmdbid: int, title: str,
                  seasons: dict[int, dict[int, str]],
                  air_dates: dict[int, dict[int, str | None]] | None = None
                  ) -> str:
    """Persist the episode map; returns the path written.

    ``air_dates`` (``{season: {episode: "YYYY-MM-DD"}}``) is stored
    alongside the titles: TMDB sometimes lists two real seasons
    under one TMDB season number (青之箱 S1 = 37 episodes on TMDB
    = 25 + 12), and the air-date gap between the two runs is what
    tells them apart (see mapper.py).
    """
    data = {
        "tmdbid": tmdbid,
        "title": title,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "seasons": {str(s): {str(e): t for e, t in eps.items()}
                    for s, eps in seasons.items()},
    }
    if air_dates:
        data["air_dates"] = {
            str(s): {str(e): d for e, d in eps.items() if d}
            for s, eps in air_dates.items() if eps}
    path = episodes_path(show_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load_episodes(show_dir: str) -> dict | None:
    """``{"tmdbid", "title", "seasons": {int: {int: str}}, ...}`` or None.

    Keys are converted back to int.  A corrupt file is treated as
    missing (the caller logs it and tells the user to run refresh).
    ``air_dates`` (when present) comes back the same way, as
    ``{int: {int: str}}``; a cache written before air dates were
    stored simply has none.
    """
    path = episodes_path(show_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "seasons" not in data:
        return None
    try:
        seasons = {int(s): {int(e): t for e, t in eps.items()}
                   for s, eps in data["seasons"].items()}
    except (ValueError, AttributeError):
        return None
    data["seasons"] = seasons
    air_dates = data.get("air_dates") or {}
    try:
        data["air_dates"] = {
            int(s): {int(e): d for e, d in eps.items()}
            for s, eps in air_dates.items()}
    except (ValueError, AttributeError):
        data["air_dates"] = {}
    return data
