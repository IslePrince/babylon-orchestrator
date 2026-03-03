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
                chapters.append({
                    "chapter_id": cid,
                    "title": chapter["title"],
                    "status": chapter["status"],
                    "production": chapter["production"],
                    "costs": chapter["costs"],
                    "approved": chapter["meta"]["approved"]
                })
            except FileNotFoundError:
                chapters.append({
                    "chapter_id": cid,
                    "status": "schema_missing",
                    "production": {}
                })

        return {
            "project_id": self.project.id,
            "pipeline_stage": self.project.get_pipeline_stage(),
            "chapters": chapters,
            "gates": self._get_gate_status(),
            "world_version": self.project.get_world_version()
        }

    def _get_gate_status(self) -> dict:
        gates = self.project.data["pipeline"]["gates"]
        approvals = self.project.data["pipeline"].get("gate_approvals", {})
        result = {}
        for gate_name, gate_cfg in gates.items():
            result[gate_name] = {
                "requires": gate_cfg["requires"],
                "approved": gate_name in approvals and approvals[gate_name].get("approved", False),
                "approver": approvals.get(gate_name, {}).get("approver"),
                "timestamp": approvals.get(gate_name, {}).get("timestamp")
            }
        return result

    def get_chapter_status(self, chapter_id: str) -> dict:
        """Detailed status for one chapter including all scenes and shots."""
        chapter = self.project.load_chapter(chapter_id)
        scenes = []

        for scene_id in self._get_scene_ids_for_chapter(chapter_id):
            try:
                scene = self.project.load_scene(chapter_id, scene_id)
                shot_summary = self._get_shot_summary(chapter_id, scene_id)
                scenes.append({
                    "scene_id": scene_id,
                    "title": scene["title"],
                    "status": scene["meta"],
                    "dialogue_lines": scene["dialogue"]["line_count"],
                    "audio_generated": scene["audio"]["generated"],
                    "shots": shot_summary
                })
            except FileNotFoundError:
                scenes.append({"scene_id": scene_id, "status": "missing"})

        return {
            "chapter_id": chapter_id,
            "title": chapter["title"],
            "status": chapter["status"],
            "production": chapter["production"],
            "scenes": scenes,
            "costs": chapter["costs"]
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
            shots = index.get("shots", [])
            return {
                "total": len(shots),
                "storyboard_approved": sum(1 for s in shots if s.get("storyboard_approved")),
                "audio_approved": sum(1 for s in shots if s.get("audio_approved")),
                "built": sum(1 for s in shots if s.get("built")),
                "preview_rendered": sum(1 for s in shots if s.get("preview_rendered")),
                "final_rendered": sum(1 for s in shots if s.get("final_rendered")),
                "flagged": [s["shot_id"] for s in shots if s.get("flags")]
            }
        except FileNotFoundError:
            return {"total": 0}

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

    def get_ready_for_audio(self) -> List[dict]:
        """
        Dialogue lines ready for ElevenLabs generation.
        Gate: storyboard_to_audio must be approved.
        """
        if not self.project.is_gate_open("storyboard_to_audio"):
            return []

        ready = []
        for chapter_id in self.project.get_all_chapter_ids():
            for scene_id in self._get_scene_ids_for_chapter(chapter_id):
                try:
                    scene = self.project.load_scene(chapter_id, scene_id)
                    if not scene["meta"].get("storyboard_approved"):
                        continue
                    for line in scene["dialogue"]["lines"]:
                        if line["audio_status"] == "pending":
                            ready.append({
                                "chapter_id": chapter_id,
                                "scene_id": scene_id,
                                "line_id": line["line_id"],
                                "character_id": line["character_id"],
                                "text": line["text"],
                                "audio_ref": line["audio_ref"]
                            })
                except FileNotFoundError:
                    pass
        return ready

    def get_ready_for_mesh_generation(self) -> List[dict]:
        """
        Assets approved for generation in the manifest.
        Gate: assets_to_scene must be approved.
        """
        if not self.project.is_gate_open("audio_to_assets"):
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
        print(f"\n{'═'*55}")
        print(f"  {status['project_id']}  |  Stage: {status['pipeline_stage']}  |  World: v{world_ver}")
        print(f"{'═'*55}")

        print(f"\n  {'Chapter':<35} {'Status':<15} {'Shots':>5} {'Built':>5} {'$':>6}")
        print(f"  {'-'*35} {'-'*15} {'-'*5} {'-'*5} {'-'*6}")

        for ch in status["chapters"]:
            prod = ch.get("production", {})
            total = prod.get("total_shots", 0)
            built = prod.get("scenes_built_in_ue5", 0)
            cost = ch.get("costs", {}).get("chapter_total_usd", 0.0)
            print(f"  {ch['title']:<35} {ch['status']:<15} {total:>5} {built:>5} ${cost:>5.2f}")

        print(f"\n  Gates:")
        for gate_name, gate in status["gates"].items():
            icon = "✓" if gate["approved"] else "✗"
            print(f"    {icon} {gate_name}")

        drift = self.find_version_drift()
        if drift:
            print(f"\n  ⚠️  Version Drift: {len(drift)} shots built against old world version")
            print(f"     Run: orchestrator check-drift  for details")

        print()
