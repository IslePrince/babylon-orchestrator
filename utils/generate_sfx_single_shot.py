"""Generate SFX for a single shot — a cheap test run of the SoundFXStage
pipeline without committing to a full-chapter spend.

Runs the same two-step flow as :class:`stages.pipeline.SoundFXStage`:
1. Ask Claude to suggest 1-3 SFX prompts for the shot.
2. Generate each via ElevenLabs and save to
   ``audio/{chapter_id}/{shot_id}/sfx_NNN.mp3``.

Writes the references back onto ``shot.audio.sound_effects`` and records
costs in the project ledger, so downstream Editing Room code can pick
them up exactly as it would after a full SoundFXStage run.

Usage:
    PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \\
    py -3.12 utils/generate_sfx_single_shot.py \\
        --project <path> --shot ch01_sc01_sh006 [--dry-run]
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ORCH_ROOT = SCRIPT_DIR.parent
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

from core.project import Project  # noqa: E402
from core.cost_manager import CostManager  # noqa: E402
from apis.claude_client import ClaudeClient  # noqa: E402
from apis.elevenlabs import ElevenLabsClient  # noqa: E402
from apis.comfyui_audio import ComfyUIAudioClient  # noqa: E402
from apis.prompt_builder import build_sfx_context_block  # noqa: E402
from apis.sfx_router import route as route_sfx, explain as explain_route  # noqa: E402


def _parse_shot_id(shot_id: str) -> tuple[str, str]:
    """Split ``ch01_sc01_sh006`` into ``(ch01, ch01_sc01)``."""
    parts = shot_id.split("_")
    if len(parts) < 3:
        raise ValueError(f"Unexpected shot_id format: {shot_id!r}")
    chapter_id = parts[0]
    scene_id = "_".join(parts[:2])
    return chapter_id, scene_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--shot", required=True,
                    help="Shot ID, e.g. ch01_sc01_sh006")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print suggestions; don't call ElevenLabs or write files")
    ap.add_argument("--replace", action="store_true",
                    help="Overwrite existing shot.audio.sound_effects")
    ap.add_argument(
        "--provider",
        choices=("auto", "comfyui", "elevenlabs"),
        default="auto",
        help="Generator to use. 'auto' (default) routes each prompt per "
             "apis.sfx_router keyword heuristic; explicit value forces "
             "that provider for every prompt.",
    )
    args = ap.parse_args()

    project = Project(args.project)
    chapter_id, scene_id = _parse_shot_id(args.shot)

    try:
        shot = project.load_shot(chapter_id, scene_id, args.shot)
    except FileNotFoundError:
        print(f"ERROR: shot not found: {args.shot}")
        return 2

    existing = (shot.get("audio") or {}).get("sound_effects") or []
    if existing and not args.replace:
        print(f"Shot already has {len(existing)} SFX; pass --replace to overwrite.")
        for e in existing:
            print(f"  - {e.get('prompt')}")
        return 0

    label = shot.get("label", "")
    cin = shot.get("cinematic", {})
    movement = cin.get("camera_movement", {})
    sound_design = (shot.get("audio") or {}).get("sound_design", [])

    context_block = build_sfx_context_block(project, chapter_id, shot=shot)

    prompt_parts = []
    if context_block:
        prompt_parts.append(context_block)
    prompt_parts.append(
        f"Shot: {args.shot}\n"
        f"Label: {label}\n"
        f"Shot type: {cin.get('shot_type', 'unknown')}\n"
        f"Camera movement: {movement.get('type', 'static')}\n"
        f"Existing sound_design hints: {sound_design}"
    )
    prompt_parts.append(
        "Suggest 1-3 sound effects for this shot. Each prompt must be "
        "a SHORT, CONCRETE foley description — ideally 3-8 words "
        "naming ONE specific sound source and surface (e.g., "
        "'sandals scuffing on dry dirt', 'donkey braying distant', "
        "'cloth robe rustle', 'marketplace chatter'). "
        "DO NOT write flowery scene descriptions or stack multiple "
        "sounds in one prompt. Each prompt generates exactly one sound — "
        "keep it physical and specific. Avoid modern/electronic sources. "
        "No dialogue, no voices speaking words, no music.\n\n"
        "For each SFX also decide offset_sec — how many seconds INTO "
        "the shot the sound should start. Use 0 for sounds that fire "
        "at the shot's opening beat (e.g., a knock that begins the "
        "scene). Use a small positive number (0.5-2.0) for sounds that "
        "arrive slightly later. Use 0 for continuous ambience. Stagger "
        "the offsets when multiple sounds would overlap.\n\n"
        "Return JSON array: "
        "[{\"prompt\": \"...\", \"duration_sec\": N, \"offset_sec\": N}]"
    )
    prompt = "\n\n".join(prompt_parts)

    print(f"Asking Claude for SFX suggestions for {args.shot} ({label!r})...")
    claude = ClaudeClient()
    parsed = claude._call_json(
        system=(
            "You are a film sound designer. For each shot, suggest "
            "1-3 concise SFX generation prompts."
        ),
        user=prompt,
        max_tokens=300,
    )
    suggestions = (
        parsed if isinstance(parsed, list)
        else parsed.get("sfx") or parsed.get("effects") or []
    )
    suggestions = [s for s in suggestions if isinstance(s, dict) and s.get("prompt")]

    if not suggestions:
        print("Claude returned no usable suggestions.")
        return 1

    # Decide provider per prompt now so the cost preview is accurate.
    force = None if args.provider == "auto" else args.provider
    routed = []
    est_cost = 0.0
    for s in suggestions:
        provider, reason = explain_route(s["prompt"])
        if force:
            provider = force
            reason = f"forced by --provider {force}"
        routed.append({"sfx": s, "provider": provider, "reason": reason})
        if provider == "elevenlabs":
            est_cost += 0.10

    print(f"\nSuggested {len(suggestions)} SFX (provider={args.provider}):")
    for r in routed:
        s = r["sfx"]
        tag = "$0.10 ElevenLabs" if r["provider"] == "elevenlabs" else "$0.00 ComfyUI"
        print(f"  [{tag}] {s['prompt']} ({s.get('duration_sec', 5)}s)")
        print(f"    -> {r['reason']}")
    print(f"\nEstimated cost: ${est_cost:.2f}")

    if args.dry_run:
        print("DRY RUN — no API calls made.")
        return 0

    costs = CostManager(project)
    # Only check the ElevenLabs gate/budget if we'll actually call it.
    if est_cost > 0:
        costs.check_api_allowed("elevenlabs", required_gate="cut_to_sound")
        costs.check_budget("elevenlabs", est_cost)

    el_client: ElevenLabsClient | None = None
    cu_client: ComfyUIAudioClient | None = None
    project_root = Path(project.root).resolve()
    sfx_refs = []
    for i, r in enumerate(routed):
        s = r["sfx"]
        provider = r["provider"]
        audio_ref = f"audio/{chapter_id}/{args.shot}/sfx_{i:03d}.mp3"
        output_path = project_root / audio_ref
        print(f"\n  [{i+1}/{len(routed)}] ({provider}) {s['prompt']}")

        if provider == "elevenlabs":
            if el_client is None:
                el_client = ElevenLabsClient()
            result = el_client.generate_sound_effect(
                prompt=s["prompt"],
                duration_sec=s.get("duration_sec", 5),
                output_path=str(output_path),
            )
            cost = result.get("cost_usd", 0.10)
            costs.record("elevenlabs", cost, "sound_fx",
                         f"SFX: {s['prompt'][:40]}", args.shot)
        else:
            if cu_client is None:
                cu_client = ComfyUIAudioClient()
            result = cu_client.generate_sound_effect(
                prompt=s["prompt"],
                duration_sec=s.get("duration_sec", 5),
                output_path=str(output_path),
            )
            cost = 0.0

        sfx_refs.append({
            "sfx_id": f"{args.shot}_sfx_{i:03d}",
            "prompt": s["prompt"],
            "audio_ref": audio_ref,
            "duration_sec": s.get("duration_sec", 5),
            "provider": provider,
            "offset_sec": float(s.get("offset_sec", 0) or 0),
        })
        print(f"      -> {result.get('size_bytes', 0)} bytes (${cost:.2f})")

    audio = shot.setdefault("audio", {})
    audio["sound_effects"] = sfx_refs
    project.save_shot(chapter_id, scene_id, args.shot, shot)
    print(f"\nSaved {len(sfx_refs)} SFX references to shot.audio.sound_effects.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
