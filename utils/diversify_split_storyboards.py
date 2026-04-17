"""Diversify the storyboards of split continuation shots.

When ``utils/split_long_shots.py`` breaks a long dialogue shot in two,
both halves inherit the parent's storyboard — so the preview cut looks
like the camera is frozen while the character keeps talking. This
utility asks Claude to propose an alternate camera angle for each
continuation shot (reverse, close-up insert, 30° move) and re-renders
the storyboard (both 16:9 and 9:16) through ComfyUI so the cut
actually cuts.

Character LoRAs are reused, so identity stays locked across the angle
change. Any stale ``preview_*.mp4`` on the touched shots is deleted
so the next preview-video batch re-renders them against the new
framing.

Usage:
    python3 utils/diversify_split_storyboards.py --project <path> --chapter ch01
    python3 utils/diversify_split_storyboards.py --project <path> --chapter ch01 --shot ch01_sc01_sh006b
    python3 utils/diversify_split_storyboards.py --project <path> --chapter ch01 --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
ORCH_ROOT = SCRIPT_DIR.parent
if str(ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(ORCH_ROOT))

from core.project import Project  # noqa: E402


# Detect split-continuation shot ids like ``ch01_sc01_sh006b``,
# ``sh024c``, ``sh030_2`` — anything where the id ends with a letter
# suffix or ``_<int>`` after a ``shNNN`` stem.
_SPLIT_SUFFIX_RE = re.compile(r"^(.+_sh\d+)([a-z]|_\d+)$")


def _parent_shot_id(shot_id: str) -> Optional[str]:
    m = _SPLIT_SUFFIX_RE.match(shot_id)
    return m.group(1) if m else None


def _is_continuation(shot: dict) -> bool:
    sid = shot.get("shot_id") or ""
    if _parent_shot_id(sid):
        return True
    label = (shot.get("label") or "").strip().lower()
    return "(cont'd)" in label or "(recovered)" in label


def _suggest_alternate_framing(claude, parent_shot: dict, world: dict,
                               child_shot: dict) -> dict:
    """Ask Claude for a new storyboard prompt for the continuation."""
    parent_prompt = (parent_shot.get("storyboard") or {}).get(
        "storyboard_prompt") or ""
    parent_cin = parent_shot.get("cinematic") or {}
    parent_label = parent_shot.get("label") or ""
    child_label = child_shot.get("label") or ""

    setting = (world.get("setting") or {})
    period = (setting.get("time_period") or "").strip()
    location = (setting.get("location") or "").strip()

    characters = [
        c.get("character_id", c) if isinstance(c, dict) else c
        for c in (child_shot.get("characters_in_frame") or [])
    ]
    dialogue = " / ".join(child_shot.get("dialogue_in_shot") or [])

    system = (
        "You are a film cinematographer deciding the cut between two "
        "halves of a single long dialogue line. The second half needs "
        "a different camera angle from the first so the edit feels "
        "purposeful, not frozen. Respond ONLY with valid JSON."
    )
    user = (
        f"Period/Location: {period} — {location}\n"
        f"Parent shot label: {parent_label!r}\n"
        f"Parent shot type: {parent_cin.get('shot_type', 'unknown')}\n"
        f"Parent framing: {parent_cin.get('framing', 'unknown')}\n"
        f"Parent storyboard_prompt (DO NOT REUSE VERBATIM):\n"
        f"  {parent_prompt}\n\n"
        f"Continuation shot: {child_label!r}\n"
        f"Characters in frame: {characters}\n"
        f"Dialogue during the continuation: {dialogue!r}\n\n"
        "Propose an alternate framing for the continuation. Pick one "
        "of: reverse_shot, over_the_shoulder, close_up_insert, "
        "medium_push_in, side_angle, listener_reaction. Keep all "
        "character, costume, location, lighting, and world-bible "
        "details identical to the parent — only the camera moves. "
        "Output a storyboard_prompt in the SAME style as the parent "
        "(same length, same technical vocabulary, pen-and-ink style "
        "description preserved).\n\n"
        "Return JSON:\n"
        "{\n"
        "  \"storyboard_prompt\": \"<full prompt for SDXL>\",\n"
        "  \"shot_type\": \"<close_up|medium|wide|over_the_shoulder|reverse|insert>\",\n"
        "  \"framing\": \"<framing>\",\n"
        "  \"rationale\": \"<one sentence on why this angle>\"\n"
        "}"
    )
    return claude._call_json(system=system, user=user, max_tokens=800)


def _character_lora_configs(project: Project, characters: list[str]) -> list[dict]:
    """Build the LoRA config list ComfyUIClient expects from the
    characters visible in the shot. Looks up the trained LoRA at
    ``characters/<cid>/lora/<cid>_char.safetensors`` (the layout
    LoRATrainingStage writes to)."""
    configs = []
    for cid in characters:
        try:
            c = project.load_character(cid)
        except FileNotFoundError:
            continue
        lora_path = project._path(
            "characters", cid, "lora", f"{cid}_char.safetensors"
        )
        if not lora_path.exists():
            continue
        trig = (c.get("visual_tag") or cid).strip()
        configs.append({
            "file": f"{cid}_char.safetensors",
            "weight": 1.0,
            "trigger_word": trig,
        })
    return configs


def _load_character_visuals(project: Project) -> dict:
    """Load character_visuals dict the same way StoryboardStage does.
    build_storyboard_prompt expects this shape so the style prefix,
    inline character descriptions, and costume fields come out right."""
    visuals: dict = {}
    project_visuals = project._path("characters", "visual_tags.json")
    if project_visuals.exists():
        try:
            visuals = project._load(project_visuals).get("characters", {}) or {}
        except Exception:  # noqa: BLE001
            visuals = {}

    # Enrich from full character.json so inline descriptions have
    # age, costume, props.
    for cid in list(visuals.keys()):
        try:
            full = project.load_character(cid)
            desc = full.get("description") or {}
            assets = full.get("assets") or {}
            if desc.get("age"):
                visuals[cid]["age"] = desc["age"]
            if assets.get("signature_props"):
                visuals[cid]["signature_props"] = assets["signature_props"]
            if assets.get("costume_variants"):
                visuals[cid]["costume_variants"] = assets["costume_variants"]
        except (FileNotFoundError, KeyError):
            continue
    return visuals


def _world_visual_style(world: dict) -> str:
    """Pull the world bible's visual_style and run it through the
    storyboard-medium adapter so photoreal terms get stripped before
    we hand it to SDXL. Matches what StoryboardStage does."""
    from apis.prompt_builder import adapt_style_for_storyboard
    style = (world or {}).get("visual_style") or ""
    return adapt_style_for_storyboard(style) if style else ""


def diversify_shot(
    project: Project, shot: dict, parent_shot: dict,
    world: dict, comfy, claude, dry_run: bool,
) -> dict:
    """Render an alternate-angle storyboard for one continuation shot."""
    shot_id = shot["shot_id"]
    chapter_id = shot.get("chapter_id") or shot_id.split("_")[0]
    scene_id = shot.get("scene_id") or "_".join(shot_id.split("_")[:2])
    shot_dir = project._path("chapters", chapter_id, "shots", shot_id)

    suggestion = _suggest_alternate_framing(
        claude=claude, parent_shot=parent_shot, world=world, child_shot=shot,
    )
    new_prompt = (suggestion.get("storyboard_prompt") or "").strip()
    if not new_prompt:
        return {"shot_id": shot_id, "status": "claude_no_prompt",
                "raw": suggestion}

    chars = [
        c.get("character_id", c) if isinstance(c, dict) else c
        for c in (shot.get("characters_in_frame") or [])
    ]
    loras = _character_lora_configs(project, chars)

    # Wrap Claude's action prompt in the full pen-and-ink style
    # prefix + character descriptions + camera/quality suffix the
    # main StoryboardStage uses. Without this the continuation loses
    # the pen-and-ink look and renders as generic SDXL output.
    from apis.prompt_builder import build_storyboard_prompt
    character_visuals = _load_character_visuals(project)
    visual_style = _world_visual_style(world)
    # Temporarily swap the shot's storyboard_prompt so
    # build_storyboard_prompt picks up the new action text without us
    # mutating shot.json until after the render succeeds.
    shot_for_prompt = json.loads(json.dumps(shot))
    shot_for_prompt.setdefault("storyboard", {})["storyboard_prompt"] = new_prompt
    final_prompt = build_storyboard_prompt(
        scene_prompt=new_prompt,
        shot=shot_for_prompt,
        character_visuals=character_visuals,
        visual_style=visual_style,
    )

    result: dict = {
        "shot_id": shot_id,
        "status": "would_render" if dry_run else "rendering",
        "new_action_prompt": new_prompt[:160],
        "full_sdxl_prompt": final_prompt[:220],
        "shot_type": suggestion.get("shot_type"),
        "framing": suggestion.get("framing"),
        "rationale": suggestion.get("rationale"),
        "loras": [c["file"] for c in loras],
    }
    if dry_run:
        return result

    # Reuse the parent's seed so the LoRA-locked character identity,
    # lighting, and palette stay on-model across the cut. The action
    # text / camera angle differ; the seed keeps everything else in
    # the same "family". Fall back to a fresh seed only when the
    # parent wasn't recorded (legacy shot.json without generation_meta).
    parent_meta = (parent_shot.get("storyboard") or {}).get("generation_meta") or {}
    parent_seed = parent_meta.get("seed")
    try:
        seed = int(parent_seed) if parent_seed is not None else None
    except (TypeError, ValueError):
        seed = None
    if seed is None:
        seed = int(time.time()) & 0x7FFFFFFF
        result["seed_source"] = "new_random"
    else:
        result["seed_source"] = "parent"
    result["seed"] = seed

    # Serialize GPU access against any other ComfyUI job running.
    from core.gpu_lock import gpu_exclusive
    with gpu_exclusive(f"diversify_storyboard:{shot_id}", blocking=False):
        h_path = shot_dir / "storyboard.png"
        v_path = shot_dir / "storyboard_vertical.png"

        # Horizontal
        if loras:
            h_meta = comfy.generate_storyboard_with_loras(
                prompt=final_prompt, output_path=str(h_path),
                lora_configs=loras, width=1344, height=768, seed=seed,
            )
        else:
            h_meta = comfy.generate_storyboard(
                prompt=final_prompt, output_path=str(h_path),
                width=1344, height=768, seed=seed,
            )
        # Vertical — same seed keeps the composition family feel
        if loras:
            v_meta = comfy.generate_storyboard_with_loras(
                prompt=final_prompt, output_path=str(v_path),
                lora_configs=loras, width=768, height=1344, seed=seed,
            )
        else:
            v_meta = comfy.generate_storyboard_vertical(
                prompt=final_prompt, output_path=str(v_path), seed=seed,
            )

    # Update shot.json. Preserve the parent-inherited cinematic data
    # but stamp the new prompt + any angle metadata Claude returned.
    storyboard = shot.setdefault("storyboard", {})
    storyboard["storyboard_prompt"] = new_prompt
    storyboard["generated"] = True
    storyboard["generation_meta"] = {
        "provider": "comfyui",
        "diversified_from": parent_shot["shot_id"],
        "shot_type_suggested": suggestion.get("shot_type"),
        "framing_suggested": suggestion.get("framing"),
        "rationale": suggestion.get("rationale"),
        "final_prompt": final_prompt,
        "seed": seed,
        "seed_source": result.get("seed_source"),
        "loras_used": [c["file"] for c in loras],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "horizontal": h_meta,
        "vertical": v_meta,
    }
    if suggestion.get("shot_type"):
        shot.setdefault("cinematic", {})["shot_type"] = suggestion["shot_type"]
    if suggestion.get("framing"):
        shot["cinematic"]["framing"] = suggestion["framing"]

    # Stale preview mp4s — drop so the next batch re-renders with the
    # new framing. preview_raw.mp4 also goes (it was baked off the old
    # still).
    for stale in ("preview.mp4", "preview_vertical.mp4",
                  "preview_raw.mp4", "preview_vertical_raw.mp4"):
        p = shot_dir / stale
        if p.exists():
            p.unlink()
    shot.pop("preview", None)

    project.save_shot(chapter_id, scene_id, shot_id, shot)

    result["status"] = "rendered"
    result["h_bytes"] = h_meta.get("size_bytes")
    result["v_bytes"] = v_meta.get("size_bytes")
    return result


def diversify_chapter(
    project: Project, chapter_id: str, only_shot: Optional[str],
    dry_run: bool,
) -> dict:
    from apis.claude_client import ClaudeClient
    from apis.comfyui import ComfyUIClient

    world = project.load_world_bible().get("world_bible", {}) or {}
    shots_dir = project._path("chapters", chapter_id, "shots")
    if not shots_dir.exists():
        return {"chapter_id": chapter_id, "status": "no_shots_dir"}

    # Collect continuation shots.
    continuations: list[tuple[Path, dict]] = []
    all_shots: dict[str, dict] = {}
    for sd in sorted(shots_dir.iterdir()):
        if not sd.is_dir():
            continue
        sp = sd / "shot.json"
        if not sp.exists():
            continue
        with open(sp, "r", encoding="utf-8") as f:
            shot = json.load(f)
        all_shots[shot["shot_id"]] = shot
        if only_shot and shot["shot_id"] != only_shot:
            continue
        if _is_continuation(shot):
            continuations.append((sd, shot))

    if not continuations:
        return {"chapter_id": chapter_id, "status": "no_continuations",
                "only_shot": only_shot}

    claude = ClaudeClient()
    summary: dict = {
        "chapter_id": chapter_id,
        "to_diversify": len(continuations),
        "diversified": 0,
        "failed": [],
        "entries": [],
    }

    with ComfyUIClient() as comfy:
        for i, (sd, shot) in enumerate(continuations):
            sid = shot["shot_id"]
            parent_id = _parent_shot_id(sid) or sid
            parent = all_shots.get(parent_id) or shot
            try:
                r = diversify_shot(
                    project, shot, parent, world, comfy, claude, dry_run,
                )
                summary["entries"].append(r)
                if r["status"] == "rendered":
                    summary["diversified"] += 1
                print(f"  [{i+1}/{len(continuations)}] {sid} → "
                      f"{r['status']} {r.get('shot_type', '')}/"
                      f"{r.get('framing', '')}")
            except Exception as e:  # noqa: BLE001
                summary["failed"].append({"shot_id": sid, "error": str(e)})
                print(f"  [{i+1}/{len(continuations)}] {sid} → FAIL: {e}")

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--shot", default=None,
                    help="Restrict to a single continuation shot_id.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Call Claude for suggestions but don't render "
                         "and don't write shot.json.")
    args = ap.parse_args()
    project = Project(args.project)

    result = diversify_chapter(
        project=project, chapter_id=args.chapter,
        only_shot=args.shot, dry_run=args.dry_run,
    )
    print()
    print(json.dumps({k: v for k, v in result.items() if k != "entries"},
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
