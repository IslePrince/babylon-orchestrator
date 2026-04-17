"""
ui/routes/stage_routes.py — Stage execution and SSE streaming.

Runs pipeline stages in background threads with progress callbacks
streamed to the browser via Server-Sent Events (SSE).
"""

import json
import logging
import queue
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

# File logger for stage execution — tail with: tail -f logs/stages.log
_log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
_log_dir.mkdir(exist_ok=True)
_log_file = _log_dir / "stages.log"

logger = logging.getLogger("babylon.stages")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(str(_log_file), encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)

stage_bp = Blueprint("stage", __name__)

# Module-level state for job tracking.
# This is the one exception to the "no global state" rule —
# SSE streams span multiple HTTP connections.
job_queues = {}   # job_id -> queue.Queue
job_meta = {}     # job_id -> {stage, slug, chapter_id, started_at, status}


# ------------------------------------------------------------------
# Stage class dispatch
# ------------------------------------------------------------------

STAGE_MAP = {
    "ingest":           "stages.pipeline:IngestStage",
    "screenplay":       "stages.pipeline:ScreenplayStage",
    "characters":       "stages.pipeline:CharacterStage",
    "character_sheets": "stages.character_sheets:CharacterSheetStage",
    "lora_training":    "stages.character_sheets:LoRATrainingStage",
    "cinematographer":  "stages.pipeline:CinematographerStage",
    "storyboard":       "stages.pipeline:StoryboardStage",
    "voice_recording":  "stages.pipeline:VoiceRecordingStage",
    "sound_fx":         "stages.pipeline:SoundFXStage",
    "audio_score":      "stages.pipeline:AudioScoreStage",
    "preview_video":    "stages.pipeline:PreviewVideoStage",
    "assets":           "stages.pipeline:AssetManifestStage",
    "props_staging":    "stages.pipeline:PropsAndStagingStage",
    "meshes":           "stages.mesh_animation:MeshStage",
    "animate":          "stages.mesh_animation:AnimationStage",
}

# Auto-advance transition table: current_stage -> next_stage
# NOTE: transition keys in project.json are "{from}_to_{to}" format
AUTO_ADVANCE_TRANSITIONS = {
    "ingest": "screenplay",
    "screenplay": "characters",
    "characters": "character_sheets",
    "character_sheets": None,      # Manual — user does voice casting then recording
    "lora_training": None,         # Manual — user decides when to train LoRAs
    "voice_recording": None,       # Manual: user reviews recordings in screenplay review
    # screenplay_review: UI-only, no stage class
    "cinematographer": "storyboard",
    "storyboard": None,            # Manual: user does editing room
    # editing_room: UI-only, no stage class
    "sound_fx": "audio_score",
    "audio_score": None,           # Gate: sound_to_assets
    "assets": None,                # Gate: sound_to_assets
    "meshes": "animate",
    "animate": None,               # Gate: assets_to_scene
}


def _import_stage(stage_name):
    """Lazily import and return a stage class."""
    if stage_name not in STAGE_MAP:
        return None
    module_path, class_name = STAGE_MAP[stage_name].split(":")
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


# ------------------------------------------------------------------
# POST /api/<slug>/stages/run — start a stage in a background thread
# ------------------------------------------------------------------

@stage_bp.route("/<slug>/stages/run", methods=["POST"])
def run_stage(slug):
    from flask import current_app
    from core.project import Project

    data = request.get_json(force=True)
    stage_name = data.get("stage")
    if not stage_name:
        return jsonify({"error": "stage is required"}), 400

    StageClass = _import_stage(stage_name)
    if StageClass is None:
        return jsonify({"error": f"Unknown stage: {stage_name}"}), 400

    # Resolve project path NOW (while in request context) so the
    # background thread doesn't need Flask's current_app.
    project_path = current_app.config["PROJECTS"].get(slug)
    if not project_path:
        return jsonify({"error": f"Project '{slug}' not found"}), 404

    # Create job
    job_id = str(uuid.uuid4())[:8]
    q = queue.Queue()
    job_queues[job_id] = q
    job_meta[job_id] = {
        "stage": stage_name,
        "slug": slug,
        "chapter_id": data.get("chapter_id"),
        "started_at": datetime.now().isoformat(),
        "status": "running",
    }

    # Build progress callback
    cost_so_far = [0.0]

    def progress_callback(pct, message, cost=0.0):
        cost_so_far[0] += cost
        # Store latest progress in job_meta so poll endpoint can serve it
        job_meta[job_id]["progress"] = pct
        job_meta[job_id]["message"] = message
        job_meta[job_id]["cost_so_far"] = cost_so_far[0]
        q.put({
            "progress": pct,
            "message": message,
            "cost_so_far": cost_so_far[0],
        })

    # Build run kwargs from request data
    run_kwargs = {"dry_run": data.get("dry_run", False), "progress_callback": progress_callback}

    if stage_name == "ingest":
        run_kwargs["source_text_path"] = data.get("source")
    elif stage_name == "characters":
        pass  # CharacterStage scans all chapters, no chapter_id needed
    elif stage_name in ("character_sheets", "lora_training"):
        if data.get("character_id"):
            run_kwargs["character_id"] = data["character_id"]
        if data.get("force"):
            run_kwargs["force"] = True
    elif stage_name in ("screenplay", "voice_recording", "sound_fx", "audio_score"):
        run_kwargs["chapter_id"] = data.get("chapter_id")
    elif stage_name == "storyboard":
        run_kwargs["chapter_id"] = data.get("chapter_id")
        if data.get("force"):
            run_kwargs["force"] = True
    elif stage_name == "cinematographer":
        run_kwargs["chapter_id"] = data.get("chapter_id")
        if data.get("scene_id"):
            run_kwargs["scene_id"] = data["scene_id"]
    elif stage_name == "assets":
        if data.get("chapter_id"):
            run_kwargs["chapter_id"] = data["chapter_id"]
    elif stage_name == "meshes":
        if data.get("batch_id"):
            run_kwargs["batch_id"] = data["batch_id"]
    elif stage_name == "animate":
        if data.get("character_id"):
            run_kwargs["character_id"] = data["character_id"]
        if data.get("chapter_id"):
            run_kwargs["chapter_id"] = data["chapter_id"]
    elif stage_name == "preview_video":
        # Either shot_id (single) or chapter_id (batch), plus optional
        # orientation ("horizontal"/"vertical"/"both"), force, seed.
        if data.get("shot_id"):
            run_kwargs["shot_id"] = data["shot_id"]
        if data.get("chapter_id"):
            run_kwargs["chapter_id"] = data["chapter_id"]
        if data.get("orientation"):
            run_kwargs["orientation"] = data["orientation"]
        if data.get("force"):
            run_kwargs["force"] = True
        if data.get("seed") is not None:
            run_kwargs["seed"] = int(data["seed"])

    def _run():
        logger.info(f"[{job_id}] Starting stage '{stage_name}' for project '{slug}'")
        logger.debug(f"[{job_id}] kwargs: {run_kwargs}")
        try:
            # Create Project directly from path — no Flask context needed
            project = Project(project_path)
            stage_instance = StageClass(project)
            result = stage_instance.run(**run_kwargs)

            logger.info(f"[{job_id}] Stage '{stage_name}' completed successfully")

            # Check auto-advance
            auto_advance_job = None
            auto_advance_stage = None
            if not data.get("dry_run", False):
                auto_advance_job = _check_auto_advance(
                    slug, project_path, stage_name, data.get("chapter_id")
                )
                if auto_advance_job:
                    auto_advance_stage = AUTO_ADVANCE_TRANSITIONS.get(stage_name)

            done_event = {
                "done": True,
                "status": "complete",
                "result": result,
            }
            if auto_advance_job:
                done_event["auto_advance_job_id"] = auto_advance_job
                done_event["auto_advance_stage"] = auto_advance_stage

            q.put(done_event)
            job_meta[job_id]["status"] = "complete"
            job_meta[job_id]["result"] = result
            job_meta[job_id]["finished_at"] = datetime.now().isoformat()
            if auto_advance_job:
                job_meta[job_id]["auto_advance_job_id"] = auto_advance_job
                job_meta[job_id]["auto_advance_stage"] = auto_advance_stage
        except Exception as e:
            logger.error(f"[{job_id}] Stage '{stage_name}' FAILED: {e}")
            logger.debug(traceback.format_exc())
            q.put({
                "done": True,
                "status": "error",
                "error": str(e),
            })
            job_meta[job_id]["status"] = "error"
            job_meta[job_id]["error"] = str(e)
            job_meta[job_id]["finished_at"] = datetime.now().isoformat()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "stage": stage_name})


def _check_auto_advance(slug, project_path, completed_stage, chapter_id):
    """
    If auto-advance is enabled and the next stage exists,
    spawn a new background job for the next stage.
    Returns the new job_id if spawned, else None.
    """
    from core.project import Project

    next_stage = AUTO_ADVANCE_TRANSITIONS.get(completed_stage)
    if not next_stage:
        return None

    try:
        project = Project(project_path)
        auto_cfg = project.data.get("pipeline", {}).get("auto_advance", {})
        if not auto_cfg.get("enabled"):
            return None

        transitions = auto_cfg.get("transitions", {})
        transition_key = f"{completed_stage}_to_{next_stage}"
        if not transitions.get(transition_key, False):
            return None
    except Exception:
        return None

    # Spawn next stage
    NextClass = _import_stage(next_stage)
    if NextClass is None:
        return None

    next_job_id = str(uuid.uuid4())[:8]
    next_q = queue.Queue()
    job_queues[next_job_id] = next_q
    job_meta[next_job_id] = {
        "stage": next_stage,
        "slug": slug,
        "chapter_id": chapter_id,
        "started_at": datetime.now().isoformat(),
        "status": "running",
        "auto_advanced_from": completed_stage,
    }

    next_cost = [0.0]

    def next_progress(pct, message, cost=0.0):
        next_cost[0] += cost
        next_q.put({"progress": pct, "message": message, "cost_so_far": next_cost[0]})

    next_kwargs = {"dry_run": False, "progress_callback": next_progress}
    if chapter_id and next_stage in ("screenplay", "cinematographer", "storyboard", "voice_recording", "sound_fx", "audio_score"):
        next_kwargs["chapter_id"] = chapter_id

    # After cinematographer, force-regenerate storyboards since prompts changed
    if completed_stage == "cinematographer" and next_stage == "storyboard":
        next_kwargs["force"] = True

    def _run_next():
        logger.info(f"[{next_job_id}] Auto-advance: starting '{next_stage}' (from {completed_stage})")
        logger.debug(f"[{next_job_id}] kwargs: {next_kwargs}")
        try:
            p = Project(project_path)
            inst = NextClass(p)
            result = inst.run(**next_kwargs)
            logger.info(f"[{next_job_id}] Auto-advance '{next_stage}' completed")
            next_q.put({"done": True, "status": "complete", "result": result})
            job_meta[next_job_id]["status"] = "complete"
            job_meta[next_job_id]["finished_at"] = datetime.now().isoformat()
        except Exception as e:
            logger.error(f"[{next_job_id}] Auto-advance '{next_stage}' FAILED: {e}")
            logger.debug(traceback.format_exc())
            next_q.put({"done": True, "status": "error", "error": str(e)})
            job_meta[next_job_id]["status"] = "error"
            job_meta[next_job_id]["finished_at"] = datetime.now().isoformat()

    threading.Thread(target=_run_next, daemon=True).start()
    return next_job_id


# ------------------------------------------------------------------
# GET /api/stream/<job_id> — SSE progress stream
# ------------------------------------------------------------------

@stage_bp.route("/stream/<job_id>")
def stream(job_id):
    if job_id not in job_queues:
        return jsonify({"error": "Unknown job_id"}), 404

    def generate():
        q = job_queues[job_id]
        while True:
            try:
                event = q.get(timeout=30)
                # Trim large result payloads to avoid blocking the SSE stream
                if event.get("done") and event.get("result"):
                    result = event["result"]
                    if isinstance(result, dict):
                        # Send a lightweight summary instead of full result
                        event = {**event, "result": _trim_result(result)}
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("done"):
                    break
            except queue.Empty:
                # Heartbeat to keep connection alive
                yield f"data: {json.dumps({'heartbeat': True})}\n\n"

        # Clean up queue (meta stays for polling fallback)
        job_queues.pop(job_id, None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ------------------------------------------------------------------
# GET /api/jobs — list active/recent jobs
# GET /api/jobs/<job_id> — poll single job status (SSE fallback)
# ------------------------------------------------------------------

@stage_bp.route("/jobs")
def list_jobs():
    return jsonify([
        {"job_id": jid, **meta}
        for jid, meta in job_meta.items()
    ])


@stage_bp.route("/<slug>/jobs/active")
def active_jobs(slug):
    """Return currently running jobs for a project (used to rehydrate UI after navigation)."""
    active = [
        {"job_id": jid, **meta}
        for jid, meta in job_meta.items()
        if meta.get("slug") == slug and meta.get("status") == "running"
    ]
    return jsonify(active)


@stage_bp.route("/jobs/<job_id>")
def get_job(job_id):
    meta = job_meta.get(job_id)
    if not meta:
        return jsonify({"error": "Unknown job_id"}), 404
    return jsonify({"job_id": job_id, **meta})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _trim_result(result: dict) -> dict:
    """Trim large stage results for SSE delivery.
    Keeps summary stats but drops per-shot detail to avoid
    multi-KB JSON payloads that block the SSE flush.
    """
    trimmed = {}
    for k, v in result.items():
        if isinstance(v, list) and len(v) > 5:
            # e.g. storyboard results list — send count only
            trimmed[k] = f"[{len(v)} items]"
        elif isinstance(v, str) and len(v) > 500:
            trimmed[k] = v[:500] + "..."
        else:
            trimmed[k] = v
    return trimmed
