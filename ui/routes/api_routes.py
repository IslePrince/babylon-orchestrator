"""
ui/routes/api_routes.py — JSON API endpoints.

All endpoints return JSON. State is reloaded from disk on every request
(no caching). Project instances are created fresh via get_project().
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file, current_app

api_bp = Blueprint("api", __name__)


def _get_project(slug):
    from ui.server import get_project
    return get_project(slug)


def _get_orchestrator_version():
    from ui.version import get_current_version
    return get_current_version()


def _load_source_text(project) -> str:
    """Load source text from the project's source/ directory.

    Checks for any .txt file in the source/ folder (the filename varies
    per project — e.g., 'TheRichestManInBabylon.txt', 'source.txt', etc.).
    """
    source_dir = Path(project.root) / "source"
    if not source_dir.exists():
        return ""
    try:
        for txt_file in sorted(source_dir.glob("*.txt")):
            return txt_file.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def _extract_screenplay_context(project, character_ids: list = None) -> dict:
    """
    Extract screenplay dialogue and actions for characters.

    Scans all chapter shots and audio metadata to build a text block per
    character showing what they actually say and do in the screenplay.
    This is the ground truth for who a character is — their gender, age,
    social class, and occupation are all revealed through their dialogue.

    Args:
        project: Project instance
        character_ids: Optional list of character_ids to extract for.
                       If None, extracts for all characters found in shots.

    Returns:
        Dict mapping character_id -> condensed screenplay context string
    """
    context = {}  # char_id -> list of lines

    for chapter_id in project.get_all_chapter_ids():
        # Find all shot directories for this chapter
        shots_dir = project._path("chapters", chapter_id, "shots")
        if not shots_dir.exists():
            continue

        for shot_dir in sorted(shots_dir.iterdir()):
            shot_path = shot_dir / "shot.json"
            if not shot_path.exists():
                continue

            try:
                shot = json.loads(shot_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            shot_id = shot.get("shot_id", shot_dir.name)
            shot_label = shot.get("label", "")

            # Characters visible in this shot
            chars_in_frame = [
                c.get("character_id")
                for c in shot.get("characters_in_frame", [])
                if c.get("character_id")
            ]

            # Dialogue preview from shot data
            dialogue_preview = shot.get("dialogue_in_shot", [])

            # Full dialogue from audio metadata
            audio_lines = shot.get("audio", {}).get("lines", [])
            for line_info in audio_lines:
                cid = line_info.get("character_id")
                if not cid:
                    continue
                if character_ids and cid not in character_ids:
                    continue

                # Try to load full dialogue text from meta file
                line_id = line_info.get("line_id", "")
                meta_path = project._path(
                    "audio", chapter_id, shot_id, f"{line_id}.meta.json"
                )
                full_text = ""
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        full_text = meta.get("text", "")
                    except (json.JSONDecodeError, OSError):
                        pass

                if not full_text and dialogue_preview:
                    # Fallback to dialogue_in_shot snippet
                    full_text = " ".join(dialogue_preview)

                if full_text:
                    if cid not in context:
                        context[cid] = []
                    context[cid].append(
                        f"[{chapter_id}/{shot_id}] {shot_label}: "
                        f"\"{full_text}\""
                    )

            # Also note when a character appears in frame but doesn't speak
            for cid in chars_in_frame:
                if character_ids and cid not in character_ids:
                    continue
                if cid not in context:
                    context[cid] = []
                # Only add stage direction note if they have no dialogue in this shot
                if not any(
                    l.get("character_id") == cid for l in audio_lines
                ):
                    if shot_label:
                        context[cid].append(
                            f"[{chapter_id}/{shot_id}] appears in: {shot_label}"
                        )

    # Condense: max ~2000 chars per character to keep prompt manageable
    result = {}
    for cid, lines in context.items():
        text = "\n".join(lines)
        if len(text) > 2000:
            text = text[:2000] + "\n... (truncated)"
        result[cid] = text

    return result


# ------------------------------------------------------------------
# Project listing
# ------------------------------------------------------------------

@api_bp.route("/projects")
def list_projects():
    """Return all discovered projects."""
    from core.project import Project
    result = []
    for slug, path in current_app.config["PROJECTS"].items():
        try:
            p = Project(path)
            result.append({
                "slug": p.id,
                "display_name": p.data.get("display_name", p.id),
                "pipeline_stage": p.get_pipeline_stage(),
                "chapter_count": len(p.get_all_chapter_ids()),
            })
        except Exception as e:
            result.append({"slug": slug, "display_name": slug, "error": str(e)})
    return jsonify(result)


# ------------------------------------------------------------------
# Project status
# ------------------------------------------------------------------

@api_bp.route("/<slug>/status")
def project_status(slug):
    """Full project status for the dashboard."""
    from core.state_manager import StateManager
    project = _get_project(slug)
    sm = StateManager(project)
    return jsonify(sm.get_project_status())


@api_bp.route("/<slug>/chapter/<chapter_id>")
def chapter_detail(slug, chapter_id):
    """Deep chapter status with scenes and shots."""
    from core.state_manager import StateManager
    project = _get_project(slug)
    sm = StateManager(project)
    return jsonify(sm.get_chapter_status(chapter_id))


# ------------------------------------------------------------------
# Costs
# ------------------------------------------------------------------

@api_bp.route("/<slug>/costs")
def project_costs(slug):
    """Cost ledger with transactions and totals."""
    project = _get_project(slug)
    ledger = project.load_cost_ledger()
    # Attach per-API budget info for the UI bars
    api_budgets = {}
    for api_name, config in project.data.get("apis", {}).items():
        api_budgets[api_name] = {
            "budget_usd": config.get("budget_usd", 0),
            "enabled": config.get("enabled", False),
        }
    ledger["api_budgets"] = api_budgets
    return jsonify(ledger)


# ------------------------------------------------------------------
# World Bible + Chapters (for Ingest page)
# ------------------------------------------------------------------

@api_bp.route("/<slug>/world-bible")
def get_world_bible(slug):
    """Return world bible JSON."""
    project = _get_project(slug)
    try:
        return jsonify(project.load_world_bible())
    except FileNotFoundError:
        return jsonify({"error": "World bible not found. Run Ingest first."}), 404


@api_bp.route("/<slug>/world-bible/visual-style", methods=["PUT"])
def update_visual_style(slug):
    """Update the visual_style field in the world bible."""
    project = _get_project(slug)
    data = request.get_json(force=True)
    visual_style = data.get("visual_style", "").strip()
    if not visual_style:
        return jsonify({"error": "visual_style is required"}), 400
    try:
        wb = project.load_world_bible()
        bible = wb.get("world_bible", wb)
        bible["visual_style"] = visual_style
        project.save_world_bible(wb)
        return jsonify({"ok": True, "visual_style": visual_style})
    except FileNotFoundError:
        return jsonify({"error": "World bible not found"}), 404


@api_bp.route("/<slug>/chapters")
def list_chapters(slug):
    """Return all chapters with summaries and source text status."""
    project = _get_project(slug)
    chapters = []
    for ch_id in project.get_all_chapter_ids():
        try:
            ch = project.load_chapter(ch_id)
            source_path = Path(project.root) / "chapters" / ch_id / "source_text.txt"
            chapters.append({
                "chapter_id": ch_id,
                "title": ch.get("title", ch_id),
                "summary": ch.get("narrative", {}).get("logline", ""),
                "characters": ch.get("characters", {}).get("featured", []),
                "locations": ch.get("locations", {}).get("used", []),
                "status": ch.get("status", "pending"),
                "has_source_text": source_path.exists(),
                "source_text_size": source_path.stat().st_size if source_path.exists() else 0,
                "has_screenplay": (Path(project.root) / "chapters" / ch_id / "screenplay.md").exists(),
            })
        except FileNotFoundError:
            chapters.append({"chapter_id": ch_id, "title": ch_id, "status": "missing"})
    return jsonify(chapters)


# ------------------------------------------------------------------
# Budget update
# ------------------------------------------------------------------

@api_bp.route("/<slug>/settings/budgets", methods=["POST"])
def update_budgets(slug):
    """Update per-API budget limits. Expects {api_name: new_budget_usd, ...}."""
    data = request.get_json(force=True)
    project = _get_project(slug)
    updated = {}
    for api_name, new_budget in data.items():
        if api_name in project.data.get("apis", {}):
            project.data["apis"][api_name]["budget_usd"] = float(new_budget)
            updated[api_name] = float(new_budget)
    project.save_project()
    return jsonify({"status": "saved", "updated": updated})


# ------------------------------------------------------------------
# Screenplay
# ------------------------------------------------------------------

@api_bp.route("/<slug>/chapter/<chapter_id>/screenplay")
def get_screenplay(slug, chapter_id):
    """Return screenplay markdown content for a chapter."""
    project = _get_project(slug)
    sp_path = Path(project.root) / "chapters" / chapter_id / "screenplay.md"
    if not sp_path.exists():
        return jsonify({"error": "Screenplay not found", "chapter_id": chapter_id}), 404
    content = sp_path.read_text(encoding="utf-8")
    # Also return approval status from chapter schema
    try:
        chapter = project.load_chapter(chapter_id)
        sp_meta = chapter.get("screenplay", {})
    except Exception:
        sp_meta = {}
    return jsonify({
        "chapter_id": chapter_id,
        "content": content,
        "approved": sp_meta.get("approved", False),
        "status": sp_meta.get("status", "pending"),
    })


@api_bp.route("/<slug>/chapter/<chapter_id>/screenplay-review")
def get_screenplay_review(slug, chapter_id):
    """Return screenplay with audio recordings for inline playback."""
    project = _get_project(slug)
    sp_path = Path(project.root) / "chapters" / chapter_id / "screenplay.md"
    if not sp_path.exists():
        return jsonify({"error": "Screenplay not found"}), 404
    content = sp_path.read_text(encoding="utf-8")

    # Load recordings manifest
    recordings = []
    try:
        recs = project.load_recordings(chapter_id)
        for rec in recs.get("recordings", []):
            ref = rec.get("audio_ref", "")
            audio_path = Path(project.root) / ref if ref else None
            file_exists = audio_path.exists() if audio_path else False
            recordings.append({
                "recording_id": rec["recording_id"],
                "character_id": rec["character_id"],
                "text": rec["text"],
                "duration_sec": rec.get("duration_sec", 0),
                "direction": rec.get("direction", ""),
                "audio_url": f"/api/{slug}/audio/{ref}" if file_exists else "",
            })
    except FileNotFoundError:
        pass

    return jsonify({
        "chapter_id": chapter_id,
        "content": content,
        "recordings": recordings,
        "total_recordings": len(recordings),
    })


@api_bp.route("/<slug>/chapter/<chapter_id>/screenplay/approve", methods=["POST"])
def approve_screenplay(slug, chapter_id):
    """Toggle screenplay approval for a chapter."""
    project = _get_project(slug)
    try:
        chapter = project.load_chapter(chapter_id)
    except FileNotFoundError:
        return jsonify({"error": "Chapter not found"}), 404

    sp = chapter.setdefault("screenplay", {})
    sp["approved"] = not sp.get("approved", False)
    sp["status"] = "approved" if sp["approved"] else "pending"
    sp["reviewed_at"] = datetime.now().isoformat()
    project.save_chapter(chapter_id, chapter)
    return jsonify({
        "chapter_id": chapter_id,
        "approved": sp["approved"],
        "status": sp["status"],
    })


@api_bp.route("/<slug>/chapter/<chapter_id>/screenplay", methods=["PUT"])
def save_screenplay(slug, chapter_id):
    """Save edited screenplay markdown to disk."""
    project = _get_project(slug)
    data = request.get_json(force=True)
    content = data.get("content", "")
    if not content.strip():
        return jsonify({"error": "Screenplay content is empty"}), 400

    sp_path = Path(project.root) / "chapters" / chapter_id / "screenplay.md"

    # Back up current version for undo
    if sp_path.exists():
        bak_path = sp_path.with_suffix(".md.bak")
        shutil.copy2(sp_path, bak_path)

    # Write new content
    sp_path.write_text(content, encoding="utf-8")

    # Reset approval
    try:
        chapter = project.load_chapter(chapter_id)
    except FileNotFoundError:
        return jsonify({"error": "Chapter not found"}), 404

    sp_meta = chapter.setdefault("screenplay", {})
    sp_meta["approved"] = False
    sp_meta["status"] = "draft"
    sp_meta["edited_at"] = datetime.now().isoformat()
    project.save_chapter(chapter_id, chapter)

    # Check for downstream work
    shots_index = Path(project.root) / "chapters" / chapter_id / "shots" / "_index.json"
    downstream_warning = False
    if shots_index.exists():
        try:
            idx = json.loads(shots_index.read_text(encoding="utf-8"))
            downstream_warning = len(idx.get("shots", [])) > 0
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "chapter_id": chapter_id,
        "status": "draft",
        "downstream_warning": downstream_warning,
    })


@api_bp.route("/<slug>/chapter/<chapter_id>/screenplay/revise", methods=["POST"])
def revise_screenplay(slug, chapter_id):
    """Use Claude to revise a screenplay based on a director's instruction."""
    from apis.claude_client import ClaudeClient
    from core.cost_manager import CostManager

    project = _get_project(slug)
    data = request.get_json(force=True)
    instruction = data.get("instruction", "").strip()
    if not instruction:
        return jsonify({"error": "instruction is required"}), 400

    # Use content from request body if provided, else read from disk
    screenplay_text = data.get("content", "").strip()
    if not screenplay_text:
        sp_path = Path(project.root) / "chapters" / chapter_id / "screenplay.md"
        if not sp_path.exists():
            return jsonify({"error": "Screenplay not found"}), 404
        screenplay_text = sp_path.read_text(encoding="utf-8")

    # Load context
    try:
        chapter = project.load_chapter(chapter_id)
    except FileNotFoundError:
        return jsonify({"error": "Chapter not found"}), 404

    try:
        world_bible = project.load_world_bible()
    except Exception:
        world_bible = {}

    characters = []
    for cid in chapter.get("characters", {}).get("featured", []):
        try:
            characters.append(project.load_character(cid))
        except Exception:
            pass

    # Cost check
    costs = CostManager(project)
    costs.check_api_allowed("claude")
    input_tokens = int((len(screenplay_text) + len(instruction) + 500) * 1.3 / 4)
    estimated = costs.estimate_claude(input_tokens=input_tokens, output_tokens=8000)
    costs.check_budget("claude", estimated)

    # Call Claude
    with ClaudeClient() as claude:
        result = claude.revise_screenplay(
            screenplay_text=screenplay_text,
            instruction=instruction,
            chapter_outline=chapter,
            characters=characters,
            world_bible=world_bible,
        )

    actual_cost = result.get("_cost_usd", estimated)
    costs.record(
        "claude", actual_cost, "screenplay_revision",
        f"Screenplay revision for {chapter_id}: {instruction[:80]}",
        entity_id=chapter_id,
    )

    return jsonify({
        "ok": True,
        "revised": result["revised"],
        "cost_usd": actual_cost,
        "chapter_id": chapter_id,
    })


@api_bp.route("/<slug>/chapter/<chapter_id>/screenplay/undo", methods=["POST"])
def undo_screenplay(slug, chapter_id):
    """Restore the previous version of the screenplay from backup."""
    project = _get_project(slug)
    sp_path = Path(project.root) / "chapters" / chapter_id / "screenplay.md"
    bak_path = sp_path.with_suffix(".md.bak")

    if not bak_path.exists():
        return jsonify({"error": "No backup available"}), 404

    # Restore backup
    content = bak_path.read_text(encoding="utf-8")
    sp_path.write_text(content, encoding="utf-8")
    bak_path.unlink()

    # Reset to draft
    try:
        chapter = project.load_chapter(chapter_id)
        sp_meta = chapter.setdefault("screenplay", {})
        sp_meta["approved"] = False
        sp_meta["status"] = "draft"
        project.save_chapter(chapter_id, chapter)
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "content": content,
        "chapter_id": chapter_id,
    })


# ------------------------------------------------------------------
# Assets
# ------------------------------------------------------------------

@api_bp.route("/<slug>/assets/manifest")
def asset_manifest(slug):
    project = _get_project(slug)
    return jsonify(project.load_asset_manifest())


@api_bp.route("/<slug>/asset/<asset_id>/approve", methods=["POST"])
def toggle_asset_approval(slug, asset_id):
    project = _get_project(slug)
    manifest = project.load_asset_manifest()
    for category, assets in manifest.get("assets", {}).items():
        for asset in assets:
            if asset.get("asset_id") == asset_id:
                asset["approved_for_generation"] = not asset.get("approved_for_generation", False)
                project.save_asset_manifest(manifest)
                return jsonify({"asset_id": asset_id, "approved": asset["approved_for_generation"]})
    return jsonify({"error": f"Asset '{asset_id}' not found"}), 404


# ------------------------------------------------------------------
# Gates
# ------------------------------------------------------------------

@api_bp.route("/<slug>/gates/approve", methods=["POST"])
def approve_gate(slug):
    data = request.get_json(force=True)
    gate_name = data.get("gate_name")
    if not gate_name:
        return jsonify({"error": "gate_name required"}), 400

    project = _get_project(slug)
    if project.is_gate_open(gate_name):
        return jsonify({"status": "already_approved", "gate": gate_name})

    project.approve_gate(gate_name, approver="web_ui")
    return jsonify({"status": "approved", "gate": gate_name})


# ------------------------------------------------------------------
# Shot review
# ------------------------------------------------------------------

@api_bp.route("/<slug>/shot/<chapter_id>/<scene_id>/<shot_id>")
def get_shot(slug, chapter_id, scene_id, shot_id):
    project = _get_project(slug)
    try:
        shot = project.load_shot(chapter_id, scene_id, shot_id)
        return jsonify(shot)
    except FileNotFoundError:
        return jsonify({"error": "Shot not found"}), 404


@api_bp.route("/<slug>/shot/<shot_id>/review", methods=["POST"])
def review_shot(slug, shot_id):
    """Approve or reject a shot. Expects {action, notes, chapter_id, scene_id}."""
    data = request.get_json(force=True)
    action = data.get("action")  # "approve" or "reject"
    notes = data.get("notes", "")
    chapter_id = data.get("chapter_id")
    scene_id = data.get("scene_id")

    if not all([action, chapter_id, scene_id]):
        return jsonify({"error": "action, chapter_id, and scene_id required"}), 400

    project = _get_project(slug)
    try:
        shot = project.load_shot(chapter_id, scene_id, shot_id)
    except FileNotFoundError:
        return jsonify({"error": "Shot not found"}), 404

    if action == "approve":
        shot.setdefault("storyboard", {})["approved"] = True
        shot.setdefault("storyboard", {})["reviewed"] = True
    elif action == "reject":
        shot.setdefault("storyboard", {})["approved"] = False
        shot.setdefault("storyboard", {})["reviewed"] = True
        shot.setdefault("meta", {}).setdefault("flags", []).append({
            "reason": notes or "Rejected in review",
            "flagged_at": datetime.now().isoformat(),
            "flagged_by": "web_ui",
        })

    project.save_shot(chapter_id, scene_id, shot_id, shot)

    # Sync shot index and chapter production counter
    try:
        index = project.load_shot_index(chapter_id, "")
        for entry in index.get("shots", []):
            if entry.get("shot_id") == shot_id:
                entry["storyboard_approved"] = (action == "approve")
                break
        project.save_shot_index(chapter_id, index)

        approved_count = sum(
            1 for s in index.get("shots", []) if s.get("storyboard_approved")
        )
        chapter = project.load_chapter(chapter_id)
        chapter.setdefault("production", {})["shots_approved"] = approved_count
        project.save_chapter(chapter_id, chapter)
    except FileNotFoundError:
        pass

    if notes:
        project.append_shot_note(chapter_id, shot_id, notes, author="web_ui")

    return jsonify({"status": action + "d", "shot_id": shot_id})


# ------------------------------------------------------------------
# Editing Room
# ------------------------------------------------------------------

@api_bp.route("/<slug>/editing-room/<chapter_id>")
def editing_room_data(slug, chapter_id):
    """Return all shots for a chapter with edit state for the editing room."""
    from core.state_manager import StateManager
    project = _get_project(slug)
    sm = StateManager(project)
    detail = sm.get_chapter_status(chapter_id)

    shots = []
    for scene in detail.get("scenes", []):
        shot_ids = (scene.get("shots") or {}).get("shot_ids", [])
        for shot_id in shot_ids:
            scene_id = scene["scene_id"]
            try:
                shot = project.load_shot(chapter_id, scene_id, shot_id)
            except FileNotFoundError:
                continue

            edit = shot.get("edit", {})
            dialogue = shot.get("dialogue_in_shot", [])
            cin = shot.get("cinematic", {})
            audio = shot.get("audio", {})
            original_dur = shot.get("duration_sec",
                                    cin.get("duration_sec", 3))

            # Build audio line refs for inline playback
            audio_lines = []
            for line in audio.get("lines", []):
                audio_lines.append({
                    "line_id": line.get("line_id", ""),
                    "character_id": line.get("character_id", ""),
                    "text": line.get("text", ""),
                    "audio_ref": line.get("audio_ref", ""),
                    "direction": line.get("direction", ""),
                    "start_time_sec": line.get("start_time_sec"),
                    "end_time_sec": line.get("end_time_sec"),
                })
            sound_effects = [
                {
                    "sfx_id": sfx.get("sfx_id", ""),
                    "prompt": sfx.get("prompt", ""),
                    "audio_ref": sfx.get("audio_ref", ""),
                    "duration_sec": sfx.get("duration_sec", 0),
                    "offset_sec": float(sfx.get("offset_sec", 0) or 0),
                    "provider": sfx.get("provider", ""),
                }
                for sfx in audio.get("sound_effects", [])
                if sfx.get("audio_ref")
            ]

            shots.append({
                "shot_id": shot_id,
                "scene_id": scene_id,
                "label": shot.get("label", ""),
                "shot_type": cin.get("shot_type", ""),
                "characters_in_frame": [
                    c.get("character_id", c) if isinstance(c, dict) else c
                    for c in shot.get("characters_in_frame", [])
                ],
                "dialogue_in_shot": dialogue,
                "has_dialogue": len(dialogue) > 0,
                "original_duration_sec": original_dur,
                "storyboard_approved": shot.get("storyboard", {}).get("approved", False),
                "has_storyboard": bool(shot.get("storyboard", {}).get("image_ref")),
                "image_url": f"/api/{slug}/image/chapters/{chapter_id}/shots/{shot_id}/storyboard.png",
                "image_url_vertical": f"/api/{slug}/image/chapters/{chapter_id}/shots/{shot_id}/storyboard_vertical.png",
                "preview_video_url": (
                    f"/api/{slug}/video/{shot.get('preview', {}).get('video_ref')}"
                    if (shot.get("preview") or {}).get("video_ref") else None
                ),
                "preview_video_url_vertical": (
                    f"/api/{slug}/video/{shot.get('preview', {}).get('video_ref_vertical')}"
                    if (shot.get("preview") or {}).get("video_ref_vertical") else None
                ),
                "audio_status": audio.get("status", "pending"),
                "audio_lines": audio_lines,
                "sound_effects": sound_effects,
                "edit": {
                    "enabled": edit.get("enabled", True),
                    "duration_sec": edit.get("duration_sec", None),
                    "notes": edit.get("notes", ""),
                },
            })

    total_shots = len(shots)
    enabled_shots = sum(1 for s in shots if s["edit"]["enabled"])
    total_original_dur = sum(s["original_duration_sec"] for s in shots)
    total_cut_dur = sum(
        (s["edit"]["duration_sec"] or s["original_duration_sec"])
        for s in shots if s["edit"]["enabled"]
    )
    dialogue_shots_disabled = sum(
        1 for s in shots
        if not s["edit"]["enabled"] and s["has_dialogue"]
    )

    return jsonify({
        "chapter_id": chapter_id,
        "shots": shots,
        "summary": {
            "total_shots": total_shots,
            "enabled_shots": enabled_shots,
            "disabled_shots": total_shots - enabled_shots,
            "original_duration_sec": round(total_original_dur, 1),
            "cut_duration_sec": round(total_cut_dur, 1),
            "dialogue_shots_disabled": dialogue_shots_disabled,
        }
    })


@api_bp.route("/<slug>/editing-room/shot/<shot_id>/edit", methods=["POST"])
def update_shot_edit(slug, shot_id):
    """Update edit state for a single shot."""
    data = request.get_json(force=True)
    chapter_id = data.get("chapter_id")
    scene_id = data.get("scene_id")
    if not chapter_id or not scene_id:
        return jsonify({"error": "chapter_id and scene_id required"}), 400

    project = _get_project(slug)
    try:
        shot = project.load_shot(chapter_id, scene_id, shot_id)
    except FileNotFoundError:
        return jsonify({"error": "Shot not found"}), 404

    edit = shot.setdefault("edit", {})

    if "enabled" in data:
        edit["enabled"] = bool(data["enabled"])
    if "duration_sec" in data:
        edit["duration_sec"] = (
            round(float(data["duration_sec"]), 2)
            if data["duration_sec"] is not None else None
        )
    if "notes" in data:
        edit["notes"] = str(data["notes"])

    project.save_shot(chapter_id, scene_id, shot_id, shot)

    return jsonify({"status": "saved", "shot_id": shot_id, "edit": edit})


@api_bp.route("/<slug>/editing-room/<chapter_id>/batch-edit", methods=["POST"])
def batch_edit_shots(slug, chapter_id):
    """Batch update edit state for multiple shots.

    Expects {shots: [{shot_id, scene_id, enabled?, duration_sec?, notes?}, ...]}
    Or {action: "enable_all" | "disable_non_dialogue" | "reset"}
    """
    data = request.get_json(force=True)
    project = _get_project(slug)
    action = data.get("action")

    if action:
        # Bulk action across all shots
        from core.state_manager import StateManager
        sm = StateManager(project)
        detail = sm.get_chapter_status(chapter_id)
        updated = []
        for scene in detail.get("scenes", []):
            for shot_id in (scene.get("shots") or {}).get("shot_ids", []):
                scene_id = scene["scene_id"]
                try:
                    shot = project.load_shot(chapter_id, scene_id, shot_id)
                except FileNotFoundError:
                    continue

                edit = shot.setdefault("edit", {})
                if action == "enable_all":
                    edit["enabled"] = True
                    edit["duration_sec"] = None
                    edit["notes"] = ""
                elif action == "disable_non_dialogue":
                    has_dialogue = len(shot.get("dialogue_in_shot", [])) > 0
                    if not has_dialogue:
                        edit["enabled"] = False
                elif action == "reset":
                    shot.pop("edit", None)

                project.save_shot(chapter_id, scene_id, shot_id, shot)
                updated.append(shot_id)

        return jsonify({"status": "saved", "action": action, "updated": updated})

    # Per-shot batch
    shot_edits = data.get("shots", [])
    updated = []
    for se in shot_edits:
        shot_id = se.get("shot_id")
        scene_id = se.get("scene_id")
        if not shot_id or not scene_id:
            continue
        try:
            shot = project.load_shot(chapter_id, scene_id, shot_id)
        except FileNotFoundError:
            continue

        edit = shot.setdefault("edit", {})
        if "enabled" in se:
            edit["enabled"] = bool(se["enabled"])
        if "duration_sec" in se:
            edit["duration_sec"] = (
                round(float(se["duration_sec"]), 2)
                if se["duration_sec"] is not None else None
            )
        if "notes" in se:
            edit["notes"] = str(se["notes"])

        project.save_shot(chapter_id, scene_id, shot_id, shot)
        updated.append(shot_id)

    return jsonify({"status": "saved", "updated": updated})


# ------------------------------------------------------------------
# Characters / Voice casting
# ------------------------------------------------------------------

@api_bp.route("/<slug>/characters")
def list_characters(slug):
    project = _get_project(slug)
    try:
        index = project.load_character_index()
        characters = []
        for entry in index.get("characters", []):
            cid = entry.get("character_id")
            try:
                char = project.load_character(cid)
                lora_info = char.get("assets", {}).get("lora", {})
                training_dir = Path(project.root) / "characters" / cid / "training_images"
                training_count = len(list(training_dir.glob("*.png"))) if training_dir.exists() else 0
                characters.append({
                    "character_id": cid,
                    "display_name": char.get("display_name", cid),
                    "role": char.get("description", {}).get("role", char.get("role", "")),
                    "tier": char.get("tier", ""),
                    "visual_tag": char.get("visual_tag", ""),
                    "costume_default": char.get("costume_default", ""),
                    "voice_id": char.get("voice", {}).get("voice_id"),
                    "has_voice": bool(char.get("voice", {}).get("voice_id")),
                    "has_reference_image": (Path(project.root) / "characters" / cid / "reference.png").exists(),
                    "has_training_images": training_count > 0,
                    "training_images_count": training_count,
                    "has_lora": bool(
                        lora_info.get("file")
                        and (Path(project.root) / lora_info["file"]).exists()
                    ),
                    "lora_trigger_word": lora_info.get("trigger_word", ""),
                })
            except FileNotFoundError:
                characters.append({"character_id": cid, "display_name": cid, "has_voice": False})
        return jsonify(characters)
    except FileNotFoundError:
        return jsonify([])


@api_bp.route("/<slug>/character/<char_id>")
def get_character(slug, char_id):
    project = _get_project(slug)
    try:
        return jsonify(project.load_character(char_id))
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404


@api_bp.route("/<slug>/character/<char_id>", methods=["PUT"])
def update_character(slug, char_id):
    """Update any character fields. Syncs visual_tags.json and _index.json."""
    updates = request.get_json(force=True)

    project = _get_project(slug)
    try:
        char = project.load_character(char_id)
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404

    # Apply updates — supports nested fields via dot-free top-level keys
    ALLOWED_TOP = {
        "display_name", "visual_tag", "costume_default",
        "description", "narrative", "voice", "animation", "assets",
    }
    for key, val in updates.items():
        if key not in ALLOWED_TOP:
            continue
        if isinstance(val, dict) and isinstance(char.get(key), dict):
            # Merge sub-keys (e.g. description.age, description.role)
            char[key].update(val)
        else:
            char[key] = val

    project.save_character(char_id, char)

    # Sync visual_tags.json if visual_tag or costume_default changed
    vt_path = Path(project.root) / "characters" / "visual_tags.json"
    if vt_path.exists():
        try:
            vt_data = project._load(vt_path)
            chars = vt_data.get("characters", {})
            if char_id in chars:
                if "visual_tag" in updates:
                    chars[char_id]["visual_tag"] = char["visual_tag"]
                if "costume_default" in updates:
                    chars[char_id]["costume_default"] = char["costume_default"]
                if "display_name" in updates:
                    chars[char_id]["display_name"] = char["display_name"]
                project._save(vt_path, vt_data)
        except Exception:
            pass

    # Sync _index.json if display_name, role, or visual_tag changed
    idx_path = Path(project.root) / "characters" / "_index.json"
    if idx_path.exists():
        try:
            idx = project._load(idx_path)
            for entry in idx.get("characters", []):
                if entry.get("character_id") == char_id:
                    if "display_name" in updates:
                        entry["display_name"] = char["display_name"]
                    if "visual_tag" in updates:
                        entry["visual_tag"] = char["visual_tag"]
                    desc_updates = updates.get("description", {})
                    if isinstance(desc_updates, dict) and "role" in desc_updates:
                        entry["role"] = char.get("description", {}).get("role", "")
                    break
            project._save(idx_path, idx)
        except Exception:
            pass

    return jsonify({"ok": True, "character_id": char_id})


@api_bp.route("/<slug>/character/<char_id>/ai-edit", methods=["POST"])
def ai_edit_character(slug, char_id):
    """Use Claude to revise a character based on natural-language feedback."""
    from apis.claude_client import ClaudeClient
    from core.cost_manager import CostManager

    data = request.get_json(force=True)
    feedback = data.get("feedback", "").strip()
    if not feedback:
        return jsonify({"error": "feedback is required"}), 400

    project = _get_project(slug)
    try:
        char = project.load_character(char_id)
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404

    # Load world bible for setting context
    try:
        world_bible = project.load_world_bible()
    except FileNotFoundError:
        world_bible = {}

    # Build scene context — find scenes this character appears in
    scene_context_parts = []
    chapters = (char.get("narrative", {}).get("chapters", []))
    for ch_id in chapters[:4]:  # cap at 4 chapters to limit tokens
        try:
            chapter = project.load_chapter(ch_id)
        except FileNotFoundError:
            continue
        scene_ids = []
        for act in chapter.get("structure", {}).get("acts", []):
            scene_ids.extend(act.get("scene_ids", []))
        for sid in scene_ids:
            try:
                scene = project.load_scene(ch_id, sid)
                present = scene.get("characters", {}).get("present", [])
                present_ids = [c.get("character_id", c) if isinstance(c, dict) else c for c in present]
                if char_id in present_ids:
                    loc = scene.get("location", {})
                    loc_name = loc.get("name", "") if isinstance(loc, dict) else str(loc)
                    title = scene.get("title", sid)
                    summary = scene.get("summary", scene.get("description", ""))
                    scene_context_parts.append(
                        f"[{sid}] {title} — Location: {loc_name}\n  {summary[:300]}"
                    )
            except FileNotFoundError:
                continue
    scene_context = "\n".join(scene_context_parts[:12])  # cap scenes

    # Cost check
    costs = CostManager(project)
    estimated = costs.estimate_claude(
        input_tokens=int((len(json.dumps(char)) + len(scene_context) + 500) * 1.3 / 4),
        output_tokens=3000,
    )
    costs.check_api_allowed("claude")
    costs.check_budget("claude", estimated)

    # Call Claude
    with ClaudeClient() as claude:
        result = claude.revise_character(
            character=char,
            feedback=feedback,
            world_bible=world_bible,
            scene_context=scene_context,
        )

    cost = result.pop("_cost_usd", estimated)
    costs.record("claude", cost, "character_revision",
                 f"AI revision for {char.get('display_name', char_id)}: {feedback[:80]}",
                 entity_id=char_id)

    # Preserve fields Claude shouldn't overwrite
    result["character_id"] = char_id
    if char.get("assets", {}).get("reference_image"):
        result.setdefault("assets", {})["reference_image"] = char["assets"]["reference_image"]
    if char.get("voice", {}).get("elevenlabs_voice_id"):
        result.setdefault("voice", {})["elevenlabs_voice_id"] = char["voice"]["elevenlabs_voice_id"]

    project.save_character(char_id, result)

    # Sync visual_tags.json
    vt_path = Path(project.root) / "characters" / "visual_tags.json"
    if vt_path.exists():
        try:
            vt_data = project._load(vt_path)
            chars_vt = vt_data.get("characters", {})
            if char_id in chars_vt:
                chars_vt[char_id]["visual_tag"] = result.get("visual_tag", "")
                chars_vt[char_id]["costume_default"] = result.get("costume_default", "")
                chars_vt[char_id]["display_name"] = result.get("display_name", "")
                project._save(vt_path, vt_data)
        except Exception:
            pass

    # Sync _index.json
    idx_path = Path(project.root) / "characters" / "_index.json"
    if idx_path.exists():
        try:
            idx = project._load(idx_path)
            for entry in idx.get("characters", []):
                if entry.get("character_id") == char_id:
                    entry["display_name"] = result.get("display_name", "")
                    entry["visual_tag"] = result.get("visual_tag", "")
                    entry["role"] = result.get("description", {}).get("role", "")
                    break
            project._save(idx_path, idx)
        except Exception:
            pass

    return jsonify({"ok": True, "character": result, "cost_usd": cost})


@api_bp.route("/<slug>/characters/generate-missing-visuals", methods=["POST"])
def generate_missing_visuals(slug):
    """Generate visual tags for all characters that have empty visual_tag fields."""
    from apis.claude_client import ClaudeClient
    from core.cost_manager import CostManager

    project = _get_project(slug)

    # Find characters with empty visual_tag
    try:
        index = project.load_character_index()
    except FileNotFoundError:
        return jsonify({"error": "No character index found"}), 404

    stub_chars = []
    existing_visuals = {}
    for entry in index.get("characters", []):
        cid = entry.get("character_id")
        try:
            char = project.load_character(cid)
        except FileNotFoundError:
            continue
        if not char.get("visual_tag", "").strip():
            stub_chars.append(char)
        else:
            existing_visuals[cid] = {
                "display_name": char.get("display_name", cid),
                "visual_tag": char.get("visual_tag", ""),
                "costume_default": char.get("costume_default", ""),
            }

    if not stub_chars:
        return jsonify({"ok": True, "updated": 0, "message": "All characters already have visual tags"})

    # Load world bible for context
    try:
        world_bible = project.load_world_bible()
    except FileNotFoundError:
        world_bible = {}

    # Load source text for context clues
    source_text = _load_source_text(project)

    # Cost check
    costs = CostManager(project)
    estimated = costs.estimate_claude(
        input_tokens=int((len(source_text[:30000]) + 2000) * 1.3 / 4),
        output_tokens=len(stub_chars) * 500,
    )
    costs.check_api_allowed("claude")
    costs.check_budget("claude", estimated)

    # Extract screenplay dialogue for each stub character
    stub_ids = [ch["character_id"] for ch in stub_chars]
    screenplay_context = _extract_screenplay_context(project, stub_ids)

    # Batch characters in groups of 8 to avoid token limit / truncation issues.
    # 23 characters at ~300 tokens each = ~7000 output tokens, which can exceed
    # reliable JSON generation limits. Batches of 8 keep output under ~3000 tokens.
    BATCH_SIZE = 8
    generated = {}
    total_cost = 0.0

    with ClaudeClient() as claude:
        for batch_start in range(0, len(stub_chars), BATCH_SIZE):
            batch = stub_chars[batch_start:batch_start + BATCH_SIZE]
            batch_ids = [ch["character_id"] for ch in batch]
            batch_context = {k: v for k, v in screenplay_context.items() if k in batch_ids}

            try:
                result = claude.generate_stub_character_visuals(
                    stub_characters=batch,
                    world_bible=world_bible,
                    existing_visuals=existing_visuals,
                    source_text=source_text,
                    screenplay_context=batch_context,
                )
                batch_cost = result.pop("_cost_usd", 0.0)
                total_cost += batch_cost
                batch_generated = result.get("characters", {})
                generated.update(batch_generated)

                # Add successfully generated chars to existing_visuals for contrast
                for cid, gen in batch_generated.items():
                    if gen.get("visual_tag"):
                        existing_visuals[cid] = {
                            "display_name": gen.get("display_name", cid),
                            "visual_tag": gen["visual_tag"],
                            "costume_default": gen.get("costume_default", ""),
                        }
            except Exception as e:
                current_app.logger.error(f"Batch visual gen failed: {e}")
                # Continue with next batch rather than failing entirely
                continue

    cost = total_cost

    # Apply generated visuals to each stub character
    updated_ids = []
    for char in stub_chars:
        cid = char["character_id"]
        gen = generated.get(cid)
        if not gen or not gen.get("visual_tag"):
            continue

        # Merge generated fields into existing character
        char["visual_tag"] = gen["visual_tag"]
        char["costume_default"] = gen.get("costume_default", char.get("costume_default", ""))
        if gen.get("display_name"):
            char["display_name"] = gen["display_name"]

        # Merge description fields (don't overwrite existing non-empty ones)
        gen_desc = gen.get("description", {})
        char_desc = char.get("description", {})
        if isinstance(char_desc, str):
            char_desc = {"role": char_desc}
        for key in ("role", "archetype", "physical_appearance", "age", "personality_traits"):
            if gen_desc.get(key) and not char_desc.get(key):
                char_desc[key] = gen_desc[key]
        char["description"] = char_desc

        # Merge animation if empty
        if not char.get("animation") and gen.get("animation"):
            char["animation"] = gen["animation"]

        project.save_character(cid, char)
        updated_ids.append(cid)

    # Sync visual_tags.json
    vt_path = Path(project.root) / "characters" / "visual_tags.json"
    if vt_path.exists():
        try:
            vt_data = project._load(vt_path)
            chars_vt = vt_data.get("characters", {})
            for cid in updated_ids:
                char = project.load_character(cid)
                chars_vt[cid] = {
                    "display_name": char.get("display_name", cid),
                    "visual_tag": char.get("visual_tag", ""),
                    "costume_default": char.get("costume_default", ""),
                }
            vt_data["characters"] = chars_vt
            project._save(vt_path, vt_data)
        except Exception:
            pass

    # Sync _index.json
    idx_path = Path(project.root) / "characters" / "_index.json"
    if idx_path.exists():
        try:
            idx = project._load(idx_path)
            for entry in idx.get("characters", []):
                cid = entry.get("character_id")
                if cid in updated_ids:
                    char = project.load_character(cid)
                    entry["display_name"] = char.get("display_name", cid)
                    entry["visual_tag"] = char.get("visual_tag", "")
                    entry["role"] = char.get("description", {}).get("role", "")
            project._save(idx_path, idx)
        except Exception:
            pass

    costs.record("claude", cost, "character_visuals",
                 f"Generated visual tags for {len(updated_ids)} stub characters",
                 entity_id="batch_visuals")

    return jsonify({
        "ok": True,
        "updated": len(updated_ids),
        "character_ids": updated_ids,
        "cost_usd": cost,
    })


@api_bp.route("/<slug>/characters/regenerate-visuals", methods=["POST"])
def regenerate_character_visuals(slug):
    """Regenerate visual tags for characters that already have them.

    Unlike generate-missing-visuals which fills blanks, this endpoint
    REPLACES existing visual_tag, costume_default, and description.role
    using the screenplay dialogue as ground truth. Use this to fix
    characters whose visual identity is wrong (e.g., wrong occupation,
    wrong gender, non-period clothing).

    Request JSON:
        character_ids: list of character_ids to regenerate (optional — all if omitted)
    """
    from apis.claude_client import ClaudeClient
    from core.cost_manager import CostManager

    project = _get_project(slug)
    data = request.get_json(force=True) if request.content_length else {}
    requested_ids = data.get("character_ids", [])

    # Load all characters
    try:
        index = project.load_character_index()
    except FileNotFoundError:
        return jsonify({"error": "No character index found"}), 404

    chars_to_regen = []
    for entry in index.get("characters", []):
        cid = entry.get("character_id")
        if requested_ids and cid not in requested_ids:
            continue
        try:
            char = project.load_character(cid)
        except FileNotFoundError:
            continue
        chars_to_regen.append(char)

    if not chars_to_regen:
        return jsonify({"ok": True, "updated": 0, "message": "No characters to regenerate"})

    # Load world bible
    try:
        world_bible = project.load_world_bible()
    except FileNotFoundError:
        world_bible = {}

    # Load source text
    source_text = _load_source_text(project)

    # Extract screenplay dialogue for each character
    regen_ids = [ch["character_id"] for ch in chars_to_regen]
    screenplay_context = _extract_screenplay_context(project, regen_ids)

    # Cost check
    costs = CostManager(project)
    estimated = costs.estimate_claude(
        input_tokens=int((len(source_text[:30000]) + 3000) * 1.3 / 4),
        output_tokens=len(chars_to_regen) * 400,
    )
    costs.check_api_allowed("claude")
    costs.check_budget("claude", estimated)

    # Call Claude
    with ClaudeClient() as claude:
        result = claude.regenerate_character_visuals(
            characters=chars_to_regen,
            world_bible=world_bible,
            source_text=source_text,
            screenplay_context=screenplay_context,
        )

    cost = result.pop("_cost_usd", estimated)
    generated = result.get("characters", {})

    # Apply regenerated visuals — this time we OVERWRITE, not just fill blanks
    updated_ids = []
    for char in chars_to_regen:
        cid = char["character_id"]
        gen = generated.get(cid)
        if not gen or not gen.get("visual_tag"):
            continue

        # Overwrite visual fields
        char["visual_tag"] = gen["visual_tag"]
        if gen.get("costume_default"):
            char["costume_default"] = gen["costume_default"]
        if gen.get("display_name"):
            char["display_name"] = gen["display_name"]

        # Overwrite description fields (replace, not merge)
        gen_desc = gen.get("description", {})
        char_desc = char.get("description", {})
        if isinstance(char_desc, str):
            char_desc = {"role": char_desc}
        for key in ("role", "archetype", "physical_appearance", "age", "personality_traits"):
            if gen_desc.get(key):
                char_desc[key] = gen_desc[key]
        char["description"] = char_desc

        project.save_character(cid, char)
        updated_ids.append(cid)

    # Sync visual_tags.json
    vt_path = Path(project.root) / "characters" / "visual_tags.json"
    if vt_path.exists():
        try:
            vt_data = project._load(vt_path)
            chars_vt = vt_data.get("characters", {})
            for cid in updated_ids:
                char = project.load_character(cid)
                chars_vt[cid] = {
                    "display_name": char.get("display_name", cid),
                    "visual_tag": char.get("visual_tag", ""),
                    "costume_default": char.get("costume_default", ""),
                }
            vt_data["characters"] = chars_vt
            project._save(vt_path, vt_data)
        except Exception:
            pass

    # Sync _index.json
    idx_path = Path(project.root) / "characters" / "_index.json"
    if idx_path.exists():
        try:
            idx = project._load(idx_path)
            for entry in idx.get("characters", []):
                cid = entry.get("character_id")
                if cid in updated_ids:
                    char = project.load_character(cid)
                    entry["display_name"] = char.get("display_name", cid)
                    entry["visual_tag"] = char.get("visual_tag", "")
                    entry["role"] = char.get("description", {}).get("role", "")
            project._save(idx_path, idx)
        except Exception:
            pass

    costs.record("claude", cost, "character_visuals_regen",
                 f"Regenerated visual tags for {len(updated_ids)} characters",
                 entity_id="batch_regen")

    return jsonify({
        "ok": True,
        "updated": len(updated_ids),
        "character_ids": updated_ids,
        "cost_usd": cost,
    })


@api_bp.route("/<slug>/character/<char_id>/generate-reference", methods=["POST"])
def generate_character_reference(slug, char_id):
    """Generate a reference portrait image for a character."""
    data = request.get_json(force=True)
    prompt_override = data.get("prompt_override", "").strip()
    feedback = data.get("feedback", "").strip()

    project = _get_project(slug)
    try:
        char = project.load_character(char_id)
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404

    # Build prompt
    if prompt_override:
        prompt = prompt_override
    else:
        from apis.prompt_builder import build_character_reference_prompt
        from apis.claude_client import ClaudeClient
        visual_style = ""
        world_context = {}
        try:
            wb = project.load_world_bible()
            bible = wb.get("world_bible", wb)
            visual_style = bible.get("visual_style", "")
            world_context = ClaudeClient._extract_world_context(wb)
        except FileNotFoundError:
            pass
        prompt = build_character_reference_prompt(
            char, visual_style, world_context=world_context
        )

    if feedback:
        prompt += f". Additional direction: {feedback}"

    # Cost check & provider selection: prefer ComfyUI (local, free) → Stability
    from core.cost_manager import CostManager
    costs = CostManager(project)

    use_comfyui = False
    if project.is_api_enabled("comfyui"):
        from apis.comfyui import ComfyUIClient
        comfyui_url = project.get_api_config("comfyui").get("url")
        if ComfyUIClient.is_available(comfyui_url):
            use_comfyui = True

    output_path = str(Path(project.root) / "characters" / char_id / "reference.png")

    if use_comfyui:
        api_name = "comfyui"
        try:
            comfyui_cfg = project.get_api_config("comfyui")
            with ComfyUIClient(base_url=comfyui_cfg.get("url"), checkpoint=comfyui_cfg.get("checkpoint")) as client:
                result = client.generate_character_reference(prompt, output_path)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        api_name = "stabilityai"
        costs.check_api_allowed("stabilityai")
        costs.check_budget("stabilityai", 0.04)
        from apis.stability import StabilityClient
        try:
            with StabilityClient() as client:
                result = client.generate_character_reference(prompt, output_path)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Record cost (skip for free local generation)
    if result["cost_usd"] > 0:
        costs.record(api_name, result["cost_usd"], "character_reference",
                      f"Reference image for {char.get('display_name', char_id)}",
                      entity_id=char_id)

    # Save reference path to character
    char.setdefault("assets", {})["reference_image"] = f"characters/{char_id}/reference.png"
    project.save_character(char_id, char)

    return jsonify({
        "status": "generated",
        "character_id": char_id,
        "provider": api_name,
        "cost_usd": result["cost_usd"],
        "prompt_used": prompt[:200],
    })


@api_bp.route("/<slug>/character/<char_id>/reference-image")
def serve_character_reference(slug, char_id):
    """Serve the character reference PNG."""
    project = _get_project(slug)
    ref_path = Path(project.root) / "characters" / char_id / "reference.png"
    if not ref_path.exists():
        return jsonify({"error": "No reference image"}), 404
    return send_file(str(ref_path), mimetype="image/png")


@api_bp.route("/<slug>/character/<char_id>/training-images")
def list_training_images(slug, char_id):
    """List all training sheet images for a character."""
    project = _get_project(slug)
    training_dir = Path(project.root) / "characters" / char_id / "training_images"
    if not training_dir.exists():
        return jsonify([])
    images = sorted(training_dir.glob("*.png"))
    result = []
    for img in images:
        caption_file = img.with_suffix(".txt")
        caption = ""
        if caption_file.exists():
            caption = caption_file.read_text(encoding="utf-8").strip()
        # Derive a human label from filename: kobbi_full_body_front_default -> full body front (default)
        label = img.stem
        if label.startswith(char_id + "_"):
            label = label[len(char_id) + 1:]
        label = label.replace("_", " ")
        result.append({
            "filename": img.name,
            "label": label,
            "caption": caption,
        })
    return jsonify(result)


@api_bp.route("/<slug>/character/<char_id>/training-image/<filename>")
def serve_training_image(slug, char_id, filename):
    """Serve a single training sheet image."""
    project = _get_project(slug)
    img_path = Path(project.root) / "characters" / char_id / "training_images" / filename
    if not img_path.exists():
        return jsonify({"error": "Image not found"}), 404
    # Security: ensure the resolved path is within the training_images dir
    training_dir = Path(project.root) / "characters" / char_id / "training_images"
    if not img_path.resolve().is_relative_to(training_dir.resolve()):
        return jsonify({"error": "Invalid path"}), 403
    return send_file(str(img_path), mimetype="image/png")


@api_bp.route("/<slug>/character/<char_id>/lora-status")
def character_lora_status(slug, char_id):
    """Check LoRA status: local file, ComfyUI deployment, metadata."""
    project = _get_project(slug)
    try:
        char = project.load_character(char_id)
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404

    lora_info = char.get("assets", {}).get("lora", {})
    trigger_word = lora_info.get("trigger_word", "")
    safetensors_name = f"{trigger_word}.safetensors" if trigger_word else ""

    # Check local file
    local_file = lora_info.get("file", "")
    local_exists = False
    local_size_mb = 0
    if local_file:
        local_path = Path(project.root) / local_file
        if local_path.exists() and local_path.stat().st_size > 1000:
            local_exists = True
            local_size_mb = round(local_path.stat().st_size / 1024 / 1024, 1)

    # Check ComfyUI
    in_comfyui = False
    comfyui_available = False
    if project.is_api_enabled("comfyui") and safetensors_name:
        from apis.comfyui import ComfyUIClient
        comfyui_url = project.get_api_config("comfyui").get("url")
        if ComfyUIClient.is_available(comfyui_url):
            comfyui_available = True
            try:
                with ComfyUIClient(base_url=comfyui_url) as client:
                    available = client.get_available_loras()
                    in_comfyui = safetensors_name in available
            except Exception:
                pass

    # Training images count
    training_dir = Path(project.root) / "characters" / char_id / "training_images"
    training_count = len(list(training_dir.glob("*.png"))) if training_dir.exists() else 0

    return jsonify({
        "character_id": char_id,
        "trigger_word": trigger_word,
        "safetensors_name": safetensors_name,
        "local_exists": local_exists,
        "local_size_mb": local_size_mb,
        "in_comfyui": in_comfyui,
        "comfyui_available": comfyui_available,
        "trained_at": lora_info.get("trained_at", ""),
        "deployed_to": lora_info.get("deployed_to", ""),
        "training_images_count": training_count,
        "ready_to_train": training_count >= 10,
    })


@api_bp.route("/<slug>/shot/<shot_id>/regenerate", methods=["POST"])
def regenerate_shot(slug, shot_id):
    """Regenerate a single shot's storyboard image with optional feedback."""
    data = request.get_json(force=True)
    chapter_id = data.get("chapter_id")
    scene_id = data.get("scene_id")
    feedback = data.get("feedback", "").strip()
    provider_choice = data.get("provider", "auto")
    visual_style_override = data.get("visual_style_override")  # None = use project default
    seed_override = data.get("seed")  # None = random, int = fixed seed

    if not all([chapter_id, scene_id]):
        return jsonify({"error": "chapter_id and scene_id required"}), 400

    project = _get_project(slug)
    try:
        shot = project.load_shot(chapter_id, scene_id, shot_id)
    except FileNotFoundError:
        return jsonify({"error": "Shot not found"}), 404

    # Load character visuals (same pattern as StoryboardStage)
    character_visuals = _load_character_visuals(project)

    # Style handling: user overrides (from dropdown) are used as-is.
    # World bible style is adapted for storyboard (strip photorealistic
    # medium, prepend pen/ink/marker, keep scene elements).
    from apis.prompt_builder import adapt_style_for_storyboard
    if visual_style_override is not None:
        visual_style = visual_style_override  # user chose a preset — use directly
    else:
        visual_style = ""
        try:
            wb = project.load_world_bible()
            bible = wb.get("world_bible", wb)
            visual_style = bible.get("visual_style", "")
        except FileNotFoundError:
            pass
        visual_style = adapt_style_for_storyboard(visual_style)

    # Cost manager for budget checks
    from core.cost_manager import CostManager
    costs = CostManager(project)

    # If feedback provided, use Claude to rewrite the shot prompt + composition notes
    if feedback:
        costs.check_api_allowed("claude")
        costs.check_budget("claude", 0.02)

        # Get character display names for the rewriter
        char_names = []
        for entry in shot.get("characters_in_frame", []):
            cid = entry.get("character_id", "").lower() if isinstance(entry, dict) else str(entry).lower()
            vis = character_visuals.get(cid)
            if vis:
                char_names.append(vis.get("display_name", cid.title()))

        from apis.claude_client import ClaudeClient
        with ClaudeClient() as claude:
            rewrite = claude.rewrite_shot_prompt(shot, feedback, char_names)

        claude_cost = rewrite.pop("_cost_usd", 0.01)
        costs.record("claude", claude_cost, "prompt_rewrite",
                      f"Rewrite {shot_id}: {feedback[:80]}", entity_id=chapter_id)

        # Update shot data with rewritten fields
        if rewrite.get("storyboard_prompt"):
            shot.setdefault("storyboard", {})["storyboard_prompt"] = rewrite["storyboard_prompt"]
        if rewrite.get("composition_notes"):
            shot.setdefault("cinematic", {})["composition_notes"] = rewrite["composition_notes"]

    # Build enriched prompt from (potentially rewritten) shot data
    from apis.prompt_builder import build_storyboard_prompt, build_negative_prompt
    base_prompt = shot.get("storyboard", {}).get("storyboard_prompt", "")
    prompt = build_storyboard_prompt(base_prompt, shot, character_visuals, visual_style)

    # Pick image provider based on user choice or auto-select
    _comfyui_cfg = project.get_api_config("comfyui")

    if provider_choice == "comfyui":
        api_name = "comfyui"
    elif provider_choice == "stabilityai":
        api_name = "stabilityai"
        costs.check_api_allowed("stabilityai")
        costs.check_budget("stabilityai", 0.08)
    elif provider_choice == "google_imagen":
        api_name = "google_imagen"
        costs.check_api_allowed("google_imagen")
        costs.check_budget("google_imagen", 0.08)
    else:
        # "auto" — prefer ComfyUI (local, free) → Imagen → Stability
        api_name = None
        if project.is_api_enabled("comfyui"):
            from apis.comfyui import ComfyUIClient
            if ComfyUIClient.is_available(_comfyui_cfg.get("url")):
                api_name = "comfyui"

        if not api_name and project.is_api_enabled("google_imagen"):
            try:
                costs.check_api_allowed("google_imagen")
                costs.check_budget("google_imagen", 0.08)
                api_name = "google_imagen"
            except Exception:
                pass

        if not api_name:
            api_name = "stabilityai"
            costs.check_api_allowed("stabilityai")
            costs.check_budget("stabilityai", 0.08)

    # Build the image client with correct config
    def _make_client():
        if api_name == "comfyui":
            from apis.comfyui import ComfyUIClient
            return ComfyUIClient(base_url=_comfyui_cfg.get("url"), checkpoint=_comfyui_cfg.get("checkpoint"))
        elif api_name == "google_imagen":
            from apis.google_imagen import GoogleImagenClient
            return GoogleImagenClient()
        else:
            from apis.stability import StabilityClient
            return StabilityClient()

    # Generate 16:9 + 9:16
    output_path = str(Path(project.root) / "chapters" / chapter_id / "shots" / shot_id / "storyboard.png")
    vertical_path = output_path.replace("storyboard.png", "storyboard_vertical.png")

    # Resolve seed: explicit override > project common_seed > random
    gen_seed = None
    if seed_override is not None:
        try:
            gen_seed = int(seed_override)
        except (TypeError, ValueError):
            pass
    elif api_name == "comfyui":
        comfyui_cfg = project.get_api_config("comfyui")
        gen_seed = comfyui_cfg.get("common_seed")

    # Build gender-aware negative prompt
    from apis.prompt_builder import gender_negative_terms
    _gneg = gender_negative_terms(shot, character_visuals)
    neg = build_negative_prompt(_gneg)

    try:
        with _make_client() as client:
            result_h = client.generate_storyboard(prompt, output_path, seed=gen_seed, negative_prompt=neg)
            result_v = client.generate_storyboard_vertical(prompt, vertical_path, seed=gen_seed, negative_prompt=neg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    total_cost = result_h["cost_usd"] + result_v["cost_usd"]
    if total_cost > 0:  # Skip ledger for free local generation (ComfyUI)
        costs.record(api_name, total_cost, "storyboard",
                      f"Regenerate {shot_id}", entity_id=chapter_id)

    # Update shot metadata
    from apis.prompt_builder import DEFAULT_VISUAL_STYLE
    sb = shot.setdefault("storyboard", {})
    sb["generated"] = True
    sb["approved"] = False
    sb["reviewed"] = False
    sb["regenerated_at"] = datetime.now().isoformat()
    sb["regeneration_feedback"] = feedback
    used_seed = result_h.get("seed")
    gen_meta = {
        "provider": result_h.get("provider", api_name),
        "model": result_h.get("model", ""),
        "final_prompt": prompt,
        "negative_prompt": neg,
        "visual_style": visual_style.strip() or DEFAULT_VISUAL_STYLE,
        "generated_at": datetime.now().isoformat(),
        "cost_usd": total_cost,
    }
    if used_seed is not None:
        gen_meta["seed"] = used_seed
    sb["generation_meta"] = gen_meta
    project.save_shot(chapter_id, scene_id, shot_id, shot)

    if feedback:
        project.append_shot_note(chapter_id, shot_id,
                                  f"Regeneration feedback: {feedback}", author="web_ui")

    return jsonify({
        "status": "regenerated",
        "shot_id": shot_id,
        "cost_usd": total_cost,
        "provider": result_h.get("provider", api_name),
        "model": result_h.get("model", ""),
        "seed": used_seed,
    })


@api_bp.route("/<slug>/comfyui/common-seed", methods=["GET", "POST"])
def comfyui_common_seed(slug):
    """Get or set the ComfyUI common seed for reproducible generation."""
    project = _get_project(slug)
    comfyui_cfg = project.get_api_config("comfyui")

    if request.method == "GET":
        return jsonify({"common_seed": comfyui_cfg.get("common_seed")})

    data = request.get_json(force=True)
    new_seed = data.get("common_seed")
    if new_seed is not None:
        try:
            new_seed = int(new_seed)
        except (TypeError, ValueError):
            return jsonify({"error": "Seed must be an integer or null"}), 400

    # Update project.json
    proj_data = project._load(Path(project.root) / "project.json")
    proj_data.setdefault("apis", {}).setdefault("comfyui", {})["common_seed"] = new_seed
    project._save(Path(project.root) / "project.json", proj_data)
    return jsonify({"ok": True, "common_seed": new_seed})


@api_bp.route("/<slug>/storyboard/providers")
def storyboard_providers(slug):
    """Return available image generation providers and visual style presets."""
    project = _get_project(slug)
    from apis.prompt_builder import DEFAULT_VISUAL_STYLE

    providers = []
    if project.is_api_enabled("comfyui"):
        from apis.comfyui import ComfyUIClient
        comfyui_cfg = project.get_api_config("comfyui")
        if ComfyUIClient.is_available(comfyui_cfg.get("url")):
            providers.append({
                "id": "comfyui",
                "label": "ComfyUI Local (Free)",
                "model": "SDXL (local)",
                "cost_per_image": 0.0,
            })
    if project.is_api_enabled("stabilityai"):
        providers.append({
            "id": "stabilityai",
            "label": "Stability AI / SD3.5 Medium",
            "model": "sd3.5-medium",
            "cost_per_image": 0.035,
        })
    if project.is_api_enabled("google_imagen"):
        providers.append({
            "id": "google_imagen",
            "label": "Google Imagen 4.0",
            "model": "imagen-4.0-generate-001",
            "cost_per_image": 0.04,
        })

    # Storyboard style presets.  The first entry (value="") means "use
    # default" — the adapter will combine pen/ink/marker medium with
    # scene elements from the world bible.  Other presets are full
    # overrides that skip the adapter entirely.
    return jsonify({
        "providers": providers,
        "visual_style_presets": [
            {"id": "default", "label": "Pen, Ink & Marker (Default)",
             "value": ""},
            {"id": "painted", "label": "Oil Painting",
             "value": "Oil painting style, warm rich colors, textured brushwork, "
                      "classical composition, dramatic chiaroscuro lighting, fine art quality"},
            {"id": "watercolor", "label": "Watercolor",
             "value": "Soft watercolor illustration, delicate washes, gentle color transitions, "
                      "loose brushwork, artistic quality"},
            {"id": "cinematic", "label": "Cinematic Realism",
             "value": "Cinematic film still, anamorphic lens, shallow depth of field, "
                      "natural lighting, film grain, ARRI Alexa look"},
        ],
    })


def _load_character_visuals(project) -> dict:
    """Load character visual data for prompt building (mirrors StoryboardStage pattern)."""
    character_visuals = {}
    project_visuals = Path(project.root) / "characters" / "visual_tags.json"
    if project_visuals.exists():
        try:
            character_visuals = project._load(project_visuals).get("characters", {})
        except Exception:
            pass

    # Enrich with full character data
    for cid in list(character_visuals.keys()):
        try:
            full_char = project.load_character(cid)
            desc = full_char.get("description", {})
            assets = full_char.get("assets", {})
            if desc.get("age"):
                character_visuals[cid]["age"] = desc["age"]
            if assets.get("signature_props"):
                character_visuals[cid]["signature_props"] = assets["signature_props"]
            if assets.get("costume_variants"):
                character_visuals[cid]["costume_variants"] = assets["costume_variants"]
        except (FileNotFoundError, KeyError):
            pass

    return character_visuals


@api_bp.route("/<slug>/voices/library")
def voice_library(slug):
    """Fetch available ElevenLabs voices for casting."""
    project = _get_project(slug)

    if not project.is_api_enabled("elevenlabs"):
        return jsonify({"error": "ElevenLabs API not enabled"}), 400

    try:
        from apis.elevenlabs import ElevenLabsClient
        with ElevenLabsClient() as client:
            voices = client.list_voices()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Return a lightweight list with the fields the UI needs
    results = []
    for v in voices:
        results.append({
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "category": v.get("category", ""),
            "description": v.get("description", ""),
            "labels": v.get("labels", {}),
            "preview_url": v.get("preview_url", ""),
        })

    # Sort: premade first, then by name
    results.sort(key=lambda v: (0 if v["category"] == "premade" else 1, v["name"].lower()))
    return jsonify(results)


@api_bp.route("/<slug>/voices/shared")
def voice_library_shared(slug):
    """Search the ElevenLabs shared voice library."""
    project = _get_project(slug)

    if not project.is_api_enabled("elevenlabs"):
        return jsonify({"error": "ElevenLabs API not enabled"}), 400

    params = {}
    for key in ("search", "gender", "age", "accent", "language", "use_cases", "category", "page_size"):
        val = request.args.get(key)
        if val:
            params[key] = val

    try:
        from apis.elevenlabs import ElevenLabsClient
        with ElevenLabsClient() as client:
            voices = client.search_shared_voices(params)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    results = []
    for v in voices:
        results.append({
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "category": v.get("category", ""),
            "description": v.get("description", ""),
            "labels": v.get("labels", {}),
            "preview_url": v.get("preview_url", ""),
        })

    return jsonify(results)


@api_bp.route("/<slug>/voice/<char_id>/match", methods=["POST"])
def match_voices(slug, char_id):
    """Use Claude to rank ElevenLabs voices against a character profile."""
    project = _get_project(slug)

    try:
        char = project.load_character(char_id)
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404

    # Need both APIs
    from core.cost_manager import CostManager
    costs = CostManager(project)
    costs.check_api_allowed("claude")
    costs.check_budget("claude", 0.03)

    if not project.is_api_enabled("elevenlabs"):
        return jsonify({"error": "ElevenLabs API not enabled"}), 400

    # Fetch voice library
    from apis.elevenlabs import ElevenLabsClient
    try:
        with ElevenLabsClient() as el_client:
            voices = el_client.list_voices()
    except Exception as e:
        return jsonify({"error": f"ElevenLabs: {e}"}), 500

    # Load world bible setting for accent/period context
    setting = {}
    try:
        wb = project.load_world_bible()
        bible = wb.get("world_bible", wb)
        setting = bible.get("setting", {})
    except FileNotFoundError:
        pass

    # Ask Claude to rank them
    from apis.claude_client import ClaudeClient
    try:
        with ClaudeClient() as claude:
            result = claude.match_voices(char, voices, setting=setting)
    except Exception as e:
        return jsonify({"error": f"Claude: {e}"}), 500

    claude_cost = result.pop("_cost_usd", 0.02)
    costs.record("claude", claude_cost, "voice_match",
                  f"Voice match for {char_id}", entity_id=char_id)

    matches = result.get("matches", [])

    # Enrich matches with preview_url from original voice data
    voice_map = {v["voice_id"]: v for v in voices}
    for m in matches:
        src = voice_map.get(m.get("voice_id"), {})
        m["preview_url"] = src.get("preview_url", "")
        m["labels"] = src.get("labels", {})
        m["category"] = src.get("category", "")

    return jsonify({"character_id": char_id, "matches": matches})


@api_bp.route("/<slug>/voice/<char_id>/select", methods=["POST"])
def select_voice(slug, char_id):
    """Save a voice_id to the character."""
    data = request.get_json(force=True)
    voice_id = data.get("voice_id")
    if not voice_id:
        return jsonify({"error": "voice_id required"}), 400

    project = _get_project(slug)
    try:
        char = project.load_character(char_id)
        char.setdefault("voice", {})["voice_id"] = voice_id
        char["voice"]["assigned_at"] = datetime.now().isoformat()
        project.save_character(char_id, char)
        return jsonify({"status": "saved", "character_id": char_id, "voice_id": voice_id})
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404


@api_bp.route("/<slug>/voice/<char_id>/lines")
def character_audio_lines(slug, char_id):
    """Return all recorded audio lines for a character across all chapters."""
    project = _get_project(slug)

    lines = []
    for chapter_id in project.get_all_chapter_ids():
        # Shot index is per-chapter (scene_id param is ignored), load once
        try:
            index = project.load_shot_index(chapter_id, "")
        except FileNotFoundError:
            continue
        for shot_summary in index.get("shots", []):
            shot_id = shot_summary["shot_id"]
            scene_id = "_".join(shot_id.split("_")[:2])
            try:
                shot = project.load_shot(chapter_id, scene_id, shot_id)
            except FileNotFoundError:
                continue
            for audio_line in (shot.get("audio") or {}).get("lines", []):
                if audio_line.get("character_id") != char_id:
                    continue
                ref = audio_line.get("audio_ref", "")
                # Read text/direction from meta file, fall back to shot data
                text = audio_line.get("text", "")
                direction = audio_line.get("direction", "")
                if ref:
                    meta_path = (Path(project.root) / ref).with_suffix(".meta.json")
                    if meta_path.exists():
                        try:
                            import json as _json
                            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                            text = meta.get("text", "") or text
                            direction = meta.get("direction", "") or direction
                        except Exception:
                            pass
                audio_url = f"/api/{slug}/audio/{ref}" if ref else ""
                audio_path = Path(project.root) / ref if ref else None
                file_exists = audio_path.exists() if audio_path else False
                recorded_at = ""
                if file_exists:
                    from datetime import datetime, timezone
                    mtime = audio_path.stat().st_mtime
                    recorded_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                lines.append({
                    "line_id": audio_line.get("line_id", ""),
                    "chapter_id": chapter_id,
                    "scene_id": scene_id,
                    "shot_id": shot_id,
                    "text": text,
                    "direction": direction,
                    "audio_url": audio_url,
                    "audio_ref": ref,
                    "file_exists": file_exists,
                    "recorded_at": recorded_at,
                })

    return jsonify({"character_id": char_id, "lines": lines, "total": len(lines)})


@api_bp.route("/<slug>/voice/<char_id>/rerecord", methods=["POST"])
def rerecord_character_audio(slug, char_id):
    """Re-record all audio lines for a character using their current voice."""
    project = _get_project(slug)

    try:
        char = project.load_character(char_id)
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404

    voice_id = (char.get("voice") or {}).get("voice_id")
    if not voice_id:
        return jsonify({"error": "No voice assigned to this character"}), 400

    if not project.is_api_enabled("elevenlabs"):
        return jsonify({"error": "ElevenLabs API not enabled"}), 400

    # Gather all lines for this character, with fresh emotional context
    # from the screenplay (meta files may lack context fields).
    from stages.pipeline import VoiceRecordingStage

    pending_lines = []
    for chapter_id in project.get_all_chapter_ids():
        # Re-parse screenplay to build a text→context map
        sp_path = project._path("chapters", chapter_id, "screenplay.md")
        context_map = {}  # text[:100] → {direction, previous_text, next_text}
        if sp_path.exists():
            dialogue = VoiceRecordingStage._parse_screenplay_dialogue(
                sp_path.read_text(encoding="utf-8")
            )
            for idx, dl in enumerate(dialogue):
                parts = []
                if dl.get("direction"):
                    parts.append(f"[Direction: {dl['direction']}]")
                if dl.get("preceding_action"):
                    parts.append(dl["preceding_action"])
                if idx > 0:
                    parts.append(dialogue[idx - 1]["text"])
                prev_t = " ".join(parts)[:500] if parts else ""
                next_t = dialogue[idx + 1]["text"][:500] if idx < len(dialogue) - 1 else ""
                context_map[dl["text"][:100]] = {
                    "direction": dl.get("direction", ""),
                    "previous_text": prev_t,
                    "next_text": next_t,
                }

        # Shot index is per-chapter (scene_id param is ignored), load once
        try:
            index = project.load_shot_index(chapter_id, "")
        except FileNotFoundError:
            continue
        for shot_summary in index.get("shots", []):
            shot_id = shot_summary["shot_id"]
            scene_id = "_".join(shot_id.split("_")[:2])
            try:
                shot = project.load_shot(chapter_id, scene_id, shot_id)
            except FileNotFoundError:
                continue
            for audio_line in (shot.get("audio") or {}).get("lines", []):
                if audio_line.get("character_id") != char_id:
                    continue
                ref = audio_line.get("audio_ref", "")
                # Get text from shot data or meta file
                text = audio_line.get("text", "")
                if not text and ref:
                    meta_path = (Path(project.root) / ref).with_suffix(".meta.json")
                    if meta_path.exists():
                        try:
                            import json as _json
                            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                            text = meta.get("text", "")
                        except Exception:
                            pass
                if not text:
                    continue
                # Look up fresh emotional context from screenplay
                ctx = context_map.get(text[:100], {})
                direction = ctx.get("direction", "")
                prev_ctx = ctx.get("previous_text", "")
                next_ctx = ctx.get("next_text", "")
                # Delete existing meta so cache hash won't match
                if ref:
                    meta_path = (Path(project.root) / ref).with_suffix(".meta.json")
                    if meta_path.exists():
                        meta_path.unlink()
                pending_lines.append({
                    "line_id": audio_line.get("line_id", ""),
                    "character_id": char_id,
                    "text": text,
                    "audio_ref": ref,
                    "direction": direction,
                    "previous_text": prev_ctx,
                    "next_text": next_ctx,
                })

    if not pending_lines:
        return jsonify({"error": "No recorded lines found for this character"}), 404

    # Budget check
    from core.cost_manager import CostManager
    costs = CostManager(project)
    total_chars = sum(len(l["text"]) for l in pending_lines)
    estimated = round((total_chars / 1000) * 0.30, 4)
    costs.check_api_allowed("elevenlabs")
    costs.check_budget("elevenlabs", estimated)

    # Re-generate
    from apis.elevenlabs import ElevenLabsClient
    character_map = {char_id: char}
    with ElevenLabsClient() as el:
        results = el.generate_line_batch(
            lines=pending_lines,
            character_map=character_map,
            project_root=str(project.root),
            dry_run=False,
        )

    # Record costs
    generated = 0
    for r in results:
        if r["status"] == "generated" and r.get("cost_usd", 0) > 0:
            costs.record("elevenlabs", r["cost_usd"], "audio",
                         f"Re-record {r['line_id']}", entity_id=char_id)
            generated += 1

    actual_cost = sum(r.get("cost_usd", 0) for r in results)
    return jsonify({
        "character_id": char_id,
        "voice_id": voice_id,
        "lines_rerecorded": generated,
        "total_lines": len(pending_lines),
        "cost_usd": actual_cost,
    })


@api_bp.route("/<slug>/voice/<char_id>/rerecord-line", methods=["POST"])
def rerecord_single_line(slug, char_id):
    """Re-record a single audio line for a character."""
    data = request.get_json(force=True)
    line_id = data.get("line_id")
    audio_ref = data.get("audio_ref")
    text = data.get("text")
    if not line_id or not audio_ref or not text:
        return jsonify({"error": "line_id, audio_ref, and text required"}), 400

    project = _get_project(slug)
    try:
        char = project.load_character(char_id)
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404

    voice_id = (char.get("voice") or {}).get("voice_id")
    if not voice_id:
        return jsonify({"error": "No voice assigned to this character"}), 400
    if not project.is_api_enabled("elevenlabs"):
        return jsonify({"error": "ElevenLabs API not enabled"}), 400

    # Build emotional context from screenplay
    from stages.pipeline import VoiceRecordingStage
    chapter_id = line_id.split("_")[0]  # e.g. ch01 from ch01_sc01_sh009_line002
    sp_path = project._path("chapters", chapter_id, "screenplay.md")
    direction = ""
    prev_ctx = ""
    next_ctx = ""
    if sp_path.exists():
        dialogue = VoiceRecordingStage._parse_screenplay_dialogue(
            sp_path.read_text(encoding="utf-8")
        )
        for idx, dl in enumerate(dialogue):
            if dl["text"][:100] == text[:100]:
                parts = []
                if dl.get("direction"):
                    parts.append(f"[Direction: {dl['direction']}]")
                    direction = dl["direction"]
                if dl.get("preceding_action"):
                    parts.append(dl["preceding_action"])
                if idx > 0:
                    parts.append(dialogue[idx - 1]["text"])
                prev_ctx = " ".join(parts)[:500] if parts else ""
                next_ctx = dialogue[idx + 1]["text"][:500] if idx < len(dialogue) - 1 else ""
                break

    # Delete meta to bust cache
    meta_path = (Path(project.root) / audio_ref).with_suffix(".meta.json")
    if meta_path.exists():
        meta_path.unlink()

    line = {
        "line_id": line_id,
        "character_id": char_id,
        "text": text,
        "audio_ref": audio_ref,
        "direction": direction,
        "previous_text": prev_ctx,
        "next_text": next_ctx,
    }

    from core.cost_manager import CostManager
    costs = CostManager(project)
    estimated = round((len(text) / 1000) * 0.30, 4)
    costs.check_api_allowed("elevenlabs")
    costs.check_budget("elevenlabs", estimated)

    from apis.elevenlabs import ElevenLabsClient
    character_map = {char_id: char}
    with ElevenLabsClient() as el:
        results = el.generate_line_batch(
            lines=[line],
            character_map=character_map,
            project_root=str(project.root),
            dry_run=False,
        )

    result = results[0] if results else {}
    cost = result.get("cost_usd", 0)
    if result.get("status") == "generated" and cost > 0:
        costs.record("elevenlabs", cost, "audio",
                      f"Re-record {line_id}", entity_id=char_id)

    return jsonify({
        "line_id": line_id,
        "status": result.get("status", "failed"),
        "cost_usd": cost,
    })


@api_bp.route("/<slug>/voice/<char_id>/settings", methods=["POST"])
def save_voice_settings(slug, char_id):
    """Save voice generation settings (stability, style, etc.) for a character."""
    data = request.get_json(force=True)
    project = _get_project(slug)
    try:
        char = project.load_character(char_id)
    except FileNotFoundError:
        return jsonify({"error": "Character not found"}), 404

    char.setdefault("voice", {}).setdefault("settings", {})
    for key in ("stability", "similarity_boost", "style"):
        if key in data:
            char["voice"]["settings"][key] = float(data[key])
    project.save_character(char_id, char)
    return jsonify({"status": "saved", "settings": char["voice"]["settings"]})


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------

@api_bp.route("/<slug>/settings/auto-advance", methods=["POST"])
def update_auto_advance(slug):
    data = request.get_json(force=True)
    project = _get_project(slug)
    project.data["pipeline"]["auto_advance"] = data
    project.save_project()
    return jsonify({"status": "saved"})


@api_bp.route("/<slug>/settings")
def get_settings(slug):
    project = _get_project(slug)
    return jsonify({
        "auto_advance": project.data["pipeline"]["auto_advance"],
        "apis": project.data.get("apis", {}),
        "gates": project.data["pipeline"]["gates"],
    })


# ------------------------------------------------------------------
# File serving (images, audio)
# ------------------------------------------------------------------

@api_bp.route("/<slug>/image/<path:filepath>")
def serve_image(slug, filepath):
    project = _get_project(slug)
    full_path = Path(project.root) / filepath
    if not full_path.exists():
        return jsonify({"error": "Image not found"}), 404
    mime = "image/png" if filepath.endswith(".png") else "image/jpeg"
    return send_file(str(full_path), mimetype=mime)


@api_bp.route("/<slug>/audio/<path:filepath>")
def serve_audio(slug, filepath):
    project = _get_project(slug)
    full_path = Path(project.root) / filepath
    if not full_path.exists():
        return jsonify({"error": "Audio not found"}), 404
    return send_file(str(full_path), mimetype="audio/mpeg")


@api_bp.route("/<slug>/shot/<shot_id>/mix-preview", methods=["POST"])
def mix_shot_preview(slug, shot_id):
    """Re-mix the shot's SFX into its preview.mp4 (or preview_vertical.mp4)
    without re-rendering the video. Cheap (a few seconds of ffmpeg)
    so we can expose it as a synchronous button in the Editing Room."""
    project = _get_project(slug)
    data = request.get_json(silent=True) or {}
    orientation = data.get("orientation", "horizontal")
    if orientation not in ("horizontal", "vertical", "both"):
        return jsonify({"error": "bad orientation"}), 400

    from utils.mix_preview_audio import mix_shot
    orientations = (["horizontal", "vertical"]
                    if orientation == "both" else [orientation])
    results = []
    for o in orientations:
        try:
            results.append(mix_shot(project, shot_id, o))
        except FileNotFoundError as e:
            results.append({"shot_id": shot_id, "orientation": o,
                            "status": "error", "error": str(e)})
    return jsonify({"results": results})


@api_bp.route("/<slug>/video/<path:filepath>")
def serve_video(slug, filepath):
    project = _get_project(slug)
    full_path = Path(project.root) / filepath
    if not full_path.exists():
        return jsonify({"error": "Video not found"}), 404
    suffix = full_path.suffix.lower()
    mime = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(suffix, "application/octet-stream")
    # Range-capable so the browser can seek / stream large files.
    return send_file(str(full_path), mimetype=mime, conditional=True)


def _get_shot_audio_urls(slug, project, chapter_id, shot_id, shot):
    """Get audio URLs for a shot from its JSON lines only.

    Only serves files explicitly listed in shot.audio.lines to avoid
    stale files from previous audio runs polluting playback.
    """
    audio_lines = (shot.get("audio") or {}).get("lines", [])
    if not audio_lines:
        return []
    urls = []
    for line in audio_lines:
        ref = line.get("audio_ref", "")
        if ref:
            urls.append(f"/api/{slug}/audio/{ref}")
    return urls


@api_bp.route("/<slug>/chapter-timeline/<chapter_id>")
def chapter_timeline(slug, chapter_id):
    """Return ordered timeline of all shots with audio URLs for chapter playback."""
    from core.state_manager import StateManager
    project = _get_project(slug)
    sm = StateManager(project)
    detail = sm.get_chapter_status(chapter_id)

    timeline = []
    for scene in detail.get("scenes", []):
        shot_ids = (scene.get("shots") or {}).get("shot_ids", [])
        for shot_id in shot_ids:
            try:
                shot = project.load_shot(chapter_id, scene["scene_id"], shot_id)
            except FileNotFoundError:
                shot = {}

            duration = shot.get("duration_sec", 3)
            label = shot.get("label", "")
            shot_type = (shot.get("cinematic") or {}).get("shot_type", "")
            img_url = f"/api/{slug}/image/chapters/{chapter_id}/shots/{shot_id}/storyboard.png"
            audio_urls = _get_shot_audio_urls(slug, project, chapter_id, shot_id, shot)

            timeline.append({
                "shot_id": shot_id,
                "scene_id": scene["scene_id"],
                "label": label,
                "shot_type": shot_type,
                "duration_sec": duration,
                "image_url": img_url,
                "audio_urls": audio_urls,
            })

    return jsonify({"chapter_id": chapter_id, "timeline": timeline})


@api_bp.route("/<slug>/audio-lines/<chapter_id>/<shot_id>")
def list_audio_lines(slug, chapter_id, shot_id):
    """List audio files for a shot, using shot JSON as source of truth."""
    project = _get_project(slug)

    # Load shot to get authoritative audio.lines
    scene_id = "_".join(shot_id.split("_")[:2])
    try:
        shot = project.load_shot(chapter_id, scene_id, shot_id)
    except FileNotFoundError:
        shot = {}

    urls = _get_shot_audio_urls(slug, project, chapter_id, shot_id, shot)
    lines = []
    for url in urls:
        filename = url.rsplit("/", 1)[-1]
        lines.append({"filename": filename, "url": url})
    return jsonify({"lines": lines})


# ------------------------------------------------------------------
# New project creation
# ------------------------------------------------------------------

@api_bp.route("/projects/new", methods=["POST"])
def create_project():
    """Create a new project from the wizard form data."""
    data = request.get_json(force=True)
    slug = data.get("slug", "").strip()
    if not slug:
        return jsonify({"error": "slug required"}), 400

    projects_dir = Path(current_app.config["PROJECTS_DIR"])
    project_dir = projects_dir / slug
    if project_dir.exists():
        return jsonify({"error": f"Directory '{slug}' already exists"}), 409

    # Copy project template
    template_dir = Path(__file__).resolve().parent.parent.parent / "project_template"
    if not template_dir.exists():
        return jsonify({"error": "Project template not found"}), 500

    shutil.copytree(str(template_dir), str(project_dir))

    # Build placeholder substitution map
    placeholders = {
        "{{PROJECT_SLUG}}": slug,
        "{{PROJECT_NAME}}": data.get("display_name", slug),
        "{{SOURCE_TITLE}}": data.get("source_title", ""),
        "{{SOURCE_AUTHOR}}": data.get("source_author", ""),
        "{{SOURCE_YEAR}}": data.get("source_year", ""),
        "{{COPYRIGHT_STATUS}}": data.get("copyright_status", "Unknown"),
        "{{SOURCE_FILE}}": data.get("source_file", "source.txt"),
        "{{PERIOD}}": data.get("period", ""),
        "{{LOCATION}}": data.get("location", ""),
        "{{VISUAL_REF_1}}": data.get("visual_ref_1", ""),
        "{{VISUAL_REF_2}}": data.get("visual_ref_2", ""),
        "{{TONE}}": data.get("tone", "Epic"),
        "{{GIT_REMOTE_URL}}": data.get("git_remote_url", ""),
        "{{CREATED_AT}}": datetime.now().isoformat(),
        "{{ORCHESTRATOR_VERSION}}": _get_orchestrator_version(),
        "{{INITIALIZED_BY}}": "web_ui",
    }

    # Replace placeholders in all text files
    for fpath in project_dir.rglob("*"):
        if fpath.is_file() and fpath.suffix in (".json", ".md", ".txt", ".gitignore", ".gitattributes"):
            try:
                content = fpath.read_text(encoding="utf-8")
                for placeholder, value in placeholders.items():
                    content = content.replace(placeholder, value)
                fpath.write_text(content, encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                pass

    # Update API budgets if provided
    project_json_path = project_dir / "project.json"
    if project_json_path.exists() and "api_budgets" in data:
        pj = json.loads(project_json_path.read_text(encoding="utf-8"))
        for api_name, budget in data["api_budgets"].items():
            if api_name in pj.get("apis", {}):
                pj["apis"][api_name]["budget_usd"] = float(budget)
        project_json_path.write_text(json.dumps(pj, indent=2), encoding="utf-8")

    # Update auto-advance if provided
    if "auto_advance" in data:
        pj = json.loads(project_json_path.read_text(encoding="utf-8"))
        pj["pipeline"]["auto_advance"] = data["auto_advance"]
        project_json_path.write_text(json.dumps(pj, indent=2), encoding="utf-8")

    # Re-scan projects so the new one is immediately available
    from ui.server import scan_projects
    current_app.config["PROJECTS"] = scan_projects(current_app.config["PROJECTS_DIR"])

    return jsonify({"status": "created", "slug": slug})


# ------------------------------------------------------------------
# Version check & update
# ------------------------------------------------------------------

@api_bp.route("/version")
def api_version():
    """Return current version, latest available, and whether an update exists."""
    from ui.version import check_for_update
    return jsonify(check_for_update())


@api_bp.route("/update", methods=["POST"])
def api_update():
    """Pull latest code from origin and restart the server process."""
    import os
    import sys
    from ui.version import pull_latest

    result = pull_latest()
    if not result["ok"]:
        return jsonify(result), 500

    # Schedule restart after response is sent
    import threading

    def _restart():
        import time
        time.sleep(1)  # let the response flush
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True, "message": "Pulling and restarting..."})
