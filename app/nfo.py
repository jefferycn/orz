"""tvshow.nfo read/write (Kodi/Jellyfin format, same fields as the PHP)."""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape


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
    """Create ``Season 01`` / ``Specials`` folders; return created paths."""
    created = []
    for season in show.get("seasons", []):
        n = season["season_number"]
        name = "Specials" if n == 0 else f"Season {n:02d}"
        path = os.path.join(show_dir, name)
        if not os.path.exists(path):
            os.makedirs(path)
            created.append(path)
    return created
