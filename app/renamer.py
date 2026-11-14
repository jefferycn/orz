"""Rename files inside one Season folder.

Rules (pain point 3 — following a series):
- already-renamed files (``Title - S01E05 - EpTitle.ext``) define the
  episodes that exist: their max episode and their gaps;
- a file with no trustworthy number fills the *lowest gap* first,
  else max + 1 (folder has 1-5 -> 6; folder has 1,2,3,5 -> 4);
- a number colliding with an existing episode is never overwritten:
  the file is marked pending;
- a file that cannot be parsed *and* cannot be gap-filled is pending,
  never silently renamed to 01.

State lives in ``<show>/.orz/state.json`` (one file per show, with a
per-season section): file fingerprint -> episode, plus the pending
list.  A re-scan reuses the stored mapping instead of re-guessing, so
an ambiguous file keeps the episode it was given.  Fingerprints are
size + sha256 of the first 4 MiB (cheap, stable across renames).

The watcher passes ``stable_files`` (names whose size has been
unchanged for a few seconds); a group with an unstable file is left
alone as pending ("still copying") instead of being renamed mid-copy.
A manual ``folder`` run passes nothing and processes everything.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from .mapper import EpisodeMapper, load_override
from .nfo import find_season_dir
from .parser import (
    MEDIA_EXTS, SUBTITLE_EXTS, VIDEO_EXTS, base_name, batch_parse,
    is_already_named, lang_suffix, looks_like_non_episode,
    parse_episode, season_number_from_folder,
)

log = logging.getLogger("orz.renamer")

STATE_DIRNAME = ".orz"
STATE_FILENAME = "state.json"


def _fingerprint(path: str) -> str:
    h = hashlib.sha256()
    size = os.path.getsize(path)
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(4 * 1024 * 1024))
    return f"{size}:{h.hexdigest()[:16]}"


def state_path(show_dir: str) -> str:
    return os.path.join(show_dir, STATE_DIRNAME, STATE_FILENAME)


def load_state(show_dir: str) -> dict:
    path = state_path(show_dir)
    if not os.path.exists(path):
        return {"seasons": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"seasons": {}}
    except (json.JSONDecodeError, OSError):
        return {"seasons": {}}


def save_state(show_dir: str, state: dict) -> None:
    os.makedirs(os.path.dirname(state_path(show_dir)), exist_ok=True)
    tmp = state_path(show_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path(show_dir))


def _sanitize(name: str) -> str:
    return name.replace("/", " ").replace("\\", " ").strip()


def target_filename(show_title: str, season: int, episode: int,
                    ep_title: str, filename: str) -> str:
    """``Title - S01E02 - EpTitle`` + original extension (+ lang tag)."""
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    base = f"{show_title} - S{season:02d}E{episode:02d}"
    if ep_title:
        base += f" - {ep_title}"
    if ext.lower() in SUBTITLE_EXTS:
        lang = lang_suffix(filename)
        return _sanitize(base) + lang + "." + ext
    return _sanitize(base) + "." + ext


def group_files(season_dir: str) -> dict[str, list[str]]:
    """Group video + subtitle files by base name (lang suffix stripped)."""
    groups: dict[str, list[str]] = {}
    for name in sorted(os.listdir(season_dir)):
        path = os.path.join(season_dir, name)
        if not os.path.isfile(path):
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in MEDIA_EXTS:
            continue
        groups.setdefault(base_name(name), []).append(name)
    return groups


def existing_episodes(season_dir: str) -> set[int]:
    """Episode numbers already held by *organised videos* in this folder.

    Only files already in the clean ``Title - SxxEyy - ...`` form
    count, and only videos: a scene-named file
    (``Show.S01E01.1080p...``) does not hold its episode — it is
    the file waiting to be renamed, and counting it would make it
    collide with itself (v2.7.5: a whole season of scene-named
    files would otherwise all pend "episode N already exists").
    A subtitle never holds an episode either: several subtitles
    share one episode, and a subtitle arriving after its video
    must still be renamed to match it.  A misplaced ``S02E01``
    file sitting in ``Season 1`` is not an existing episode of
    Season 1 either — counting it would make the real ``S01E01``
    collide with it.
    """
    season = season_number_from_folder(os.path.basename(season_dir))
    eps: set[int] = set()
    for name in os.listdir(season_dir):
        path = os.path.join(season_dir, name)
        if not os.path.isfile(path):
            continue
        if name.rsplit(".", 1)[-1].lower() not in VIDEO_EXTS:
            continue
        if not is_already_named(name, season):
            continue
        guess = parse_episode(name)
        if guess.confidence != "high" or guess.episode is None:
            continue
        ep_season = guess.season if guess.season is not None else season
        if season is None or ep_season == season:
            eps.add(guess.episode)
    return eps


def lowest_gap(existing: set[int]) -> int:
    """Lowest missing episode, else max + 1.

    Gaps are only searched from the *lowest episode already present*:
    a folder holding only 26-29 (earlier episodes live elsewhere /
    were already renamed) must fill 30 next, not "1".  A folder
    holding 1,2,3,5 still fills 4; 1-5 fills 6.
    """
    if not existing:
        return 1
    n = min(existing)
    while n in existing:
        n += 1
    return n


class RenameResult:
    def __init__(self):
        self.renamed: list[tuple[str, str]] = []  # (old, new)
        self.pending: list[dict] = []              # {file, reason}
        self.skipped: list[str] = []

    def to_dict(self) -> dict:
        return {
            "renamed": [{"from": o, "to": n} for o, n in self.renamed],
            "pending": self.pending,
            "skipped": self.skipped,
        }


def rename_season_folder(season_dir: str, show_title: str, show_dir: str,
                         mapper: EpisodeMapper, dry_run: bool = False,
                         stable_files: set[str] | None = None
                         ) -> RenameResult:
    """Rename every unorganised group in one Season folder.

    ``stable_files``: names (in this folder) that have finished
    copying, as judged by the watcher.  None means "treat all as
    stable" (manual ``folder`` / ``dry-run`` use).
    """
    result = RenameResult()
    season = season_number_from_folder(os.path.basename(season_dir))
    if season is None:
        result.pending.append({"file": season_dir,
                               "reason": "not a Season folder"})
        return result

    state = load_state(show_dir)
    season_state = state.setdefault("seasons", {}).setdefault(
        str(season), {"files": {}, "pending": []})
    known: dict[str, int] = season_state.setdefault("files", {})
    _known_before = dict(known)
    _pending_before = list(season_state.get("pending", []))

    # Absolute-numbering show?  If Season 01 already holds episodes
    # past TMDB S1's count (青之箱: S01E26-29 are really S2E01-04),
    # the show is numbered absolutely in Season 01 — S2 files must
    # continue there (S2E05 -> S01E30) instead of starting Season 02.
    # Only applies when renaming inside Season 01 itself.
    if season == 1 and mapper.seasons.get(1):
        s01_dir = os.path.join(show_dir, "Season 01")
        if os.path.isdir(s01_dir):
            if any(ep > len(mapper.seasons[1])
                   for ep in existing_episodes(s01_dir)):
                mapper.absolute_to = 1

    existing = existing_episodes(season_dir)
    # Episodes already claimed by stored mappings count as taken too.
    taken = set(existing) | set(known.values())

    # Batch common-text guesses: files dropped in together share the
    # release group / show title / codec — strip what is common to
    # the whole batch (token-level) and what is left per file is
    # (almost) only the episode number.  Computed once per scan over
    # the representative (video) file of each unorganised group.
    _groups = group_files(season_dir)
    _reps = []
    _rep_of_group: dict[str, str] = {}
    for base, files in _groups.items():
        if all(is_already_named(f, season) for f in files):
            continue
        rep = next((f for f in files
                    if f.rsplit(".", 1)[-1].lower() in VIDEO_EXTS), files[0])
        _reps.append(rep)
        _rep_of_group[base] = rep
    _batch = batch_parse(_reps)
    if _batch:
        log.info("Batch common-text parse: %d file(s) guessed from "
                 "shared text", len(_batch))

    for base, files in group_files(season_dir).items():
        # Whole group already organised -> skip.  "Organised" means
        # the clean library form ``Title - S01E05 - ...`` (see
        # ``is_already_named``): a scene release name
        # (``Show.S01E01.1080p.WEB-DL...``) carries a marker but is
        # a download name, and orz's job is to normalise it.
        if all(is_already_named(f, season) for f in files):
            result.skipped.extend(files)
            continue
        # Still being copied/moved in: do not touch it this scan.
        # (The watcher re-checks it next scan; the pending reason is
        # logged so the user can see why nothing happened yet.)
        if stable_files is not None and any(
                f not in stable_files for f in files):
            result.pending.append({
                "file": files[0],
                "reason": "file still copying/moving (size not stable yet), "
                          "will retry next scan"})
            continue
        if any(is_already_named(f, season) for f in files):
            # Mixed group: organised files stay, the rest follow them.
            ep = next(
                parse_episode(f).episode
                for f in files
                if is_already_named(f, season)
                and parse_episode(f).episode is not None)
            _rename_group(season_dir, show_title, files, season, ep,
                          mapper, result, dry_run, taken, known=known, show_dir=show_dir)
            continue

        video = next((f for f in files
                      if f.rsplit(".", 1)[-1].lower() in VIDEO_EXTS), files[0])
        # Episode slots belong to videos: a subtitle never claims
        # one and never blocks a video (or another subtitle) from
        # it.  A subtitle-only group therefore skips the `taken`
        # checks below — a subtitle arriving after its video must
        # still be renamed to match it.  A real filename clash is
        # still caught by _rename_group's target-exists check.
        group_has_video = any(
            f.rsplit(".", 1)[-1].lower() in VIDEO_EXTS for f in files)
        # The fingerprint (first 4 MiB of the video, hashed) is lazy:
        # it is only needed to look up a stored mapping from an
        # earlier scan, or to record a gap-fill guess.  A file whose
        # name parses to an episode needs neither — a successful
        # rename is recognised as organised on the next scan, so
        # there is nothing to re-guess.  Computing it eagerly here
        # cost ~4 GiB of page cache on a full-library startup scan.
        fingerprint = None

        # 1. Stored mapping from a previous scan wins — no re-guessing.
        if known:
            fingerprint = _fingerprint(os.path.join(season_dir, video))
            if fingerprint in known:
                ep = known[fingerprint]
                mapped = mapper.map(season, ep)
                if mapped and (not group_has_video
                               or mapped[1] not in taken - {ep}):
                    _rename_group(season_dir, show_title, files, mapped[0],
                                  mapped[1], mapper, result, dry_run, taken,
                                  ep_title=mapped[2], known=known,
                                  show_dir=show_dir)
                    continue
                result.pending.append({"file": video,
                                       "reason": "stored episode no longer maps"})
                continue

        # 2. Parse the filename.  An explicit SxxEyy / Eyy / 第x集
        # wins; otherwise a batch common-text guess wins over the
        # per-file bare-number guess — the shared text (release
        # group, show title, "Vol.3", codec) has already been
        # stripped, so a number that is common to the whole batch
        # cannot look like a second episode candidate.
        guess = parse_episode(video, show_title)
        batch_guess = _batch.get(_rep_of_group.get(base, ""))
        if guess.confidence != "high" and batch_guess is not None:
            guess = batch_guess
            log.info("Batch parse %s -> episode %s (season %s)",
                     video, guess.episode, guess.season)
        if guess.episode is not None:
            # Arc-title match (v2.8.0): a medium-confidence number
            # with no season marker can be arc-relative —
            # "[凡人修仙传 慕兰之战][24]" is 慕兰之战 #24 (TMDB
            # S01E200), not #24 of the whole show.  When the
            # filename's text + the number exactly equals one TMDB
            # episode title, that title wins over the bare number.
            # High-confidence markers (SxxEyy / Eyy / 第x集) and
            # guesses with a season marker keep their own path.
            mapped = None
            if guess.confidence == "medium" and guess.season is None:
                mapped = mapper.map_by_title(
                    video, guess.episode, season, show_title)
                if mapped is not None:
                    log.info("Title match: %s -> S%02dE%02d (%s)",
                             video, mapped[0], mapped[1], mapped[2])
            if mapped is None:
                mapped = mapper.map(guess.season if guess.season is not None
                                    else season, guess.episode)
            if mapped is None:
                result.pending.append({
                    "file": video,
                    "reason": f"episode {guess.episode} has no TMDB match "
                              f"(season {guess.season or season})"})
                continue
            target_season, target_ep, title = mapped
            if group_has_video and target_ep in taken:
                result.pending.append({
                    "file": video,
                    "reason": f"episode {target_ep} already exists "
                              "(collision, not overwriting)"})
                continue
            _rename_group(season_dir, show_title, files, target_season,
                          target_ep, mapper, result, dry_run, taken,
                          fingerprint=fingerprint, ep_title=title, known=known, show_dir=show_dir)
            continue

        # 3. No trustworthy number: gap-fill (lowest gap, else max + 1).
        # Never for a file that looks like a special / trailer /
        # extra: "Sp01", "30分钟前瞻PV", "Sherlock...Extras" are
        # not episodes, and gap-filling them invents one.
        if looks_like_non_episode(video):
            result.pending.append({
                "file": video,
                "reason": "looks like a special/trailer/extra, "
                          "not an episode — left alone"})
            continue
        ep = lowest_gap(taken)
        mapped = mapper.map(season, ep)
        if mapped is None:
            result.pending.append({
                "file": video,
                "reason": f"no episode number in name, and gap-fill "
                          f"episode {ep} has no TMDB match"})
            continue
        if fingerprint is None:
            # Gap-fill is a guess: record it, so a re-scan reuses
            # the same episode instead of guessing again.
            fingerprint = _fingerprint(os.path.join(season_dir, video))
        _rename_group(season_dir, show_title, files, mapped[0], mapped[1],
                      mapper, result, dry_run, taken,
                      fingerprint=fingerprint, ep_title=mapped[2],
                      inferred=True, known=known, show_dir=show_dir)

    season_state["pending"] = result.pending
    # Don't rewrite state.json on a no-op scan (avoids a disk write
    # for every season on every poll round).
    known_changed = known != _known_before
    if not dry_run and (result.renamed or known_changed
                        or result.pending != _pending_before):
        save_state(show_dir, state)
    return result


def _rename_group(season_dir: str, show_title: str, files: list[str],
                  season: int, episode: int, mapper: EpisodeMapper,
                  result: RenameResult, dry_run: bool, taken: set[int],
                  fingerprint: str | None = None, ep_title: str | None = None,
                  inferred: bool = False,
                  known: dict | None = None,
                  show_dir: str | None = None) -> None:
    if ep_title is None:
        ep_title = mapper._episode_title(season, episode)
    # An empty title is fine (absolute mapping past TMDB's listed
    # episodes): the filename just omits the " - title" part.
    # Absolute mapping can land in a different season than the folder
    # the file sits in (S2E01 -> S01E13): move it to that season's
    # folder when one exists (the standard padded name, e.g.
    # ``Season 01`` — see ``normalize_season_folders``).  When no
    # folder for the target season exists, the file stays where it
    # is — orz never creates season folders during a rename.
    dest_dir = season_dir
    if show_dir and season != season_number_from_folder(
            os.path.basename(season_dir)):
        target_dir = find_season_dir(show_dir, season)
        if target_dir is not None:
            dest_dir = target_dir
    for filename in files:
        new = target_filename(show_title, season, episode, ep_title, filename)
        if filename == new and dest_dir == season_dir:
            result.skipped.append(filename)
            continue
        dest = os.path.join(dest_dir, new)
        if os.path.exists(dest):
            result.pending.append({"file": filename,
                                   "reason": f"target {new} already exists"})
            return
        if not dry_run:
            os.rename(os.path.join(season_dir, filename), dest)
        result.renamed.append((filename, new))
        log.info("Renaming: %s -> %s%s", filename, new,
                 " (gap-fill)" if inferred else "")
    # Only a video claims its episode slot (see the `taken` note in
    # rename_season_folder): a subtitle renamed here must not block
    # the video of the same episode arriving later.
    if any(f.rsplit(".", 1)[-1].lower() in VIDEO_EXTS for f in files):
        taken.add(episode)
    if fingerprint is not None and known is not None and not dry_run:
        # Remember fingerprint -> episode so a re-scan reuses this
        # mapping instead of re-guessing (gap-fill stays stable).
        known[fingerprint] = episode
