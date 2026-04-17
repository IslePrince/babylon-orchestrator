"""
stages/mesh_animation.py
Orchestrates asset generation and character animation.

Two separate concerns:
  MeshStage     — runs Meshy batches from approved asset manifest
  AnimationStage — builds Cartwheel motion libraries for named characters,
                   Meshy animations for background types
"""

from pathlib import Path
from datetime import datetime
from typing import Optional

from core.project import Project
from core.cost_manager import CostManager, GateLockError, BudgetExceededError
from core.git_manager import GitManager
from apis.meshy import MeshyClient
from apis.cartwheel import CartwheelClient


class MeshStage:
    """
    Generates 3D assets from approved manifest batches.
    Gate: sound_to_assets must be approved.
    """

    def __init__(self, project: Project):
        self.project = project
        self.costs = CostManager(project)
        self.git = GitManager(project)

    def run(
        self,
        batch_id: Optional[str] = None,
        dry_run: bool = False,
        progress_callback=None
    ) -> dict:
        """
        Run one batch or all approved pending batches.
        If batch_id is None, runs next pending batch only
        (safest — review after each batch).
        """
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Mesh Generation")
        print(f"{'-'*55}")

        _progress(5, "Loading manifest...")

        manifest = self.project.load_asset_manifest()

        # Cost overview
        estimate = MeshyClient(None).estimate_manifest_cost(manifest) if False else \
            self._estimate_without_client(manifest)
        print(f"\n  Pending assets estimate: ${estimate['total']:.2f}")
        for cat, cost in estimate['by_category'].items():
            if cost > 0:
                print(f"    {cat:<15} ${cost:.2f}")

        # Select batch
        batches = manifest.get("generation_batches", [])
        if batch_id:
            targets = [b for b in batches if b["batch_id"] == batch_id]
        else:
            # Run next pending approved batch
            targets = [b for b in batches
                       if b.get("status") == "pending" and b.get("approved")][:1]

        if not targets:
            pending_unapproved = [b for b in batches if b.get("status") == "pending"]
            if pending_unapproved:
                print(f"\n  {len(pending_unapproved)} batches pending approval.")
                print(f"  Set approved=true in manifest.json generation_batches to proceed.")
            else:
                print("\n  No pending batches found.")
            return {"status": "nothing_to_run"}

        batch = targets[0]
        batch_est = self._estimate_batch(manifest, batch)
        print(f"\n  Running batch: {batch['batch_id']} ({batch['label']})")
        print(f"  Assets: {len(batch['asset_ids'])}")
        print(f"  Estimated: ${batch_est:.2f}")

        if dry_run:
            print("  DRY RUN")
            _progress(100, "Dry run complete", batch_est)
            return {"status": "dry_run", "estimated": batch_est}

        # Gate + budget checks only when actually spending money
        self.costs.check_api_allowed("meshy", required_gate="sound_to_assets")
        self.costs.check_budget("meshy", batch_est)
        _progress(20, f"Generating batch: {batch['batch_id']}...")

        with MeshyClient() as meshy:
            result = meshy.process_manifest_batch(
                batch=batch,
                manifest=manifest,
                project_root=str(self.project.root),
                dry_run=False
            )

        # Record costs
        for r in result.get("results", []):
            if r["status"] == "completed":
                self.costs.record(
                    "meshy", r["cost_usd"], "mesh_generation",
                    f"Mesh: {r['asset_id']}"
                )

        # Save updated manifest
        self.project.save_asset_manifest(manifest)

        try:
            self.git.create_stage_branch("meshes", batch["batch_id"])
            self.git.commit_stage_artifacts(
                "meshes", batch["batch_id"],
                f"meshes: batch {batch['batch_id']} — "
                f"{result['completed']}/{len(batch['asset_ids'])} assets"
            )
        except (RuntimeError, Exception) as e:
            print(f"  [WARN] Git skipped: {e}")

        self.project.set_pipeline_stage("mesh_generation")
        print(f"\n  [OK] Batch complete: {result['completed']} assets, "
              f"${result['cost_usd']:.2f} spent")

        if result["completed"] < len(batch["asset_ids"]):
            print(f"  WARNING: Some assets failed — check manifest for status=failed entries")

        _progress(100, f"Mesh batch complete: {result['completed']} assets")
        return result

    def run_background_characters(
        self,
        dry_run: bool = False
    ) -> dict:
        """
        Generate mesh + animations for all background character types
        using Meshy's combined pipeline.
        """
        print(f"\n{'-'*55}")
        print(f"  STAGE: Background Character Generation")
        print(f"{'-'*55}")

        self.costs.check_api_allowed("meshy", required_gate="sound_to_assets")

        bg_index = self.project.load_background_types()
        if not bg_index:
            print("  No background types found in characters/background_types/")
            return {"status": "nothing_to_do"}

        bg_types_path = self.project._path("characters", "background_types")
        results = []
        total_cost = 0.0

        with MeshyClient() as meshy:
            for type_id, type_meta in bg_index.items():
                # Load full background type schema
                try:
                    bg_type = self.project._load(
                        bg_types_path / f"{type_id}.json"
                    )
                except FileNotFoundError:
                    print(f"  [FAIL] Schema not found: {type_id}")
                    continue

                # Build motion prompts from behavior list
                period_prefix = "ancient Babylon 600 BCE, Mesopotamian,"
                behaviors = bg_type.get("animation", {}).get("behaviors", ["slow walk"])
                motion_prompts = [f"{period_prefix} {b}" for b in behaviors]

                output_dir = str(
                    self.project._path("assets", "background_characters", type_id)
                )

                result = meshy.generate_background_character(
                    bg_type=bg_type,
                    motion_prompts=motion_prompts,
                    output_dir=output_dir,
                    dry_run=dry_run
                )

                if not dry_run and result["status"] == "completed":
                    self.costs.record(
                        "meshy", result["cost_usd"], "mesh_generation",
                        f"Background character: {type_id}"
                    )
                    total_cost += result["cost_usd"]

                results.append(result)

        if not dry_run:
            try:
                self.git.create_stage_branch("meshes", "background_characters")
                self.git.commit_stage_artifacts(
                    "meshes", "background_characters",
                    f"meshes: {len(results)} background character types generated"
                )
            except (RuntimeError, Exception) as e:
                print(f"  [WARN] Git skipped: {e}")

        done = sum(1 for r in results if r.get("status") == "completed")
        print(f"\n  [OK] {done}/{len(results)} background character types complete — ${total_cost:.2f}")
        return {"status": "complete", "generated": done, "cost_usd": total_cost}

    def _estimate_without_client(self, manifest: dict) -> dict:
        rates = {"hero": 0.50, "medium": 0.25, "low": 0.10}
        by_cat = {}
        total = 0.0
        for cat in ["environments", "props", "costumes", "vegetation"]:
            c = sum(
                rates.get(a.get("detail_level", "medium"), 0.25)
                for a in manifest.get("assets", {}).get(cat, [])
                if a.get("meshy", {}).get("status") == "pending"
            )
            by_cat[cat] = round(c, 2)
            total += c
        return {"by_category": by_cat, "total": round(total, 2)}

    def _estimate_batch(self, manifest: dict, batch: dict) -> float:
        rates = {"hero": 0.50, "medium": 0.25, "low": 0.10}
        total = 0.0
        for aid in batch.get("asset_ids", []):
            for cat in ["environments", "props", "costumes", "vegetation"]:
                for a in manifest.get("assets", {}).get(cat, []):
                    if a["asset_id"] == aid:
                        total += rates.get(a.get("detail_level", "medium"), 0.25)
        return round(total, 2)


class AnimationStage:
    """
    Builds Cartwheel motion libraries for named characters.
    Run after mesh generation — characters need to exist in UE5 first,
    but animations can be generated in parallel.
    """

    def __init__(self, project: Project):
        self.project = project
        self.costs = CostManager(project)
        self.git = GitManager(project)

    def run(
        self,
        character_id: Optional[str] = None,
        chapter_id: Optional[str] = None,
        dry_run: bool = False,
        progress_callback=None
    ) -> dict:
        """
        Generate motion library for one character or all named characters.
        Scans shots to determine what motions are needed.
        """
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Character Animation (Cartwheel)")
        print(f"{'-'*55}")

        _progress(5, "Gathering characters...")

        # Gather characters to process
        if character_id:
            char_ids = [character_id]
        else:
            char_ids = self.project.get_all_character_ids(tier="primary") + \
                       self.project.get_all_character_ids(tier="secondary")

        # Gather shots to scan
        all_shots = self._gather_shots(chapter_id)
        print(f"  Scanning {len(all_shots)} shots for animation requirements")
        _progress(10, f"Scanning {len(all_shots)} shots for animation requirements")

        if dry_run:
            print("  DRY RUN -- skipping Cartwheel API calls")
            _progress(100, f"Dry run complete: {len(char_ids)} characters, {len(all_shots)} shots scanned")
            return {"status": "dry_run", "characters": len(char_ids), "shots_scanned": len(all_shots)}

        # Gate check only when actually spending money
        self.costs.check_api_allowed("cartwheel", required_gate="sound_to_assets")

        results = []
        total_cost = 0.0

        with CartwheelClient() as cartwheel:
            for cid in char_ids:
                try:
                    character = self.project.load_character(cid)
                except FileNotFoundError:
                    print(f"  [FAIL] Character not found: {cid}")
                    continue

                if not character.get("animation", {}).get("cartwheel", {}).get("api_enabled"):
                    print(f"  [SKIP] Cartwheel disabled for {cid}")
                    continue

                # Extract what motions this character needs
                requirements = cartwheel.extract_shot_requirements(cid, all_shots)
                if not requirements:
                    print(f"  [INFO] No shots found for {cid}")
                    continue

                output_dir = str(
                    self.project._path("animation", "characters", cid, "cartwheel")
                )

                char_idx = char_ids.index(cid)
                char_pct = 15 + int((char_idx / max(len(char_ids), 1)) * 70)
                _progress(char_pct, f"Generating motions for {cid}...")

                result = cartwheel.build_character_motion_library(
                    character=character,
                    shot_requirements=requirements,
                    output_dir=output_dir,
                    dry_run=dry_run
                )

                if not dry_run and result.get("status") == "completed":
                    self.costs.record(
                        "cartwheel", result["cost_usd"], "scene_assembly",
                        f"Cartwheel motions: {cid}"
                    )
                    total_cost += result["cost_usd"]

                    # Save UE5 import notes
                    notes_path = Path(output_dir) / "ue5_import_notes.txt"
                    notes = cartwheel.get_ue5_import_notes(character)
                    notes_path.write_text(notes)
                    print(f"    -> UE5 import notes saved")

                    # Update character schema with motion index ref
                    character["animation"]["cartwheel"]["motion_index_ref"] = \
                        str(Path(output_dir) / "motion_index.json")
                    self.project.save_character(cid, character)

                results.append(result)

        if not dry_run and results:
            try:
                self.git.create_stage_branch("animation", chapter_id or "all")
                self.git.commit_stage_artifacts(
                    "animation", chapter_id or "all",
                    f"animation: Cartwheel motions for {len(results)} characters"
                )
            except (RuntimeError, Exception) as e:
                print(f"  [WARN] Git skipped: {e}")

        self.project.set_pipeline_stage("animation")
        done = sum(1 for r in results if r.get("status") == "completed")
        print(f"\n  [OK] {done}/{len(results)} characters animated — ${total_cost:.2f}")
        _progress(100, f"Animation complete: {done} characters", total_cost)
        return {"status": "complete", "characters": done, "cost_usd": total_cost}

    def print_ue5_notes(self, character_id: str):
        """Print UE5 import/retargeting instructions for a character."""
        character = self.project.load_character(character_id)
        with CartwheelClient() as cw:
            print(cw.get_ue5_import_notes(character))

    def _gather_shots(self, chapter_id: Optional[str] = None) -> list:
        chapter_ids = [chapter_id] if chapter_id else self.project.get_all_chapter_ids()
        shots = []
        for cid in chapter_ids:
            chapter = self.project.load_chapter(cid)
            for act in chapter.get("structure", {}).get("acts", []):
                for scene_id in act.get("scene_ids", []):
                    try:
                        index = self.project.load_shot_index(cid, scene_id)
                        for shot_s in index["shots"]:
                            try:
                                shot = self.project.load_shot(cid, scene_id, shot_s["shot_id"])
                                shots.append(shot)
                            except FileNotFoundError:
                                pass
                    except FileNotFoundError:
                        pass
        return shots
