"""tvshow.nfo read/write (Kodi/Jellyfin format, same fields as the PHP)."""
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from .parser import season_number_from_folder

log = logging.getLogger("orz.nfo")

_UNPADDED_SEASON_RE = re.compile(r"Season ([1-9]\d*)$")


def find_season_dir(show_dir: str, season: int) -> str | None:
    """Existing folder for a season number, in any naming variant.

    ``Season 1`` / ``Season 01`` / ``Specials`` (season 0) all count.
    When both ``Season 1`` and ``Season 01`` exist, the padded one
    wins — padded is the standard name (see
    ``normalize_season_folders``); the unpadded one is a leftover
    that gets merged away on the next scan.  Returns None when no
    folder for that season exists (the caller must not invent one: a
    rename that cannot find its target season folder stays where it
    is).
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
        if name == f"Season {season:02d}":
            return (0, name)
        if name == f"Season {season}":
            return (1, name)
        return (2, name)

    return os.path.join(show_dir, sorted(matches, key=_rank)[0])


def _season_nfo_duplicate(src: str, dst: str) -> bool:
    """True when two season.nfo files describe the same season.

    season.nfo is a regenerable sidecar (Jellyfin/Kodi write it; orz
    never reads it).  When a merge retires the old folder, the copy
    in it is dead data — but only delete it when it is provably the
    same season's record: identical bytes, or the same
    <seasonnumber> when both parse as XML.  Anything else is left
    in place, like any other colliding file.
    """
    try:
        with open(src, "rb") as f:
            src_bytes = f.read()
        with open(dst, "rb") as f:
            dst_bytes = f.read()
    except OSError:
        return False
    if src_bytes == dst_bytes:
        return True
    try:
        src_num = ET.parse(src).getroot().findtext("seasonnumber")
        dst_num = ET.parse(dst).getroot().findtext("seasonnumber")
    except (ET.ParseError, OSError):
        return False
    if src_num is None or dst_num is None:
        return False
    return src_num.strip() == dst_num.strip()


def normalize_season_dir(season_dir: str) -> str:
    """Rename an unpadded ``Season 1`` folder to the standard ``Season 01``.

    When the padded folder already exists, the files are moved into
    it instead; a file whose name already exists there is left in
    place (never overwritten) — except a duplicate season.nfo for
    the same season, which is removed (see ``_season_nfo_duplicate``).
    The source folder is removed only once it is empty — when files
    had to stay behind, the old folder stays too and the path
    returned is the old one, so the caller still processes the
    leftovers.  Returns the folder's path after normalizing
    (unchanged when already standard).
    """
    parent = os.path.dirname(season_dir.rstrip("/"))
    name = os.path.basename(season_dir.rstrip("/"))
    m = _UNPADDED_SEASON_RE.match(name)
    if not m:
        return season_dir
    target_name = f"Season {int(m.group(1)):02d}"
    target = os.path.join(parent, target_name)
    if target == season_dir:
        return season_dir
    if os.path.isdir(target):
        for entry in sorted(os.listdir(season_dir)):
            src = os.path.join(season_dir, entry)
            dst = os.path.join(target, entry)
            if os.path.exists(dst):
                if entry == "season.nfo" and _season_nfo_duplicate(src, dst):
                    os.remove(src)
                    log.info("Removing duplicate season.nfo %s "
                             "(same season as %s)", src, dst)
                    continue
                log.info("Not merging %s into %s: %s already exists",
                         src, target, entry)
                continue
            os.rename(src, dst)
        try:
            os.rmdir(season_dir)
        except OSError:
            log.info("Leaving %s: files remain after merge into %s",
                     season_dir, target)
            return season_dir
        log.info("Merged %s into %s", season_dir, target)
        return target
    os.rename(season_dir, target)
    log.info("Renamed season folder %s -> %s", season_dir, target)
    return target


def normalize_season_folders(show_dir: str) -> dict[str, str]:
    """Rename every unpadded season folder in a show to the standard.

    ``Season 1`` -> ``Season 01`` (merging into an existing
    ``Season 01`` when there is one).  ``Specials`` and already
    padded folders are untouched.  Returns {old name: new name}
    for the folders that changed.  Cheap (listdir + renames only),
    so the scan runs it for every show even when the snapshots say
    nothing changed — a folder renamed by hand must still be
    standardised.
    """
    renamed = {}
    try:
        entries = sorted(os.listdir(show_dir))
    except OSError:
        return renamed
    for entry in entries:
        path = os.path.join(show_dir, entry)
        if not os.path.isdir(path):
            continue
        if season_number_from_folder(entry) is None:
            continue
        new_path = normalize_season_dir(path)
        if new_path != path:
            renamed[entry] = os.path.basename(new_path)
    return renamed


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
    e.g. the user's own ``Season 1`` — is left alone: the folder is
    renamed to the padded standard by ``normalize_season_folders``
    (run before this in ``refresh_show`` / the scan), not duplicated
    here.  New folders are always padded (``Season 01``).
    """
    created = []
    for season in show.get("seasons", []):
        n = season["season_number"]
        if find_season_dir(show_dir, n) is not None:
            continue
        name = "Specials" if n == 0 else f"Season {n:02d}"
        path = os.path.join(show_dir, name)
        if not os.path.exists(path):
            os.makedirs(path)
            created.append(path)
    return created
