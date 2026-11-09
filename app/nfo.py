"""tvshow.nfo read/write (Kodi/Jellyfin format, same fields as the PHP)."""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from .parser import season_number_from_folder


def find_season_dir(show_dir: str, season: int) -> str | None:
    """Existing folder for a season number, in any naming variant.

    ``Season 1`` / ``Season 01`` / ``Specials`` (season 0) all count.
    When both ``Season 1`` and ``Season 01`` exist, the unpadded
    one wins — it is the user's own folder; the padded one was
    probably created by an earlier orz refresh.  Returns None when
    no folder for that season exists (the caller must not invent
    one: a rename that cannot find its target season folder stays
    where it is).
    """
    try:
        entries = sorted(os.listdir(show_dir))
    except OSError:
        return None
    matches = [e for e in entries
               if os.path.isdir(os.path.join(show_dir, e))
               and season_number_from_folder(e) == season]
    if not matches:
        return None

    def _rank(name: str) -> tuple[int, str]:
        if name == f"Season {season}":
            return (0, name)
        if name == f"Season {season:02d}":
            return (1, name)
        return (2, name)

    return os.path.join(show_dir, sorted(matches, key=_rank)[0])


def nfo_path(show_dir: str) -> str:
    return os.path.join(show_dir, "tvshow.nfo")


def generate_nfo(show: dict, show_dir: str) -> str:
    """Write tvshow.nfo from a TMDB ``/tv/{id}`` (or search) payload."""
    first_air = show.get("first_air_date") or ""
    root = ET.Element("tvshow")
    fields = [
        ("title", show.get("name", "")),
        ("originaltitle", show.get("original_name", "")),
        ("tmdbid", str(show.get("id", ""))),
        ("plot", show.get("overview", "")),
        ("rating", str(show.get("vote_average", ""))),
        ("votes", str(show.get("vote_count", ""))),
        ("year", first_air[:4]),
        ("premiered", first_air),
    ]
    for tag, value in fields:
        el = ET.SubElement(root, tag)
        el.text = value
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    path = nfo_path(show_dir)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def read_nfo(show_dir: str) -> dict | None:
    """``{"tmdbid": int, "title": str}`` or None when no/invalid nfo."""
    path = nfo_path(show_dir)
    if not os.path.exists(path):
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    tmdbid = root.findtext("tmdbid")
    title = root.findtext("title")
    if not tmdbid or not tmdbid.isdigit():
        return None
    return {"tmdbid": int(tmdbid), "title": title or ""}


def generate_season_folders(show: dict, show_dir: str) -> list[str]:
    """Create ``Season 01`` / ``Specials`` folders; return created paths.

    A season that already has a folder — in *any* naming variant,
    e.g. the user's own ``Season 1`` — is left alone.  Never create
    a padded ``Season 01`` next to an existing ``Season 1``.
    New folders follow the show's existing style: if the show
    already uses unpadded ``Season 1`` / ``Season 2``, a new
    season is ``Season 3``, not ``Season 03``; with no existing
    season folders the default is padded (``Season 01``).
    """
    try:
        existing = os.listdir(show_dir)
    except OSError:
        existing = []
    unpadded = any(re.match(r"Season [1-9]\d*$", e) for e in existing)
    created = []
    for season in show.get("seasons", []):
        n = season["season_number"]
        if find_season_dir(show_dir, n) is not None:
            continue
        if n == 0:
            name = "Specials"
        elif unpadded:
            name = f"Season {n}"
        else:
            name = f"Season {n:02d}"
        path = os.path.join(show_dir, name)
        if not os.path.exists(path):
            os.makedirs(path)
            created.append(path)
    return created
