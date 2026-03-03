"""
stages/pipeline.py
Pipeline stage orchestrators.
Each stage: loads context → calls API → updates schemas → commits to git.
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from core.project import Project
from core.cost_manager import CostManager, GateLockError, BudgetExceededError
from core.git_manager import GitManager
from apis.claude_client import ClaudeClient
from apis.elevenlabs import ElevenLabsClient
from apis.meshy import MeshyClient
from apis.stability import StabilityClient


class PipelineStage:
    """Base class for all pipeline stages."""

    def __init__(self, project: Project):
        self.project = project
        self.costs = CostManager(project)
        self.git = GitManager(project)

    def _load_characters_for_chapter(self, chapter_id: str) -> list:
        chapter = self.project.load_chapter(chapter_id)
        character_ids = chapter.get("characters", {}).get("featured", [])
        characters = []
        for cid in character_ids:
            try:
                characters.append(self.project.load_character(cid))
            except FileNotFoundError:
                pass
        return characters

    def _get_scene_ids(self, chapter_id: str) -> list:
        chapter = self.project.load_chapter(chapter_id)
        scene_ids = []
        for act in chapter.get("structure", {}).get("acts", []):
            scene_ids.extend(act.get("scene_ids", []))
        return scene_ids


# ------------------------------------------------------------------
# Stage: Ingest + World Bible
# ------------------------------------------------------------------

class IngestStage(PipelineStage):

    def run(self, source_text_path: str, dry_run: bool = False) -> dict:
        """
        Ingest raw source text and generate:
        - Chapter index
        - World bible (draft)
        """
        print(f"\n{'─'*55}")
        print(f"  STAGE: Ingest + World Bible")
        print(f"{'─'*55}")

        source_text = Path(source_text_path).read_text(encoding="utf-8")
        print(f"  Source: {len(source_text)} characters, {len(source_text.split())} words")

        estimated = self.costs.estimate_claude(
            input_tokens=int(len(source_text.split()) * 1.3),
            output_tokens=3000
        )
        print(f"  Estimated Claude cost: ${estimated:.4f}")

        if dry_run:
            print("  DRY RUN — no API calls")
            return {"status": "dry_run"}

        self.costs.check_api_allowed("claude")
        self.costs.check_budget("claude", estimated)

        with ClaudeClient() as claude:
            # Ingest
            print("\n  Running source ingest...")
            ingest_result = claude.ingest_source(source_text, self.project.id)
            self.costs.record("claude", ingest_result.pop("_cost_usd", 0), "ingest",
                              "Source ingest pass")

            # Save chapter index
            chapter_index = {
                "$schema": "babylon-studio/schemas/chapter-index/v1",
                "project_id": self.project.id,
                "total_chapters": ingest_result["total_chapters"],
                "chapters": [
                    {
                        "chapter_id": c["chapter_id"],
                        "chapter_number": c["chapter_number"],
                        "title": c["title"],
                        "status": "pending",
                        "approved": False
                    }
                    for c in ingest_result["chapters"]
                ]
            }
            self.project._save(
                self.project._path("chapters", "_index.json"),
                chapter_index
            )

            # Save individual chapter stubs
            for ch_data in ingest_result["chapters"]:
                chapter_stub = self._build_chapter_stub(ch_data)
                self.project._path("chapters", ch_data["chapter_id"]).mkdir(
                    parents=True, exist_ok=True
                )
                self.project.save_chapter(ch_data["chapter_id"], chapter_stub)

            print(f"  ✓ {ingest_result['total_chapters']} chapters indexed")

            # World bible
            print("\n  Generating world bible...")
            source_summary = "\n".join([
                f"Ch{c['chapter_number']}: {c['title']} — {c['summary']}"
                for c in ingest_result["chapters"]
            ])
            world_result = claude.generate_world_bible(source_summary, self.project.data)
            cost = world_result.pop("_cost_usd", 0)
            self.costs.record("claude", cost, "world_bible", "World bible generation")

            # Merge into existing world bible template if it exists
            wb_path = self.project._path("world", "world_bible.json")
            wb_path.parent.mkdir(parents=True, exist_ok=True)
            world_result["project_id"] = self.project.id
            world_result["schema_version"] = "1.0"
            world_result["meta"] = {"approved": False, "cinematographer_reviewed": False}
            self.project._save(wb_path, world_result)
            print("  ✓ World bible generated (draft — needs human review)")

        self.git.create_stage_branch("ingest", self.project.id)
        self.git.commit_stage_artifacts("ingest", self.project.id,
                                        "ingest: source parsed and world bible drafted")
        self.git.merge_to_main("ingest", self.project.id)

        self.project.set_pipeline_stage("world_bible_review")
        print(f"\n  ✓ Ingest complete. Review world_bible.json before proceeding.")
        return {"status": "complete", "chapters": ingest_result["total_chapters"]}

    def _build_chapter_stub(self, ch_data: dict) -> dict:
        """Build a chapter.json stub from ingest data."""
        return {
            "$schema": "babylon-studio/schemas/chapter/v1",
            "project_id": self.project.id,
            "chapter_id": ch_data["chapter_id"],
            "chapter_number": ch_data["chapter_number"],
            "title": ch_data["title"],
            "status": "pending",
            "source": {
                "adaptation_notes": ch_data.get("summary", "")
            },
            "narrative": {
                "logline": ch_data.get("summary", ""),
                "theme": ch_data.get("theme", ""),
                "parable_lesson": ch_data.get("parable_lesson", ""),
                "tone": "reflective_hopeful"
            },
            "structure": {"acts": []},
            "characters": {
                "featured": ch_data.get("key_characters", []),
                "background": []
            },
            "locations": {"used": ch_data.get("key_locations", [])},
            "screenplay": {"ref": f"chapters/{ch_data['chapter_id']}/screenplay.md",
                           "status": "pending", "approved": False},
            "production": {
                "total_shots": 0, "shots_approved": 0,
                "audio_lines_total": 0, "audio_lines_generated": 0,
                "scenes_built_in_ue5": 0, "preview_renders_complete": 0,
                "final_renders_complete": 0
            },
            "costs": {
                "stage_totals_usd": {
                    "screenplay": 0.0, "storyboard": 0.0, "audio": 0.0,
                    "mesh_generation": 0.0, "scene_assembly": 0.0,
                    "preview_render": 0.0, "final_render": 0.0
                },
                "chapter_total_usd": 0.0
            },
            "meta": {"schema_version": "1.0", "created": datetime.now().isoformat(),
                     "approved": False}
        }


# ------------------------------------------------------------------
# Stage: Screenplay
# ------------------------------------------------------------------

class ScreenplayStage(PipelineStage):

    def run(self, chapter_id: str, dry_run: bool = False) -> dict:
        print(f"\n{'─'*55}")
        print(f"  STAGE: Screenplay — {chapter_id}")
        print(f"{'─'*55}")

        chapter = self.project.load_chapter(chapter_id)
        world_bible = self.project.load_world_bible()
        characters = self._load_characters_for_chapter(chapter_id)

        estimated = self.costs.estimate_claude(input_tokens=3000, output_tokens=5000)
        print(f"  Characters: {[c.get('display_name') for c in characters]}")
        print(f"  Estimated cost: ${estimated:.4f}")

        if dry_run:
            print("  DRY RUN")
            return {"status": "dry_run"}

        self.costs.check_api_allowed("claude")
        self.costs.check_budget("claude", estimated)

        with ClaudeClient() as claude:
            print(f"  Writing screenplay...")
            adaptation_notes = chapter.get("source", {}).get("adaptation_notes", "")
            screenplay = claude.generate_screenplay(
                chapter_outline=chapter,
                world_bible=world_bible,
                characters=characters,
                adaptation_notes=adaptation_notes
            )
            actual_cost = self.costs.estimate_claude(
                int(len(adaptation_notes.split()) * 1.3),
                int(len(screenplay.split()) * 1.3)
            )
            self.costs.record("claude", actual_cost, "screenplay",
                              f"Screenplay {chapter_id}", entity_id=chapter_id)

        # Save screenplay
        screenplay_path = self.project._path("chapters", chapter_id, "screenplay.md")
        screenplay_path.parent.mkdir(parents=True, exist_ok=True)
        screenplay_path.write_text(screenplay, encoding="utf-8")

        # Update chapter
        chapter["screenplay"]["status"] = "draft"
        chapter["status"] = "screenplay"
        self.project.save_chapter(chapter_id, chapter)

        self.git.create_stage_branch("screenplay", chapter_id)
        self.git.commit_stage_artifacts(
            "screenplay", chapter_id,
            f"screenplay: {chapter_id} draft complete",
            paths=[f"chapters/{chapter_id}/"]
        )

        self.project.append_chapter_note(
            chapter_id,
            f"Screenplay draft generated. Word count: {len(screenplay.split())}. Needs human review.",
            author="screenplay_stage"
        )

        print(f"  ✓ Screenplay saved: {screenplay_path}")
        print(f"  ⚠️  Review screenplay before running cinematographer pass.")
        return {"status": "complete", "path": str(screenplay_path)}


# ------------------------------------------------------------------
# Stage: Cinematographer Pass
# ------------------------------------------------------------------

class CinematographerStage(PipelineStage):

    def run(self, chapter_id: str, scene_id: Optional[str] = None,
            dry_run: bool = False) -> dict:
        print(f"\n{'─'*55}")
        print(f"  STAGE: Cinematographer — {chapter_id}")
        print(f"{'─'*55}")

        world_bible = self.project.load_world_bible()
        chapter = self.project.load_chapter(chapter_id)

        screenplay_path = self.project._path("chapters", chapter_id, "screenplay.md")
        if not screenplay_path.exists():
            raise FileNotFoundError(f"Screenplay not found: {screenplay_path}")

        screenplay_text = screenplay_path.read_text(encoding="utf-8")

        # Process one scene or all scenes
        scene_ids = [scene_id] if scene_id else self._get_scene_ids(chapter_id)
        if not scene_ids:
            print("  ⚠️  No scenes defined in chapter structure yet.")
            print("  Running full-chapter shot breakdown...")
            scene_ids = [f"{chapter_id}_sc01"]

        all_shots = []
        total_cost = 0.0

        with ClaudeClient() as claude:
            for sid in scene_ids:
                print(f"\n  Scene: {sid}")

                # Try to load scene schema, build minimal one if missing
                try:
                    scene = self.project.load_scene(chapter_id, sid)
                    location = scene.get("location", {})
                    chars_in_scene = scene.get("characters", {}).get("present", [])
                    char_ids = [c["character_id"] for c in chars_in_scene]
                except FileNotFoundError:
                    location = {}
                    char_ids = chapter["characters"]["featured"]

                characters = []
                for cid in char_ids:
                    try:
                        characters.append(self.project.load_character(cid))
                    except FileNotFoundError:
                        pass

                # Extract scene text from screenplay
                scene_text = self._extract_scene_from_screenplay(screenplay_text, sid)

                estimated = self.costs.estimate_claude(input_tokens=2000, output_tokens=3000)
                if dry_run:
                    print(f"  DRY RUN — estimated ${estimated:.4f}")
                    continue

                self.costs.check_budget("claude", estimated)

                result = claude.cinematographer_pass(
                    scene_text=scene_text,
                    scene_id=sid,
                    location=location,
                    characters_in_scene=characters,
                    world_bible=world_bible,
                    chapter_id=chapter_id
                )
                cost = result.pop("_cost_usd", estimated)
                self.costs.record("claude", cost, "screenplay",
                                  f"Cinematographer pass {sid}", entity_id=chapter_id)
                total_cost += cost

                shots = result.get("shots", [])
                print(f"  ✓ {len(shots)} shots generated for {sid}")

                # Save shot files
                self._save_shots(chapter_id, sid, shots)
                all_shots.extend(shots)

        # Update chapter production count
        chapter["production"]["total_shots"] = len(all_shots)
        self.project.save_chapter(chapter_id, chapter)

        self.git.create_stage_branch("cinematographer", chapter_id)
        self.git.commit_stage_artifacts(
            "cinematographer", chapter_id,
            f"cinematographer: {chapter_id} shot list complete ({len(all_shots)} shots)"
        )

        print(f"\n  ✓ Cinematographer pass complete: {len(all_shots)} shots, ${total_cost:.4f}")
        return {"status": "complete", "shots": len(all_shots), "cost": total_cost}

    def _extract_scene_from_screenplay(self, screenplay: str, scene_id: str) -> str:
        """
        Attempt to extract a specific scene from screenplay text.
        Falls back to returning full screenplay if scene markers not found.
        """
        lines = screenplay.split("\n")
        scene_num = scene_id.split("sc")[-1].lstrip("0") if "sc" in scene_id else "1"
        markers = [f"SCENE {scene_num}", f"SC{scene_num.zfill(2)}", f"SC {scene_num}"]

        for i, line in enumerate(lines):
            if any(m in line.upper() for m in markers):
                end = len(lines)
                for j in range(i + 1, len(lines)):
                    if any(m in lines[j].upper() for m in ["INT.", "EXT.", "SCENE "]):
                        end = j
                        break
                return "\n".join(lines[i:end])

        # Return full screenplay if no markers found
        return screenplay[:4000]

    def _save_shots(self, chapter_id: str, scene_id: str, shots: list):
        """Save shot schemas and index for a scene."""
        shots_dir = self.project._path("chapters", chapter_id, "shots")
        shots_dir.mkdir(parents=True, exist_ok=True)

        index_entries = []
        for shot_data in shots:
            shot_number = shot_data.get("shot_number", 1)
            shot_id = f"{scene_id}_sh{str(shot_number).zfill(3)}"

            # Build full shot schema
            shot = {
                "$schema": "babylon-studio/schemas/shot/v1",
                "project_id": self.project.id,
                "chapter_id": chapter_id,
                "scene_id": scene_id,
                "shot_id": shot_id,
                "shot_number": shot_number,
                "label": shot_data.get("label", ""),
                "status": "generated",
                "cinematic": {
                    "shot_type": shot_data.get("shot_type", "medium"),
                    "framing": shot_data.get("framing", "medium_shot"),
                    "lens_mm_equiv": shot_data.get("lens_mm_equiv", 35),
                    "camera_movement": shot_data.get("camera_movement", {}),
                    "composition_notes": shot_data.get("composition_notes", ""),
                    "vertical_reframe": shot_data.get("vertical_reframe", {})
                },
                "characters_in_frame": [
                    {"character_id": cid}
                    for cid in shot_data.get("characters_in_frame", [])
                ],
                "dialogue_in_shot": shot_data.get("dialogue_lines_covered", []),
                "audio": {"sound_design": []},
                "assets_required": {
                    "assets_needed_raw": shot_data.get("assets_needed", [])
                },
                "storyboard": {
                    "image_ref": f"chapters/{chapter_id}/shots/{shot_id}/storyboard.png",
                    "generated": False,
                    "approved": False,
                    "storyboard_prompt": shot_data.get("storyboard_prompt", "")
                },
                "build_status": {
                    "world_version_built_against": None,
                    "assets_placed": False,
                    "lighting_set": False,
                    "animation_keyed": False,
                    "audio_synced": False,
                    "preview_rendered": False,
                    "final_rendered": False
                },
                "version_notes": [{
                    "version": "1.0",
                    "date": datetime.now().isoformat(),
                    "author": "ai_cinematographer_pass",
                    "note": "Initial shot generated from screenplay"
                }],
                "meta": {
                    "schema_version": "1.0",
                    "created": datetime.now().isoformat(),
                    "approved": False,
                    "flags": shot_data.get("flags", [])
                }
            }

            shot_dir = shots_dir / shot_id
            shot_dir.mkdir(parents=True, exist_ok=True)
            self.project._save(shot_dir / "shot.json", shot)

            # Create empty notes file
            notes_path = shot_dir / "notes.md"
            if not notes_path.exists():
                notes_path.write_text(
                    f"# Shot Notes: {shot_id}\n## {shot_data.get('label', '')}\n\n"
                    f"## Version History\n\n### v1.0 — {datetime.now().strftime('%Y-%m-%d')} — ai\n"
                    f"Initial shot generated from cinematographer pass.\n"
                )

            index_entries.append({
                "shot_id": shot_id,
                "label": shot_data.get("label", ""),
                "shot_type": shot_data.get("shot_type", ""),
                "duration_sec": shot_data.get("duration_sec", 0),
                "dialogue_lines": len(shot_data.get("dialogue_lines_covered", [])),
                "status": "generated",
                "storyboard_approved": False,
                "audio_approved": False,
                "built": False,
                "preview_rendered": False,
                "final_rendered": False,
                "world_version": None,
                "flags": shot_data.get("flags", [])
            })

        # Save shot index
        self.project._save(shots_dir / "_index.json", {
            "scene_id": scene_id,
            "total_shots": len(index_entries),
            "shots": index_entries
        })
        print(f"    Saved {len(index_entries)} shot files")


# ------------------------------------------------------------------
# Stage: Storyboard
# ------------------------------------------------------------------

class StoryboardStage(PipelineStage):

    def run(self, chapter_id: str, dry_run: bool = False) -> dict:
        print(f"\n{'─'*55}")
        print(f"  STAGE: Storyboard — {chapter_id}")
        print(f"{'─'*55}")

        # Gather all shots for chapter
        all_shots = []
        for scene_id in self._get_scene_ids(chapter_id):
            try:
                index = self.project.load_shot_index(chapter_id, scene_id)
                for shot_summary in index["shots"]:
                    shot = self.project.load_shot(chapter_id, scene_id, shot_summary["shot_id"])
                    shot["chapter_id"] = chapter_id
                    all_shots.append(shot)
            except FileNotFoundError:
                pass

        if not all_shots:
            print("  No shots found. Run cinematographer pass first.")
            return {"status": "no_shots"}

        estimated = self.costs.estimate_stability(len(all_shots) * 2)
        print(f"  Shots to storyboard: {len(all_shots)}")
        print(f"  Estimated cost (16:9 + 9:16): ${estimated:.2f}")

        if dry_run:
            print("  DRY RUN")
            return {"status": "dry_run"}

        self.costs.check_api_allowed("stabilityai")
        self.costs.check_budget("stabilityai", estimated)

        with StabilityClient() as stability:
            results = stability.generate_shot_boards(
                shots=all_shots,
                project_root=str(self.project.root),
                include_vertical=True
            )

        # Update shot schemas
        generated_count = 0
        for result in results:
            if result["status"] == "generated":
                shot_id = result["shot_id"]
                chapter_id_local = chapter_id
                scene_id = "_".join(shot_id.split("_")[:3])
                try:
                    shot = self.project.load_shot(chapter_id_local, scene_id, shot_id)
                    shot["storyboard"]["generated"] = True
                    self.project.save_shot(chapter_id_local, scene_id, shot_id, shot)
                    self.costs.record("stabilityai", result.get("cost_usd", 0.08),
                                      "storyboard", f"Storyboard {shot_id}",
                                      entity_id=chapter_id)
                    generated_count += 1
                except FileNotFoundError:
                    pass

        self.git.create_stage_branch("storyboard", chapter_id)
        self.git.commit_stage_artifacts("storyboard", chapter_id,
                                        f"storyboard: {chapter_id} {generated_count} images generated")

        print(f"\n  ✓ {generated_count}/{len(all_shots)} storyboards generated")
        print(f"  Review storyboards then run: approve-gate storyboard_to_audio")
        return {"status": "complete", "generated": generated_count}


# ------------------------------------------------------------------
# Stage: Audio
# ------------------------------------------------------------------

class AudioStage(PipelineStage):

    def run(self, chapter_id: str, scene_id: Optional[str] = None,
            dry_run: bool = False) -> dict:
        print(f"\n{'─'*55}")
        print(f"  STAGE: Audio — {chapter_id}")
        print(f"{'─'*55}")

        self.costs.check_api_allowed("elevenlabs", required_gate="storyboard_to_audio")

        # Build character map
        character_map = {}
        for cid in self.project.get_all_character_ids():
            try:
                character_map[cid] = self.project.load_character(cid)
            except FileNotFoundError:
                pass

        # Gather pending dialogue lines
        scene_ids = [scene_id] if scene_id else self._get_scene_ids(chapter_id)
        pending_lines = []

        for sid in scene_ids:
            try:
                scene = self.project.load_scene(chapter_id, sid)
                for line in scene["dialogue"]["lines"]:
                    if line["audio_status"] == "pending":
                        line["chapter_id"] = chapter_id
                        line["scene_id"] = sid
                        pending_lines.append(line)
            except FileNotFoundError:
                pass

        if not pending_lines:
            print("  No pending audio lines found.")
            return {"status": "nothing_to_do"}

        estimated = self.costs.estimate_elevenlabs(
            " ".join(l["text"] for l in pending_lines)
        )
        print(f"  Lines to generate: {len(pending_lines)}")
        print(f"  Estimated cost: ${estimated:.4f}")

        if dry_run:
            print("  DRY RUN")
            return {"status": "dry_run"}

        self.costs.check_budget("elevenlabs", estimated)

        with ElevenLabsClient() as el:
            results = el.generate_line_batch(
                lines=pending_lines,
                character_map=character_map,
                project_root=str(self.project.root),
                dry_run=dry_run
            )

        # Update scene schemas with audio status and duration
        for result in results:
            if result["status"] != "generated":
                continue
            line_id = result["line_id"]
            for sid in scene_ids:
                try:
                    scene = self.project.load_scene(chapter_id, sid)
                    for line in scene["dialogue"]["lines"]:
                        if line["line_id"] == line_id:
                            line["audio_status"] = "generated"
                            line["duration_sec"] = result.get("estimated_duration_sec", 0)
                    scene["audio"]["generated"] = sum(
                        1 for l in scene["dialogue"]["lines"]
                        if l["audio_status"] == "generated"
                    )
                    self.project.save_scene(chapter_id, sid, scene)
                    self.costs.record("elevenlabs", result.get("cost_usd", 0),
                                      "audio", f"Audio {line_id}", entity_id=chapter_id)
                except FileNotFoundError:
                    pass

        generated = sum(1 for r in results if r["status"] == "generated")
        self.git.create_stage_branch("audio", chapter_id)
        self.git.commit_stage_artifacts("audio", chapter_id,
                                        f"audio: {chapter_id} {generated} lines generated")

        print(f"\n  ✓ {generated}/{len(pending_lines)} audio lines generated")
        return {"status": "complete", "generated": generated}


# ------------------------------------------------------------------
# Stage: Asset Manifest Builder
# ------------------------------------------------------------------

class AssetManifestStage(PipelineStage):

    def run(self, chapter_id: Optional[str] = None, dry_run: bool = False) -> dict:
        print(f"\n{'─'*55}")
        print(f"  STAGE: Asset Manifest Builder")
        print(f"{'─'*55}")

        world_bible = self.project.load_world_bible()

        # Load existing manifest or start fresh
        try:
            manifest = self.project.load_asset_manifest()
        except FileNotFoundError:
            manifest = {
                "$schema": "babylon-studio/schemas/asset-manifest/v1",
                "project_id": self.project.id,
                "assets": {"environments": [], "props": [], "costumes": [], "vegetation": []},
                "deduplication_log": {"entries": []},
                "generation_batches": []
            }

        # Collect all shots
        chapter_ids = [chapter_id] if chapter_id else self.project.get_all_chapter_ids()
        all_shots = []
        for cid in chapter_ids:
            for sid in self._get_scene_ids(cid):
                try:
                    index = self.project.load_shot_index(cid, sid)
                    for shot_s in index["shots"]:
                        shot = self.project.load_shot(cid, sid, shot_s["shot_id"])
                        all_shots.append(shot)
                except FileNotFoundError:
                    pass

        print(f"  Scanning {len(all_shots)} shots for asset requirements...")
        estimated = self.costs.estimate_claude(input_tokens=3000, output_tokens=4000)
        print(f"  Estimated Claude cost: ${estimated:.4f}")

        if dry_run:
            print("  DRY RUN")
            return {"status": "dry_run"}

        self.costs.check_api_allowed("claude")
        self.costs.check_budget("claude", estimated)

        with ClaudeClient() as claude:
            new_assets = claude.build_asset_entries(
                shots=all_shots,
                existing_manifest=manifest,
                world_bible=world_bible,
                chapter_id=chapter_id or "all"
            )
            cost = new_assets.pop("_cost_usd", estimated)
            self.costs.record("claude", cost, "asset_manifest",
                              "Asset manifest build pass")

        # Merge new assets into manifest
        for category in ["environments", "props", "costumes", "vegetation"]:
            new = new_assets.get("new_assets", {}).get(category, [])
            manifest["assets"].setdefault(category, []).extend(new)
            if new:
                print(f"  + {len(new)} new {category}")

        dedup = new_assets.get("deduplication_log", [])
        manifest["deduplication_log"]["entries"].extend(dedup)
        if dedup:
            print(f"  ✓ {len(dedup)} assets deduplicated")

        # Recalculate summary
        total = sum(
            len(manifest["assets"].get(cat, []))
            for cat in ["environments", "props", "costumes", "vegetation"]
        )
        manifest["summary"] = {
            "total_assets": total,
            "generated": sum(
                1 for cat in ["environments", "props", "costumes", "vegetation"]
                for a in manifest["assets"].get(cat, [])
                if a.get("meshy", {}).get("status") == "completed"
            ),
            "pending": sum(
                1 for cat in ["environments", "props", "costumes", "vegetation"]
                for a in manifest["assets"].get(cat, [])
                if a.get("meshy", {}).get("status") == "pending"
            )
        }

        self.project.save_asset_manifest(manifest)

        self.git.create_stage_branch("assets", "manifest")
        self.git.commit_stage_artifacts("assets", "manifest",
                                        f"assets: manifest updated, {total} total assets")

        print(f"\n  ✓ Manifest updated: {total} total assets")
        print(f"  Review manifest and set approved_for_generation=true on priority assets.")
        print(f"  Then run: approve-gate audio_to_assets")
        return {"status": "complete", "total_assets": total}
