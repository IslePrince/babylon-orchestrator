"""
apis/cartwheel.py
Cartwheel (getcartwheel.com) API client.
Handles motion generation for NAMED characters only.
Outputs UE5-ready FBX animation, compatible with MetaHuman retargeting.

Why Cartwheel over Meshy animation for named characters:
  - Works on any rigged skeleton, not just Meshy-generated meshes
  - MetaHuman retargeting workflow is well-supported
  - Better nuance for performance-critical dialogue scenes
  - Supports interaction between two characters
  - More control over timing and style

Workflow in UE5:
  1. Import Cartwheel FBX into UE5
  2. Use IKRetargeter to map to MetaHuman skeleton
  3. Apply to Sequencer track for the character
  4. Fine-tune timing to match ElevenLabs audio
"""

import json
import time
from pathlib import Path
from typing import Optional
from .base import BaseAPIClient, APIError


class ConfigurationError(Exception):
    """Raised when Cartwheel API is not properly configured."""
    pass


class CartwheelClient(BaseAPIClient):

    API_NAME = "cartwheel"
    BASE_URL = "https://api.getcartwheel.com/v1"
    ENV_KEY = "CARTWHEEL_API_KEY"

    def __init__(self):
        import os
        self.api_key = os.getenv(self.ENV_KEY, "")
        if not self.api_key:
            raise ConfigurationError(
                "Cartwheel API key not set -- animation stage unavailable.\n"
                "Set CARTWHEEL_API_KEY in your .env file to enable character animation.\n"
                "Get a key at https://getcartwheel.com/"
            )
        import httpx
        self.client = httpx.Client(timeout=120.0)

    POLL_INTERVAL = 10
    MAX_POLL_TIME = 300

    # Approximate cost per animation (update when Cartwheel publishes pricing)
    COST_PER_CLIP = 0.15

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    # ------------------------------------------------------------------
    # Motion generation
    # ------------------------------------------------------------------

    def submit_motion(
        self,
        prompt: str,
        duration_sec: float = 4.0,
        character_height_cm: float = 170.0,
        style: str = "realistic",
        seed: Optional[int] = None
    ) -> str:
        """
        Submit a text-to-motion generation job.
        Returns job_id.

        prompt examples:
          "elderly man sitting, gesturing slowly with one hand while speaking wisely"
          "middle-aged craftsman standing, frustrated, arms crossed, head down"
          "two men sitting across from each other in conversation"
        """
        payload = {
            "prompt": prompt,
            "duration": duration_sec,
            "character_height": character_height_cm / 100.0,  # Cartwheel uses meters
            "style": style,
        }
        if seed is not None:
            payload["seed"] = seed

        resp = self.post("/motions", json=payload)
        data = resp.json()
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise APIError("cartwheel", 0, f"No job ID: {resp.text}")

        print(f"    -> Cartwheel job: {job_id}")
        return job_id

    def get_motion_status(self, job_id: str) -> dict:
        resp = self.get(f"/motions/{job_id}")
        return resp.json()

    def poll_motion(self, job_id: str, label: str = "") -> dict:
        tag = f" [{label}]" if label else ""
        elapsed = 0
        while elapsed < self.MAX_POLL_TIME:
            data = self.get_motion_status(job_id)
            status = data.get("status", "").lower()
            print(f"    {tag} {status}    ", end="\r")

            if status in ("completed", "succeeded", "done"):
                print(f"    [OK]{tag} motion complete   ")
                return data
            if status in ("failed", "error"):
                raise APIError("cartwheel", 0, f"Motion failed: {data.get('error')}")

            time.sleep(self.POLL_INTERVAL)
            elapsed += self.POLL_INTERVAL

        raise APIError("cartwheel", 0, f"Motion timed out after {self.MAX_POLL_TIME}s")

    def download_motion(self, job_id: str, output_path: str) -> str:
        """Download completed FBX animation file."""
        resp = self.get(f"/motions/{job_id}/download",
                        headers={"Accept": "application/octet-stream"})

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "wb") as f:
            f.write(resp.content)
        print(f"      -> {out.name} saved ({len(resp.content):,} bytes)")
        return str(out)

    def generate_motion(
        self,
        prompt: str,
        output_path: str,
        duration_sec: float = 4.0,
        character_height_cm: float = 170.0,
        style: str = "realistic"
    ) -> dict:
        """
        Full pipeline: submit → poll → download.
        Returns file path and metadata.
        """
        job_id = self.submit_motion(
            prompt=prompt,
            duration_sec=duration_sec,
            character_height_cm=character_height_cm,
            style=style
        )
        data = self.poll_motion(job_id, label=prompt[:30])
        path = self.download_motion(job_id, output_path)

        return {
            "job_id": job_id,
            "path": path,
            "duration_sec": duration_sec,
            "prompt": prompt,
            "cost_usd": self.COST_PER_CLIP
        }

    # ------------------------------------------------------------------
    # Character motion library builder
    # ------------------------------------------------------------------

    def build_character_motion_library(
        self,
        character: dict,
        shot_requirements: list,
        output_dir: str,
        dry_run: bool = False
    ) -> dict:
        """
        Generate a motion library for a named character based on
        what shots they appear in.

        character: full character.json schema dict
        shot_requirements: list of dicts:
          [{"shot_id": "...", "duration_sec": 8, "animation_note": "idle listening",
            "expression_id": "neutral_wise"}]

        Returns: dict of shot_id → animation file path
        """
        char_id = character["character_id"]
        char_name = character["display_name"]
        cartwheel_cfg = character.get("animation", {}).get("cartwheel", {})
        motion_style = cartwheel_cfg.get("motion_style", "")
        prompt_prefix = cartwheel_cfg.get("prompt_prefix", "")
        height = character.get("description", {}).get("height_cm", 170.0)

        print(f"\n  Building motion library: {char_name} ({len(shot_requirements)} shots)")

        # Deduplicate similar animation notes into reusable clips
        unique_motions = self._deduplicate_motions(shot_requirements)
        est_cost = len(unique_motions) * self.COST_PER_CLIP
        print(f"  Unique motions after dedup: {len(unique_motions)}")
        print(f"  Estimated: ${est_cost:.2f}")

        if dry_run:
            return {"character_id": char_id, "status": "dry_run",
                    "unique_motions": len(unique_motions), "cost_usd": est_cost}

        results = {}
        motion_index = []
        total_cost = 0.0

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        for motion in unique_motions:
            motion_id = motion["motion_id"]
            note = motion["animation_note"]
            duration = motion["duration_sec"]

            # Build full prompt from prefix + motion style + note
            prompt_parts = [p for p in [prompt_prefix, f"{motion_style} style", note] if p]
            full_prompt = ", ".join(prompt_parts)

            output_path = str(out / f"{motion_id}.fbx")
            print(f"\n  [{motion_id}] {full_prompt[:70]}")

            try:
                result = self.generate_motion(
                    prompt=full_prompt,
                    output_path=output_path,
                    duration_sec=duration,
                    character_height_cm=height
                )
                results[motion_id] = result["path"]
                motion_index.append({
                    "motion_id": motion_id,
                    "prompt": full_prompt,
                    "duration_sec": duration,
                    "path": result["path"],
                    "used_by_shots": motion["used_by_shots"]
                })
                total_cost += self.COST_PER_CLIP

            except APIError as e:
                print(f"  [FAIL] {motion_id}: {e}")
                results[motion_id] = None

        # Save motion index
        index_path = out / "motion_index.json"
        with open(index_path, "w") as f:
            json.dump({
                "character_id": char_id,
                "character_name": char_name,
                "total_clips": len(motion_index),
                "total_cost_usd": total_cost,
                "motions": motion_index
            }, f, indent=2)

        success = sum(1 for v in results.values() if v)
        print(f"\n  [OK] {char_name}: {success}/{len(unique_motions)} clips generated -- ${total_cost:.2f}")
        return {
            "character_id": char_id,
            "status": "completed",
            "motion_results": results,
            "motion_index": str(index_path),
            "cost_usd": total_cost
        }

    def _deduplicate_motions(self, shot_requirements: list) -> list:
        """
        Group similar animation notes to avoid generating the same
        motion multiple times. Returns list of unique motion dicts.
        """
        seen = {}
        for req in shot_requirements:
            note = req.get("animation_note", "idle").lower().strip()
            dur = req.get("duration_sec", 4.0)

            # Round duration to nearest 2s for dedup (4.3s ≈ 4s)
            dur_bucket = round(dur / 2) * 2
            key = f"{note}_{dur_bucket}"

            if key not in seen:
                motion_id = f"motion_{len(seen):03d}_{note.replace(' ','_')[:20]}"
                seen[key] = {
                    "motion_id": motion_id,
                    "animation_note": note,
                    "duration_sec": max(dur, 2.0),
                    "used_by_shots": []
                }
            seen[key]["used_by_shots"].append(req.get("shot_id", "unknown"))

        return list(seen.values())

    # ------------------------------------------------------------------
    # Shot animation requirements extractor
    # ------------------------------------------------------------------

    def extract_shot_requirements(
        self,
        character_id: str,
        shots: list
    ) -> list:
        """
        Scan shot list and extract animation requirements for one character.
        shots: list of full shot.json dicts
        """
        requirements = []
        for shot in shots:
            for char in shot.get("characters_in_frame", []):
                if char.get("character_id") == character_id:
                    requirements.append({
                        "shot_id": shot["shot_id"],
                        "duration_sec": shot.get("cinematic", {}).get(
                            "camera_movement", {}).get("duration_sec",
                            shot.get("meta", {}).get("estimated_render_min", 4)
                        ),
                        "animation_note": char.get("animation_note", "idle neutral"),
                        "expression_id": char.get("expression_id", "neutral")
                    })
        return requirements

    # ------------------------------------------------------------------
    # UE5 integration notes
    # ------------------------------------------------------------------

    def get_ue5_import_notes(self, character: dict) -> str:
        """Return UE5 import and retargeting instructions for this character."""
        char_name = character["display_name"]
        mh_id = character.get("animation", {}).get("unreal", {}).get("metahuman_id", "")

        return f"""
UE5 Cartwheel Import Instructions -- {char_name}
{'='*50}

1. Import FBX animations:
   Content Browser → Import → select .fbx files
   Import options: Animation only, select UE4 mannequin skeleton

2. Create IK Retargeter:
   Create → Animation → IK Retargeter
   Source: UE4_Mannequin_Skeleton
   Target: {mh_id if mh_id else f'{char_name}_MetaHuman_Skeleton'}

3. Retarget animations:
   Select all Cartwheel .fbx imports
   Right-click → Retarget → select IK Retargeter
   Export to: Animation/Characters/{character["character_id"]}/Retargeted/

4. Apply to Sequencer:
   Open scene Sequencer
   Character track → Animation → Add retargeted clip
   Trim/blend to match ElevenLabs audio timing

5. Layer facial animation:
   Audio2Face or MetaHuman Animator for lip sync
   Expression library clips layered on top of body animation
   Blend weights in Sequencer animation track

Notes:
   - Cartwheel outputs at 30fps, UE5 project may need 24fps
   - Check foot IK after retargeting -- ground contact may need adjustment
   - Motion clips are generated per-shot duration, extend via loop if needed
"""
