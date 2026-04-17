"""Migrate legacy per-shot audio into a per-chapter recordings.json manifest
and backfill ``shot.audio.lines`` so the Editing Room can play dialogue.

Background
----------
Prior to the 2026-03-10 pipeline restructure, VoiceRecordingStage wrote
per-shot audio at ``audio/{ch}/{shot_id}/{shot_id}_line{NNN}.mp3`` with a
sibling ``.meta.json`` describing each line (text, character_id, text_hash,
duration_sec, direction).

After the restructure, downstream stages (Screenplay Review UI,
CinematographerStage pacing, SoundFXStage, AudioScoreStage, Editing Room)
read ``audio/{ch}/recordings.json`` — a manifest that maps screenplay-line
IDs (``{ch}_line{NNN}``) to audio refs, durations, and text — and
CinematographerStage populates ``shot.audio.lines`` referencing those
recordings so the Editing Room knows which audio belongs to each shot.

Projects recorded under the old layout have (1) no recordings.json and
(2) empty ``shot.audio`` objects. This script fixes both:

1. Rebuild recordings.json from existing ``.meta.json`` files (audio_ref
   points at the legacy mp3 locations — no file moves).
2. Walk every shot.json in the chapter and populate ``shot.audio.lines``
   by matching ``shot.dialogue_in_shot`` entries against recordings.

Usage
-----
    python3 utils/migrate_audio_to_recordings_json.py --project <path> [--dry-run]
    python3 utils/migrate_audio_to_recordings_json.py --project <path> --chapter ch01
    python3 utils/migrate_audio_to_recordings_json.py --project <path> --force
    python3 utils/migrate_audio_to_recordings_json.py --project <path> --force-shots
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


# Make orchestrator modules importable whether the script is invoked from
# the orchestrator dir or from the repo root.
SCRIPT_DIR = Path(__file__).resolve().parent
ORCH_ROOT = SCRIPT_DIR.parent
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

from core.project import Project  # noqa: E402
from stages.pipeline import VoiceRecordingStage  # noqa: E402


def _load_meta_files(chapter_audio_dir: Path) -> list[dict]:
    """Scan audio/{ch}/*/*.meta.json and return parsed records with their mp3 path."""
    records = []
    for meta_path in sorted(chapter_audio_dir.glob("*/*.meta.json")):
        mp3_path = meta_path.with_suffix("").with_suffix(".mp3")
        if not mp3_path.exists():
            print(f"    [WARN] missing mp3 next to {meta_path.name}")
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"    [WARN] unreadable {meta_path.name}: {e}")
            continue
        records.append({
            "meta": meta,
            "mp3_rel": mp3_path,
            "meta_rel": meta_path,
        })
    return records


def _index_by_hash(records: list[dict]) -> dict[str, dict]:
    """Build {text_hash -> record} for primary match."""
    out = {}
    for r in records:
        h = r["meta"].get("text_hash")
        if h and h not in out:
            out[h] = r
    return out


def _index_by_prefix(records: list[dict]) -> dict[tuple[str, str], dict]:
    """Build {(character_id, text[:100]) -> record} for fallback match."""
    out = {}
    for r in records:
        m = r["meta"]
        key = (m.get("character_id", ""), (m.get("text") or "")[:100])
        if key not in out:
            out[key] = r
    return out


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _build_recordings(project: Project, chapter_id: str, stats: dict) -> list | None:
    """Build the recordings array for a chapter from legacy .meta.json files.

    Returns the list on success, or None if the chapter can't be migrated
    (e.g. no legacy audio). Side-effect: mutates ``stats``.
    """
    chapter_audio = Path(project.root) / "audio" / chapter_id

    screenplay_path = project._path("chapters", chapter_id, "screenplay.md")
    if not screenplay_path.exists():
        stats["status"] = "skip_no_screenplay"
        return None

    screenplay_text = screenplay_path.read_text(encoding="utf-8")
    dialogue = VoiceRecordingStage._parse_screenplay_dialogue(screenplay_text)
    stats["screenplay_lines"] = len(dialogue)

    old_records = _load_meta_files(chapter_audio)
    stats["old_recordings"] = len(old_records)

    if not old_records:
        stats["status"] = "skip_no_legacy_audio"
        return None

    hash_idx = _index_by_hash(old_records)
    prefix_idx = _index_by_prefix(old_records)
    used_meta_paths: set[Path] = set()

    recordings = []
    project_root = Path(project.root).resolve()

    for i, dl in enumerate(dialogue):
        line_id = f"{chapter_id}_line{str(i + 1).zfill(3)}"
        text = dl["text"]
        character_id = dl["character_id"]
        text_hash = _md5(text)

        matched = hash_idx.get(text_hash)
        match_kind = "hash" if matched else None

        if not matched:
            matched = prefix_idx.get((character_id, text[:100]))
            if matched:
                match_kind = "prefix"

        if not matched:
            stats["unmatched_lines"] += 1
            continue

        if matched["meta_rel"] in used_meta_paths:
            stats["unmatched_lines"] += 1
            continue
        used_meta_paths.add(matched["meta_rel"])

        if match_kind == "hash":
            stats["matched_by_hash"] += 1
        else:
            stats["matched_by_prefix"] += 1

        meta = matched["meta"]
        audio_ref = matched["mp3_rel"].resolve().relative_to(project_root).as_posix()

        recordings.append({
            "recording_id": line_id,
            "character_id": character_id,
            "text": text,
            "text_hash": text_hash,
            "audio_ref": audio_ref,
            "duration_sec": meta.get("duration_sec", 0),
            "direction": dl.get("direction", "") or meta.get("direction", ""),
            "recorded_at": "migrated-from-meta-json",
        })

    stats["orphan_recordings"] = len(old_records) - len(used_meta_paths)
    return recordings


def _find_recording_for_shot_line(
    dialogue_text: str,
    candidate_characters: list[str],
    recordings: list[dict],
) -> dict | None:
    """Match a shot's short dialogue string to a full recording entry.

    Shots often store truncated dialogue (e.g. "May the Gods bless thee")
    while recordings have the full line. Text is the primary signal;
    ``candidate_characters`` is a tiebreaker, not a filter — a speaker may
    be off-camera, so a recording whose character_id isn't in
    ``characters_in_frame`` can still be the right match.

    Strategies, in order:
    1. Same first 100 chars (what CinematographerStage uses on fresh runs).
    2. Recording text starts with the shot's dialogue text (legacy short
       form).
    3. Ditto, case-insensitive.
    4. Shot's dialogue text appears anywhere inside a recording — handles
       long lines split across multiple shots, where only the first shot
       has the opening text and later shots carry mid-line excerpts.
    """
    if not dialogue_text:
        return None
    dt = dialogue_text.strip()
    if not dt:
        return None
    dt_lower = dt.lower()
    key = dt[:100]

    def _pick(candidates: list[dict]) -> dict | None:
        if not candidates:
            return None
        if candidate_characters:
            preferred = [
                r for r in candidates
                if r.get("character_id") in candidate_characters
            ]
            if preferred:
                return preferred[0]
        return candidates[0]

    exact = [r for r in recordings if r.get("text", "")[:100] == key]
    if exact:
        return _pick(exact)

    prefix = [r for r in recordings if r.get("text", "").startswith(dt)]
    if prefix:
        return _pick(prefix)

    ci_prefix = [
        r for r in recordings
        if r.get("text", "").lower().startswith(dt_lower)
    ]
    if ci_prefix:
        return _pick(ci_prefix)

    # Avoid very short fragments matching a word that appears in many
    # lines — e.g. "Yes" would hit half the chapter.
    if len(dt) < 8:
        return None

    substr = [r for r in recordings if dt in r.get("text", "")]
    if substr:
        return _pick(substr)

    substr_ci = [
        r for r in recordings
        if dt_lower in r.get("text", "").lower()
    ]
    return _pick(substr_ci)


_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def _load_alignment(project_root: Path, audio_ref: str) -> dict | None:
    mp3_path = (project_root / audio_ref).resolve()
    align_path = mp3_path.with_suffix(mp3_path.suffix + ".alignment.json")
    if not align_path.exists():
        return None
    try:
        with open(align_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _find_slice(dialogue_text: str, alignment: dict) -> tuple[float, float, float | None] | None:
    """Locate ``dialogue_text`` inside an aligned recording and return
    ``(start_sec, end_sec, next_word_start_sec)`` covering the first and
    last word of the match. ``next_word_start_sec`` is the aligned start
    of the word immediately after the slice, or None if the slice ends
    at the last word of the recording — callers use it to extend the
    tail without bleeding into the next word's attack.

    Matches on a whitespace- and punctuation-insensitive token sequence.
    Returns None if the shot's dialogue isn't found as a contiguous word
    subsequence within the recording's words.
    """
    dialogue_tokens = _tokenize(dialogue_text)
    if not dialogue_tokens:
        return None

    words = alignment.get("words") or []
    if not words:
        return None

    # Each aligned word may carry trailing punctuation ("More,"). Use the
    # first token of each; that's always the spoken word.
    word_first = []
    for w in words:
        tokens = _tokenize(w.get("word", ""))
        word_first.append(tokens[0] if tokens else "")

    m = len(dialogue_tokens)
    n = len(word_first)
    for i in range(n - m + 1):
        if word_first[i : i + m] == dialogue_tokens:
            start = words[i].get("start")
            end = words[i + m - 1].get("end")
            if start is None or end is None:
                continue
            next_start = None
            for j in range(i + m, n):
                ns = words[j].get("start")
                if ns is not None:
                    next_start = float(ns)
                    break
            return float(start), float(end), next_start
    return None


# Alignment-based word timings on wav2vec2 LS960 underestimate actual
# acoustic word-ends by ~200-300ms — the model's CTC "end-of-word" label
# fires before the consonant release / vowel fade. The pads below mirror
# that drift. When the next word is close, we'd rather bleed slightly
# into its attack than cut the current word's release mid-phoneme.
_SLICE_LEAD_IN_SEC = 0.08
_SLICE_TAIL_MIN_SEC = 0.30
_SLICE_TAIL_MAX_SEC = 0.50
_SLICE_TAIL_GAP_RATIO = 0.75


def _pad_slice_bounds(start: float, end: float, next_start: float | None,
                      duration: float) -> tuple[float, float]:
    """Apply lead-in / tail-out padding, clamped by adjacent-word gap."""
    padded_start = max(0.0, start - _SLICE_LEAD_IN_SEC)

    if next_start is None:
        # Last word in the recording — give the full tail.
        tail = _SLICE_TAIL_MAX_SEC
    else:
        gap = max(0.0, next_start - end)
        tail = min(_SLICE_TAIL_MAX_SEC,
                   max(_SLICE_TAIL_MIN_SEC, gap * _SLICE_TAIL_GAP_RATIO))

    padded_end = end + tail
    if duration:
        padded_end = min(float(duration), padded_end)
    return padded_start, padded_end


def _backfill_shots(
    project: Project,
    chapter_id: str,
    recordings: list[dict],
    dry_run: bool,
    force_shots: bool,
    stats: dict,
) -> None:
    """Populate shot.audio.lines for every shot in the chapter.

    Skips shots that already have audio.lines unless force_shots is set.
    When sidecar ``{mp3}.alignment.json`` files exist (produced by
    ``utils/align_recordings.py``), uses word-level timestamps to compute
    per-shot ``start_time_sec`` / ``end_time_sec`` so multiple shots
    referencing the same recording each get their own slice.
    """
    shots_dir = project._path("chapters", chapter_id, "shots")
    if not shots_dir.exists():
        return

    project_root = Path(project.root).resolve()
    alignment_cache: dict[str, dict | None] = {}  # recording_id -> alignment

    def _alignment_for(rec: dict) -> dict | None:
        rid = rec.get("recording_id")
        if rid in alignment_cache:
            return alignment_cache[rid]
        al = _load_alignment(project_root, rec.get("audio_ref") or "")
        alignment_cache[rid] = al
        return al

    for shot_dir in sorted(shots_dir.iterdir()):
        if not shot_dir.is_dir():
            continue
        shot_path = shot_dir / "shot.json"
        if not shot_path.exists():
            continue

        with open(shot_path, "r", encoding="utf-8") as f:
            shot = json.load(f)

        stats["shots_scanned"] += 1

        audio = shot.get("audio") or {}
        existing_lines = audio.get("lines") or []
        if existing_lines and not force_shots:
            stats["shots_already_populated"] += 1
            continue

        dialogue_in_shot = shot.get("dialogue_in_shot") or []
        if not dialogue_in_shot:
            continue

        characters = [
            c.get("character_id") if isinstance(c, dict) else c
            for c in (shot.get("characters_in_frame") or [])
        ]
        characters = [c for c in characters if c]

        new_lines = []
        used_rec_ids: set[str] = set()
        for dtext in dialogue_in_shot:
            rec = _find_recording_for_shot_line(dtext, characters, recordings)
            if not rec or rec["recording_id"] in used_rec_ids:
                stats["shot_lines_unmatched"] += 1
                continue
            used_rec_ids.add(rec["recording_id"])
            stats["shot_lines_matched"] += 1

            duration = rec.get("duration_sec", 0) or 0
            start_sec, end_sec = 0.0, float(duration)

            alignment = _alignment_for(rec)
            if alignment:
                slice_result = _find_slice(dtext, alignment)
                if slice_result is not None:
                    raw_start, raw_end, next_start = slice_result
                    start_sec, end_sec = _pad_slice_bounds(
                        raw_start, raw_end, next_start, float(duration),
                    )
                    stats["shot_lines_sliced"] += 1
                else:
                    stats["shot_lines_align_miss"] += 1

            new_lines.append({
                "recording_id": rec["recording_id"],
                "line_id": rec["recording_id"],
                "character_id": rec["character_id"],
                "audio_ref": rec["audio_ref"],
                "text": rec["text"],
                "direction": rec.get("direction", ""),
                "start_time_sec": round(start_sec, 3),
                "end_time_sec": round(end_sec, 3),
            })

        if not new_lines:
            continue

        shot["audio"] = {
            "lines": new_lines,
            "sound_design": audio.get("sound_design", []),
        }

        if not dry_run:
            with open(shot_path, "w", encoding="utf-8") as f:
                json.dump(shot, f, indent=2, ensure_ascii=False)
        stats["shots_updated"] += 1


def _refine_shot_slices(
    project: Project,
    chapter_id: str,
    dry_run: bool,
    stats: dict,
) -> None:
    """Non-destructive slice refinement for shots that already have
    ``audio.lines``. For each entry, look up the sidecar alignment and
    map the shot's ``dialogue_in_shot`` excerpt to word-level start/end
    times, tightening the slice so multiple shots sharing one recording
    each play only their portion.

    Leaves entries alone when:
    - the recording has no alignment sidecar
    - ``dialogue_in_shot`` is empty or lacks a matching entry
    - the alignment can't locate the excerpt as a word subsequence
    """
    shots_dir = project._path("chapters", chapter_id, "shots")
    if not shots_dir.exists():
        return

    project_root = Path(project.root).resolve()
    alignment_cache: dict[str, dict | None] = {}

    def _alignment_for_ref(audio_ref: str) -> dict | None:
        if audio_ref in alignment_cache:
            return alignment_cache[audio_ref]
        al = _load_alignment(project_root, audio_ref)
        alignment_cache[audio_ref] = al
        return al

    for shot_dir in sorted(shots_dir.iterdir()):
        if not shot_dir.is_dir():
            continue
        shot_path = shot_dir / "shot.json"
        if not shot_path.exists():
            continue

        with open(shot_path, "r", encoding="utf-8") as f:
            shot = json.load(f)

        audio = shot.get("audio") or {}
        lines = audio.get("lines") or []
        if not lines:
            continue

        dialogue_in_shot = shot.get("dialogue_in_shot") or []
        if not dialogue_in_shot:
            continue

        changed = False
        consumed_dialogue_idx: set[int] = set()
        for k, line in enumerate(lines):
            audio_ref = line.get("audio_ref") or ""
            if not audio_ref:
                continue
            alignment = _alignment_for_ref(audio_ref)
            if not alignment:
                continue

            # Prefer the dialogue_in_shot entry at the same index; fall
            # back to any unclaimed entry that locates inside this
            # recording's alignment.
            order = []
            if k < len(dialogue_in_shot) and k not in consumed_dialogue_idx:
                order.append(k)
            for j in range(len(dialogue_in_shot)):
                if j != k and j not in consumed_dialogue_idx:
                    order.append(j)

            picked = None
            for j in order:
                result = _find_slice(dialogue_in_shot[j], alignment)
                if result is not None:
                    picked = (j, result)
                    break
            if picked is None:
                stats["refine_miss"] += 1
                continue

            j, (raw_start, raw_end, next_start) = picked
            consumed_dialogue_idx.add(j)
            duration = alignment.get("duration_sec") or 0
            new_start, new_end = _pad_slice_bounds(
                raw_start, raw_end, next_start, float(duration),
            )
            new_start = round(new_start, 3)
            new_end = round(new_end, 3)

            if (line.get("start_time_sec") != new_start
                    or line.get("end_time_sec") != new_end):
                line["start_time_sec"] = new_start
                line["end_time_sec"] = new_end
                stats["refine_sliced"] += 1
                changed = True

        if changed:
            stats["refine_shots_updated"] += 1
            if not dry_run:
                with open(shot_path, "w", encoding="utf-8") as f:
                    json.dump(shot, f, indent=2, ensure_ascii=False)


def _bridge_recording_gaps(
    project: Project,
    chapter_id: str,
    dry_run: bool,
    stats: dict,
) -> None:
    """For each recording, make the shots that reference it tile the
    audio continuously — so the full spoken line is heard as the cut
    plays across shots. Without this, dialogue between aligned excerpts
    (e.g. "my good friend. Yet, it does appear..." between shots 6 and
    7) is silently skipped.

    Algorithm: for each unique ``audio_ref`` used by any shot in the
    chapter, collect the shots that reference it, sort by slice start,
    then for each consecutive pair set ``shot[N].end = shot[N+1].start``
    so there's no gap. The last shot in a group extends to the recording
    duration.

    Only touches ``end_time_sec``. Skips entries that don't have a valid
    numeric start/end pair.
    """
    shots_dir = project._path("chapters", chapter_id, "shots")
    if not shots_dir.exists():
        return

    project_root = Path(project.root).resolve()
    alignment_cache: dict[str, dict | None] = {}

    # First pass: load every shot + collect per-recording claims.
    loaded: dict[Path, dict] = {}
    claims: dict[str, list[dict]] = {}  # audio_ref -> [{shot_path, line_idx, start, end, shot}]

    for shot_dir in sorted(shots_dir.iterdir()):
        if not shot_dir.is_dir():
            continue
        shot_path = shot_dir / "shot.json"
        if not shot_path.exists():
            continue
        with open(shot_path, "r", encoding="utf-8") as f:
            shot = json.load(f)
        loaded[shot_path] = shot

        for k, line in enumerate((shot.get("audio") or {}).get("lines") or []):
            ar = line.get("audio_ref")
            s = line.get("start_time_sec")
            e = line.get("end_time_sec")
            if not ar or not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
                continue
            claims.setdefault(ar, []).append({
                "shot_path": shot_path,
                "line_idx": k,
                "start": float(s),
                "end": float(e),
            })

    # Second pass: per recording, sort and tile.
    dirty_paths: set[Path] = set()
    for audio_ref, entries in claims.items():
        entries.sort(key=lambda e: e["start"])
        # Determine recording duration from the alignment sidecar.
        if audio_ref not in alignment_cache:
            alignment_cache[audio_ref] = _load_alignment(project_root, audio_ref)
        alignment = alignment_cache[audio_ref]
        recording_duration = float(alignment.get("duration_sec") or 0.0) if alignment else 0.0

        for i, entry in enumerate(entries):
            if i + 1 < len(entries):
                # Extend to the start of the next shot's slice.
                new_end = entries[i + 1]["start"]
            else:
                # Last shot referencing this recording — run to the end.
                new_end = recording_duration if recording_duration > 0 else entry["end"]

            new_end = round(max(entry["start"], new_end), 3)
            if abs(new_end - entry["end"]) < 0.001:
                continue

            shot = loaded[entry["shot_path"]]
            line = shot["audio"]["lines"][entry["line_idx"]]
            line["end_time_sec"] = new_end
            dirty_paths.add(entry["shot_path"])
            stats["bridge_extended"] += 1

    if not dry_run:
        for p in dirty_paths:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(loaded[p], f, indent=2, ensure_ascii=False)
    stats["bridge_shots_updated"] = len(dirty_paths)


def _set_dialogue_shot_durations(
    project: Project,
    chapter_id: str,
    dry_run: bool,
    stats: dict,
) -> None:
    """Set ``shot.duration_sec`` to the total slice length for shots whose
    audio.lines have real start/end times. Without this, the Editing Room
    falls back to the 3s default for every dialogue shot, making the cut
    timeline wrong.

    Only writes when:
    - at least one audio.lines entry has both start_time_sec and
      end_time_sec set (alignment succeeded)
    - the resulting slice length is > 0
    """
    shots_dir = project._path("chapters", chapter_id, "shots")
    if not shots_dir.exists():
        return

    for shot_dir in sorted(shots_dir.iterdir()):
        if not shot_dir.is_dir():
            continue
        shot_path = shot_dir / "shot.json"
        if not shot_path.exists():
            continue

        with open(shot_path, "r", encoding="utf-8") as f:
            shot = json.load(f)

        lines = (shot.get("audio") or {}).get("lines") or []
        slice_total = 0.0
        for line in lines:
            start = line.get("start_time_sec")
            end = line.get("end_time_sec")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
                slice_total += end - start
        if slice_total <= 0:
            continue

        # Round to 0.1s — the editing-room duration slider renders at that
        # granularity anyway.
        new_duration = round(slice_total, 1)
        if shot.get("duration_sec") == new_duration:
            continue

        shot["duration_sec"] = new_duration
        stats["shot_durations_set"] += 1

        if not dry_run:
            with open(shot_path, "w", encoding="utf-8") as f:
                json.dump(shot, f, indent=2, ensure_ascii=False)


def migrate_chapter(project: Project, chapter_id: str, dry_run: bool,
                    force: bool, force_shots: bool) -> dict:
    """Migrate one chapter. Returns a stats dict for reporting."""
    stats = {
        "chapter_id": chapter_id,
        "status": "ok",
        "screenplay_lines": 0,
        "old_recordings": 0,
        "matched_by_hash": 0,
        "matched_by_prefix": 0,
        "unmatched_lines": 0,
        "orphan_recordings": 0,
        "wrote_file": False,
        "loaded_existing": False,
        "shots_scanned": 0,
        "shots_updated": 0,
        "shots_already_populated": 0,
        "shot_lines_matched": 0,
        "shot_lines_unmatched": 0,
        "shot_lines_sliced": 0,
        "shot_lines_align_miss": 0,
        "refine_shots_updated": 0,
        "refine_sliced": 0,
        "refine_miss": 0,
        "shot_durations_set": 0,
        "bridge_extended": 0,
        "bridge_shots_updated": 0,
    }

    chapter_audio = Path(project.root) / "audio" / chapter_id
    recordings_path = chapter_audio / "recordings.json"

    if not chapter_audio.exists():
        stats["status"] = "skip_no_audio_dir"
        return stats

    # Phase 1: recordings.json.
    recordings: list[dict] | None = None
    if recordings_path.exists() and not force:
        # Already built — load it so we can still backfill shots.
        try:
            existing = project.load_recordings(chapter_id)
            recordings = existing.get("recordings", [])
            stats["loaded_existing"] = True
        except (FileNotFoundError, json.JSONDecodeError):
            recordings = None
    if recordings is None:
        recordings = _build_recordings(project, chapter_id, stats)
        if recordings is None:
            return stats
        if not dry_run:
            project.save_recordings(chapter_id, {
                "chapter_id": chapter_id,
                "recordings": recordings,
            })
            stats["wrote_file"] = True

    # Phase 2: shot backfill — always run when recordings are available.
    _backfill_shots(project, chapter_id, recordings, dry_run, force_shots, stats)

    # Phase 3: non-destructive slice refinement using alignment sidecars.
    _refine_shot_slices(project, chapter_id, dry_run, stats)

    # Phase 4: bridge gaps — shots that reference the same recording
    # tile it continuously, so dialogue between aligned excerpts
    # ("my good friend. Yet, ...") isn't skipped during the cut.
    _bridge_recording_gaps(project, chapter_id, dry_run, stats)

    # Phase 5: set shot.duration_sec to total slice length so dialogue
    # shots don't all fall back to the 3s default in the Editing Room.
    _set_dialogue_shot_durations(project, chapter_id, dry_run, stats)

    return stats


def _print_stats(stats: dict) -> None:
    cid = stats["chapter_id"]
    status = stats["status"]
    if status.startswith("skip"):
        print(f"  {cid}: SKIP ({status})")
        return

    if stats["loaded_existing"]:
        rec_info = "recordings.json=existing"
    elif stats["wrote_file"]:
        rec_info = (
            f"recordings.json=WROTE "
            f"(lines={stats['screenplay_lines']} hash={stats['matched_by_hash']} "
            f"prefix={stats['matched_by_prefix']} unmatched={stats['unmatched_lines']} "
            f"orphans={stats['orphan_recordings']})"
        )
    else:
        rec_info = (
            f"recordings.json=dry-run "
            f"(lines={stats['screenplay_lines']} hash={stats['matched_by_hash']} "
            f"prefix={stats['matched_by_prefix']} unmatched={stats['unmatched_lines']})"
        )

    shot_info = (
        f"shots scanned={stats['shots_scanned']} "
        f"updated={stats['shots_updated']} "
        f"already={stats['shots_already_populated']} "
        f"matched={stats['shot_lines_matched']} "
        f"unmatched={stats['shot_lines_unmatched']} "
        f"sliced={stats['shot_lines_sliced']} "
        f"align_miss={stats['shot_lines_align_miss']} "
        f"| refine: shots={stats['refine_shots_updated']} "
        f"lines={stats['refine_sliced']} miss={stats['refine_miss']} "
        f"| bridge: shots={stats['bridge_shots_updated']} extended={stats['bridge_extended']} "
        f"| durations_set={stats['shot_durations_set']}"
    )
    print(f"  {cid}: {rec_info} | {shot_info}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, help="Path to project repo root")
    ap.add_argument("--chapter", help="Only migrate this chapter (e.g. ch01)")
    ap.add_argument("--dry-run", action="store_true", help="Report only; do not write")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing recordings.json")
    ap.add_argument("--force-shots", action="store_true",
                    help="Re-populate shot.audio.lines even when already set")
    args = ap.parse_args()

    project_root = Path(args.project).resolve()
    if not project_root.exists():
        print(f"ERROR: project path not found: {project_root}")
        return 2

    project = Project(str(project_root))

    if args.chapter:
        chapter_ids = [args.chapter]
    else:
        chapters_dir = project_root / "chapters"
        chapter_ids = sorted(
            p.name for p in chapters_dir.iterdir()
            if p.is_dir() and (p / "screenplay.md").exists()
        )

    mode = "DRY RUN" if args.dry_run else "WRITE"
    print(f"Migration ({mode}) — project={project_root.name} chapters={chapter_ids}")

    results = []
    for cid in chapter_ids:
        stats = migrate_chapter(
            project, cid,
            dry_run=args.dry_run,
            force=args.force,
            force_shots=args.force_shots,
        )
        _print_stats(stats)
        results.append(stats)

    wrote = sum(1 for r in results if r["wrote_file"])
    shot_updates = sum(r["shots_updated"] for r in results)
    print(
        f"\nDone. recordings.json written for {wrote}/{len(results)} chapter(s); "
        f"shot.audio.lines updated in {shot_updates} shot(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
