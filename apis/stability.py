"""
apis/stability.py
Stability AI client for storyboard placeholder generation.
Low cost, fast — used only for visual approval before any real spend.
"""

import base64
from pathlib import Path
from typing import Optional
from .base import BaseAPIClient, APIError


class StabilityClient(BaseAPIClient):

    API_NAME = "stabilityai"
    BASE_URL = "https://api.stability.ai/v1"
    ENV_KEY = "STABILITY_API_KEY"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json"
        }

    def generate_storyboard(
        self,
        prompt: str,
        output_path: str,
        width: int = 1024,
        height: int = 576,
        steps: int = 20,
        cfg_scale: float = 7.0,
        style_preset: str = "cinematic"
    ) -> dict:
        """
        Generate a storyboard placeholder image.
        16:9 by default (matches cinematic master framing).
        Returns metadata dict.
        """
        payload = {
            "text_prompts": [
                {"text": prompt, "weight": 1.0},
                {"text": "photorealistic, cinematic lighting, film still", "weight": 0.5},
                {"text": "blurry, cartoon, anime, low quality, modern, contemporary", "weight": -1.0}
            ],
            "cfg_scale": cfg_scale,
            "height": height,
            "width": width,
            "steps": steps,
            "samples": 1,
            "style_preset": style_preset
        }

        response = self.post(
            "/generation/stable-diffusion-xl-1024-v1-0/text-to-image",
            json=payload
        )

        data = response.json()
        artifacts = data.get("artifacts", [])
        if not artifacts:
            raise APIError("stabilityai", 0, "No image in response")

        image_b64 = artifacts[0]["base64"]
        image_bytes = base64.b64decode(image_b64)

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "wb") as f:
            f.write(image_bytes)

        return {
            "path": str(output),
            "size_bytes": len(image_bytes),
            "width": width,
            "height": height,
            "cost_usd": 0.04
        }

    def generate_storyboard_vertical(
        self,
        prompt: str,
        output_path: str
    ) -> dict:
        """Generate 9:16 vertical version of storyboard."""
        return self.generate_storyboard(
            prompt=prompt,
            output_path=output_path,
            width=576,
            height=1024
        )

    def generate_shot_boards(
        self,
        shots: list,
        project_root: str,
        include_vertical: bool = True,
        dry_run: bool = False
    ) -> list:
        """
        Generate storyboard images for a list of shot dicts.
        Each shot needs: shot_id, chapter_id, storyboard.storyboard_prompt,
        storyboard.image_ref

        Returns results list.
        """
        results = []
        estimated_cost = len(shots) * 0.04 * (2 if include_vertical else 1)
        print(f"\n  Storyboard generation: {len(shots)} shots")
        print(f"  Estimated cost: ${estimated_cost:.2f}")

        if dry_run:
            print("  DRY RUN — no API calls")
            return [{"shot_id": s["shot_id"], "status": "dry_run"} for s in shots]

        for i, shot in enumerate(shots):
            shot_id = shot["shot_id"]
            chapter_id = shot["chapter_id"]
            storyboard_cfg = shot.get("storyboard", {})
            prompt = storyboard_cfg.get("storyboard_prompt", "")

            if not prompt:
                print(f"  ✗ No storyboard_prompt for {shot_id}, skipping")
                results.append({"shot_id": shot_id, "status": "skipped", "reason": "no_prompt"})
                continue

            image_ref = storyboard_cfg.get("image_ref", "")
            if not image_ref:
                image_ref = f"chapters/{chapter_id}/shots/{shot_id}/storyboard.png"

            output_path = Path(project_root) / image_ref
            print(f"  [{i+1}/{len(shots)}] {shot_id}")

            try:
                # Cinematic (16:9)
                result_16_9 = self.generate_storyboard(
                    prompt=prompt,
                    output_path=str(output_path)
                )
                print(f"    ✓ 16:9 storyboard saved")

                result = {
                    "shot_id": shot_id,
                    "status": "generated",
                    "image_16_9": result_16_9["path"],
                    "cost_usd": result_16_9["cost_usd"]
                }

                # Vertical (9:16)
                if include_vertical:
                    vertical_path = str(output_path).replace(
                        "storyboard.png", "storyboard_vertical.png"
                    )
                    result_9_16 = self.generate_storyboard_vertical(
                        prompt=prompt,
                        output_path=vertical_path
                    )
                    result["image_9_16"] = result_9_16["path"]
                    result["cost_usd"] += result_9_16["cost_usd"]
                    print(f"    ✓ 9:16 storyboard saved")

                results.append(result)

            except APIError as e:
                print(f"    ✗ Failed: {e}")
                results.append({"shot_id": shot_id, "status": "failed", "error": str(e)})

        generated = sum(1 for r in results if r["status"] == "generated")
        total_cost = sum(r.get("cost_usd", 0) for r in results)
        print(f"\n  Done: {generated}/{len(shots)} storyboards, ${total_cost:.2f} spent")
        return results

    def estimate_cost(self, shot_count: int, include_vertical: bool = True) -> float:
        multiplier = 2 if include_vertical else 1
        return round(shot_count * 0.04 * multiplier, 2)
