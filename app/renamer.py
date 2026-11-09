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
from .parser import (
    MEDIA_EXTS, SUBTITLE_EXTS, VIDEO_EXTS, base_name, is_organized,
    lang_suffix, organized_episode, parse_episode, season_number_from_folder,
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
    """Episode numbers already present as organised files in this folder."""
    eps: set[int] = set()
    for name in os.listdir(season_dir):
        parsed = organized_episode(name)
        if parsed:
            eps.add(parsed[1])
    return eps


def lowest_gap(existing: set[int]) -> int:
    """Lowest missing episode starting at 1, else max + 1."""
    n = 1
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

    existing = existing_episodes(season_dir)
    # Episodes already claimed by stored mappings count as taken too.
    taken = set(existing) | set(known.values())

    for base, files in group_files(season_dir).items():
        # Whole group already organised -> skip.
        if all(is_organized(f, show_title) for f in files):
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
        if any(is_organized(f, show_title) for f in files):
            # Mixed group: organised files stay, the rest follow them.
            ep = next(organized_episode(f)[1]
                      for f in files if organized_episode(f))
            _rename_group(season_dir, show_title, files, season, ep,
                          mapper, result, dry_run, taken, known=known, show_dir=show_dir)
            continue

        video = next((f for f in files
                      if f.rsplit(".", 1)[-1].lower() in VIDEO_EXTS), files[0])
        fingerprint = _fingerprint(os.path.join(season_dir, video))

        # 1. Stored mapping from a previous scan wins — no re-guessing.
        if fingerprint in known:
            ep = known[fingerprint]
            mapped = mapper.map(season, ep)
            if mapped and mapped[1] not in taken - {ep}:
                _rename_group(season_dir, show_title, files, season,
                              mapped[1], mapper, result, dry_run, taken,
                              target_season=mapped[0], known=known, show_dir=show_dir)
                continue
            result.pending.append({"file": video,
                                   "reason": "stored episode no longer maps"})
            continue

        # 2. Parse the filename.
        guess = parse_episode(video, show_title)
        if guess.episode is not None:
            mapped = mapper.map(guess.season if guess.season is not None
                                else season, guess.episode)
            if mapped is None:
                result.pending.append({
                    "file": video,
                    "reason": f"episode {guess.episode} has no TMDB match "
                              f"(season {guess.season or season})"})
                continue
            target_season, target_ep, title = mapped
            if target_ep in taken:
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
        ep = lowest_gap(taken)
        mapped = mapper.map(season, ep)
        if mapped is None:
            result.pending.append({
                "file": video,
                "reason": f"no episode number in name, and gap-fill "
                          f"episode {ep} has no TMDB match"})
            continue
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
                  target_season: int | None = None,
                  inferred: bool = False,
                  known: dict | None = None,
                  show_dir: str | None = None) -> None:
    if ep_title is None:
        ep_title = mapper._episode_title(season, episode)
    # An empty title is fine (absolute mapping past TMDB's listed
    # episodes): the filename just omits the " - title" part.
    # Absolute mapping can land in a different season than the folder
    # the file sits in (S2E01 -> S01E13): move it to that season's
    # folder when it exists, instead of leaving a S01E13 file in
    # Season 02.
    dest_dir = season_dir
    if show_dir and season != season_number_from_folder(
            os.path.basename(season_dir)):
        target_dir = os.path.join(show_dir, f"Season {season:02d}")
        if os.path.isdir(target_dir):
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
    taken.add(episode)
    if fingerprint is not None and known is not None and not dry_run:
        # Remember fingerprint -> episode so a re-scan reuses this
        # mapping instead of re-guessing (gap-fill stays stable).
        known[fingerprint] = episode
