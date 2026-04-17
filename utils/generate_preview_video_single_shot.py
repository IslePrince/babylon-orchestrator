"""Render a preview video (talking storyboard) for one shot.

Uses Wan 2.1 + InfiniTalk via ComfyUI — runs free on the local GPU.
Takes the shot's storyboard image + the sliced dialogue audio(s), and
saves an mp4 next to the shot's storyboard:

    chapters/<ch>/shots/<shot_id>/preview.mp4

Single-speaker by default; if the shot has two distinct
character_ids across its ``audio.lines`` entries, the utility
automatically switches to InfiniTalk's two_speakers mode.

Usage:
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \\
    python3 utils/generate_preview_video_single_shot.py \\
        --project <path> --shot ch01_sc01_sh006 [--orientation horizontal|vertical]
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ORCH_ROOT = SCRIPT_DIR.parent
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

from core.project import Project  # noqa: E402
from apis.comfyui_video import ComfyUIVideoClient, FPS  # noqa: E402


ORIENTATION_DIMS = {
    "horizontal": (832, 480),   # 16:9-ish, matches reference workflow
    "vertical":   (480, 832),   # 9:16 portrait
}


def _parse_shot_id(shot_id: str) -> tuple[str, str]:
    parts = shot_id.split("_")
    if len(parts) < 3:
        raise ValueError(f"Unexpected shot_id format: {shot_id!r}")
    return parts[0], "_".join(parts[:2])


def _shot_prompt(shot: dict) -> str:
    """Build a short prompt cue for the talking head — keeps camera
    direction and characters on screen so Wan doesn't drift."""
    label = (shot.get("label") or "").strip()
    cin = shot.get("cinematic", {}) or {}
    movement = (cin.get("camera_movement", {}) or {}).get("type", "static")
    chars = [
        c.get("character_id", c) if isinstance(c, dict) else c
        for c in (shot.get("characters_in_frame") or [])
    ]
    who = ", ".join(c for c in chars if c) or "character"
    return (
        f"{who.title()} speaking on camera, cinematic storyboard frame, "
        f"natural head and mouth motion synced to audio, "
        f"{movement} camera, {label}".strip(" ,.")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--shot", required=True)
    ap.add_argument(
        "--orientation", choices=("horizontal", "vertical"), default="horizontal",
        help="Which storyboard to animate. horizontal uses storyboard.png; "
             "vertical uses storyboard_vertical.png.",
    )
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument(
        "--replace", action="store_true",
        help="Overwrite an existing preview.mp4 for the shot.",
    )
    args = ap.parse_args()

    project = Project(args.project)
    chapter_id, scene_id = _parse_shot_id(args.shot)

    try:
        shot = project.load_shot(chapter_id, scene_id, args.shot)
    except FileNotFoundError:
        print(f"ERROR: shot not found: {args.shot}")
        return 2

    shot_dir = Path(project.root) / "chapters" / chapter_id / "shots" / args.shot
    if args.orientation == "horizontal":
        storyboard = shot_dir / "storyboard.png"
        out_name = "preview.mp4"
    else:
        storyboard = shot_dir / "storyboard_vertical.png"
        out_name = "preview_vertical.mp4"

    if not storyboard.exists():
        print(f"ERROR: storyboard missing: {storyboard}")
        return 2

    out_path = shot_dir / out_name
    if out_path.exists() and not args.replace:
        print(f"{out_path} already exists; pass --replace to overwrite.")
        return 0

    lines = (shot.get("audio") or {}).get("lines") or []
    if not lines:
        print("ERROR: shot has no audio.lines — run sound_fx / voice_recording "
              "migration first so slices are populated.")
        return 2

    # Group lines into 1 or 2 speakers in dialogue order.
    speakers: list[dict] = []
    seen_speakers: set[str] = set()
    project_root = Path(project.root).resolve()
    for line in lines:
        cid = line.get("character_id") or ""
        if cid in seen_speakers:
            # Same speaker delivering a follow-up — append to their slice
            speakers[-1]["end_time_sec"] = max(
                speakers[-1]["end_time_sec"],
                float(line.get("end_time_sec") or 0),
            )
            continue
        seen_speakers.add(cid)
        audio_ref = line.get("audio_ref") or ""
        if not audio_ref:
            continue
        speakers.append({
            "character_id": cid,
            "audio_source": str(project_root / audio_ref),
            "start_time_sec": float(line.get("start_time_sec") or 0),
            "end_time_sec": float(line.get("end_time_sec") or 0),
        })
        if len(speakers) == 2:
            break  # InfiniTalk caps at two speakers per clip

    if not speakers:
        print("ERROR: no valid audio slice on this shot.")
        return 2

    width, height = ORIENTATION_DIMS[args.orientation]

    print(
        f"Shot {args.shot}: {len(speakers)} speaker(s), "
        f"orientation={args.orientation} ({width}x{height})"
    )
    for i, sp in enumerate(speakers):
        d = sp["end_time_sec"] - sp["start_time_sec"]
        print(
            f"  speaker {i+1}: {sp['character_id']}  "
            f"slice {sp['start_time_sec']:.2f}-{sp['end_time_sec']:.2f}s "
            f"({d:.2f}s)  src={Path(sp['audio_source']).name}"
        )
    print(f"  storyboard={storyboard.name}")
    print(f"  output    ={out_path}")

    with tempfile.TemporaryDirectory(prefix="babylon_video_") as tmp:
        with ComfyUIVideoClient() as client:
            result = client.generate_preview_video(
                shot_id=args.shot,
                storyboard_path=storyboard,
                speakers=speakers,
                width=width,
                height=height,
                prompt=_shot_prompt(shot),
                output_path=out_path,
                seed=args.seed,
                scratch_dir=tmp,
            )

    print(
        f"\nGenerated {result['frames']} frames @ {FPS}fps "
        f"({result['duration_sec']:.2f}s source audio) in {result['wall_sec']}s "
        f"-> {result.get('path')} ({result['bytes']} bytes)"
    )

    # Persist reference on the shot for the Editing Room.
    preview = shot.setdefault("preview", {})
    key = "video_ref_vertical" if args.orientation == "vertical" else "video_ref"
    preview[key] = f"chapters/{chapter_id}/shots/{args.shot}/{out_name}"
    preview["generated_at"] = result.get("wall_sec")
    project.save_shot(chapter_id, scene_id, args.shot, shot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
