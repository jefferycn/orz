"""Episode-number parsing.

Fixes the three pain points of the old PHP version:
- resolution / codec / release-group noise (1080p, 720p, x264, [Group])
  is stripped *before* looking for an episode number;
- the show-name prefix (taken from the folder name) is stripped first,
  so a show whose name contains digits (``24``, ``86``) is never
  mistaken for an episode number;
- a low-confidence guess never falls back to episode 01.  It returns
  ``None`` and the caller records the file as pending.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VIDEO_EXTS = {"mp4", "mkv", "avi", "mpg", "mpeg", "mov", "ts", "m2ts", "wmv"}
SUBTITLE_EXTS = {"srt", "ass", "ssa", "vtt", "sub"}
MEDIA_EXTS = VIDEO_EXTS | SUBTITLE_EXTS

# Language tags that sit between the base name and the subtitle extension,
# e.g. ``Show - S01E02 - Title.zh.srt`` / ``...en.ass``.
LANG_TAGS = {
    "zh", "chs", "cht", "cn", "tw", "hk",
    "en", "eng", "jp", "jpn", "ja", "kor", "ko",
    "other", "default", "forced",
}

# Tokens that are never an episode number.  Matched as whole tokens
# (separated by space / ``-`` / ``_`` / ``.``) after bracket groups are gone.
_NOISE_TOKENS = {
    "1080p", "720p", "2160p", "1080i", "720i", "480p", "576p",
    "4k", "8k", "2k", "uhd", "fhd", "hd", "sd",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid",
    "10bit", "8bit", "hdr", "hdr10", "dolby", "dts", "aac", "flac",
    "web", "web-dl", "webdl", "bluray", "blu-ray", "bdrip", "hdtv",
    "proper", "repack", "complete", "multi", "dual", "audio",
}
_NOISE_RE = re.compile(
    r"(?i)\b(?:1080p|720p|2160p|1080i|720i|480p|576p|4k|8k|2k|uhd|fhd|hd|sd|"
    r"x264|x265|h264|h265|hevc|avc|xvid|10bit|8bit|hdr10?|dolby|dts|aac|flac|"
    r"web-?dl|bluray|blu-ray|bdrip|hdtv|proper|repack|complete)\b"
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass
class EpisodeGuess:
    """Result of parsing one filename.

    ``episode`` is None when no trustworthy number was found.
    ``confidence``: ``"high"`` (explicit SxxEyy / Eyy / 第x集),
    ``"medium"`` (exactly one bare number left after stripping),
    ``"none"`` (nothing usable — do not rename, mark pending).
    """

    episode: int | None
    season: int | None = None
    confidence: str = "none"
    reason: str = ""


def parse_folder_name(folder_name: str) -> tuple[str, int | None]:
    """``绝望主妇 (2004)`` -> (``绝望主妇``, 2004).

    The year is the last ``(YYYY)`` group; a folder without one
    returns ``year=None`` instead of guessing.
    """
    name = folder_name.strip()
    m = re.search(r"\((\d{4})\)\s*$", name)
    if m:
        return name[: m.start()].strip(), int(m.group(1))
    return name, None


def strip_lang_suffix(filename: str) -> str:
    """``Title.zh.srt`` -> ``Title``; ``Title.mkv`` -> ``Title``."""
    stem = filename
    # Remove the real extension first.
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    # Then a language tag, if present (``.zh`` / ``.en`` / ``.chs`` ...).
    if "." in stem:
        head, tail = stem.rsplit(".", 1)
        if tail.lower() in LANG_TAGS:
            return head
    return stem


def base_name(filename: str) -> str:
    """Base name used for grouping video + subtitles of one episode."""
    return strip_lang_suffix(filename)


def lang_suffix(filename: str) -> str:
    """``.zh`` for ``Title.zh.srt``, else ``""`` (kept on rename)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    if "." in stem:
        head, tail = stem.rsplit(".", 1)
        if tail.lower() in LANG_TAGS:
            return "." + tail
    return ""


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_\.]+", " ", text).strip().lower()


def _strip_show_prefix(name: str, show_title: str | None) -> str:
    """Remove a leading show-title prefix, if the filename starts with it.

    Only the *leading* occurrence is removed, and only when followed by
    a separator (or end of string) — so ``24 - 05`` loses the ``24``
    but ``24点`` inside a title is untouched.  Matching is
    separator-insensitive: ``罗梅莉亚战记`` also matches
    ``罗梅莉亚战记-02`` / ``罗梅莉亚战记_02``.
    """
    if not show_title:
        return name
    title = show_title.strip()
    if not title:
        return name
    # Separator-insensitive prefix match on the raw string.
    pattern = re.compile(
        r"^\s*" + re.escape(title) + r"(?=$|[\s\-_\.\[\]\(\)])",
        re.IGNORECASE,
    )
    m = pattern.match(name)
    if m:
        return name[m.end():].strip(" -_.[]()")
    # Fallback: normalised comparison (handles full-width / spacing drift).
    if _norm(name).startswith(_norm(title)):
        rest = name[len(title):]
        return rest.strip(" -_.[]()")
    return name


def _strip_brackets(name: str) -> str:
    """Remove ``[...]`` release-group / tag groups entirely.

    ``[1080p]`` and ``[Group]`` both go.  ``(...)`` groups are only
    removed when they look like a tag (resolution / codec / year /
    group), because a show title itself may contain parentheses.
    """
    name = re.sub(r"\[[^\]]*\]", " ", name)
    def _paren(m: re.Match) -> str:
        inner = m.group(1)
        if (
            _NOISE_RE.search(inner)
            or _YEAR_RE.search(inner)
            or re.fullmatch(r"[A-Za-z0-9\-_ ]{1,30}", inner)
            and re.search(r"(?i)(fansub|sub|rip|dl|264|265|1080|720|2160)", inner)
        ):
            return " "
        return m.group(0)
    return re.sub(r"\(([^)]*)\)", _paren, name)


def _clean(name: str, show_title: str | None) -> str:
    """Strip brackets, show prefix, noise tokens and years.

    Returns the remaining string that episode-number patterns run on.
    """
    name = _strip_brackets(name)
    name = _strip_show_prefix(name, show_title)
    name = _NOISE_RE.sub(" ", name)
    name = _YEAR_RE.sub(" ", name)
    # Lone resolution-ish numbers that survived as ``1080`` / ``720``
    # (without the ``p``) are noise too.
    name = re.sub(r"\b(?:1080|720|2160|480)\b", " ", name)
    return re.sub(r"[\s\-_\.]+", " ", name).strip()


def parse_episode(filename: str, show_title: str | None = None) -> EpisodeGuess:
    """Parse the episode (and optional season) out of one filename.

    Priority: ``SxxEyy`` > ``Eyy`` > ``第x集/话`` > bare number.
    Anything ambiguous returns ``episode=None`` — never a 01 fallback.
    """
    stem = strip_lang_suffix(filename)
    cleaned = _clean(stem, show_title)

    # 1. SxxEyy / Sxx Eyy / 2x05 / 第x季第y集 / Season x Episode y
    m = re.search(r"(?i)\bS(\d{1,2})\s*E(\d{1,3})\b", cleaned)
    if m:
        return EpisodeGuess(int(m.group(2)), int(m.group(1)), "high", "SxxEyy")
    m = re.search(r"(?i)\b(\d{1,2})\s*[xX]\s*(\d{1,3})\b", cleaned)
    if m:
        return EpisodeGuess(int(m.group(2)), int(m.group(1)), "high", "NxNN")
    m = re.search(r"第\s*(\d{1,2})\s*季\s*第\s*(\d{1,3})\s*[集话話]", cleaned)
    if m:
        return EpisodeGuess(int(m.group(2)), int(m.group(1)), "high", "第x季第y集")
    m = re.search(r"(?i)\bseason\s*(\d{1,2})\s*episode\s*(\d{1,3})\b", cleaned)
    if m:
        return EpisodeGuess(int(m.group(2)), int(m.group(1)), "high", "Season x Episode y")

    # Season-only marker (``S2 01`` / ``Season 2 - 05`` / ``第二季 03``):
    # remember it, then let the bare-number step find the episode.
    # "Season 2" must be stripped *before* the bare-number step, or
    # its "2" looks like a second episode candidate ("Season 2 - 05"
    # used to come out as ambiguous ['2', '05'] and never rename).
    season: int | None = None
    m = re.search(r"(?i)\bseason\s*(\d{1,2})\b", cleaned)
    if m:
        season = int(m.group(1))
        cleaned = re.sub(r"(?i)\bseason\s*\d{1,2}\b", " ", cleaned)
    else:
        m = re.search(r"(?i)\bS(\d{1,2})\b", cleaned)
        if m:
            season = int(m.group(1))
            cleaned = re.sub(r"(?i)\bS\d{1,2}\b", " ", cleaned)
        else:
            m = re.search(r"第\s*(\d{1,2})\s*季", cleaned)
            if m:
                season = int(m.group(1))
                cleaned = re.sub(r"第\s*\d{1,2}\s*季", " ", cleaned)
            else:
                m = re.search(r"第([一二三四五六七八九十])季", cleaned)
                if m:
                    season = "一二三四五六七八九十".index(m.group(1)) + 1
                    cleaned = re.sub(r"第[一二三四五六七八九十]季", " ", cleaned)

    # 2. Eyy alone / EPyy / 第x集 / 第x话
    m = re.search(r"(?i)\bE(?:P|p)?\s*(\d{1,3})\b", cleaned)
    if m:
        return EpisodeGuess(int(m.group(1)), season, "high", "Eyy")
    m = re.search(r"第\s*(\d{1,3})\s*[集话話]", cleaned)
    if m:
        return EpisodeGuess(int(m.group(1)), season, "high", "第x集")

    # 3. Bare number: exactly one standalone number may remain.
    # After stripping, tokens are space-separated; a token that is
    # purely digits is a candidate.  Zero / one / many decide.
    tokens = [t for t in re.split(r"\s+", cleaned) if t]
    numbers = [t for t in tokens if t.isdigit()]
    # A show named with digits (``24``, ``86``) contributes its own
    # number as a token; that number is the show, never the episode.
    if show_title and show_title.strip().isdigit():
        numbers = [t for t in numbers if int(t) != int(show_title.strip())]
    if len(numbers) == 1:
        return EpisodeGuess(int(numbers[0]), season, "medium", "bare number")
    if len(numbers) == 0:
        return EpisodeGuess(None, season, "none", "no number found")
    return EpisodeGuess(
        None, season, "none",
        f"ambiguous numbers: {numbers} (not guessing)",
    )


def season_number_from_folder(folder_name: str) -> int | None:
    """``Season 01`` -> 1, ``Specials`` -> 0, else None."""
    name = folder_name.strip()
    if name.lower() == "specials":
        return 0
    m = re.match(r"(?i)season\s*(\d+)", name)
    return int(m.group(1)) if m else None


def is_organized(filename: str, show_title: str) -> bool:
    """Already in ``Title - S01E05 - EpTitle.ext`` form?  Skip it."""
    pattern = re.compile(
        r"^" + re.escape(show_title) + r" - S\d{2}E\d{2} - ",
        re.IGNORECASE,
    )
    return bool(pattern.match(filename))


def organized_episode(filename: str) -> tuple[int, int] | None:
    """``(season, episode)`` from an already-organised filename."""
    m = re.search(r"(?i)\bS(\d{2})E(\d{2,3})\b", filename)
    return (int(m.group(1)), int(m.group(2))) if m else None
