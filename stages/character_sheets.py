"""
stages/character_sheets.py
Character sheet generation and LoRA training.

Two stages:
  CharacterSheetStage  — generates 20-25 training images per character via ComfyUI
                         (full body poses, close-up expressions, medium shots)
                         plus matching .txt caption files for LoRA training.

  LoRATrainingStage    — trains SDXL LoRAs from character sheet images using
                         kohya_ss / sd-scripts. Produces .safetensors files
                         that are loaded by ComfyUI during storyboard generation.

SAFETY: These stages NEVER modify existing character data (voice mappings,
        visual_tag, costume_default, etc.). They only ADD new files under
        characters/{id}/training_images/ and characters/{id}/lora/.
        The only character.json field written is assets.lora (new sub-dict).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from core.project import Project
from core.cost_manager import CostManager
from core.git_manager import GitManager


class CharacterSheetStage:
    """
    Generates LoRA training images for each character using ComfyUI.

    For each character:
      - 4 full-body poses × N costumes (default + up to 3 variants)
      - 4 close-up expressions
      - 4 medium shots with action/context
      = ~20-25 images per character (with 1 costume)

    Output:
      characters/{char_id}/training_images/{char_id}_{pose}_{costume}.png
      characters/{char_id}/training_images/{char_id}_{pose}_{costume}.txt

    Updates character.json with:
      assets.lora.trigger_word
      assets.lora.training_images_count
      assets.lora.training_images_path
    """

    def __init__(self, project: Project):
        self.project = project
        self.costs = CostManager(project)
        self.git = GitManager(project)

    def run(
        self,
        character_id: Optional[str] = None,
        dry_run: bool = False,
        progress_callback=None,
        force: bool = False,
    ) -> dict:
        """
        Generate character sheet training images.

        Args:
            character_id: Generate for a single character, or all if None.
            dry_run: If True, estimate only without generating.
            progress_callback: Optional (pct, msg, cost) callback for SSE.
            force: Regenerate even if images already exist.

        Returns:
            Dict with status, per-character results, and total counts.
        """
        # GPU-heavy (ComfyUI SDXL). Serialize against other GPU jobs.
        if not dry_run:
            from core.gpu_lock import gpu_exclusive
            label = f"character_sheets:{character_id or 'all'}"
            with gpu_exclusive(label, blocking=False):
                return self._run_locked(
                    character_id=character_id, dry_run=dry_run,
                    progress_callback=progress_callback, force=force,
                )
        return self._run_locked(
            character_id=character_id, dry_run=dry_run,
            progress_callback=progress_callback, force=force,
        )

    def _run_locked(
        self,
        character_id: Optional[str] = None,
        dry_run: bool = False,
        progress_callback=None,
        force: bool = False,
    ) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Character Sheet Generation")
        print(f"{'-'*55}")

        _progress(5, "Loading character data...")

        # Determine which characters to process
        if character_id:
            char_ids = [character_id]
        else:
            char_ids = self.project.get_all_character_ids()

        if not char_ids:
            print("  No characters found in project.")
            _progress(100, "No characters to process")
            return {"status": "no_characters"}

        # Load character data
        characters = []
        for cid in char_ids:
            try:
                char_data = self.project.load_character(cid)
                characters.append(char_data)
            except FileNotFoundError:
                print(f"  [WARN] Character '{cid}' not found, skipping")

        if not characters:
            print("  No valid character data found.")
            _progress(100, "No valid characters")
            return {"status": "no_characters"}

        print(f"  Characters to process: {len(characters)}")
        for c in characters:
            print(f"    - {c.get('display_name', c.get('character_id', '?'))}")

        # Load world context for period-accurate training image prompts
        from apis.prompt_builder import (
            build_character_sheet_prompts, make_trigger_word,
            character_gender_negatives, build_character_sheet_negative,
        )
        from apis.claude_client import ClaudeClient

        world_context = {}
        try:
            wb = self.project.load_world_bible()
            world_context = ClaudeClient._extract_world_context(wb)
            if world_context.get("period"):
                print(f"  World context: {world_context['period']}")
        except FileNotFoundError:
            print("  [WARN] No world bible found — training images will lack period context")

        all_work = []
        for char_data in characters:
            cid = char_data.get("character_id", "unknown")
            prompts = build_character_sheet_prompts(char_data, world_context=world_context)
            output_dir = self.project._path("characters", cid, "training_images")

            # Skip characters that already have full training sets
            if not force and output_dir.exists():
                existing = list(output_dir.glob("*.png"))
                if len(existing) >= len(prompts):
                    print(f"  [SKIP] {cid}: {len(existing)} images already exist "
                          f"({len(prompts)} needed)")
                    continue

            # Gender-aware negative prompt to prevent gender drift
            gender_neg = character_gender_negatives(char_data)
            neg_prompt = build_character_sheet_negative(gender_neg)
            if gender_neg:
                print(f"  {cid}: gender negatives applied → {gender_neg[:40]}...")

            # Look for existing character reference image to guide generation
            ref_image = self.project._path("characters", cid, "reference.png")
            ref_path = str(ref_image) if ref_image.exists() else None
            if ref_path:
                print(f"  {cid}: using reference image for consistency")

            all_work.append({
                "character_id": cid,
                "character_data": char_data,
                "prompts": prompts,
                "output_dir": str(output_dir),
                "trigger_word": make_trigger_word(cid),
                "negative_prompt": neg_prompt,
                "reference_image_path": ref_path,
            })

        if not all_work:
            print("  All characters already have complete training sets.")
            _progress(100, "All character sheets already generated")
            return {
                "status": "complete",
                "characters": len(characters),
                "new_images": 0,
            }

        total_images = sum(len(w["prompts"]) for w in all_work)
        print(f"\n  Total images to generate: {total_images}")
        print(f"  Estimated cost: $0.00 (ComfyUI local)")

        if dry_run:
            print("  DRY RUN — no generation")
            _progress(100, f"Dry run: {total_images} images for {len(all_work)} characters")
            return {
                "status": "dry_run",
                "characters": len(all_work),
                "total_images": total_images,
            }

        # Verify ComfyUI is available
        from apis.comfyui import ComfyUIClient

        comfyui_cfg = self.project.get_api_config("comfyui")
        base_url = comfyui_cfg.get("url")
        checkpoint = comfyui_cfg.get("checkpoint")

        if not ComfyUIClient.is_available(base_url):
            print("  [FAIL] ComfyUI is not running. Start ComfyUI and try again.")
            _progress(100, "ComfyUI not available")
            return {"status": "failed", "error": "ComfyUI not available"}

        _progress(10, f"Generating sheets for {len(all_work)} characters...")

        # Generate images for each character
        per_char_results = {}
        images_done = 0

        with ComfyUIClient(base_url=base_url, checkpoint=checkpoint) as client:
            for ci, work in enumerate(all_work):
                cid = work["character_id"]
                char_data = work["character_data"]
                trigger_word = work["trigger_word"]

                char_pct_start = int(10 + (ci / len(all_work)) * 85)
                char_pct_end = int(10 + ((ci + 1) / len(all_work)) * 85)
                _progress(
                    char_pct_start,
                    f"Character {ci+1}/{len(all_work)}: "
                    f"{char_data.get('display_name', cid)}..."
                )

                print(f"\n  ── {char_data.get('display_name', cid)} "
                      f"({len(work['prompts'])} images) ──")

                def _img_progress(pct, msg):
                    # Map image-level progress into this character's range
                    mapped = char_pct_start + int(
                        (pct / 100) * (char_pct_end - char_pct_start)
                    )
                    _progress(mapped, msg)

                results = client.generate_character_sheet_batch(
                    sheet_prompts=work["prompts"],
                    output_dir=work["output_dir"],
                    dry_run=False,
                    progress_callback=_img_progress,
                    reference_image_path=work.get("reference_image_path"),
                    negative_prompt=work.get("negative_prompt"),
                    force=force,
                )

                generated = sum(1 for r in results if r["status"] == "generated")
                skipped = sum(1 for r in results if r["status"] == "already_exists")
                failed = sum(1 for r in results if r["status"] == "failed")
                images_done += generated + skipped

                per_char_results[cid] = {
                    "generated": generated,
                    "skipped": skipped,
                    "failed": failed,
                    "total": len(results),
                    "trigger_word": trigger_word,
                }

                # Update character.json with LoRA metadata
                # ONLY writes to assets.lora — never touches voice, visual_tag, etc.
                lora_meta = char_data.get("assets", {}).get("lora", {})
                lora_meta["trigger_word"] = trigger_word
                lora_meta["training_images_count"] = generated + skipped
                lora_meta["training_images_path"] = (
                    f"characters/{cid}/training_images"
                )
                lora_meta["training_images_generated_at"] = (
                    datetime.now().isoformat()
                )

                if "assets" not in char_data:
                    char_data["assets"] = {}
                char_data["assets"]["lora"] = lora_meta
                self.project.save_character(cid, char_data)
                print(f"  Updated {cid}/character.json with lora.trigger_word: "
                      f"{trigger_word}")

        # Git commit
        try:
            commit_paths = [
                str(self.project._path("characters"))
            ]
            self.git.create_stage_branch("character_sheets", self.project.id)
            self.git.commit_stage_artifacts(
                "character_sheets", self.project.id,
                f"character_sheets: {images_done} training images for "
                f"{len(all_work)} characters",
                paths=commit_paths,
            )
        except (RuntimeError, Exception) as e:
            print(f"  [WARN] Git skipped: {e}")

        _progress(100, f"Character sheets complete: {images_done} images")
        print(f"\n  [OK] Character sheet generation complete")
        print(f"  Characters: {len(all_work)}")
        print(f"  Images generated: {images_done}")
        print(f"  Cost: $0.00 (ComfyUI local)")

        return {
            "status": "complete",
            "characters": len(all_work),
            "total_images": images_done,
            "per_character": per_char_results,
            "cost": 0.0,
        }


class LoRATrainingStage:
    """
    Trains SDXL LoRAs from character sheet images using kohya_ss / sd-scripts.

    Prerequisites:
      - kohya_ss / sd-scripts installed locally
      - Path set in project.json: apis.comfyui.kohya_path
      - Training images generated by CharacterSheetStage

    Output:
      characters/{char_id}/lora/{trigger_word}.safetensors

    Updates character.json with:
      assets.lora.file
      assets.lora.trained_at
      assets.lora.training_config
    """

    def __init__(self, project: Project):
        self.project = project
        self.costs = CostManager(project)
        self.git = GitManager(project)

    def run(
        self,
        character_id: Optional[str] = None,
        dry_run: bool = False,
        progress_callback=None,
        force: bool = False,
    ) -> dict:
        """
        Train LoRA(s) for character(s).

        Args:
            character_id: Train for a single character, or all with training images.
            dry_run: Preview training config without running.
            progress_callback: Optional (pct, msg, cost) callback.
            force: Retrain even if a LoRA already exists and is deployed.
        """
        # LoRA training pegs the GPU for tens of minutes. Serialize
        # against storyboard / preview video / other LoRA runs.
        if not dry_run:
            from core.gpu_lock import gpu_exclusive
            with gpu_exclusive(
                f"lora_training:{character_id or 'all'}",
                blocking=False,
            ):
                return self._run_locked(
                    character_id=character_id, dry_run=dry_run,
                    progress_callback=progress_callback, force=force,
                )
        return self._run_locked(
            character_id=character_id, dry_run=dry_run,
            progress_callback=progress_callback, force=force,
        )

    def _run_locked(
        self,
        character_id: Optional[str] = None,
        dry_run: bool = False,
        progress_callback=None,
        force: bool = False,
    ) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: LoRA Training")
        print(f"{'-'*55}")

        _progress(5, "Checking prerequisites...")

        # Check kohya_ss path
        comfyui_cfg = self.project.get_api_config("comfyui")
        kohya_path = comfyui_cfg.get("kohya_path")

        if not kohya_path:
            msg = (
                "LoRA training requires kohya_ss / sd-scripts.\n"
                "  1. Clone: git clone https://github.com/kohya-ss/sd-scripts\n"
                "  2. Install: cd sd-scripts && pip install -r requirements.txt\n"
                "  3. Set path in project.json: apis.comfyui.kohya_path\n"
                "     (point to the sd-scripts directory)"
            )
            print(f"  [FAIL] {msg}")
            _progress(100, "kohya_path not configured")
            return {"status": "failed", "error": msg}

        kohya_dir = Path(kohya_path)
        train_script = kohya_dir / "sdxl_train_network.py"
        if not train_script.exists():
            print(f"  [FAIL] Training script not found: {train_script}")
            print(f"  Ensure kohya_path points to the sd-scripts directory.")
            _progress(100, "Training script not found")
            return {
                "status": "failed",
                "error": f"sdxl_train_network.py not found at {kohya_dir}",
            }

        # Determine the base model (same checkpoint ComfyUI uses)
        from apis.comfyui import ComfyUIClient
        base_url = comfyui_cfg.get("url")
        checkpoint_name = comfyui_cfg.get("checkpoint")

        if not checkpoint_name and ComfyUIClient.is_available(base_url):
            with ComfyUIClient(base_url=base_url) as client:
                checkpoint_name = client.checkpoint

        if not checkpoint_name:
            print("  [FAIL] Cannot determine base checkpoint for training.")
            _progress(100, "No checkpoint configured")
            return {"status": "failed", "error": "No checkpoint configured"}

        # Find the checkpoint file path via ComfyUI models directory
        # Users typically have a base_path config or we can infer from known paths
        checkpoint_path = self._find_checkpoint_path(checkpoint_name)
        if not checkpoint_path:
            print(f"  [FAIL] Cannot find checkpoint file: {checkpoint_name}")
            print(f"  Set the full path in project.json: apis.comfyui.checkpoint_path")
            _progress(100, "Checkpoint file not found")
            return {
                "status": "failed",
                "error": f"Checkpoint file not found: {checkpoint_name}",
            }

        # Determine which characters to train
        if character_id:
            char_ids = [character_id]
        else:
            char_ids = self.project.get_all_character_ids()

        # Filter to characters that have training images
        trainable = []
        for cid in char_ids:
            try:
                char_data = self.project.load_character(cid)
            except FileNotFoundError:
                continue

            training_dir = self.project._path(
                "characters", cid, "training_images"
            )
            if not training_dir.exists():
                print(f"  [SKIP] {cid}: No training images. "
                      f"Run character-sheets stage first.")
                continue

            images = list(training_dir.glob("*.png"))
            captions = list(training_dir.glob("*.txt"))
            if len(images) < 10:
                print(f"  [SKIP] {cid}: Only {len(images)} images "
                      f"(minimum 10 needed)")
                continue

            if len(captions) < len(images):
                print(f"  [WARN] {cid}: {len(images)} images but only "
                      f"{len(captions)} caption files")

            trainable.append({
                "character_id": cid,
                "character_data": char_data,
                "training_dir": str(training_dir),
                "image_count": len(images),
                "trigger_word": char_data.get("assets", {}).get(
                    "lora", {}
                ).get("trigger_word", f"{cid}_char"),
            })

        if not trainable:
            print("  No characters ready for training.")
            _progress(100, "No trainable characters found")
            return {"status": "no_trainable_characters"}

        # Smart skip: verify LoRA exists locally AND is deployed to ComfyUI.
        # If local file exists but ComfyUI doesn't have it, redeploy instead
        # of retraining. If force is set, retrain everything.
        comfyui_available = ComfyUIClient.is_available(base_url)
        available_loras = []
        if comfyui_available:
            try:
                with ComfyUIClient(base_url=base_url) as client:
                    available_loras = client.get_available_loras()
            except Exception:
                pass

        skipped = []
        redeployed = []
        still_trainable = []

        for t in trainable:
            cid = t["character_id"]
            trigger = t["trigger_word"]
            lora_file = self.project._path(
                "characters", cid, "lora", f"{trigger}.safetensors"
            )
            safetensors_name = f"{trigger}.safetensors"
            local_exists = lora_file.exists() and lora_file.stat().st_size > 1000
            in_comfyui = safetensors_name in available_loras

            if force:
                still_trainable.append(t)
                if local_exists:
                    print(f"  {cid}: force=True — will retrain "
                          f"(existing LoRA: {lora_file.stat().st_size / 1024 / 1024:.1f} MB)")
                continue

            if local_exists and in_comfyui:
                print(f"  [SKIP] {cid}: LoRA exists locally AND in ComfyUI "
                      f"({safetensors_name})")
                skipped.append(cid)
                continue

            if local_exists and not in_comfyui:
                # File exists locally but not in ComfyUI — redeploy
                print(f"  {cid}: LoRA exists locally but NOT in ComfyUI — redeploying...")
                deployed = self._deploy_lora_to_comfyui(lora_file, trigger)
                if deployed:
                    # Update character.json with deployment path
                    char_data = t["character_data"]
                    if "assets" not in char_data:
                        char_data["assets"] = {}
                    if "lora" not in char_data["assets"]:
                        char_data["assets"]["lora"] = {}
                    char_data["assets"]["lora"]["deployed_to"] = deployed
                    self.project.save_character(cid, char_data)
                    redeployed.append(cid)
                    print(f"  [OK] {cid}: Redeployed to {deployed}")
                    skipped.append(cid)
                    continue
                else:
                    # Redeploy failed — retrain to get a fresh copy
                    print(f"  {cid}: Redeploy failed — will retrain")
                    still_trainable.append(t)
                    continue

            # No local file — needs training
            still_trainable.append(t)

        if skipped:
            print(f"\n  Skipped (already complete): {', '.join(skipped)}")
        if redeployed:
            print(f"  Redeployed to ComfyUI: {', '.join(redeployed)}")

        trainable = still_trainable

        if not trainable:
            msg = "All LoRAs already exist and are deployed to ComfyUI"
            if redeployed:
                msg = f"Redeployed {len(redeployed)} LoRA(s) to ComfyUI, none need retraining"
            print(f"  {msg}")
            _progress(100, msg)
            return {
                "status": "complete",
                "trained": 0,
                "skipped": skipped,
                "redeployed": redeployed,
                "results": {},
            }

        print(f"\n  Characters ready for training: {len(trainable)}")
        for t in trainable:
            print(f"    - {t['character_id']}: {t['image_count']} images, "
                  f"trigger: {t['trigger_word']}")

        if dry_run:
            print("\n  DRY RUN — generating configs only")
            configs = {}
            for t in trainable:
                config = self._build_training_config(
                    character_id=t["character_id"],
                    training_dir=t["training_dir"],
                    checkpoint_path=checkpoint_path,
                    trigger_word=t["trigger_word"],
                )
                configs[t["character_id"]] = config
                print(f"\n  Config for {t['character_id']}:")
                for key, val in config.items():
                    print(f"    {key}: {val}")

            _progress(100, f"Dry run: {len(trainable)} configs generated")
            return {
                "status": "dry_run",
                "characters": len(trainable),
                "configs": configs,
            }

        # Train each character
        _progress(10, f"Training {len(trainable)} LoRAs...")
        results = {}

        for ci, t in enumerate(trainable):
            cid = t["character_id"]
            pct = int(10 + (ci / len(trainable)) * 85)
            _progress(pct, f"Training LoRA for {cid}...")

            print(f"\n  ── Training: {cid} ──")

            # Create output directory
            lora_dir = self.project._path("characters", cid, "lora")
            lora_dir.mkdir(parents=True, exist_ok=True)
            output_name = f"{t['trigger_word']}"
            output_file = lora_dir / f"{output_name}.safetensors"

            # Build kohya-compatible dataset layout:
            #   dataset_dir/<repeats>_<trigger_word>/ -> symlinks to images
            # kohya_ss requires this subdirectory structure.
            dataset_dir = lora_dir / "dataset"
            repeat_dir = dataset_dir / f"10_{t['trigger_word']}"
            repeat_dir.mkdir(parents=True, exist_ok=True)

            training_images_dir = Path(t["training_dir"])
            for img in training_images_dir.glob("*.png"):
                link = repeat_dir / img.name
                if not link.exists():
                    shutil.copy2(str(img), str(link))
                # Copy matching caption file too
                caption = img.with_suffix(".txt")
                cap_link = repeat_dir / caption.name
                if caption.exists() and not cap_link.exists():
                    shutil.copy2(str(caption), str(cap_link))

            # Build training config
            config = self._build_training_config(
                character_id=cid,
                training_dir=str(dataset_dir),
                checkpoint_path=checkpoint_path,
                trigger_word=t["trigger_word"],
                output_dir=str(lora_dir),
                output_name=output_name,
            )

            # Write config to TOML
            config_path = lora_dir / "training_config.toml"
            self._write_toml_config(config, config_path)
            print(f"  Config written: {config_path}")

            # Run training
            try:
                result = self._run_training(
                    kohya_dir=kohya_dir,
                    config_path=config_path,
                )

                if output_file.exists():
                    file_size_mb = output_file.stat().st_size / 1024 / 1024
                    print(f"  [OK] LoRA saved: {output_file}")
                    print(f"       Size: {file_size_mb:.1f} MB")

                    # Auto-deploy to ComfyUI lora directory
                    deployed_path = self._deploy_lora_to_comfyui(
                        output_file, t["trigger_word"]
                    )

                    # Update character.json
                    char_data = t["character_data"]
                    if "assets" not in char_data:
                        char_data["assets"] = {}
                    if "lora" not in char_data["assets"]:
                        char_data["assets"]["lora"] = {}

                    char_data["assets"]["lora"]["file"] = (
                        f"characters/{cid}/lora/{output_name}.safetensors"
                    )
                    char_data["assets"]["lora"]["safetensors_name"] = (
                        f"{output_name}.safetensors"
                    )
                    char_data["assets"]["lora"]["trained_at"] = (
                        datetime.now().isoformat()
                    )
                    char_data["assets"]["lora"]["training_config"] = config
                    if deployed_path:
                        char_data["assets"]["lora"]["deployed_to"] = deployed_path
                    self.project.save_character(cid, char_data)

                    results[cid] = {
                        "status": "trained",
                        "file": str(output_file),
                        "deployed_to": deployed_path,
                    }
                else:
                    # List what IS in the output dir to diagnose
                    lora_files = list(lora_dir.glob("*.safetensors"))
                    print(f"  [FAIL] Training completed but output not found")
                    print(f"         Expected: {output_file}")
                    if lora_files:
                        print(f"         Found: {[f.name for f in lora_files]}")
                    else:
                        print(f"         No .safetensors files in {lora_dir}")
                    results[cid] = {"status": "failed", "error": "Output not found"}

            except Exception as e:
                print(f"  [FAIL] Training failed: {e}")
                results[cid] = {"status": "failed", "error": str(e)}

        # Git commit
        try:
            self.git.create_stage_branch("lora_training", self.project.id)
            self.git.commit_stage_artifacts(
                "lora_training", self.project.id,
                f"lora_training: {sum(1 for r in results.values() if r['status'] == 'trained')} "
                f"LoRAs trained",
                paths=[str(self.project._path("characters"))],
            )
        except (RuntimeError, Exception) as e:
            print(f"  [WARN] Git skipped: {e}")

        trained = sum(1 for r in results.values() if r["status"] == "trained")
        _progress(100, f"LoRA training complete: {trained} trained")
        print(f"\n  [OK] LoRA training complete: {trained}/{len(trainable)}")

        return {
            "status": "complete",
            "trained": trained,
            "results": results,
        }

    def _build_training_config(
        self,
        character_id: str,
        training_dir: str,
        checkpoint_path: str,
        trigger_word: str,
        output_dir: str = None,
        output_name: str = None,
    ) -> dict:
        """
        Build kohya_ss training config optimized for RTX 4090 / 24GB VRAM.

        Returns dict of training parameters (written as TOML).
        """
        if output_dir is None:
            output_dir = str(
                self.project._path("characters", character_id, "lora")
            )
        if output_name is None:
            output_name = trigger_word

        return {
            # Model
            "pretrained_model_name_or_path": checkpoint_path,
            "output_dir": output_dir,
            "output_name": output_name,
            "save_model_as": "safetensors",

            # Dataset
            "train_data_dir": training_dir,
            "resolution": "1024,1024",
            "caption_extension": ".txt",
            "shuffle_caption": True,
            "keep_tokens": 1,  # Keep trigger word in first position

            # Network (LoRA)
            "network_module": "networks.lora",
            "network_dim": 32,
            "network_alpha": 16,

            # Training
            "learning_rate": 1e-4,
            "unet_lr": 1e-4,
            "text_encoder_lr": 5e-5,
            "lr_scheduler": "cosine_with_restarts",
            "lr_warmup_steps": 100,
            "max_train_epochs": 10,
            "train_batch_size": 1,
            "gradient_accumulation_steps": 4,

            # Optimization
            "mixed_precision": "fp16",
            "cache_latents": True,
            "cache_latents_to_disk": True,
            "optimizer_type": "AdamW8bit",
            "max_token_length": 225,

            # Memory optimization (SDXL needs ~14GB without these)
            "gradient_checkpointing": True,
            "sdpa": True,  # PyTorch native memory-efficient attention

            # SDXL-specific
            "no_half_vae": True,
            "sdxl": True,

            # Saves
            "save_every_n_epochs": 5,
            "save_precision": "fp16",

            # Logging
            "logging_dir": str(
                self.project._path("characters", character_id, "lora", "logs")
            ),
        }

    def _write_toml_config(self, config: dict, path: Path):
        """Write training config as TOML file."""
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = ["# Auto-generated LoRA training config"]
        lines.append(f"# Generated: {datetime.now().isoformat()}\n")

        for key, value in config.items():
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, str):
                # Escape backslashes for Windows paths in TOML
                escaped = value.replace("\\", "\\\\")
                lines.append(f'{key} = "{escaped}"')
            elif isinstance(value, (int, float)):
                lines.append(f"{key} = {value}")
            else:
                lines.append(f'{key} = "{value}"')

        path.write_text("\n".join(lines), encoding="utf-8")

    def _run_training(self, kohya_dir: Path, config_path: Path) -> dict:
        """
        Execute kohya_ss training script using the kohya_ss venv Python.

        Uses the venv's accelerate CLI to avoid Python 3.14 incompatibility.
        Calls: <venv>/Scripts/accelerate launch sdxl_train_network.py --config_file <config>
        """
        venv_python = self._find_venv_python(kohya_dir)
        venv_dir = Path(venv_python).parent  # Scripts/ or bin/

        # Use the accelerate CLI entrypoint directly (python -m accelerate
        # fails because accelerate has no __main__.py)
        accelerate_exe = venv_dir / "accelerate.exe"
        if not accelerate_exe.exists():
            accelerate_exe = venv_dir / "accelerate"
        if not accelerate_exe.exists():
            raise RuntimeError(
                f"accelerate CLI not found in {venv_dir}. "
                f"Install it: {venv_python} -m pip install accelerate"
            )

        cmd = [
            str(accelerate_exe), "launch",
            "--num_cpu_threads_per_process", "4",
            str(kohya_dir / "sdxl_train_network.py"),
            "--config_file", str(config_path),
        ]

        print(f"  Python:  {venv_python}")
        print(f"  Running: {' '.join(cmd[:4])}...")
        print(f"  This may take 15-30 minutes per character.")

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"

        # Stream output to a log file so we can always see the full error
        log_file = config_path.parent / "training_output.log"

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(kohya_dir),
            env=env,
        )

        # Always write full output to log file for debugging
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("=== STDOUT ===\n")
            f.write(result.stdout or "(empty)")
            f.write("\n\n=== STDERR ===\n")
            f.write(result.stderr or "(empty)")

        if result.returncode != 0:
            # Combine stdout + stderr to find the real error
            # (training errors often go to stdout, while accelerate's
            # CalledProcessError wrapper goes to stderr)
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            # Show last 2000 chars for better debugging
            error_tail = combined[-2000:] if len(combined) > 2000 else combined
            raise RuntimeError(
                f"Training failed (exit code {result.returncode})\n"
                f"Full log: {log_file}\n"
                f"Output tail:\n{error_tail}"
            )

        return {"returncode": result.returncode, "stdout": result.stdout[-200:]}

    def _find_venv_python(self, kohya_dir: Path) -> str:
        """
        Find the Python executable inside kohya_ss's virtual environment.

        kohya_ss / sd-scripts requires Python 3.10-3.12 and typically ships with
        its own venv. We search for it in several common locations:
          - kohya_dir/venv/Scripts/python.exe  (Windows, pip-based)
          - kohya_dir/.venv/Scripts/python.exe (Windows, poetry/uv)
          - kohya_dir/venv/bin/python          (Linux/Mac)
          - kohya_dir/.venv/bin/python         (Linux/Mac)

        Falls back to sys.executable if no venv is found (user may have installed
        kohya_ss into the current Python environment).
        """
        # Windows paths first (Scripts/python.exe)
        win_candidates = [
            kohya_dir / "venv" / "Scripts" / "python.exe",
            kohya_dir / ".venv" / "Scripts" / "python.exe",
        ]
        # Unix paths (bin/python)
        unix_candidates = [
            kohya_dir / "venv" / "bin" / "python",
            kohya_dir / ".venv" / "bin" / "python",
        ]

        for candidate in win_candidates + unix_candidates:
            if candidate.exists():
                print(f"  Using kohya_ss venv Python: {candidate}")
                return str(candidate)

        # Check if user configured an explicit path
        comfyui_cfg = self.project.get_api_config("comfyui")
        explicit_python = comfyui_cfg.get("kohya_python")
        if explicit_python and Path(explicit_python).exists():
            print(f"  Using configured kohya_python: {explicit_python}")
            return explicit_python

        # Fallback — warn the user since Python 3.14 is likely incompatible
        print(f"  [WARN] No kohya_ss venv found at {kohya_dir}")
        print(f"  [WARN] Falling back to {sys.executable} (may be incompatible)")
        print(f"  Tip: Set apis.comfyui.kohya_python in project.json to the correct path")
        return sys.executable

    def _find_comfyui_lora_dir(self) -> Optional[Path]:
        """
        Find ComfyUI's lora model directory for auto-deploying trained LoRAs.

        Search order:
          1. Explicit apis.comfyui.lora_dir in project.json
          2. Common Windows ComfyUI model locations
          3. Common Linux/Mac locations
        """
        comfyui_cfg = self.project.get_api_config("comfyui")

        # 1. Explicit config
        explicit = comfyui_cfg.get("lora_dir")
        if explicit:
            lora_dir = Path(explicit)
            if lora_dir.exists():
                return lora_dir
            # Try creating it if parent exists
            if lora_dir.parent.exists():
                lora_dir.mkdir(parents=True, exist_ok=True)
                return lora_dir
            print(f"  [WARN] Configured lora_dir not found: {explicit}")

        # 2. Common locations
        candidates = [
            Path("C:/ComfyUIModels/models/loras"),
            Path("C:/ComfyUI/models/loras"),
            Path("C:/ComfyUI_windows_portable/ComfyUI/models/loras"),
            Path.home() / "ComfyUI" / "models" / "loras",
            Path.home() / ".comfyui" / "models" / "loras",
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        return None

    def _deploy_lora_to_comfyui(self, lora_file: Path, trigger_word: str) -> Optional[str]:
        """
        Copy a trained LoRA .safetensors file to ComfyUI's lora directory.

        Returns the deployed path, or None if deployment failed.
        """
        lora_dir = self._find_comfyui_lora_dir()
        if not lora_dir:
            print(f"  [WARN] Cannot auto-deploy: ComfyUI lora directory not found.")
            print(f"  Tip: Set apis.comfyui.lora_dir in project.json")
            print(f"  Manual copy needed: {lora_file}")
            return None

        dest = lora_dir / lora_file.name
        try:
            shutil.copy2(str(lora_file), str(dest))
            print(f"  [OK] LoRA deployed to ComfyUI: {dest}")
            return str(dest)
        except (OSError, PermissionError) as e:
            print(f"  [WARN] Failed to deploy LoRA: {e}")
            print(f"  Manual copy needed: {lora_file} -> {lora_dir}")
            return None

    def _find_checkpoint_path(self, checkpoint_name: str) -> Optional[str]:
        """
        Try to find the full path to a checkpoint file.

        Checks common locations:
          - apis.comfyui.checkpoint_path in project.json
          - C:\\ComfyUIModels\\models\\checkpoints\\
          - ~/ComfyUI/models/checkpoints/
        """
        # First check if an explicit path is configured
        comfyui_cfg = self.project.get_api_config("comfyui")
        explicit = comfyui_cfg.get("checkpoint_path")
        if explicit and Path(explicit).exists():
            return explicit

        # Common ComfyUI model directories
        candidates = [
            Path("C:/ComfyUIModels/models/checkpoints") / checkpoint_name,
            Path("C:/ComfyUI/models/checkpoints") / checkpoint_name,
            Path.home() / "ComfyUI" / "models" / "checkpoints" / checkpoint_name,
            Path.home() / ".comfyui" / "models" / "checkpoints" / checkpoint_name,
        ]

        for path in candidates:
            if path.exists():
                return str(path)

        return None
