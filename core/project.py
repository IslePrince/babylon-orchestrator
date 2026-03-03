"""
core/project.py
Loads and manages the project schema tree.
All orchestrator operations start here.
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional


class Project:
    """
    Loads a project from its root directory.
    Provides access to all schemas and manages state writes.
    """

    def __init__(self, project_root: str):
        self.root = Path(project_root).resolve()
        if not self.root.exists():
            raise FileNotFoundError(f"Project root not found: {self.root}")

        self.project_file = self.root / "project.json"
        if not self.project_file.exists():
            raise FileNotFoundError(f"project.json not found in {self.root}")

        self.data = self._load(self.project_file)
        self.id = self.data["project_id"]

    # ------------------------------------------------------------------
    # Schema loaders
    # ------------------------------------------------------------------

    def _load(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _path(self, *parts) -> Path:
        return self.root.joinpath(*parts)

    def load_world_bible(self) -> dict:
        return self._load(self._path("world", "world_bible.json"))

    def load_character_index(self) -> dict:
        return self._load(self._path("characters", "_index.json"))

    def load_character(self, character_id: str) -> dict:
        return self._load(self._path("characters", character_id, "character.json"))

    def load_chapter_index(self) -> dict:
        return self._load(self._path("chapters", "_index.json"))

    def load_chapter(self, chapter_id: str) -> dict:
        return self._load(self._path("chapters", chapter_id, "chapter.json"))

    def load_scene(self, chapter_id: str, scene_id: str) -> dict:
        return self._load(self._path("chapters", chapter_id, "scenes", f"{scene_id}.json"))

    def load_shot(self, chapter_id: str, scene_id: str, shot_id: str) -> dict:
        return self._load(self._path("chapters", chapter_id, "shots", shot_id, "shot.json"))

    def load_shot_index(self, chapter_id: str, scene_id: str) -> dict:
        return self._load(self._path("chapters", chapter_id, "shots", "_index.json"))

    def load_asset_manifest(self) -> dict:
        return self._load(self._path("assets", "manifest.json"))

    def load_cost_ledger(self) -> dict:
        return self._load(self._path("costs", "ledger.json"))

    def load_background_types(self) -> dict:
        index_path = self._path("characters", "background_types", "_index.json")
        if not index_path.exists():
            return {}
        return self._load(index_path)

    # ------------------------------------------------------------------
    # Schema writers
    # ------------------------------------------------------------------

    def save_project(self):
        self._save(self.project_file, self.data)

    def save_chapter(self, chapter_id: str, data: dict):
        self._save(self._path("chapters", chapter_id, "chapter.json"), data)

    def save_scene(self, chapter_id: str, scene_id: str, data: dict):
        self._save(self._path("chapters", chapter_id, "scenes", f"{scene_id}.json"), data)

    def save_shot(self, chapter_id: str, scene_id: str, shot_id: str, data: dict):
        self._save(self._path("chapters", chapter_id, "shots", shot_id, "shot.json"), data)

    def save_asset_manifest(self, data: dict):
        self._save(self._path("assets", "manifest.json"), data)

    def save_cost_ledger(self, data: dict):
        self._save(self._path("costs", "ledger.json"), data)

    def save_character(self, character_id: str, data: dict):
        self._save(self._path("characters", character_id, "character.json"), data)

    # ------------------------------------------------------------------
    # Notes writers (versioned markdown)
    # ------------------------------------------------------------------

    def append_shot_note(self, chapter_id: str, shot_id: str, note: str, author: str = "orchestrator"):
        notes_path = self._path("chapters", chapter_id, "shots", shot_id, "notes.md")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n### {timestamp} — {author}\n{note}\n"
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        with open(notes_path, "a", encoding="utf-8") as f:
            f.write(entry)

    def append_chapter_note(self, chapter_id: str, note: str, author: str = "orchestrator"):
        notes_path = self._path("chapters", chapter_id, "chapter_notes.md")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n### {timestamp} — {author}\n{note}\n"
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        with open(notes_path, "a", encoding="utf-8") as f:
            f.write(entry)

    # ------------------------------------------------------------------
    # Convenience queries
    # ------------------------------------------------------------------

    def get_pipeline_stage(self) -> str:
        return self.data["pipeline"]["current_stage"]

    def set_pipeline_stage(self, stage: str):
        self.data["pipeline"]["current_stage"] = stage
        self.save_project()

    def get_all_chapter_ids(self) -> list:
        index = self.load_chapter_index()
        return [c["chapter_id"] for c in index.get("chapters", [])]

    def get_all_character_ids(self, tier: str = "all") -> list:
        index = self.load_character_index()
        if tier == "primary":
            return [c["character_id"] for c in index["characters"] if c["status"] == "primary"]
        if tier == "secondary":
            return [c["character_id"] for c in index["characters"] if c["status"] == "secondary"]
        return [c["character_id"] for c in index["characters"]]

    def get_shots_for_scene(self, chapter_id: str, scene_id: str) -> list:
        index = self.load_shot_index(chapter_id, scene_id)
        return index.get("shots", [])

    def get_world_version(self) -> str:
        wb = self.load_world_bible()
        history = wb.get("version_history", [])
        if history:
            return history[-1]["version"]
        return "1.0"

    def is_gate_open(self, gate_name: str) -> bool:
        """
        Gates are open when human_approval has been recorded.
        We store approvals in project.json under pipeline.gate_approvals.
        """
        approvals = self.data["pipeline"].get("gate_approvals", {})
        return approvals.get(gate_name, False)

    def approve_gate(self, gate_name: str, approver: str = "director"):
        if "gate_approvals" not in self.data["pipeline"]:
            self.data["pipeline"]["gate_approvals"] = {}
        self.data["pipeline"]["gate_approvals"][gate_name] = {
            "approved": True,
            "approver": approver,
            "timestamp": datetime.now().isoformat()
        }
        self.save_project()
        print(f"  ✓ Gate '{gate_name}' approved by {approver}")

    def get_api_config(self, api_name: str) -> dict:
        return self.data["apis"].get(api_name, {})

    def is_api_enabled(self, api_name: str) -> bool:
        cfg = self.get_api_config(api_name)
        return cfg.get("enabled", False)

    def get_budget_remaining(self, api_name: str) -> float:
        ledger = self.load_cost_ledger()
        cfg = self.get_api_config(api_name)
        budget = cfg.get("budget_limit_usd", 0.0)
        spent = ledger["by_api"].get(api_name, {}).get("spent", 0.0)
        return round(budget - spent, 4)

    def __repr__(self):
        return f"<Project '{self.id}' at {self.root}>"
