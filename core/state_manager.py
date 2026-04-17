"""
core/state_manager.py
Tracks production status across all entities.
Detects version drift when world or character schemas update.
Surfaces what's blocked, what's ready, what needs review.
"""

from typing import List, Dict, Optional
from .project import Project


class StateManager:

    def __init__(self, project: Project):
        self.project = project

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    def get_project_status(self) -> dict:
        """High-level status across all chapters."""
        chapter_ids = self.project.get_all_chapter_ids()
        chapters = []

        for cid in chapter_ids:
            try:
                chapter = self.project.load_chapter(cid)
                production = chapter.get("production", {})
                # Supplement total_shots from shot index if chapter JSON is stale
                if not production.get("total_shots"):
                    try:
                        index = self.project.load_shot_index(cid, "")
                        production["total_shots"] = len(index.get("shots", []))
                    except FileNotFoundError:
                        pass
                chapters.append({
                    "chapter_id": cid,
                    "title": chapter.get("title", cid),
                    "status": chapter.get("status", "pending"),
                    "production": production,
                    "costs": chapter.get("costs", {}),
                    "approved": chapter.get("meta", {}).get("approved", False)
                })
            except FileNotFoundError:
                chapters.append({
                    "chapter_id": cid,
                    "status": "schema_missing",
                    "production": {}
                })
            except Exception:
                chapters.append({
                    "chapter_id": cid,
                    "status": "error",
                    "production": {}
                })

        return {
            "project_id": self.project.id,
            "pipeline_stage": self.project.get_pipeline_stage(),
            "chapters": chapters,
            "gates": self._get_gate_status(),
            "world_version": self.project.get_world_version(),
            "voice_casting": self._get_voice_casting_summary()
        }

    def _get_gate_status(self) -> dict:
        gates = self.project.data["pipeline"]["gates"]
        approvals = self.project.data["pipeline"].get("gate_approvals", {})
        result = {}
        for gate_name, gate_cfg in gates.items():
            approval = approvals.get(gate_name, {})
            result[gate_name] = {
                "description": gate_cfg.get("description", ""),
                "approved": (
                    gate_cfg.get("approved", False)
                    or (isinstance(approval, dict) and approval.get("approved", False))
                ),
                "approver": (
                    gate_cfg.get("approved_by")
                    or (approval.get("approver") if isinstance(approval, dict) else None)
                ),
                "timestamp": (
                    gate_cfg.get("approved_at")
                    or (approval.get("timestamp") if isinstance(approval, dict) else None)
                ),
            }
        return result

    def _get_voice_casting_summary(self) -> list:
        """Return voice assignment status for all characters."""
        result = []
        try:
            char_ids = self.project.get_all_character_ids()
            for cid in char_ids:
                try:
                    char = self.project.load_character(cid)
                    voice = char.get("voice", {})
                    has_voice = bool(
                        voice.get("voice_id") or voice.get("elevenlabs_voice_id")
                    )
                    result.append({
                        "character_id": cid,
                        "display_name": char.get("display_name", cid),
                        "has_voice": has_voice,
                    })
                except FileNotFoundError:
                    pass
        except Exception:
            pass
        return result

    def get_chapter_status(self, chapter_id: str) -> dict:
        """Detailed status for one chapter including all scenes and shots."""
        chapter = self.project.load_chapter(chapter_id)
        scene_ids = self._get_scene_ids_for_chapter(chapter_id)

        # Load shot index once (one index file per chapter covers all scenes)
        try:
            index = self.project.load_shot_index(chapter_id, "")
            all_shots = index.get("shots", [])
        except FileNotFoundError:
            all_shots = []

        scenes = []
        for scene_id in scene_ids:
            # Filter shots belonging to this scene by shot_id prefix
            scene_shots = [s for s in all_shots
                           if s.get("shot_id", "").startswith(scene_id + "_")]
            scenes.append({
                "scene_id": scene_id,
                "title": scene_id,
                "status": {},
                "dialogue_lines": sum(s.get("dialogue_lines", 0) for s in scene_shots),
                "audio_generated": all(s.get("audio_approved", False) for s in scene_shots) if scene_shots else False,
                "shots": {
                    "total": len(scene_shots),
                    "shot_ids": [s["shot_id"] for s in scene_shots],
                    "shot_status": {
                        s["shot_id"]: {
                            "storyboard_approved": s.get("storyboard_approved", False),
                            "audio_approved": s.get("audio_approved", False),
                            "flags": s.get("flags", []),
                        }
                        for s in scene_shots
                    },
                    "storyboard_approved": sum(1 for s in scene_shots if s.get("storyboard_approved")),
                    "audio_approved": sum(1 for s in scene_shots if s.get("audio_approved")),
                    "built": sum(1 for s in scene_shots if s.get("built")),
                    "preview_rendered": sum(1 for s in scene_shots if s.get("preview_rendered")),
                    "final_rendered": sum(1 for s in scene_shots if s.get("final_rendered")),
                    "flagged": [s["shot_id"] for s in scene_shots if s.get("flags")],
                },
            })

        # If no scenes from chapter structure but shots exist, create a
        # synthetic scene entry so the UI can still display shots
        if not scenes and all_shots:
            scene_id = all_shots[0].get("shot_id", "").rsplit("_", 1)[0]
            # Group by scene_id prefix (first two underscore-separated parts)
            scene_groups = {}
            for s in all_shots:
                sid = "_".join(s.get("shot_id", "").split("_")[:2])
                scene_groups.setdefault(sid, []).append(s)
            for sid, shots in scene_groups.items():
                scenes.append({
                    "scene_id": sid,
                    "title": sid,
                    "status": {},
                    "dialogue_lines": sum(s.get("dialogue_lines", 0) for s in shots),
                    "audio_generated": False,
                    "shots": {
                        "total": len(shots),
                        "shot_ids": [s["shot_id"] for s in shots],
                        "shot_status": {
                            s["shot_id"]: {
                                "storyboard_approved": s.get("storyboard_approved", False),
                                "audio_approved": s.get("audio_approved", False),
                                "flags": s.get("flags", []),
                            }
                            for s in shots
                        },
                        "storyboard_approved": sum(1 for s in shots if s.get("storyboard_approved")),
                        "audio_approved": sum(1 for s in shots if s.get("audio_approved")),
                        "built": sum(1 for s in shots if s.get("built")),
                        "preview_rendered": sum(1 for s in shots if s.get("preview_rendered")),
                        "final_rendered": sum(1 for s in shots if s.get("final_rendered")),
                        "flagged": [s["shot_id"] for s in shots if s.get("flags")],
                    },
                })

        return {
            "chapter_id": chapter_id,
            "title": chapter.get("title", chapter_id),
            "status": chapter.get("status", "pending"),
            "production": chapter.get("production", {}),
            "scenes": scenes,
            "costs": chapter.get("costs", {})
        }

    def _get_scene_ids_for_chapter(self, chapter_id: str) -> List[str]:
        chapter = self.project.load_chapter(chapter_id)
        scene_ids = []
        for act in chapter.get("structure", {}).get("acts", []):
            scene_ids.extend(act.get("scene_ids", []))
        return scene_ids

    def _get_shot_summary(self, chapter_id: str, scene_id: str) -> dict:
        try:
            index = self.project.load_shot_index(chapter_id, scene_id)
            all_shots = index.get("shots", [])
            # Filter to shots belonging to this scene (shot_id prefix match)
            shots = [s for s in all_shots
                     if s.get("shot_id", "").startswith(scene_id + "_")]
            # If no matches, return all (single-scene chapter)
            if not shots:
                shots = all_shots
            return {
                "total": len(shots),
                "shot_ids": [s["shot_id"] for s in shots],
                "storyboard_approved": sum(1 for s in shots if s.get("storyboard_approved")),
                "audio_approved": sum(1 for s in shots if s.get("audio_approved")),
                "built": sum(1 for s in shots if s.get("built")),
                "preview_rendered": sum(1 for s in shots if s.get("preview_rendered")),
                "final_rendered": sum(1 for s in shots if s.get("final_rendered")),
                "flagged": [s["shot_id"] for s in shots if s.get("flags")]
            }
        except FileNotFoundError:
            return {"total": 0, "shot_ids": []}

    # ------------------------------------------------------------------
    # Version drift detection
    # ------------------------------------------------------------------

    def find_version_drift(self) -> List[dict]:
        """
        Scans all built shots and finds any where
        world_version_built_against or character_version_built_against
        is behind the current version.
        Returns list of shots that need rebuild.
        """
        current_world_version = self.project.get_world_version()
        drifted = []

        for chapter_id in self.project.get_all_chapter_ids():
            try:
                chapter = self.project.load_chapter(chapter_id)
            except FileNotFoundError:
                continue

            for act in chapter.get("structure", {}).get("acts", []):
                for scene_id in act.get("scene_ids", []):
                    try:
                        index = self.project.load_shot_index(chapter_id, scene_id)
                    except FileNotFoundError:
                        continue

                    for shot_summary in index.get("shots", []):
                        shot_id = shot_summary["shot_id"]
                        world_ver = shot_summary.get("world_version")

                        if world_ver and world_ver != current_world_version:
                            drifted.append({
                                "shot_id": shot_id,
                                "chapter_id": chapter_id,
                                "scene_id": scene_id,
                                "built_against_world": world_ver,
                                "current_world": current_world_version,
                                "drift_type": "world_version"
                            })

        return drifted

    # ------------------------------------------------------------------
    # Readiness checks — what can proceed right now
    # ------------------------------------------------------------------

    def get_ready_for_storyboard(self) -> List[str]:
        """Shots with approved screenplay but no storyboard yet."""
        ready = []
        for chapter_id in self.project.get_all_chapter_ids():
            try:
                chapter = self.project.load_chapter(chapter_id)
                if chapter["screenplay"]["approved"]:
                    for act in chapter["structure"]["acts"]:
                        for scene_id in act["scene_ids"]:
                            try:
                                index = self.project.load_shot_index(chapter_id, scene_id)
                                for shot in index["shots"]:
                                    if not shot.get("storyboard_approved"):
                                        ready.append(shot["shot_id"])
                            except FileNotFoundError:
                                pass
            except FileNotFoundError:
                pass
        return ready

    def get_ready_for_voice_recording(self) -> List[dict]:
        """
        Chapters with screenplay ready for voice recording.
        Gate: screenplay_to_voice_recording must be approved.
        """
        if not self.project.is_gate_open("screenplay_to_voice_recording"):
            return []

        ready = []
        for chapter_id in self.project.get_all_chapter_ids():
            screenplay_path = self.project._path("chapters", chapter_id, "screenplay.md")
            if not screenplay_path.exists():
                continue
            # Check if recordings already exist
            try:
                recs = self.project.load_recordings(chapter_id)
                if recs.get("recordings"):
                    continue  # already recorded
            except FileNotFoundError:
                pass
            ready.append({
                "chapter_id": chapter_id,
                "status": "screenplay_ready",
            })
        return ready

    def get_ready_for_mesh_generation(self) -> List[dict]:
        """
        Assets approved for generation in the manifest.
        Gate: assets_to_scene must be approved.
        """
        if not self.project.is_gate_open("sound_to_assets"):
            return []

        manifest = self.project.load_asset_manifest()
        ready = []

        for category in ["environments", "props", "costumes", "vegetation"]:
            for asset in manifest["assets"].get(category, []):
                if (asset.get("approved_for_generation")
                        and asset["meshy"]["status"] == "pending"):
                    ready.append({
                        "asset_id": asset["asset_id"],
                        "category": category,
                        "detail_level": asset.get("detail_level", "medium"),
                        "meshy_config": asset["meshy"]
                    })

        return ready

    # ------------------------------------------------------------------
    # Print helpers
    # ------------------------------------------------------------------

    def print_project_status(self):
        status = self.get_project_status()
        world_ver = status["world_version"]
        print(f"\n{'='*55}")
        print(f"  {status['project_id']}  |  Stage: {status['pipeline_stage']}  |  World: v{world_ver}")
        print(f"{'='*55}")

        print(f"\n  {'Chapter':<35} {'Status':<15} {'Shots':>5} {'Built':>5} {'$':>6}")
        print(f"  {'-'*35} {'-'*15} {'-'*5} {'-'*5} {'-'*6}")

        for ch in status["chapters"]:
            prod = ch.get("production", {})
            total = prod.get("total_shots", 0)
            built = prod.get("scenes_built_in_ue5", 0)
            cost = ch.get("costs", {}).get("chapter_total_usd", 0.0)
            title = ch.get("title", ch.get("chapter_id", "?"))
            print(f"  {title:<35} {ch.get('status','?'):<15} {total:>5} {built:>5} ${cost:>5.2f}")

        print(f"\n  Gates:")
        for gate_name, gate in status["gates"].items():
            icon = "[OK]" if gate["approved"] else "[  ]"
            print(f"    {icon} {gate_name}")

        drift = self.find_version_drift()
        if drift:
            print(f"\n  WARNING: Version Drift: {len(drift)} shots built against old world version")
            print(f"     Run: orchestrator check-drift  for details")

        print()
