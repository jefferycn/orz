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
# ``sc`` / ``tc`` are Simplified / Traditional Chinese, common in
# fansub releases as ``.sc.ass`` / ``.tc_v2.ass`` (``_v2`` = version).
LANG_TAGS = {
    "zh", "chs", "cht", "cn", "tw", "hk",
    "en", "eng", "jp", "jpn", "ja", "kor", "ko",
    "sc", "tc",
    "other", "default", "forced",
}


def _is_lang_tag(tail: str) -> bool:
    """Is ``tail`` (the part after the last ``.``) a language tag?

    Accepts a bare tag (``zh``, ``sc``, ``tc``) and a tag with a
    version / variant suffix (``sc_v2``, ``tc_v2``, ``chs_v2``,
    ``zh-Hans``).  The first ``_``/``-`` segment must be a known
    language code, so ``v2`` / ``2024`` / ``x265`` are never tags.
    """
    first = re.split(r"[_-]", tail.lower(), maxsplit=1)[0]
    return first in LANG_TAGS

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
    # Then a language tag, if present (``.zh`` / ``.en`` / ``.chs`` /
    # ``.sc_v2`` / ``.tc_v2`` ...).
    if "." in stem:
        head, tail = stem.rsplit(".", 1)
        if _is_lang_tag(tail):
            return head
    return stem


def base_name(filename: str) -> str:
    """Base name used for grouping video + subtitles of one episode."""
    return strip_lang_suffix(filename)


def lang_suffix(filename: str) -> str:
    """``.zh`` for ``Title.zh.srt``, ``.sc_v2`` for ``Title.sc_v2.ass``,
    else ``""`` (kept on rename — sc/tc must stay distinct)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    if "." in stem:
        head, tail = stem.rsplit(".", 1)
        if _is_lang_tag(tail):
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


def _bracketed_number(name: str) -> int | None:
    """Episode number written as ``[01]`` / ``(01)``.

    Fansub releases often put the episode in brackets, e.g.
    ``[DBD-Raws][Medalist][01][1080P]...`` or
    ``...[BDRip][01](A85E5FC2).cht.ass``.  Bracket groups are stripped
    before parsing, so this must run *first* on the raw stem.
    Returns the number only when exactly one distinct candidate
    remains; ``[1080P]`` / ``[2024]`` are not pure-digit groups and
    never match, and a lone ``1080``/``720``/``2160`` is noise.
    """
    candidates = []
    for m in re.finditer(r"\[(\d{1,3})\]|\((\d{1,3})\)", name):
        n = int(m.group(1) or m.group(2))
        if n in {1080, 720, 2160, 480, 576} or n == 0:
            continue
        candidates.append(n)
    distinct = set(candidates)
    return distinct.pop() if len(distinct) == 1 else None


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
    # CRC checksum in parentheses, e.g. (A85E5FC2) — 8 hex chars, noise.
    name = re.sub(r"\([0-9A-Fa-f]{8}\)", " ", name)
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
    # Bracketed episode first (see _bracketed_number): [01] is stripped
    # by _clean, so remember it before cleaning.
    bracket_ep = _bracketed_number(stem)
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

    # 2b. Bracketed episode [01] — remembered before _clean stripped
    # it.  Runs before the bare-number step so "[Medalist][01]"
    # (nothing left after stripping) still finds its episode, and
    # so a CRC like (A85E5FC2) cannot be mistaken for a number.
    if bracket_ep is not None:
        return EpisodeGuess(bracket_ep, season, "medium", "bracket [NN]")

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


def _tokens(name: str) -> list[str]:
    """Filename stem split into tokens (space / ``-`` / ``_`` / ``.`` /
    brackets / parens are separators).  Token-level, never
    character-level: ``01`` / ``02`` share the character ``0``,
    but they are different tokens."""
    stem = strip_lang_suffix(name)
    return [t for t in re.split(r"[\s\-_\.\[\]\(\)]+", stem) if t]


def _season_from_tokens(tokens: list[str]) -> int | None:
    """Season number inside a token list, e.g. ``Season 2`` / ``S2`` /
    ``第2季`` / ``第二季``.  Used for a season that lives in the
    common prefix (``Ao no Hako Season 2 - 05``) and would be lost
    when that prefix is stripped."""
    joined = " ".join(tokens)
    m = re.search(r"(?i)\bseason\s*(\d{1,2})\b", joined)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)\bS(\d{1,2})\b", joined)
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*(\d{1,2})\s*季", joined)
    if m:
        return int(m.group(1))
    m = re.search(r"第([一二三四五六七八九十])季", joined)
    if m:
        return "一二三四五六七八九十".index(m.group(1)) + 1
    return None


def batch_parse(filenames: list[str]) -> dict[str, EpisodeGuess]:
    """Guess episodes from the text *common* to a batch of filenames.

    The user's original idea: files dropped in together share the
    release group, show title, codec, year, etc. — strip what is
    common to all of them (token-level common prefix + suffix) and
    what is left per file is (almost) only the episode number.
    E.g. ``[Moozzi2] Giji Harem - 01 (BD 1920x1080 ...)`` /
    ``... - 02 ...`` -> rest ``01`` / ``02``; ``Show Vol.3 - 01`` /
    ``Show Vol.3 - 02`` -> the shared ``Vol.3`` is stripped, so the
    ``3`` no longer looks like a second episode candidate.

    Returns ``{filename: EpisodeGuess}`` for files it could guess.
    Fewer than 2 files -> ``{}`` (no "common" exists for one file;
    the caller falls back to ``parse_episode``).  A file whose
    remainder has no trustworthy number is simply absent — also a
    fallback to ``parse_episode``, never a 01 guess.
    """
    if len(filenames) < 2:
        return {}
    token_lists = [_tokens(f) for f in filenames]
    pre = 0
    for tup in zip(*token_lists):
        if len({t.lower() for t in tup}) == 1:
            pre += 1
        else:
            break
    if pre == 0:
        return {}
    suf = 0
    for tup in zip(*[reversed(t) for t in token_lists]):
        if len({t.lower() for t in tup}) == 1:
            suf += 1
        else:
            break
    min_len = min(len(t) for t in token_lists)
    # Prefix + suffix must not eat the whole name.
    if pre + suf >= min_len:
        suf = max(0, min_len - pre - 1)
    out: dict[str, EpisodeGuess] = {}
    for filename, toks in zip(filenames, token_lists):
        rest = toks[pre:len(toks) - suf if suf else None]
        if not rest:
            continue
        season = _season_from_tokens(toks[:pre])
        guess = parse_episode(" ".join(rest), None)
        if guess.episode is None:
            continue
        if season is not None and guess.season is None:
            guess = EpisodeGuess(guess.episode, season, guess.confidence,
                                 "common text")
        else:
            guess = EpisodeGuess(guess.episode, guess.season or season,
                                 guess.confidence,
                                 f"common text: {guess.reason}")
        out[filename] = guess
    return out


def title_candidates(filename: str, show_title: str | None) -> list[str]:
    """Text segments of a filename that could be an arc name.

    Bracket groups (``[凡人修仙传 慕兰之战]`` -> ``慕兰之战``,
    show title stripped; the unstripped group is kept too) plus
    the cleaned remainder text (``凡人修仙传 慕兰之战 24`` ->
    ``慕兰之战``).  Pure-digit groups (``[2026]``, ``[24]``) are
    numbers, not arc text.  Used by the arc-title match
    (``EpisodeMapper.map_by_title``): a candidate + the parsed
    episode number is compared against TMDB episode titles, so
    junk candidates are harmless — only an exact title match
    counts.  A spaced candidate is also tried joined
    (``慕兰 之战`` -> ``慕兰之战``), since titles have no space
    between arc and number.
    """
    stem = strip_lang_suffix(filename)
    out: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        text = re.sub(r"[\s\-_\.]+", " ", text).strip()
        if not text or len(text) < 2 or text.isdigit():
            return
        if text not in seen:
            seen.add(text)
            out.append(text)
        joined = text.replace(" ", "")
        if joined != text and joined not in seen:
            seen.add(joined)
            out.append(joined)

    for m in re.finditer(r"\[([^\]]*)\]|\(([^)]*)\)", stem):
        inner = (m.group(1) or m.group(2) or "").strip()
        if not inner:
            continue
        _add(inner)
        if show_title:
            _add(inner.replace(show_title.strip(), " "))
    # Non-bracket remainder: cleaned name minus the number tokens.
    cleaned = _clean(stem, show_title)
    cleaned = re.sub(r"\b\d+\b", " ", cleaned)
    _add(cleaned)
    return out


def season_number_from_folder(folder_name: str) -> int | None:
    """``Season 01`` -> 1, ``Specials`` -> 0, else None.

    Short season folders count too: ``S02`` / ``S03`` (Banshee's
    layout).  Without this the event watcher treats an ``S02``
    folder as a show folder and warns "No TMDB match" for it.
    """
    name = folder_name.strip()
    if name.lower() == "specials":
        return 0
    m = re.match(r"(?i)season\s*(\d+)", name)
    if m:
        return int(m.group(1))
    m = re.match(r"(?i)^S(\d{1,2})$", name)
    return int(m.group(1)) if m else None


def is_organized(filename: str, show_title: str) -> bool:
    """Already in ``Title - S01E05 - EpTitle.ext`` form?  Skip it."""
    pattern = re.compile(
        r"^" + re.escape(show_title) + r" - S\d{2}E\d{2} - ",
        re.IGNORECASE,
    )
    return bool(pattern.match(filename))


# Markers that say "this is not a regular episode": specials,
# trailers / previews, extras, openings / endings.  Such a file
# must never be gap-filled into an episode number — a preview
# called "30分钟前瞻PV" is not episode 5, and "Sp01" is not
# episode 13.  Matched as whole tokens for the short codes
# (``SP`` / ``PV`` / ``OP`` / ``ED`` / ``CM``), as substrings
# for the longer words.
_NON_EPISODE_TOKENS = {
    "sp", "special", "specials", "extra", "extras", "pv", "op", "ed",
    "ncop", "nced", "cm", "menu", "preview", "trailer", "teaser",
}
_NON_EPISODE_SUBSTRINGS = (
    "special", "extra", "trailer", "preview", "teaser",
    "预告", "特典", "花絮", "前瞻", "宣传", "先行",
)
# Short code + episode number, e.g. ``Sp01`` / ``Sp02`` — the token
# is one word, so the plain token set above never sees bare ``sp``.
_NON_EPISODE_CODE_RE = re.compile(r"(?i)^(sp|pv|op|ed|ncop|nced|cm)\d+$")


def looks_like_non_episode(filename: str) -> bool:
    """True for specials / trailers / extras / previews.

    ``Sp01`` / ``Extras`` / ``30分钟前瞻PV`` / ``预告`` all match.
    A normal episode name (``Show - S01E05 - Title``) does not.
    """
    stem = strip_lang_suffix(filename)
    tokens = [t.lower() for t in re.split(r"[\s\-_\.\[\]\(\)]+", stem) if t]
    if any(t in _NON_EPISODE_TOKENS for t in tokens):
        return True
    if any(_NON_EPISODE_CODE_RE.match(t) for t in tokens):
        return True
    lowered = stem.lower()
    return any(s in lowered for s in _NON_EPISODE_SUBSTRINGS)


def _clean_marker_re(guess) -> re.Pattern | None:
    """Regex for `` - <this episode's marker>`` at the end of a stem.

    ``S04E01`` -> `` - S0*4E0*1(?: - .*)?$``; the ``0*`` allows any
    zero padding (``S4E1`` / ``S04E01``).  Only the marker the parser
    actually found counts — a different number elsewhere in the name
    does not make the file organised.
    """
    s, e = guess.season, guess.episode
    if guess.reason == "SxxEyy":
        if s is None:
            return None
        return re.compile(rf" - S0*{s}E0*{e}(?: - .*)?$", re.IGNORECASE)
    if guess.reason == "NxNN":
        if s is None:
            return None
        return re.compile(rf" - 0*{s}[xX]0*{e}(?: - .*)?$")
    if guess.reason == "Eyy":
        return re.compile(rf" - E0*{e}(?: - .*)?$", re.IGNORECASE)
    if guess.reason == "Season x Episode y":
        if s is None:
            return None
        return re.compile(rf" - Season 0*{s} Episode 0*{e}(?: - .*)?$",
                          re.IGNORECASE)
    if guess.reason == "第x季第y集":
        if s is None:
            return None
        return re.compile(rf" - 第0*{s}季第0*{e}集(?: - .*)?$")
    return None


def is_already_named(filename: str, folder_season: int | None) -> bool:
    """Already in the clean ``Title - S01E05 - ...`` library form?

    v2.7 counted *any* explicit marker as organised — which also
    skipped scene release names (``Show.S04E01.1080p.WEB-DL...``):
    they carry an SxxEyy marker, but they are download names, not
    library names, and orz's job is to normalise exactly those
    (v2.7.5: a whole season of .ass files moved in, and nothing
    happened at all — no rename, no log).  So the marker must sit
    in clean form: `` - SxxEyy`` (or `` - NxNN`` / `` - Eyy`` /
    `` - Season x Episode y`` / `` - 第x季第y集``), followed by the
    end of the name or `` - <episode title>``.  ``Dragon Ball -
    S02E15 - ...`` and orz's own ``Title - S01E05 - ...`` count;
    ``Attack on Titan S01E01`` (no dash before the marker) and
    ``Desperate.Housewives.S04E01.1080p...`` (dots, release tags)
    do not — they get renamed to the Chinese form.  Only the
    Western markers count: a bare number, a bracket ``[01]`` and a
    plain ``第x集`` / ``第x話`` are fansub naming that orz is meant
    to normalise (ReLIFE ``第14話`` still gets renamed to
    ``S01E14``).  A marker for a *different* season is not
    "already named" either.
    """
    guess = parse_episode(filename)
    if guess.confidence != "high" or guess.episode is None:
        return False
    if guess.reason not in {"SxxEyy", "NxNN", "Season x Episode y",
                            "Eyy", "第x季第y集"}:
        return False
    if guess.season is not None and folder_season is not None \
            and guess.season != folder_season:
        return False
    # Eyy: season is the folder's season.
    marker_re = _clean_marker_re(guess)
    if marker_re is None:
        return False
    return marker_re.search(strip_lang_suffix(filename)) is not None


def organized_episode(filename: str) -> tuple[int, int] | None:
    """``(season, episode)`` from an already-organised filename."""
    m = re.search(r"(?i)\bS(\d{2})E(\d{2,3})\b", filename)
    return (int(m.group(1)), int(m.group(2))) if m else None
