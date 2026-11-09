"""TMDB client (urllib, stdlib only) with a file cache.

Fixes the old bug in PHP ``getExactMatch``: it compared every result's
year against ``results[0]['first_air_date']``.  Here each result is
checked against its *own* ``first_air_date``.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.themoviedb.org/3"
CACHE_TTL = 86400  # same 24 h as the old Symfony cache


def find_exact_match(results: list[dict], title: str, year: int | None) -> dict | None:
    """Pick the TMDB result matching name (+ year).

    Each result is checked against its own ``first_air_date``.
    Name matches ``name`` or ``original_name`` (case-insensitive).
    If no result matches both name and year, fall back to the first
    result matching the year alone; otherwise None (never a blind
    ``results[0]``).
    """
    if not results:
        return None
    title_l = title.strip().lower()

    def _year_of(r: dict) -> int | None:
        d = (r.get("first_air_date") or "")[:4]
        return int(d) if d.isdigit() else None

    def _name_ok(r: dict) -> bool:
        return title_l in {
            (r.get("name") or "").strip().lower(),
            (r.get("original_name") or "").strip().lower(),
        }

    for r in results:
        if _name_ok(r) and (year is None or _year_of(r) == year):
            return r
    if year is not None:
        for r in results:
            if _year_of(r) == year:
                return r
    return None


class TmdbClient:
    def __init__(self, api_key: str, language: str = "zh-CN",
                 cache_dir: str | None = None):
        self.api_key = api_key
        self.language = language
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(__file__), "..", "var", "cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    # -- low level -----------------------------------------------------
    def _cache_path(self, url: str) -> str:
        return os.path.join(
            self.cache_dir, hashlib.sha256(url.encode()).hexdigest() + ".json")

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["api_key"] = self.api_key
        params["language"] = self.language
        url = BASE_URL + path + "?" + urllib.parse.urlencode(params)
        cache_path = self._cache_path(url)
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached["_cached_at"] < CACHE_TTL:
                return cached["data"]
        req = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          "User-Agent": "orz/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"TMDB {e.code} for {path}: {e.read()[:200]!r}") from e
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"_cached_at": time.time(), "data": data}, f)
        return data

    # -- API -----------------------------------------------------------
    def search_tv(self, query: str) -> list[dict]:
        return self._get("/search/tv", {"query": query}).get("results", [])

    def find_show(self, title: str, year: int | None) -> dict | None:
        return find_exact_match(self.search_tv(title), title, year)

    def get_tvshow(self, show_id: int) -> dict:
        return self._get(f"/tv/{show_id}")

    def get_season(self, show_id: int, season_number: int) -> dict:
        return self._get(f"/tv/{show_id}/season/{season_number}")

    def season_episodes(self, show_id: int, season_number: int) -> dict[int, str]:
        """``{episode_number: episode_title}`` for one season."""
        data = self.get_season(show_id, season_number)
        return {ep["episode_number"]: ep.get("name") or ""
                for ep in data.get("episodes", [])}

    def all_seasons(self, show_id: int) -> dict[int, dict[int, str]]:
        """``{season_number: {episode_number: title}}`` for every season."""
        show = self.get_tvshow(show_id)
        out: dict[int, dict[int, str]] = {}
        for s in show.get("seasons", []):
            n = s["season_number"]
            out[n] = self.season_episodes(show_id, n)
        return out
