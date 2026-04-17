"""Split any shot whose dialogue slice exceeds a duration cap into
two consecutive shots, so Wan 2.1 / InfiniTalk doesn't wedge trying
to generate a ~20s clip in one shot.

Why this exists: Wan+InfiniTalk quality degrades past ~10s, and
the sampler occasionally hangs on very long clips (we've had 23s
shots running for 300+ minutes). Splitting early keeps every render
within the model's comfort window and lets the editing-room timeline
stay honest — the two halves play back-to-back at the same slices
they already own, just as two shots instead of one.

Mechanism:
  1. For each shot with total audio slice > ``--max-seconds``, look
     up the matching whisperx alignment sidecar.
  2. Pick the word-boundary closest to the midpoint that has the
     widest silence gap to the next word.
  3. Split at that gap's midpoint (natural cut, no clipped syllables).
  4. Shrink the original shot's ``audio.lines[*].end_time_sec`` to
     the split time.
  5. Create a new shot ``<shot_id>b`` that:
       - copies storyboard.png and storyboard_vertical.png (same
         image — Wan animates the still twice, once for each half)
       - inherits cinematic / characters / label + "(cont'd)"
       - takes ``start_time_sec = split, end_time_sec = original.end``
       - recomputes its own ``duration_sec``
  6. Inserts ``<shot_id>b`` into the scene's shots _index.json
     immediately after the original.
  7. Clears any stale ``preview.mp4`` / ``preview_vertical.mp4``
     from the original — the shorter slice needs re-rendering.

Usage:
    python3 utils/split_long_shots.py --project <path> --chapter ch01
    python3 utils/split_long_shots.py --project <path> --chapter ch01 --max-seconds 10 --dry-run
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
ORCH_ROOT = SCRIPT_DIR.parent
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

from core.project import Project  # noqa: E402


DEFAULT_MAX_SEC = 10.0


def _find_split_point(words: list[dict], target: float,
                      slice_start: float, slice_end: float) -> Optional[float]:
    """Choose a word-boundary inside [slice_start, slice_end] closest
    to ``target`` while preferring gaps with the most silence after
    the word. Returns the chosen split time (seconds within the
    recording), or None if there are no usable word boundaries."""
    in_slice = [w for w in words
                if w.get("start") is not None and w.get("end") is not None
                and float(w["start"]) >= slice_start
                and float(w["end"]) <= slice_end]
    if len(in_slice) < 2:
        return None

    best = None
    best_score = float("inf")
    for i in range(len(in_slice) - 1):
        w, nxt = in_slice[i], in_slice[i + 1]
        gap = float(nxt["start"]) - float(w["end"])
        # Skip obviously-glued words
        if gap < 0.01:
            continue
        # Score: distance from target penalised, gap rewarded.
        # The ratio is tuned so ~200ms of extra silence can pull the
        # split across about a second of target-distance slack.
        score = abs(float(w["end"]) - target) - gap * 3.0
        if score < best_score:
            best_score = score
            best = (w, nxt, gap)

    if best is None:
        return None
    w, _nxt, gap = best
    return float(w["end"]) + gap / 2.0


def _load_alignment(project_root: Path, audio_ref: str) -> Optional[dict]:
    mp3 = (project_root / audio_ref).resolve()
    side = mp3.with_suffix(mp3.suffix + ".alignment.json")
    if not side.exists():
        return None
    with open(side, "r", encoding="utf-8") as f:
        return json.load(f)


def _split_text_by_time(dialogue_text: str, words: list[dict],
                        split_time: float) -> tuple[str, str]:
    """Split the ``dialogue_in_shot`` string into head/tail at the
    word count that corresponds to ``split_time`` in the alignment.
    The split always lands on a whitespace boundary — we never cut
    mid-word, even when the original text has slightly different
    punctuation / contractions than what the aligner produced.

    Falls back to a space-snapped char-ratio split when alignment data
    is thin.
    """
    if not dialogue_text:
        return "", ""

    # Count how many aligned words end before ``split_time``.
    split_idx = 0
    for i, w in enumerate(words or []):
        start = float(w.get("start") or 0)
        if start >= split_time:
            split_idx = i
            break
    if split_idx == 0 and words:
        split_idx = len(words) // 2

    original_tokens = dialogue_text.split()
    if split_idx <= 0 or split_idx >= len(original_tokens):
        # Ratio fallback — snap to the nearest whitespace.
        if not original_tokens:
            return dialogue_text, ""
        ratio = (split_idx / max(1, len(words or []))) or 0.5
        cut_idx = max(1, min(len(original_tokens) - 1,
                             int(round(ratio * len(original_tokens)))))
        head = " ".join(original_tokens[:cut_idx])
        tail = " ".join(original_tokens[cut_idx:])
        return head, tail

    head = " ".join(original_tokens[:split_idx])
    tail = " ".join(original_tokens[split_idx:])
    return head, tail


def _new_shot_id(original: str, existing_ids: set[str]) -> str:
    """Pick the next unused single-letter suffix after ``original``.

    ``sh024`` -> ``sh024b``; if that's taken, ``sh024c``; and so on.
    Prevents the collision where splitting an already-split shot
    would overwrite its sibling on disk.
    """
    base = original
    # If caller passes an already-suffixed id (``sh024b``), strip it
    # back to the numeric base so a re-split keeps the lineage
    # readable.
    stem = base
    while stem and stem[-1].isalpha() and not stem[-1].isdigit():
        stem = stem[:-1]
    if not stem:
        stem = base  # all-alpha, leave alone

    for ch in "bcdefghijklmnopqrstuvwxyz":
        cand = stem + ch
        if cand not in existing_ids and cand != original:
            return cand
    # Fallback: numeric suffix if we blew past 'z' (26 re-splits).
    i = 2
    while True:
        cand = f"{stem}_{i}"
        if cand not in existing_ids:
            return cand
        i += 1


def _insert_after(index_shots: list[dict], after_shot_id: str,
                  new_entry: dict) -> None:
    """Mutate ``index_shots`` so ``new_entry`` sits right after the
    ``after_shot_id`` entry. If ``after_shot_id`` isn't present, the
    new entry is appended."""
    for i, entry in enumerate(index_shots):
        if entry.get("shot_id") == after_shot_id:
            index_shots.insert(i + 1, new_entry)
            return
    index_shots.append(new_entry)


def split_chapter(project: Project, chapter_id: str,
                  max_seconds: float, dry_run: bool) -> dict:
    project_root = Path(project.root).resolve()
    shots_dir = project._path("chapters", chapter_id, "shots")
    if not shots_dir.exists():
        return {"chapter_id": chapter_id, "status": "no_shots_dir"}

    stats = {
        "chapter_id": chapter_id,
        "max_seconds": max_seconds,
        "scanned": 0,
        "candidates": 0,
        "split": 0,
        "skipped_no_alignment": 0,
        "skipped_no_split_point": 0,
        "splits": [],
    }

    # Group shots by scene_id so we can update each scene's _index.json.
    scene_shots: dict[str, list[Path]] = {}
    for sd in sorted(shots_dir.iterdir()):
        if not sd.is_dir():
            continue
        shot_json = sd / "shot.json"
        if not shot_json.exists():
            continue
        stats["scanned"] += 1
        with open(shot_json, "r", encoding="utf-8") as f:
            shot = json.load(f)
        scene_id = shot.get("scene_id") or "_".join(sd.name.split("_")[:2])
        scene_shots.setdefault(scene_id, []).append(sd)

    for scene_id, dirs in scene_shots.items():
        index_path = project._path(
            "chapters", chapter_id, "shots", "_index.json"
        )
        # The index is per-chapter in this project shape, but some scenes
        # carry their own. Tolerate both.
        index: dict = {"shots": []}
        scene_index_path = project._path(
            "chapters", chapter_id, "shots", f"_{scene_id}_index.json"
        )
        if scene_index_path.exists():
            with open(scene_index_path, "r", encoding="utf-8") as f:
                index = json.load(f)
            index_path = scene_index_path
        elif index_path.exists():
            with open(index_path, "r", encoding="utf-8") as f:
                index = json.load(f)

        for sd in dirs:
            shot_json = sd / "shot.json"
            with open(shot_json, "r", encoding="utf-8") as f:
                shot = json.load(f)
            lines = (shot.get("audio") or {}).get("lines") or []
            if not lines:
                continue
            total = sum(
                float(l["end_time_sec"]) - float(l["start_time_sec"])
                for l in lines
                if isinstance(l.get("start_time_sec"), (int, float))
                and isinstance(l.get("end_time_sec"), (int, float))
            )
            if total <= max_seconds:
                continue

            stats["candidates"] += 1

            # Only split shots that reference a single recording in a
            # single slice — mixed multi-line shots are too risky to
            # mechanically split. Those can be handled by hand.
            if len(lines) != 1:
                continue

            line = lines[0]
            audio_ref = line.get("audio_ref") or ""
            alignment = _load_alignment(project_root, audio_ref)
            if not alignment:
                stats["skipped_no_alignment"] += 1
                continue

            start = float(line["start_time_sec"])
            end = float(line["end_time_sec"])
            midpoint = (start + end) / 2.0
            split_at = _find_split_point(
                alignment.get("words") or [], midpoint, start, end,
            )
            if split_at is None or not (start < split_at < end):
                stats["skipped_no_split_point"] += 1
                continue

            # Build the new shot.
            original_id = shot["shot_id"]
            existing_ids = {p.name for p in shots_dir.iterdir() if p.is_dir()}
            new_id = _new_shot_id(original_id, existing_ids)
            new_dir = shots_dir / new_id

            original_dialogue = (shot.get("dialogue_in_shot") or [""])[0]
            head_text, tail_text = _split_text_by_time(
                original_dialogue, alignment.get("words") or [], split_at,
            )

            new_shot = json.loads(json.dumps(shot))  # deep copy
            new_shot["shot_id"] = new_id
            new_shot["shot_number"] = shot.get("shot_number", 0)  # display only
            new_shot["label"] = (shot.get("label", "").strip()
                                 + " (cont'd)").strip()
            # Carry over cinematic + characters untouched so the second
            # half visually matches the first. Wan will animate the same
            # still; the user can regenerate the storyboard later if the
            # second half should look different.
            new_shot.setdefault("storyboard", {})["generated"] = (
                shot.get("storyboard", {}).get("generated", False)
            )
            new_shot["dialogue_in_shot"] = [tail_text] if tail_text else []

            new_audio = new_shot.setdefault("audio", {})
            new_audio["lines"] = [
                {
                    **line,
                    "start_time_sec": round(split_at, 3),
                    "end_time_sec": round(end, 3),
                    "text": line.get("text", ""),
                }
            ]
            new_audio["sound_effects"] = []
            new_shot["duration_sec"] = round(end - split_at, 1)
            new_shot.pop("preview", None)

            # Shrink the original.
            shot["audio"]["lines"] = [
                {
                    **line,
                    "end_time_sec": round(split_at, 3),
                }
            ]
            shot["dialogue_in_shot"] = [head_text] if head_text else \
                shot.get("dialogue_in_shot", [])
            shot["duration_sec"] = round(split_at - start, 1)
            # Any preview.mp4 is now stale (wrong duration).
            shot.pop("preview", None)

            stats["splits"].append({
                "original": original_id,
                "new": new_id,
                "split_at_sec": round(split_at, 3),
                "original_new_duration": shot["duration_sec"],
                "new_shot_duration": new_shot["duration_sec"],
                "head_text": head_text[:80],
                "tail_text": tail_text[:80],
            })

            if dry_run:
                continue

            # Write new shot dir, copy storyboards, shrink originals on
            # disk, update the scene index.
            new_dir.mkdir(parents=True, exist_ok=True)
            for asset in ("storyboard.png", "storyboard_vertical.png"):
                src = sd / asset
                if src.exists():
                    shutil.copy2(src, new_dir / asset)

            # Remove stale preview mp4s from the original (slice changed).
            for stale in ("preview.mp4", "preview_vertical.mp4",
                          "preview_raw.mp4", "preview_vertical_raw.mp4"):
                p = sd / stale
                if p.exists():
                    p.unlink()

            with open(new_dir / "shot.json", "w", encoding="utf-8") as f:
                json.dump(new_shot, f, indent=2, ensure_ascii=False)
            with open(shot_json, "w", encoding="utf-8") as f:
                json.dump(shot, f, indent=2, ensure_ascii=False)

            # Update the scene index so list_shots and the Editing
            # Room pick the new shot up.
            idx_entries = index.setdefault("shots", [])
            new_entry = {
                "shot_id": new_id,
                "label": new_shot["label"],
                "shot_type": (new_shot.get("cinematic", {})
                              .get("shot_type", "")),
                "duration_sec": new_shot["duration_sec"],
                "status": "generated",
                "storyboard_approved": False,
                "audio_approved": False,
                "built": False,
                "preview_rendered": False,
                "final_rendered": False,
                "world_version": None,
            }
            _insert_after(idx_entries, original_id, new_entry)

            # Update the original's duration in the index too.
            for entry in idx_entries:
                if entry.get("shot_id") == original_id:
                    entry["duration_sec"] = shot["duration_sec"]
                    entry["preview_rendered"] = False
                    break

            stats["split"] += 1

        if not dry_run:
            index_path.parent.mkdir(parents=True, exist_ok=True)
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index, f, indent=2, ensure_ascii=False)

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SEC,
                    help=f"Shots with total slice > this are split "
                         f"(default {DEFAULT_MAX_SEC}s).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    project = Project(args.project)
    stats = split_chapter(
        project, args.chapter,
        max_seconds=args.max_seconds, dry_run=args.dry_run,
    )
    print(json.dumps({k: v for k, v in stats.items() if k != "splits"},
                     indent=2))
    for s in stats["splits"]:
        print(f"  {s['original']} ({s['original_new_duration']}s) "
              f"+ {s['new']} ({s['new_shot_duration']}s) "
              f"@ t={s['split_at_sec']}s")
        print(f"      head: {s['head_text']!r}")
        print(f"      tail: {s['tail_text']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
