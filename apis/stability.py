"""
apis/stability.py
Stability AI client for storyboard image generation via SD3 Large Turbo.
Uses the v2beta REST API with multipart form data.

Credits cost: SD3 Large Turbo = 4 credits/image (~$0.04).
No daily quota wall — credit-based, so you can generate as fast as you want.
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path, override=True)

from .base import BaseAPIClient, APIError


class StabilityClient(BaseAPIClient):

    API_NAME = "stabilityai"
    BASE_URL = "https://api.stability.ai"
    ENV_KEY = "STABILITY_API_KEY"

    # SD3.5 Medium: 3.5 credits/image, cheaper for storyboard sketches
    MODEL = "sd3.5-medium"
    COST_PER_IMAGE = 0.035  # ~3.5 credits ≈ $0.035

    REQUEST_DELAY_SECS = 0.5  # gentle throttle between requests

    def __init__(self):
        super().__init__()
        self._last_request_time = 0.0

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _throttle(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.REQUEST_DELAY_SECS:
            time.sleep(self.REQUEST_DELAY_SECS - elapsed)
        self._last_request_time = time.time()

    def generate_storyboard(
        self,
        prompt: str,
        output_path: str,
        width: int = 1024,
        height: int = 576,
        negative_prompt: str = None,
        seed: int = None,
    ) -> dict:
        """
        Generate a storyboard image using SD3 Large Turbo.
        Returns metadata dict with path, size, dimensions, cost.

        prompt: structured block prompt from prompt_builder (includes quality directives)
        negative_prompt: override; defaults to prompt_builder.build_negative_prompt()
        """
        from .prompt_builder import build_negative_prompt

        full_prompt = prompt
        negative_prompt = negative_prompt or build_negative_prompt()

        if width > height:
            aspect = "16:9"
        elif height > width:
            aspect = "9:16"
        else:
            aspect = "1:1"

        self._throttle()

        import base64

        # v2beta requires multipart/form-data — use files= for httpx
        response = self.post(
            "/v2beta/stable-image/generate/sd3",
            files={
                "prompt": (None, full_prompt),
                "negative_prompt": (None, negative_prompt),
                "model": (None, self.MODEL),
                "aspect_ratio": (None, aspect),
                "output_format": (None, "png"),
            },
        )

        data = response.json()

        if data.get("finish_reason") == "CONTENT_FILTERED":
            raise APIError(self.API_NAME, 0, "Content filtered by safety system")

        image_b64 = data.get("image")
        if not image_b64:
            raise APIError(self.API_NAME, 0, "No image in response")

        image_bytes = base64.b64decode(image_b64)

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(image_bytes)

        return {
            "path": str(output),
            "size_bytes": len(image_bytes),
            "width": width,
            "height": height,
            "cost_usd": self.COST_PER_IMAGE,
            "provider": self.API_NAME,
            "model": self.MODEL,
        }

    def generate_character_reference(
        self,
        prompt: str,
        output_path: str,
        negative_prompt: str = None,
    ) -> dict:
        """Generate a 1:1 character reference portrait image."""
        return self.generate_storyboard(
            prompt=prompt,
            output_path=output_path,
            width=1024,
            height=1024,
            negative_prompt=negative_prompt,
        )

    def generate_with_style_guide(
        self,
        prompt: str,
        style_image_path: str,
        output_path: str,
        fidelity: float = 0.3,
        width: int = 1024,
        height: int = 576,
        negative_prompt: str = None,
    ) -> dict:
        """
        Generate an image using a style reference image for character consistency.
        Uses the SD3 endpoint with image input for style guidance.
        Fidelity 0.0-1.0 controls how much the style image influences output.
        """
        from .prompt_builder import build_negative_prompt

        negative_prompt = negative_prompt or build_negative_prompt()

        self._throttle()

        import base64

        # Read the style reference image
        style_image_data = Path(style_image_path).read_bytes()

        # Use SD3 with image input for style-guided generation
        # NOTE: aspect_ratio and image are mutually exclusive in the SD3 API —
        # output aspect ratio is determined by the input image when using image-to-image.
        response = self.post(
            "/v2beta/stable-image/generate/sd3",
            files={
                "prompt": (None, prompt),
                "negative_prompt": (None, negative_prompt),
                "model": (None, self.MODEL),
                "output_format": (None, "png"),
                "image": ("reference.png", style_image_data, "image/png"),
                "strength": (None, str(1.0 - fidelity)),  # strength=0.7 means 30% style influence
                "mode": (None, "image-to-image"),
            },
        )

        data = response.json()

        if data.get("finish_reason") == "CONTENT_FILTERED":
            raise APIError(self.API_NAME, 0, "Content filtered by safety system")

        image_b64 = data.get("image")
        if not image_b64:
            raise APIError(self.API_NAME, 0, "No image in response")

        image_bytes = base64.b64decode(image_b64)

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(image_bytes)

        return {
            "path": str(output),
            "size_bytes": len(image_bytes),
            "width": width,
            "height": height,
            "cost_usd": self.COST_PER_IMAGE,
            "provider": self.API_NAME,
            "model": self.MODEL,
            "style_guide_used": True,
            "fidelity": fidelity,
        }

    def generate_storyboard_vertical(
        self,
        prompt: str,
        output_path: str,
        seed: int = None,
        negative_prompt: str = None,
    ) -> dict:
        """Generate 9:16 vertical version of storyboard."""
        return self.generate_storyboard(
            prompt=prompt,
            output_path=output_path,
            width=576,
            height=1024,
            negative_prompt=negative_prompt,
        )

    def generate_shot_boards(
        self,
        shots: list,
        project_root: str,
        include_vertical: bool = True,
        dry_run: bool = False,
        character_visuals: dict = None,
        visual_style: str = "",
        force: bool = False,
    ) -> list:
        """
        Generate storyboard images for a list of shot dicts.
        Supports character_visuals for visual consistency and visual_style
        from world bible. Character descriptions are embedded in prompts
        by prompt_builder — image-to-image is NOT used for storyboards
        as it overrides shot composition with the reference portrait.
        """
        character_visuals = character_visuals or {}
        self._visual_style = visual_style
        results = []
        estimated_cost = len(shots) * self.COST_PER_IMAGE * (2 if include_vertical else 1)
        print(f"\n  Storyboard generation (Stability SD3): {len(shots)} shots")
        print(f"  Estimated cost: ${estimated_cost:.2f}")
        if character_visuals:
            print(f"  Character visual tags loaded: {list(character_visuals.keys())}")

        if dry_run:
            print("  DRY RUN -- no API calls")
            return [{"shot_id": s["shot_id"], "status": "dry_run"} for s in shots]

        skipped_existing = 0
        for i, shot in enumerate(shots):
            shot_id = shot["shot_id"]
            chapter_id = shot["chapter_id"]
            storyboard_cfg = shot.get("storyboard", {})
            prompt = storyboard_cfg.get("storyboard_prompt", "")

            if not prompt:
                print(f"  [FAIL] No storyboard_prompt for {shot_id}, skipping")
                results.append({"shot_id": shot_id, "status": "skipped", "reason": "no_prompt"})
                continue

            # Build enriched prompt with visual style + character descriptions
            from .prompt_builder import build_storyboard_prompt
            prompt = build_storyboard_prompt(prompt, shot, character_visuals, self._visual_style)

            image_ref = storyboard_cfg.get("image_ref", "")
            if not image_ref:
                image_ref = f"chapters/{chapter_id}/shots/{shot_id}/storyboard.png"

            output_path = Path(project_root) / image_ref

            # Skip shots that already have generated images
            if not force and output_path.exists() and output_path.stat().st_size > 1000:
                skipped_existing += 1
                results.append({
                    "shot_id": shot_id,
                    "status": "already_exists",
                    "image_16_9": str(output_path),
                    "cost_usd": 0,
                })
                continue

            print(f"  [{i+1}/{len(shots)}] {shot_id}")
            print(f"    Prompt: {prompt[:120]}...")

            try:
                result_16_9 = self.generate_storyboard(
                    prompt=prompt,
                    output_path=str(output_path),
                )
                print(f"    [OK] 16:9 storyboard saved (1024x576)")

                from .prompt_builder import build_negative_prompt as _build_neg, DEFAULT_VISUAL_STYLE as _DEF_STYLE
                result = {
                    "shot_id": shot_id,
                    "status": "generated",
                    "image_16_9": result_16_9["path"],
                    "cost_usd": result_16_9["cost_usd"],
                    "provider": self.API_NAME,
                    "model": self.MODEL,
                    "final_prompt": prompt,
                    "negative_prompt": _build_neg(),
                    "visual_style": self._visual_style or _DEF_STYLE,
                }

                if include_vertical:
                    vertical_path = str(output_path).replace(
                        "storyboard.png", "storyboard_vertical.png"
                    )
                    if not force and Path(vertical_path).exists() and Path(vertical_path).stat().st_size > 1000:
                        result["image_9_16"] = vertical_path
                        print(f"    [SKIP] 9:16 already exists")
                    else:
                        result_9_16 = self.generate_storyboard_vertical(
                            prompt=prompt,
                            output_path=vertical_path,
                        )
                        result["image_9_16"] = result_9_16["path"]
                        result["cost_usd"] += result_9_16["cost_usd"]
                        print(f"    [OK] 9:16 storyboard saved (576x1024)")

                results.append(result)

            except Exception as e:
                print(f"    [FAIL] Failed: {e}")
                results.append({"shot_id": shot_id, "status": "failed", "error": str(e)})

        generated = sum(1 for r in results if r["status"] == "generated")
        total_cost = sum(r.get("cost_usd", 0) for r in results)
        if skipped_existing:
            print(f"\n  Skipped {skipped_existing} shots with existing images")
        print(f"  Generated: {generated}/{len(shots)} storyboards, ${total_cost:.2f} spent")
        return results

    def estimate_cost(self, shot_count: int, include_vertical: bool = True) -> float:
        multiplier = 2 if include_vertical else 1
        return round(shot_count * self.COST_PER_IMAGE * multiplier, 2)


    # Legacy _inject_character_tags removed — now uses apis.prompt_builder
