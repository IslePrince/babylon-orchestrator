"""
Babylon Studio MCP server
=========================

Exposes the orchestrator's full film-production pipeline to remote AI
agents over Server-Sent Events, on your local network. No auth —
intended for trusted LAN use only.

Run:
    python3 mcp_server.py --projects-dir D:/babylon-orchestrator/projects
        [--host 0.0.0.0] [--port 5758] [--transport sse]

Once running, a client on the same LAN connects to
``http://<host>:5758/sse`` and can drive projects end-to-end — ingest
a story, generate a screenplay, cast voices, render storyboards,
record dialogue, build the cut, layer SFX, render Wan talking-head
preview videos.

See MCP_GUIDE.md for the recommended tool-call sequence and a
worked example of building a short-form video from a plain .txt file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

# Same UTF-8 defence as ui/server.py — Windows cp1252 stdout can't
# handle unicode arrows / bullets / em-dashes that appear inside
# some stages' print() statements.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# Keep imports relative to the orchestrator root so every stage/util
# can be invoked the same way the Flask UI does.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mcp.server.fastmcp import FastMCP  # noqa: E402

# Core + stage imports — all optional at module scope so the server
# still launches even when, say, ComfyUI is down.
from core.project import Project  # noqa: E402

logger = logging.getLogger("babylon-mcp")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


# ----------------------------------------------------------------------
# Project resolution
# ----------------------------------------------------------------------

PROJECTS_DIR: Path = Path(os.getenv("BABYLON_PROJECTS_DIR", "")).expanduser()


def _project_path(slug: str) -> Path:
    """Resolve ``<PROJECTS_DIR>/<slug>`` and validate it."""
    if not PROJECTS_DIR:
        raise RuntimeError(
            "BABYLON_PROJECTS_DIR is not set. Pass --projects-dir at "
            "launch."
        )
    path = (PROJECTS_DIR / slug).resolve()
    if not path.is_relative_to(PROJECTS_DIR.resolve()):
        raise ValueError(f"Project slug {slug!r} escapes projects dir")
    return path


def _load_project(slug: str) -> Project:
    return Project(str(_project_path(slug)))


# ----------------------------------------------------------------------
# Background job manager
# ----------------------------------------------------------------------

_JOB_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}


def _spawn_job(label: str, fn, kwargs: dict) -> str:
    """Run ``fn(progress_callback=..., **kwargs)`` on a background
    thread. Returns a job_id that callers can poll via
    ``get_job_status``. Stage classes already accept
    ``progress_callback`` so this wraps them transparently."""
    job_id = uuid.uuid4().hex[:10]
    state = {
        "job_id": job_id,
        "label": label,
        "status": "running",
        "pct": 0,
        "message": "starting...",
        "result": None,
        "error": None,
        "started_at": time.time(),
        "finished_at": None,
        "cost_so_far": 0.0,
    }
    with _JOB_LOCK:
        _JOBS[job_id] = state

    def _cb(pct: float, msg: str, cost: float = 0.0) -> None:
        with _JOB_LOCK:
            s = _JOBS.get(job_id) or {}
            s["pct"] = float(pct)
            s["message"] = str(msg)
            s["cost_so_far"] += float(cost or 0.0)

    def _run() -> None:
        try:
            result = fn(progress_callback=_cb, **kwargs)
            with _JOB_LOCK:
                s = _JOBS[job_id]
                s["status"] = "complete"
                s["pct"] = 100.0
                s["result"] = result
                s["finished_at"] = time.time()
        except Exception as e:  # noqa: BLE001
            # Surface GPU-busy errors as structured metadata so the
            # caller (an agent, typically) can introspect the blocker
            # and decide whether to poll or abort.
            with _JOB_LOCK:
                s = _JOBS[job_id]
                s["status"] = "error"
                s["error"] = f"{type(e).__name__}: {e}"
                s["traceback"] = traceback.format_exc()
                s["finished_at"] = time.time()
                if type(e).__name__ == "GPUBusyError":
                    s["blocked_by"] = getattr(e, "holder", None)
            logger.exception("Job %s failed", job_id)

    threading.Thread(target=_run, name=f"babylon-job-{job_id}",
                     daemon=True).start()
    return job_id


def _get_job(job_id: str) -> dict:
    with _JOB_LOCK:
        s = _JOBS.get(job_id)
        return dict(s) if s else {}


def _wait_job(job_id: str, timeout_sec: float = 900.0) -> dict:
    """Block until the job finishes or the deadline expires. Returns
    the job state either way — a caller that wants the result should
    check ``status == "complete"``."""
    start = time.time()
    while time.time() - start < timeout_sec:
        s = _get_job(job_id)
        if not s or s["status"] in ("complete", "error"):
            return s
        time.sleep(0.5)
    return _get_job(job_id)


# ----------------------------------------------------------------------
# FastMCP setup
# ----------------------------------------------------------------------

mcp = FastMCP(
    "babylon-studio",
    instructions=(
        "Babylon Studio orchestrator — drives AI-assisted film "
        "production. Tools are grouped by pipeline phase: project "
        "lifecycle, ingest/screenplay, characters/voices/sheets, "
        "shots (cinematographer + storyboard), audio (voice recording, "
        "SFX, score), editing, preview video (Wan InfiniTalk). See "
        "MCP_GUIDE.md for the recommended call sequence."
    ),
)


# ======================================================================
# 1. Project lifecycle
# ======================================================================

@mcp.tool()
def list_projects() -> list[dict]:
    """List every Babylon Studio project under the server's projects
    directory. Each entry has ``slug``, ``display_name`` (from
    project.json), ``pipeline_stage``, and ``chapter_count``."""
    out: list[dict] = []
    if not PROJECTS_DIR.exists():
        return out
    for p in sorted(PROJECTS_DIR.iterdir()):
        if not p.is_dir() or not (p / "project.json").exists():
            continue
        try:
            proj = Project(str(p))
            chapters_dir = p / "chapters"
            chapter_count = (
                sum(1 for c in chapters_dir.iterdir()
                    if c.is_dir() and c.name != "_index.json")
                if chapters_dir.exists() else 0
            )
            out.append({
                "slug": p.name,
                "display_name": proj.data.get("display_name", p.name),
                "pipeline_stage": proj.data.get("pipeline_stage",
                                                "not_started"),
                "chapter_count": chapter_count,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("skipped %s: %s", p, e)
    return out


@mcp.tool()
def get_project_status(slug: str) -> dict:
    """Return the project's current pipeline_stage, per-chapter status
    (which stages are done, which gates are open), and top-level
    cost totals. Use this before starting any pipeline work to know
    where to pick up."""
    from core.state_manager import StateManager
    proj = _load_project(slug)
    sm = StateManager(proj)
    return sm.get_status()


@mcp.tool()
def create_project(
    slug: str,
    display_name: str,
    source_text: Optional[str] = None,
    source_text_path: Optional[str] = None,
    budget_usd: dict | None = None,
) -> dict:
    """Bootstrap a brand-new project.

    Copies the project template, sets display_name, writes the source
    text into ``source/<slug>.txt`` (from either ``source_text`` or
    ``source_text_path``), and initialises the schema so subsequent
    tools can operate.

    ``budget_usd`` overrides per-API caps, e.g.
    ``{"claude": 50, "elevenlabs": 30, "meshy": 100}``.

    Returns the project's resolved path + the next recommended tool
    call (``run_ingest``).
    """
    if not PROJECTS_DIR:
        raise RuntimeError("projects dir not configured")
    target = PROJECTS_DIR / slug
    if target.exists():
        raise FileExistsError(f"Project '{slug}' already exists")

    template = SCRIPT_DIR / "project_template"
    if not template.exists():
        raise RuntimeError(
            f"project_template missing from orchestrator: {template}"
        )

    import shutil
    shutil.copytree(template, target)

    pj_path = target / "project.json"
    with open(pj_path, "r", encoding="utf-8") as f:
        pj = json.load(f)
    pj["display_name"] = display_name
    pj["project_id"] = slug
    if budget_usd:
        budgets = pj.setdefault("budgets", {})
        for k, v in budget_usd.items():
            budgets[k] = float(v)
    with open(pj_path, "w", encoding="utf-8") as f:
        json.dump(pj, f, indent=2, ensure_ascii=False)

    source_dir = target / "source"
    source_dir.mkdir(exist_ok=True)
    source_file = source_dir / f"{slug}.txt"
    if source_text:
        source_file.write_text(source_text, encoding="utf-8")
    elif source_text_path:
        src = Path(source_text_path)
        if not src.exists():
            raise FileNotFoundError(f"source_text_path not found: {src}")
        source_file.write_text(src.read_text(encoding="utf-8"),
                               encoding="utf-8")

    return {
        "slug": slug,
        "path": str(target),
        "source_text_path": str(source_file) if source_file.exists() else None,
        "next_step": (
            "run_ingest" if source_file.exists()
            else "write source text with write_source_text() before ingest"
        ),
    }


@mcp.tool()
def write_source_text(slug: str, text: str) -> dict:
    """Write (or overwrite) the project's source story text at
    ``source/<slug>.txt``. Use after create_project if you didn't
    pass the story content inline."""
    proj_root = _project_path(slug)
    src_dir = proj_root / "source"
    src_dir.mkdir(exist_ok=True)
    path = src_dir / f"{slug}.txt"
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "bytes": len(text.encode("utf-8"))}


# ======================================================================
# 2. Ingest (story → chapters + world bible)
# ======================================================================

@mcp.tool()
def run_ingest(
    slug: str,
    source_text_path: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Run the Ingest stage — splits the source text into chapters
    and writes a draft world bible. Uses Claude; cost logged in the
    project ledger.

    Blocks until complete (typical: 1-2 minutes). Use
    ``run_ingest_async`` for job-based invocation.

    Returns a summary of chapters created.
    """
    from stages.pipeline import IngestStage
    proj = _load_project(slug)
    stage = IngestStage(proj)
    return stage.run(
        source_text_path=source_text_path,
        dry_run=dry_run,
    )


@mcp.tool()
def run_ingest_async(
    slug: str,
    source_text_path: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Same as ``run_ingest`` but returns a job_id immediately.
    Poll with ``get_job_status`` / ``wait_for_job``."""
    from stages.pipeline import IngestStage

    def _run(**k):
        proj = _load_project(slug)
        return IngestStage(proj).run(
            source_text_path=source_text_path,
            dry_run=dry_run,
            **k,
        )
    return {"job_id": _spawn_job(f"ingest:{slug}", _run, {})}


@mcp.tool()
def list_chapters(slug: str) -> list[dict]:
    """List every chapter in the project with its title, status, and
    how many shots / lines exist so far."""
    from core.state_manager import StateManager
    proj = _load_project(slug)
    sm = StateManager(proj)
    status = sm.get_status()
    return status.get("chapters", [])


@mcp.tool()
def get_world_bible(slug: str) -> dict:
    """Return the current ``world/world_bible.json`` — period, visual
    palette, lighting rules, key locations, anachronism watchlist.
    Used by downstream stages to keep storyboards and SFX
    period-appropriate."""
    return _load_project(slug).load_world_bible()


# ======================================================================
# 3. Screenplay
# ======================================================================

@mcp.tool()
def run_screenplay(slug: str, chapter_id: str,
                   dry_run: bool = False) -> dict:
    """Generate a screenplay.md for one chapter. Requires that
    ``run_ingest`` has already produced ``chapter.json`` with an
    outline. Typical wall time: 1-3 minutes."""
    from stages.pipeline import ScreenplayStage
    proj = _load_project(slug)
    return ScreenplayStage(proj).run(
        chapter_id=chapter_id, dry_run=dry_run,
    )


@mcp.tool()
def get_screenplay(slug: str, chapter_id: str) -> dict:
    """Return the current screenplay markdown + metadata (approval,
    status, last revision notes) for a chapter."""
    proj = _load_project(slug)
    sp_path = proj._path("chapters", chapter_id, "screenplay.md")
    if not sp_path.exists():
        return {"chapter_id": chapter_id, "exists": False}
    chapter = proj.load_chapter(chapter_id)
    return {
        "chapter_id": chapter_id,
        "exists": True,
        "content": sp_path.read_text(encoding="utf-8"),
        "metadata": chapter.get("screenplay", {}),
    }


@mcp.tool()
def revise_screenplay(
    slug: str, chapter_id: str, feedback: str, dry_run: bool = False,
) -> dict:
    """Ask Claude to revise a chapter's screenplay based on free-form
    ``feedback`` (e.g. 'tighten the opening', 'make the exchange
    more formal'). Writes a new revision; the old one is preserved in
    chapter metadata."""
    from apis.claude_client import ClaudeClient
    proj = _load_project(slug)
    sp_path = proj._path("chapters", chapter_id, "screenplay.md")
    if not sp_path.exists():
        raise FileNotFoundError(f"screenplay.md missing for {chapter_id}")
    chapter = proj.load_chapter(chapter_id)
    current = sp_path.read_text(encoding="utf-8")
    if dry_run:
        return {"status": "dry_run", "chapter_id": chapter_id,
                "estimated_cost_usd": 0.05}
    claude = ClaudeClient()
    revised = claude.revise_screenplay(
        chapter=chapter, current_screenplay=current, feedback=feedback,
    )
    sp_path.write_text(revised, encoding="utf-8")
    chapter.setdefault("screenplay", {})["revised_at"] = int(time.time())
    chapter["screenplay"]["last_feedback"] = feedback
    proj.save_chapter(chapter_id, chapter)
    return {"status": "complete", "chapter_id": chapter_id,
            "new_length": len(revised)}


@mcp.tool()
def approve_screenplay(slug: str, chapter_id: str) -> dict:
    """Mark a chapter's screenplay as approved by a human reviewer.
    Downstream stages (voice recording, cinematographer) check this
    flag; approve only after you're satisfied."""
    proj = _load_project(slug)
    chapter = proj.load_chapter(chapter_id)
    sp = chapter.setdefault("screenplay", {})
    sp["approved"] = True
    sp["status"] = "approved"
    sp["approved_at"] = int(time.time())
    proj.save_chapter(chapter_id, chapter)
    return {"chapter_id": chapter_id, "approved": True}


# ======================================================================
# 4. Characters
# ======================================================================

@mcp.tool()
def run_characters(slug: str, dry_run: bool = False) -> dict:
    """Run the Character stage — extracts every named (ALL CAPS)
    character from all approved screenplays, generates
    character.json stubs, and updates the character index. Run once
    after you have at least one approved screenplay."""
    from stages.pipeline import CharacterStage
    proj = _load_project(slug)
    return CharacterStage(proj).run(dry_run=dry_run)


@mcp.tool()
def list_characters(slug: str) -> list[dict]:
    """Return every named character with display name, role,
    assigned voice_id, LoRA status, and whether a reference image
    exists."""
    proj = _load_project(slug)
    try:
        idx = proj.load_character_index()
    except FileNotFoundError:
        return []
    out = []
    for entry in idx.get("characters", []):
        cid = entry["character_id"]
        try:
            c = proj.load_character(cid)
        except FileNotFoundError:
            continue
        lora_path = proj._path("characters", cid, f"{cid}_char.safetensors")
        ref_path = proj._path("characters", cid, "reference.png")
        out.append({
            "character_id": cid,
            "display_name": c.get("display_name", cid),
            "role": (c.get("description") or {}).get("role", ""),
            "voice_id": (c.get("voice") or {}).get("voice_id", ""),
            "voice_name": (c.get("voice") or {}).get("voice_name", ""),
            "has_reference_image": ref_path.exists(),
            "has_lora": lora_path.exists(),
        })
    return out


@mcp.tool()
def get_character(slug: str, character_id: str) -> dict:
    """Return the full ``character.json`` for one character."""
    return _load_project(slug).load_character(character_id)


@mcp.tool()
def update_character(slug: str, character_id: str, updates: dict) -> dict:
    """Apply a shallow merge of ``updates`` onto the character's
    ``character.json``. Useful for tweaking visual_tag, description,
    costume_default, or nested ``voice`` fields without overwriting
    the whole file.

    Top-level keys are replaced; for nested dicts (``voice``,
    ``appearance``, etc.) pass a full sub-dict or use dotted keys —
    this tool does a one-level merge, so pass ``{"voice": {...full
    voice block...}}`` if you mean to update voice settings.

    Returns the updated character_json.
    """
    proj = _load_project(slug)
    c = proj.load_character(character_id)

    def _merge(dst, src):
        for k, v in src.items():
            if (isinstance(v, dict) and isinstance(dst.get(k), dict)):
                _merge(dst[k], v)
            else:
                dst[k] = v
    _merge(c, updates)
    proj.save_character(character_id, c)
    return c


# ======================================================================
# 5. Voices (ElevenLabs catalogue + casting)
# ======================================================================

@mcp.tool()
def list_elevenlabs_voices() -> list[dict]:
    """Pull the current ElevenLabs voice catalogue. Returns entries
    with voice_id, name, description, labels (gender, age, accent,
    use_case). Use to match characters to candidate voices."""
    from apis.elevenlabs import ElevenLabsClient
    with ElevenLabsClient() as el:
        voices = el.list_voices()
    # Trim heavy fields for chat-sized responses.
    return [
        {
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "description": (v.get("description") or "").strip(),
            "labels": v.get("labels") or {},
            "category": v.get("category"),
            "preview_url": v.get("preview_url"),
        }
        for v in voices
    ]


@mcp.tool()
def set_character_voice(
    slug: str, character_id: str, voice_id: str,
    voice_name: str | None = None,
    stability: float | None = None,
    similarity_boost: float | None = None,
    style: float | None = None,
) -> dict:
    """Assign an ElevenLabs voice_id to a character, plus optional
    style settings. Used before ``run_voice_recording``."""
    proj = _load_project(slug)
    c = proj.load_character(character_id)
    voice = c.setdefault("voice", {})
    voice["voice_id"] = voice_id
    if voice_name:
        voice["voice_name"] = voice_name
    for k, v in (("stability", stability),
                 ("similarity_boost", similarity_boost),
                 ("style", style)):
        if v is not None:
            voice[k] = float(v)
    proj.save_character(character_id, c)
    return voice


@mcp.tool()
def auto_cast_voices(slug: str, chapter_id: Optional[str] = None,
                    dry_run: bool = False) -> dict:
    """Ask Claude to match every unvoiced named character to the
    best-fit ElevenLabs voice based on each character's description
    and the story context. Writes voice_id + voice_name onto each
    character. Pass ``chapter_id`` to restrict matching to one
    chapter's speaking characters."""
    from apis.claude_client import ClaudeClient
    from apis.elevenlabs import ElevenLabsClient
    proj = _load_project(slug)

    try:
        idx = proj.load_character_index()
    except FileNotFoundError:
        return {"status": "no_characters"}

    with ElevenLabsClient() as el:
        voices = el.list_voices()

    cast = []
    claude = ClaudeClient()
    for entry in idx.get("characters", []):
        cid = entry["character_id"]
        try:
            c = proj.load_character(cid)
        except FileNotFoundError:
            continue
        if (c.get("voice") or {}).get("voice_id"):
            continue  # already voiced
        match = claude.match_voices(
            character=c, voices=voices, chapter_id=chapter_id,
        )
        if not match:
            continue
        voice_id = match.get("voice_id")
        if not voice_id:
            continue
        if dry_run:
            cast.append({"character_id": cid, "would_assign": voice_id,
                         "voice_name": match.get("name")})
            continue
        voice = c.setdefault("voice", {})
        voice["voice_id"] = voice_id
        voice["voice_name"] = match.get("name")
        voice["auto_cast_reason"] = match.get("reason", "")
        proj.save_character(cid, c)
        cast.append({"character_id": cid, "assigned": voice_id,
                     "voice_name": match.get("name")})
    return {"status": "dry_run" if dry_run else "complete", "cast": cast}


# ======================================================================
# 6. Character sheets + LoRAs (visual consistency)
# ======================================================================

@mcp.tool()
def run_character_sheets(slug: str, character_id: Optional[str] = None,
                        force: bool = False) -> dict:
    """Generate character reference sheets (multi-pose portraits used
    for LoRA training and prompt consistency). Pass a specific
    ``character_id`` to run only that one; omit to loop all. Uses
    ComfyUI + SDXL; typical time per character: 2-5 minutes.

    Blocks until done — consider ``run_character_sheets_async`` for
    long runs.
    """
    from stages.character_sheets import CharacterSheetStage
    proj = _load_project(slug)
    return CharacterSheetStage(proj).run(
        character_id=character_id, force=force,
    )


@mcp.tool()
def run_character_sheets_async(slug: str,
                              character_id: Optional[str] = None,
                              force: bool = False) -> dict:
    """Non-blocking version of run_character_sheets."""
    from stages.character_sheets import CharacterSheetStage

    def _run(**k):
        proj = _load_project(slug)
        return CharacterSheetStage(proj).run(
            character_id=character_id, force=force, **k,
        )
    return {"job_id": _spawn_job(
        f"character_sheets:{slug}:{character_id or 'all'}", _run, {}
    )}


@mcp.tool()
def generate_character_reference_image(slug: str, character_id: str,
                                       dry_run: bool = False) -> dict:
    """Generate a single full-body reference portrait at
    ``characters/<id>/reference.png`` for this character, grounded
    in the world bible's visual palette. Used by the storyboard
    stage as a style anchor."""
    from apis.prompt_builder import build_character_reference_prompt
    from apis.comfyui import ComfyUIClient
    proj = _load_project(slug)
    character = proj.load_character(character_id)
    world = proj.load_world_bible().get("world_bible", {})
    prompt = build_character_reference_prompt(character, world)
    out_path = proj._path("characters", character_id, "reference.png")
    if dry_run:
        return {"status": "dry_run", "prompt": prompt}
    with ComfyUIClient() as comfy:
        meta = comfy.generate_character_reference(
            prompt=prompt, output_path=str(out_path),
        )
    character.setdefault("assets", {})["reference_image"] = \
        str(out_path.relative_to(proj.root)).replace("\\", "/")
    proj.save_character(character_id, character)
    return {"status": "complete", "path": str(out_path),
            "meta": meta}


@mcp.tool()
def train_character_lora_async(slug: str, character_id: str,
                              force: bool = False) -> dict:
    """Kick off LoRA training for one character from their sheets.
    Returns a job_id; LoRA training is expensive (30+ minutes on a
    4090), so this is always async."""
    from stages.character_sheets import LoRATrainingStage

    def _run(**k):
        proj = _load_project(slug)
        return LoRATrainingStage(proj).run(
            character_id=character_id, force=force, **k,
        )
    return {"job_id": _spawn_job(
        f"lora:{slug}:{character_id}", _run, {}
    )}


# ======================================================================
# 7. Cinematographer (screenplay → shots)
# ======================================================================

@mcp.tool()
def run_cinematographer(slug: str, chapter_id: str,
                       scene_id: Optional[str] = None,
                       dry_run: bool = False) -> dict:
    """Break an approved screenplay into cinematic shots with camera
    direction, framing, lens, composition, and dialogue
    assignments. Typically 2-5 minutes per chapter."""
    from stages.pipeline import CinematographerStage
    proj = _load_project(slug)
    return CinematographerStage(proj).run(
        chapter_id=chapter_id, scene_id=scene_id, dry_run=dry_run,
    )


@mcp.tool()
def list_shots(slug: str, chapter_id: str) -> list[dict]:
    """List every shot in a chapter with shot_id, label, duration,
    character presence, and whether it has dialogue + storyboard +
    preview video yet."""
    proj = _load_project(slug)
    out: list[dict] = []
    shots_dir = proj._path("chapters", chapter_id, "shots")
    if not shots_dir.exists():
        return out
    for sd in sorted(shots_dir.iterdir()):
        if not sd.is_dir():
            continue
        shot_path = sd / "shot.json"
        if not shot_path.exists():
            continue
        with open(shot_path, "r", encoding="utf-8") as f:
            s = json.load(f)
        out.append({
            "shot_id": s.get("shot_id"),
            "scene_id": s.get("scene_id"),
            "label": s.get("label", ""),
            "shot_type": (s.get("cinematic") or {}).get("shot_type", ""),
            "duration_sec": s.get("duration_sec"),
            "characters": [
                c.get("character_id", c) if isinstance(c, dict) else c
                for c in s.get("characters_in_frame", [])
            ],
            "has_dialogue": bool((s.get("audio") or {}).get("lines")),
            "has_storyboard": (sd / "storyboard.png").exists(),
            "has_preview": (sd / "preview.mp4").exists(),
            "has_preview_vertical": (sd / "preview_vertical.mp4").exists(),
        })
    return out


@mcp.tool()
def get_shot(slug: str, chapter_id: str, shot_id: str) -> dict:
    """Return the full ``shot.json`` for one shot."""
    scene_id = "_".join(shot_id.split("_")[:2])
    return _load_project(slug).load_shot(chapter_id, scene_id, shot_id)


@mcp.tool()
def update_shot(slug: str, chapter_id: str, shot_id: str,
               updates: dict) -> dict:
    """Shallow-merge ``updates`` into the shot's ``shot.json``. Use
    for tweaks like ``{"edit": {"enabled": false, "notes": "cut for
    pacing"}}`` or ``{"duration_sec": 2.5}``. Nested dicts merge
    one level."""
    scene_id = "_".join(shot_id.split("_")[:2])
    proj = _load_project(slug)
    shot = proj.load_shot(chapter_id, scene_id, shot_id)

    def _merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                _merge(dst[k], v)
            else:
                dst[k] = v
    _merge(shot, updates)
    proj.save_shot(chapter_id, scene_id, shot_id, shot)
    return shot


# ======================================================================
# 8. Storyboard (shot → image)
# ======================================================================

@mcp.tool()
def run_storyboard_async(slug: str, chapter_id: str,
                        force: bool = False) -> dict:
    """Render a storyboard PNG (both 16:9 and 9:16) for every shot in
    the chapter. Uses ComfyUI + SDXL + character LoRAs for visual
    consistency. Long-running (1-3 min per shot) — always async."""
    from stages.pipeline import StoryboardStage

    def _run(**k):
        proj = _load_project(slug)
        return StoryboardStage(proj).run(
            chapter_id=chapter_id, force=force, **k,
        )
    return {"job_id": _spawn_job(f"storyboard:{slug}:{chapter_id}",
                                 _run, {})}


@mcp.tool()
def regenerate_shot_storyboard(slug: str, chapter_id: str,
                              shot_id: str,
                              feedback: Optional[str] = None,
                              dry_run: bool = False) -> dict:
    """Re-roll the storyboard for one shot, optionally rewriting the
    prompt using ``feedback``. Useful when a shot's framing looks
    wrong after the main batch run."""
    from apis.claude_client import ClaudeClient
    from apis.comfyui import ComfyUIClient
    from apis.prompt_builder import build_storyboard_prompt
    scene_id = "_".join(shot_id.split("_")[:2])
    proj = _load_project(slug)
    shot = proj.load_shot(chapter_id, scene_id, shot_id)
    world = proj.load_world_bible().get("world_bible", {})

    if feedback:
        claude = ClaudeClient()
        new_prompt = claude.rewrite_shot_prompt(
            shot=shot, feedback=feedback, world=world,
        )
        shot.setdefault("storyboard", {})["storyboard_prompt"] = new_prompt
    prompt_text = build_storyboard_prompt(shot, world, project=proj)
    if dry_run:
        return {"status": "dry_run", "prompt": prompt_text}

    out_path = proj._path("chapters", chapter_id, "shots", shot_id,
                          "storyboard.png")
    with ComfyUIClient() as comfy:
        meta = comfy.generate_storyboard(
            prompt=prompt_text, output_path=str(out_path), shot=shot,
        )
    shot["storyboard"]["generated"] = True
    shot["storyboard"]["generation_meta"] = meta
    proj.save_shot(chapter_id, scene_id, shot_id, shot)
    return {"status": "complete", "path": str(out_path), "meta": meta}


# ======================================================================
# 9. Voice recording
# ======================================================================

@mcp.tool()
def run_voice_recording_async(slug: str, chapter_id: str,
                             dry_run: bool = False) -> dict:
    """Generate ElevenLabs audio for every dialogue line in a
    chapter. Costs real money — always review the dry-run estimate
    first. Requires the ``screenplay_to_voice_recording`` gate to be
    approved (see ``approve_gate``)."""
    from stages.pipeline import VoiceRecordingStage

    def _run(**k):
        proj = _load_project(slug)
        return VoiceRecordingStage(proj).run(
            chapter_id=chapter_id, dry_run=dry_run, **k,
        )
    return {"job_id": _spawn_job(
        f"voice_recording:{slug}:{chapter_id}", _run, {}
    )}


# ======================================================================
# 10. Editing room (human edit of the cut)
# ======================================================================

@mcp.tool()
def set_shot_enabled(slug: str, chapter_id: str, shot_id: str,
                    enabled: bool,
                    notes: Optional[str] = None) -> dict:
    """Toggle whether a shot is included in the final cut. Disabling
    a dialogue shot also excludes that dialogue from the produced
    audio track."""
    scene_id = "_".join(shot_id.split("_")[:2])
    proj = _load_project(slug)
    shot = proj.load_shot(chapter_id, scene_id, shot_id)
    edit = shot.setdefault("edit", {})
    edit["enabled"] = bool(enabled)
    if notes is not None:
        edit["notes"] = notes
    proj.save_shot(chapter_id, scene_id, shot_id, shot)
    return edit


@mcp.tool()
def set_shot_duration(slug: str, chapter_id: str, shot_id: str,
                     duration_sec: float) -> dict:
    """Override the cut duration for a shot. Pass 0 to restore the
    original/audio-derived duration."""
    scene_id = "_".join(shot_id.split("_")[:2])
    proj = _load_project(slug)
    shot = proj.load_shot(chapter_id, scene_id, shot_id)
    edit = shot.setdefault("edit", {})
    edit["duration_sec"] = float(duration_sec) if duration_sec else None
    proj.save_shot(chapter_id, scene_id, shot_id, shot)
    return edit


@mcp.tool()
def approve_gate(slug: str, gate_name: str,
                approved_by: str = "mcp-agent") -> dict:
    """Open a pipeline gate so the next stage can run. Gates are the
    project's spending checkpoints (screenplay_to_voice_recording,
    cut_to_sound, sound_to_assets, etc.). Pass the gate name exactly
    as it appears in ``get_project_status().gates``."""
    from datetime import datetime
    proj = _load_project(slug)
    gates = proj.data.setdefault("pipeline", {}).setdefault("gates", {})
    if gate_name not in gates:
        # Accept the modern stage names even when the template hasn't
        # had them added yet — the runtime stages check by name.
        gates[gate_name] = {
            "approved": False, "approved_by": None, "approved_at": None,
            "description": f"MCP-created gate: {gate_name}",
        }
    g = gates[gate_name]
    g["approved"] = True
    g["approved_by"] = approved_by
    g["approved_at"] = datetime.now().isoformat()
    proj.save_project()
    return {"gate_name": gate_name, "approved": True}


# ======================================================================
# 11. Sound FX + audio score
# ======================================================================

@mcp.tool()
def run_sound_fx_async(slug: str, chapter_id: str,
                      provider: str = "auto",
                      dry_run: bool = False) -> dict:
    """Generate per-shot sound effects for the chapter. Claude
    suggests 1-3 SFX per shot using the shot-local action context;
    each prompt is routed via ``apis.sfx_router`` — broadband foley
    → local ComfyUI (free), tonal/voiced → ElevenLabs ($0.10 each).
    ``provider`` overrides to ``"comfyui"`` or ``"elevenlabs"`` to
    force one backend."""
    from stages.pipeline import SoundFXStage

    def _run(**k):
        proj = _load_project(slug)
        os.environ["SFX_PROVIDER"] = provider
        return SoundFXStage(proj).run(
            chapter_id=chapter_id, dry_run=dry_run, **k,
        )
    return {"job_id": _spawn_job(f"sound_fx:{slug}:{chapter_id}",
                                 _run, {})}


@mcp.tool()
def run_audio_score_async(slug: str, chapter_id: str,
                         dry_run: bool = False) -> dict:
    """Generate underscore music cues for the chapter. Claude
    analyses the screenplay to pick thematic pieces, ElevenLabs
    generates each. Gate: ``cut_to_sound``."""
    from stages.pipeline import AudioScoreStage

    def _run(**k):
        proj = _load_project(slug)
        return AudioScoreStage(proj).run(
            chapter_id=chapter_id, dry_run=dry_run, **k,
        )
    return {"job_id": _spawn_job(f"audio_score:{slug}:{chapter_id}",
                                 _run, {})}


@mcp.tool()
def mix_preview_audio(slug: str, shot_id: str,
                     orientation: str = "horizontal") -> dict:
    """Post-mix the shot's SFX into its preview.mp4 without
    re-rendering the video. Cheap (a few seconds of ffmpeg), keeps
    the pristine Wan render as preview_raw.mp4."""
    from utils.mix_preview_audio import mix_shot
    proj = _load_project(slug)
    if orientation == "both":
        return {
            "horizontal": mix_shot(proj, shot_id, "horizontal"),
            "vertical": mix_shot(proj, shot_id, "vertical"),
        }
    return mix_shot(proj, shot_id, orientation)


# ======================================================================
# 12. Preview video (Wan 2.1 + InfiniTalk)
# ======================================================================

@mcp.tool()
def run_preview_video_async(
    slug: str,
    shot_id: Optional[str] = None,
    chapter_id: Optional[str] = None,
    orientation: str = "horizontal",
    force: bool = False,
) -> dict:
    """Render talking-head / silent preview video(s) with Wan 2.1.
    Either single-shot (pass ``shot_id``) or chapter-batch (pass
    ``chapter_id``). Silent shots auto-route to Wan I2V; dialogue
    shots use InfiniTalk with lip sync. ``orientation``:
    ``"horizontal"``, ``"vertical"``, or ``"both"``. Always async —
    typical wall time: ~12s of GPU per 1s of output on a 4090."""
    from stages.pipeline import PreviewVideoStage

    def _run(**k):
        proj = _load_project(slug)
        return PreviewVideoStage(proj).run(
            shot_id=shot_id, chapter_id=chapter_id,
            orientation=orientation, force=force, **k,
        )
    tag = shot_id or f"chapter:{chapter_id}"
    return {"job_id": _spawn_job(f"preview_video:{slug}:{tag}",
                                 _run, {})}


# ======================================================================
# 13. Costs, gates, drift
# ======================================================================

@mcp.tool()
def get_cost_ledger(slug: str) -> dict:
    """Return the project's cost ledger — per-API totals, recent
    transactions. Use before running costly stages."""
    return _load_project(slug).load_cost_ledger()


@mcp.tool()
def get_gates(slug: str) -> dict:
    """Return every gate's approval state. Useful before calling a
    stage that requires a specific gate."""
    proj = _load_project(slug)
    return (proj.data.get("pipeline") or {}).get("gates") or {}


@mcp.tool()
def check_drift(slug: str) -> dict:
    """Find shots whose ``world_version_built_against`` is older than
    the current world bible. Run before a big render to know what
    needs refreshing."""
    from core.state_manager import StateManager
    proj = _load_project(slug)
    sm = StateManager(proj)
    return {"drifted": sm.check_drift() or []}


@mcp.tool()
def diversify_split_storyboards(
    slug: str, chapter_id: str,
    shot_id: Optional[str] = None, dry_run: bool = False,
    force: bool = False,
) -> dict:
    """For each shot in the chapter that's a split continuation
    (ids like ``ch01_sc01_sh006b``, labels ending ``(cont'd)``),
    ask Claude for an alternate camera angle and render a fresh
    storyboard (both 16:9 and 9:16) through ComfyUI so the preview
    cut doesn't show the same frame on both halves. Any stale
    ``preview_*.mp4`` on the touched shot is cleared — run
    ``run_preview_video_async`` afterward to regenerate the videos
    against the new storyboards.

    Pass ``shot_id`` to target a single continuation shot; omit to
    diversify every continuation in the chapter.

    ``force=False`` (default) skips continuations whose
    ``generation_meta.diversified_from`` is already set, so a re-run
    only touches shots that were missed on the prior pass.
    ``force=True`` re-rolls every continuation (useful if you want
    fresh Claude-suggested angles)."""
    from utils.diversify_split_storyboards import diversify_chapter
    proj = _load_project(slug)
    return diversify_chapter(
        project=proj, chapter_id=chapter_id,
        only_shot=shot_id, dry_run=dry_run, force=force,
    )


@mcp.tool()
def comfyui_unstick(force: bool = False, wait_sec: float = 5.0,
                   comfyui_url: str | None = None) -> dict:
    """Recover a wedged ComfyUI server by sending /interrupt and
    clearing the pending queue. If the current job is still running
    after ``wait_sec`` and ``force`` is true, hard-kill the ComfyUI
    process on the port. Returns before/after queue state.

    Use this when preview_video or storyboard jobs have been running
    impossibly long (e.g. 30+ minutes on a shot that should take
    minutes) — the sampler has hung and is consuming the GPU.
    """
    import httpx
    import os
    from urllib.parse import urlparse

    base = (comfyui_url or os.getenv("COMFYUI_URL",
                                     "http://localhost:8000")).rstrip("/")
    result: dict[str, Any] = {"url": base}
    with httpx.Client(timeout=15) as c:
        try:
            before = c.get(f"{base}/queue").json()
        except httpx.HTTPError as e:
            return {"status": "unreachable", "error": str(e)}
        result["before"] = {
            "running": len(before.get("queue_running", [])),
            "pending": len(before.get("queue_pending", [])),
        }
        c.post(f"{base}/interrupt")
        c.post(f"{base}/queue", json={"clear": True})
        time.sleep(wait_sec)
        after = c.get(f"{base}/queue").json()
        result["after"] = {
            "running": len(after.get("queue_running", [])),
            "pending": len(after.get("queue_pending", [])),
        }

    if result["after"]["running"] == 0:
        result["status"] = "recovered_soft"
        return result

    if not force:
        result["status"] = "still_running"
        result["advice"] = "Call again with force=True to hard-kill."
        return result

    # Hard kill.
    import subprocess
    port = urlparse(base).port or 8000
    try:
        ns = subprocess.run(["netstat", "-ano"],
                            capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return {**result, "status": "kill_failed", "error": str(e)}
    pid = None
    for line in ns.splitlines():
        if "LISTENING" in line and f":{port} " in line:
            parts = line.split()
            if parts[-1].isdigit():
                pid = int(parts[-1])
                break
    if pid is None:
        return {**result, "status": "no_pid_found", "port": port}
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True)
        else:
            os.kill(pid, 9)
        result["status"] = "killed"
        result["killed_pid"] = pid
    except Exception as e:  # noqa: BLE001
        result["status"] = "kill_failed"
        result["error"] = str(e)
    return result


@mcp.tool()
def gpu_status() -> dict:
    """Return info about the GPU lock: whether a GPU-heavy stage
    (preview video, storyboard, character sheets, LoRA training,
    ComfyUI SFX) is currently running, its label, how long it's
    held the GPU, and the job_id if the caller was a background job.

    GPU-heavy tools fail fast with a structured error that includes
    this info, so the caller can decide whether to wait or back off.
    Returns ``{"free": true}`` when nothing is using the GPU."""
    from core.gpu_lock import gpu_status as _status
    h = _status()
    if h is None:
        return {"free": True}
    return {"free": False, **h}


# ======================================================================
# 14. Job management
# ======================================================================

@mcp.tool()
def get_job_status(job_id: str) -> dict:
    """Return the current state of a background job: status
    (``running``, ``complete``, ``error``), progress pct, last
    message, cost_so_far, and (when done) the result or error."""
    s = _get_job(job_id)
    if not s:
        return {"error": "job not found", "job_id": job_id}
    return s


@mcp.tool()
def wait_for_job(job_id: str, timeout_sec: float = 900.0) -> dict:
    """Block until ``job_id`` completes or the timeout elapses.
    Returns the same shape as ``get_job_status``. Cap
    ``timeout_sec`` to the maximum time you're willing to wait in
    one call; poll again if it's still running."""
    return _wait_job(job_id, timeout_sec=timeout_sec)


@mcp.tool()
def list_jobs() -> list[dict]:
    """List every job tracked by this server since startup (includes
    completed and errored). Each entry: job_id, label, status,
    pct, message, started_at, finished_at."""
    with _JOB_LOCK:
        return [
            {k: v for k, v in s.items()
             if k not in ("traceback", "result")}
            for s in _JOBS.values()
        ]


# ======================================================================
# Main / CLI
# ======================================================================

def _parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--projects-dir", required=True,
                    help="Directory containing per-project subfolders")
    ap.add_argument("--host", default="0.0.0.0",
                    help="Bind address (default 0.0.0.0 for LAN)")
    ap.add_argument("--port", type=int, default=5758)
    ap.add_argument("--transport", default="sse",
                    choices=("sse", "stdio", "streamable-http"),
                    help="Transport. Use sse for network, stdio for "
                         "same-machine IPC.")
    return ap.parse_args()


def main() -> int:
    global PROJECTS_DIR
    args = _parse_args()
    PROJECTS_DIR = Path(args.projects_dir).expanduser().resolve()
    if not PROJECTS_DIR.exists():
        raise SystemExit(f"projects dir not found: {PROJECTS_DIR}")

    logger.info("Projects dir: %s", PROJECTS_DIR)
    logger.info("Starting MCP server on %s://%s:%s",
                args.transport, args.host, args.port)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # Both "sse" and "streamable-http" take host/port; FastMCP
        # exposes them via settings on the underlying app.
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
