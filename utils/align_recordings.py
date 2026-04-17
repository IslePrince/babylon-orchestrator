"""Forced-alignment for recordings.json audio, using whisperx.

For each recording in a chapter's ``audio/{ch}/recordings.json``, aligns the
known dialogue text to the mp3 using whisperx's wav2vec2 aligner. Writes a
sidecar ``{mp3_path}.alignment.json`` containing word-level timestamps:

    {
      "recording_id": "ch01_line001",
      "audio_ref": "audio/ch01/...mp3",
      "duration_sec": 33.6,
      "words": [
        {"word": "May", "start": 0.12, "end": 0.41},
        ...
      ]
    }

We skip Whisper's transcription pass because the text is already known —
this is pure forced alignment, which is both faster and handles archaic
language (thee, thou, needest) better than relying on Whisper's guess.

Usage:
    py -3.12 utils/align_recordings.py --project <path> [--chapter ch01] [--force]

Run on the machine with CUDA (RTX). Requires:
    py -3.12 -m pip install whisperx
    py -3.12 -m pip install --index-url https://download.pytorch.org/whl/cu126 torch torchaudio
"""

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ORCH_ROOT = SCRIPT_DIR.parent
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

from core.project import Project  # noqa: E402

# Imports below are heavy — import lazily so --help is instant.


def _load_whisperx(device: str):
    import whisperx  # noqa: F401
    return whisperx


def _probe_audio_duration(audio_path: Path) -> float:
    """Return duration in seconds using torchaudio."""
    import torchaudio
    info = torchaudio.info(str(audio_path))
    return info.num_frames / float(info.sample_rate)


def _alignment_path(mp3_path: Path) -> Path:
    return mp3_path.with_suffix(mp3_path.suffix + ".alignment.json")


def align_chapter(project: Project, chapter_id: str, align_model, metadata,
                  whisperx_mod, device: str, force: bool) -> dict:
    stats = {
        "chapter_id": chapter_id,
        "status": "ok",
        "total": 0,
        "aligned": 0,
        "skipped_existing": 0,
        "skipped_missing_mp3": 0,
        "failed": 0,
        "wall_time_sec": 0.0,
    }

    try:
        recs = project.load_recordings(chapter_id)
    except FileNotFoundError:
        stats["status"] = "skip_no_recordings_json"
        return stats

    recordings = recs.get("recordings", [])
    stats["total"] = len(recordings)
    if not recordings:
        stats["status"] = "skip_empty"
        return stats

    t_chapter_start = time.time()
    project_root = Path(project.root).resolve()

    for rec in recordings:
        mp3_rel = rec.get("audio_ref")
        if not mp3_rel:
            stats["skipped_missing_mp3"] += 1
            continue
        mp3_path = (project_root / mp3_rel).resolve()
        if not mp3_path.exists():
            stats["skipped_missing_mp3"] += 1
            continue

        align_path = _alignment_path(mp3_path)
        if align_path.exists() and not force:
            stats["skipped_existing"] += 1
            continue

        text = (rec.get("text") or "").strip()
        if not text:
            stats["failed"] += 1
            continue

        try:
            t0 = time.time()
            audio = whisperx_mod.load_audio(str(mp3_path))
            duration = len(audio) / 16000.0  # whisperx loads at 16kHz mono
            segments = [{"start": 0.0, "end": duration, "text": text}]
            result = whisperx_mod.align(
                segments, align_model, metadata, audio, device,
                return_char_alignments=False,
            )
            words = []
            for w in result.get("word_segments", []):
                start = w.get("start")
                end = w.get("end")
                if start is None or end is None:
                    continue
                words.append({
                    "word": w.get("word", ""),
                    "start": round(float(start), 3),
                    "end": round(float(end), 3),
                })

            with open(align_path, "w", encoding="utf-8") as f:
                json.dump({
                    "recording_id": rec.get("recording_id"),
                    "audio_ref": mp3_rel,
                    "duration_sec": round(duration, 3),
                    "text": text,
                    "words": words,
                    "aligned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }, f, indent=2, ensure_ascii=False)

            stats["aligned"] += 1
            elapsed = time.time() - t0
            print(
                f"    {rec.get('recording_id'):<16} "
                f"dur={duration:5.1f}s words={len(words):3d} "
                f"took={elapsed:4.1f}s"
            )
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            print(f"    {rec.get('recording_id'):<16} FAILED: {e}")

    stats["wall_time_sec"] = round(time.time() - t_chapter_start, 1)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, help="Path to project repo root")
    ap.add_argument("--chapter", help="Only align this chapter (e.g. ch01)")
    ap.add_argument("--force", action="store_true",
                    help="Re-align even if sidecar alignment.json exists")
    ap.add_argument("--language", default="en")
    args = ap.parse_args()

    project_root = Path(args.project).resolve()
    if not project_root.exists():
        print(f"ERROR: project path not found: {project_root}")
        return 2
    project = Project(str(project_root))

    whisperx_mod = _load_whisperx("cuda")
    import torch
    if not torch.cuda.is_available():
        print("WARN: CUDA not available — falling back to CPU (slow).")
        device = "cpu"
    else:
        device = "cuda"
        print(f"Using device: {device} ({torch.cuda.get_device_name(0)})")

    print(f"Loading alignment model for language={args.language}...")
    t_load = time.time()
    align_model, metadata = whisperx_mod.load_align_model(
        language_code=args.language, device=device,
    )
    print(f"  loaded in {time.time()-t_load:.1f}s")

    if args.chapter:
        chapter_ids = [args.chapter]
    else:
        chapters_dir = project_root / "chapters"
        chapter_ids = sorted(
            p.name for p in chapters_dir.iterdir()
            if p.is_dir() and (project_root / "audio" / p.name / "recordings.json").exists()
        )

    print(f"Chapters: {chapter_ids}")
    all_stats = []
    for cid in chapter_ids:
        print(f"\n  [{cid}]")
        stats = align_chapter(
            project, cid, align_model, metadata, whisperx_mod, device, args.force,
        )
        all_stats.append(stats)
        print(
            f"    -> total={stats['total']} aligned={stats['aligned']} "
            f"skipped_existing={stats['skipped_existing']} "
            f"failed={stats['failed']} "
            f"wall={stats['wall_time_sec']}s"
        )

    total_aligned = sum(s["aligned"] for s in all_stats)
    total_wall = sum(s["wall_time_sec"] for s in all_stats)
    print(f"\nDone. aligned={total_aligned} total_wall={total_wall:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
