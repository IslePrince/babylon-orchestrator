"""Post-mix a shot's SFX into its preview.mp4 using ffmpeg.

Wan InfiniTalk only bakes the dialogue track into the rendered mp4 and
Wan I2V produces a silent mp4 at all. This tool overlays the shot's
``audio.sound_effects`` entries on top — each at its ``offset_sec``
with a volume derived from the entry's ``gain`` or a keyword-based
tier (matches the JS ``_sfxVolumeFor`` heuristic in the Editing Room
so what you hear during Play Cut matches the exported clip).

The very first mix for a given preview is preserved as
``preview_raw.mp4`` (or ``preview_vertical_raw.mp4``) so re-mixing
N times never accumulates SFX passes — we always mix from the clean
Wan output.

Usage:
    python3 utils/mix_preview_audio.py --project <path> --shot <shot_id>
    python3 utils/mix_preview_audio.py --project <path> --shot <shot_id> --orientation vertical
    python3 utils/mix_preview_audio.py --project <path> --chapter ch01
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ORCH_ROOT = SCRIPT_DIR.parent
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

from core.project import Project  # noqa: E402


AMBIENT_KEYWORDS = (
    "ambient", "ambience", "background", "room tone", "distant",
    "far ", "far-off", "crowd", "hubbub", "wind", "breeze",
    "bustle", "murmur", "chatter", "rustle", "rustling",
    "drone", "atmosphere",
)
SHARP_KEYWORDS = (
    "slam", "clang", "crash", "scream", "shout", "bang",
    "gunshot", "impact",
)


def resolve_sfx_volume(sfx: dict) -> float:
    """Mirror of editing_room.js `_sfxVolumeFor` so the Editing Room's
    live-layered preview and the baked mp4 audio match volume-tier for
    volume-tier."""
    gain = sfx.get("gain")
    if isinstance(gain, (int, float)):
        return max(0.0, min(1.0, float(gain)))
    prompt = (sfx.get("prompt") or "").lower()
    for kw in AMBIENT_KEYWORDS:
        if kw in prompt:
            return 0.22
    for kw in SHARP_KEYWORDS:
        if kw in prompt:
            return 0.45
    return 0.55


def _ffprobe_has_audio(path: Path) -> bool:
    """True if ``path`` contains at least one audio stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout or "{}")
    return bool(data.get("streams"))


def mix_preview(
    *,
    preview_path: Path,
    raw_path: Path,
    sound_effects: list[dict],
    project_root: Path,
) -> dict:
    """Overwrite ``preview_path`` with a new mp4 that layers each SFX
    on top of the clean Wan render kept at ``raw_path``.

    If ``raw_path`` doesn't yet exist, the current ``preview_path`` is
    moved there first so we preserve the pristine Wan output for all
    future re-mixes.
    """
    if not preview_path.exists():
        raise FileNotFoundError(f"preview missing: {preview_path}")

    if not raw_path.exists():
        shutil.copy2(preview_path, raw_path)

    sfx = [e for e in (sound_effects or []) if e.get("audio_ref")]
    if not sfx:
        # Nothing to mix — restore raw over preview just in case an
        # earlier mix sits there, and return.
        shutil.copy2(raw_path, preview_path)
        return {
            "status": "no_sfx", "sfx_count": 0,
            "had_base_audio": _ffprobe_has_audio(raw_path),
        }

    has_base_audio = _ffprobe_has_audio(raw_path)

    # Resolve paths, offsets, volumes.
    sfx_inputs: list[dict] = []
    for i, entry in enumerate(sfx):
        audio_path = (project_root / entry["audio_ref"]).resolve()
        if not audio_path.exists():
            continue
        sfx_inputs.append({
            "idx": i + 1 if has_base_audio else i,  # ffmpeg input index
            "path": audio_path,
            "offset_ms": max(0, int(round(float(entry.get("offset_sec") or 0) * 1000))),
            "volume": resolve_sfx_volume(entry),
            "prompt": entry.get("prompt", ""),
        })
    if not sfx_inputs:
        shutil.copy2(raw_path, preview_path)
        return {"status": "no_usable_sfx_audio", "sfx_count": 0}

    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(raw_path)]
    for s in sfx_inputs:
        cmd += ["-i", str(s["path"])]

    # Build the filter graph. Each SFX gets a volume + adelay.
    filter_parts: list[str] = []
    mix_refs: list[str] = []
    if has_base_audio:
        mix_refs.append("[0:a]")  # keep dialogue audio as-is
    for s in sfx_inputs:
        lbl = f"s{s['idx']}"
        # adelay takes one value per channel; ensure stereo.
        filter_parts.append(
            f"[{s['idx']}:a]aformat=channel_layouts=stereo,"
            f"volume={s['volume']:.3f},"
            f"adelay={s['offset_ms']}|{s['offset_ms']}[{lbl}]"
        )
        mix_refs.append(f"[{lbl}]")

    # amix normalize=0 keeps our explicit volumes instead of 1/n scaling.
    filter_parts.append(
        f"{''.join(mix_refs)}amix=inputs={len(mix_refs)}"
        f":dropout_transition=0:normalize=0[aout]"
    )
    filter_complex = ";".join(filter_parts)

    # Write to a temp path next to the target, then atomically rename.
    tmp_out = preview_path.with_suffix(preview_path.suffix + ".mixing.mp4")
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(tmp_out),
    ]
    subprocess.run(cmd, check=True)
    tmp_out.replace(preview_path)

    return {
        "status": "mixed",
        "sfx_count": len(sfx_inputs),
        "had_base_audio": has_base_audio,
        "output": str(preview_path),
        "detail": [
            {"prompt": s["prompt"][:60],
             "offset_ms": s["offset_ms"],
             "volume": round(s["volume"], 3)}
            for s in sfx_inputs
        ],
    }


def _raw_for(preview_path: Path) -> Path:
    """``preview.mp4`` -> ``preview_raw.mp4`` (same for _vertical)."""
    return preview_path.with_name(
        preview_path.stem + "_raw" + preview_path.suffix
    )


def mix_shot(project: Project, shot_id: str,
             orientation: str = "horizontal") -> dict:
    parts = shot_id.split("_")
    if len(parts) < 3:
        raise ValueError(f"Bad shot_id: {shot_id!r}")
    chapter_id = parts[0]
    scene_id = "_".join(parts[:2])

    shot = project.load_shot(chapter_id, scene_id, shot_id)
    shot_dir = project._path("chapters", chapter_id, "shots", shot_id)
    preview_name = ("preview.mp4" if orientation == "horizontal"
                    else "preview_vertical.mp4")
    preview_path = shot_dir / preview_name
    if not preview_path.exists():
        return {"shot_id": shot_id, "orientation": orientation,
                "status": "no_preview"}
    raw_path = _raw_for(preview_path)

    sound_effects = (shot.get("audio") or {}).get("sound_effects") or []
    project_root = Path(project.root).resolve()

    result = mix_preview(
        preview_path=preview_path,
        raw_path=raw_path,
        sound_effects=sound_effects,
        project_root=project_root,
    )
    result.update({"shot_id": shot_id, "orientation": orientation})
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--shot", help="Single shot_id")
    ap.add_argument("--chapter", help="Mix every shot with a preview in this chapter")
    ap.add_argument("--orientation", choices=("horizontal", "vertical", "both"),
                    default="horizontal")
    args = ap.parse_args()

    if not (args.shot or args.chapter):
        print("ERROR: --shot or --chapter required")
        return 2

    project = Project(args.project)
    orientations = (["horizontal", "vertical"]
                    if args.orientation == "both" else [args.orientation])

    targets: list[tuple[str, str]] = []
    if args.shot:
        for orient in orientations:
            targets.append((args.shot, orient))
    else:
        shots_dir = project._path("chapters", args.chapter, "shots")
        for sd in sorted(shots_dir.iterdir()):
            if not sd.is_dir():
                continue
            for orient in orientations:
                name = "preview.mp4" if orient == "horizontal" else "preview_vertical.mp4"
                if (sd / name).exists():
                    targets.append((sd.name, orient))

    print(f"Mixing {len(targets)} preview(s)...")
    mixed = 0
    for sid, orient in targets:
        r = mix_shot(project, sid, orient)
        print(f"  {sid} {orient}: {r['status']}  "
              f"sfx={r.get('sfx_count', 0)}")
        if r["status"] == "mixed":
            mixed += 1
    print(f"\nDone. {mixed}/{len(targets)} mixed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
