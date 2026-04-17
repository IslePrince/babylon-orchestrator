"""Detect audio-recording ranges that no shot references and
reconstruct the missing shot so the timeline tiles the recording
again.

This exists because an early version of ``split_long_shots.py``
had a naming-collision bug: when it re-split an already-split shot
it would overwrite the existing ``_b`` sibling, orphaning whichever
range that sibling covered. This recovers the orphaned range by
recreating the shot with the next unused letter suffix (``_c`` and
onward), inheriting all other fields from its family members.

Usage:
    python3 utils/recover_lost_shot_slices.py --project <path> --chapter ch01
    python3 utils/recover_lost_shot_slices.py --project <path> --chapter ch01 --dry-run
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


TOLERANCE_SEC = 0.15  # gaps smaller than this aren't worth a new shot


def _stem(shot_id: str) -> str:
    """Strip the trailing alpha suffix: ``sh024b`` -> ``sh024``."""
    s = shot_id
    while s and s[-1].isalpha() and not s[-1].isdigit():
        s = s[:-1]
    return s or shot_id


def _next_suffix(stem: str, taken: set[str]) -> str:
    for ch in "bcdefghijklmnopqrstuvwxyz":
        cand = stem + ch
        if cand not in taken:
            return cand
    i = 2
    while True:
        cand = f"{stem}_{i}"
        if cand not in taken:
            return cand
        i += 1


def _ffprobe_duration(path: Path) -> Optional[float]:
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out or "{}")
        return float(data.get("format", {}).get("duration", 0.0)) or None
    except Exception:  # noqa: BLE001
        return None


def recover_chapter(project: Project, chapter_id: str,
                   dry_run: bool) -> dict:
    project_root = Path(project.root).resolve()
    shots_dir = project._path("chapters", chapter_id, "shots")
    audio_dir = project._path("audio", chapter_id)

    # Collect every shot that references a recording, grouped by
    # audio_ref, with their claim ranges.
    shots_by_dir: dict[Path, dict] = {}
    claims: dict[str, list[tuple[str, float, float]]] = {}
    families: dict[str, list[str]] = {}  # stem -> [shot_ids]
    for sd in sorted(shots_dir.iterdir()):
        if not sd.is_dir():
            continue
        sp = sd / "shot.json"
        if not sp.exists():
            continue
        with open(sp, "r", encoding="utf-8") as f:
            shot = json.load(f)
        shots_by_dir[sd] = shot
        families.setdefault(_stem(shot["shot_id"]), []).append(shot["shot_id"])
        for line in (shot.get("audio") or {}).get("lines") or []:
            ar = line.get("audio_ref") or ""
            s = line.get("start_time_sec")
            e = line.get("end_time_sec")
            if not ar or not isinstance(s, (int, float)) or not isinstance(e, (int, float)):
                continue
            claims.setdefault(ar, []).append((shot["shot_id"], float(s), float(e)))

    # Try to load a whisperx alignment sidecar to learn a recording's
    # duration exactly; fall back to ffprobe, then the recordings.json
    # duration_sec field.
    try:
        recordings = project.load_recordings(chapter_id).get("recordings", [])
    except FileNotFoundError:
        recordings = []
    rec_by_audio_ref: dict[str, dict] = {r.get("audio_ref"): r for r in recordings}

    all_existing_ids = {sd.name for sd in shots_dir.iterdir() if sd.is_dir()}
    gaps_found: list[dict] = []
    reconstructed: list[dict] = []

    for audio_ref, windows in claims.items():
        mp3 = (project_root / audio_ref).resolve()
        if not mp3.exists():
            continue

        # Determine the recording's true end.
        duration: Optional[float] = None
        align_path = mp3.with_suffix(mp3.suffix + ".alignment.json")
        if align_path.exists():
            with open(align_path, "r", encoding="utf-8") as f:
                duration = float(json.load(f).get("duration_sec") or 0) or None
        if not duration:
            duration = _ffprobe_duration(mp3)
        if not duration and audio_ref in rec_by_audio_ref:
            # duration_sec on recordings.json is an ElevenLabs estimate
            # so it's last-resort only.
            duration = float(rec_by_audio_ref[audio_ref].get("duration_sec") or 0) or None
        if not duration:
            continue

        # Merge overlapping claim windows, then find gaps.
        windows.sort(key=lambda w: w[1])
        merged: list[tuple[str, float, float]] = []
        for sid, s, e in windows:
            if merged and s <= merged[-1][2] + TOLERANCE_SEC:
                last_sid, last_s, last_e = merged[-1]
                merged[-1] = (last_sid, last_s, max(last_e, e))
            else:
                merged.append((sid, s, e))

        # Everything from 0 → first claim start and between adjacent
        # claims and last claim end → duration counts as a gap. But
        # if nothing claims the head, that's a real oversight we'd
        # rather not paper over — only recover INTERIOR gaps and the
        # tail.
        gaps: list[tuple[float, float, Optional[str]]] = []
        for i, (sid, s, e) in enumerate(merged[:-1]):
            ns, *_ = merged[i + 1][1:]
            gap = merged[i + 1][1] - e
            if gap > TOLERANCE_SEC:
                # Pick the "before" shot so we can slot the new shot
                # into the index right after it.
                gaps.append((e, merged[i + 1][1], sid))
        # Tail gap
        tail_sid, tail_s, tail_e = merged[-1]
        if duration - tail_e > TOLERANCE_SEC:
            gaps.append((tail_e, duration, tail_sid))

        for start, end, after_sid in gaps:
            gaps_found.append({
                "audio_ref": audio_ref, "start": round(start, 3),
                "end": round(end, 3), "duration": round(end - start, 3),
                "after_shot": after_sid,
            })

            # Use the "after" shot as the template so we inherit
            # scene_id, cinematic, characters, storyboard assets.
            template_dir = shots_dir / after_sid
            template_shot_path = template_dir / "shot.json"
            if not template_shot_path.exists():
                continue
            with open(template_shot_path, "r", encoding="utf-8") as f:
                template_shot = json.load(f)

            stem = _stem(after_sid)
            new_id = _next_suffix(stem, all_existing_ids)
            all_existing_ids.add(new_id)
            new_dir = shots_dir / new_id

            new_shot = json.loads(json.dumps(template_shot))
            new_shot["shot_id"] = new_id
            new_shot["label"] = (template_shot.get("label", "").strip()
                                 + " (recovered)").strip()
            new_shot["dialogue_in_shot"] = []
            new_shot["duration_sec"] = round(end - start, 1)
            new_shot.pop("preview", None)

            # Reshape audio.lines — inherit the line's recording_id
            # / text / audio_ref but retake start/end for the gap.
            template_lines = (template_shot.get("audio") or {}).get("lines") or []
            if template_lines:
                src_line = dict(template_lines[0])
                src_line["start_time_sec"] = round(start, 3)
                src_line["end_time_sec"] = round(end, 3)
                src_line["audio_ref"] = audio_ref
                new_shot["audio"] = {"lines": [src_line],
                                     "sound_effects": []}
            else:
                new_shot["audio"] = {"lines": [{
                    "audio_ref": audio_ref,
                    "start_time_sec": round(start, 3),
                    "end_time_sec": round(end, 3),
                }], "sound_effects": []}

            reconstructed.append({
                "audio_ref": audio_ref, "new_id": new_id,
                "start": round(start, 3), "end": round(end, 3),
                "after": after_sid,
            })

            if dry_run:
                continue

            new_dir.mkdir(parents=True, exist_ok=True)
            for asset in ("storyboard.png", "storyboard_vertical.png"):
                src = template_dir / asset
                if src.exists() and not (new_dir / asset).exists():
                    shutil.copy2(src, new_dir / asset)
            with open(new_dir / "shot.json", "w", encoding="utf-8") as f:
                json.dump(new_shot, f, indent=2, ensure_ascii=False)

    # Update the chapter's shot index so the new shots are visible in
    # the UI / list_shots. Just rebuild it from disk order so the new
    # shots sit next to their template (their names sort alongside).
    if reconstructed and not dry_run:
        index_path = project._path(
            "chapters", chapter_id, "shots", "_index.json"
        )
        index = {"shots": []}
        if index_path.exists():
            with open(index_path, "r", encoding="utf-8") as f:
                index = json.load(f)
        idx_shots = index.setdefault("shots", [])
        known = {e.get("shot_id") for e in idx_shots}
        for r in reconstructed:
            if r["new_id"] in known:
                continue
            entry = {
                "shot_id": r["new_id"],
                "label": "(recovered)",
                "shot_type": "",
                "duration_sec": round(r["end"] - r["start"], 1),
                "status": "generated",
                "storyboard_approved": False,
                "audio_approved": False,
                "built": False,
                "preview_rendered": False,
                "final_rendered": False,
                "world_version": None,
            }
            # Insert after the template.
            inserted = False
            for i, e in enumerate(idx_shots):
                if e.get("shot_id") == r["after"]:
                    idx_shots.insert(i + 1, entry)
                    inserted = True
                    break
            if not inserted:
                idx_shots.append(entry)
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)

    return {
        "chapter_id": chapter_id,
        "recordings_with_claims": len(claims),
        "gaps_found": len(gaps_found),
        "gaps": gaps_found,
        "reconstructed": reconstructed,
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    project = Project(args.project)
    result = recover_chapter(project, args.chapter, dry_run=args.dry_run)
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("gaps", "reconstructed")}, indent=2))
    for r in result["reconstructed"]:
        print(f"  + {r['new_id']} after {r['after']}: "
              f"{r['start']}-{r['end']}s of {Path(r['audio_ref']).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
