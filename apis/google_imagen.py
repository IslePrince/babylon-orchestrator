"""
apis/google_imagen.py
Google Imagen 4.0 client for storyboard image generation.
Generates two images per shot: 1024x576 (16:9) and 576x1024 (9:16).

Includes automatic retry with exponential backoff on 429 rate-limit errors
and configurable inter-request delay to stay within quota.
"""

import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Resolve .env relative to this file's parent (orchestrator root)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path, override=True)


class ConfigurationError(Exception):
    """Raised when required configuration is missing."""
    pass


class GoogleImagenClient:
    """
    Generates storyboard images using Google's Imagen 3 model
    via the google-generativeai SDK.

    Env key: GOOGLE_AI_API_KEY
    Model: imagen-3.0-generate-002
    """

    API_NAME = "google_imagen"
    ENV_KEY = "GOOGLE_AI_API_KEY"
    MODEL = "imagen-4.0-generate-001"

    # Retry settings for 429 rate-limit errors
    MAX_RETRIES = 5
    INITIAL_BACKOFF_SECS = 30      # start at 30s (quota resets are slow)
    MAX_BACKOFF_SECS = 300         # cap at 5 minutes
    REQUEST_DELAY_SECS = 1.5      # delay between consecutive requests

    def __init__(self):
        self.api_key = os.getenv(self.ENV_KEY, "")
        if not self.api_key:
            raise ConfigurationError(
                f"Missing API key: {self.ENV_KEY}\n"
                f"Get one at https://aistudio.google.com/\n"
                f"Add to your .env file:\n"
                f"  {self.ENV_KEY}=your-key-here"
            )

        try:
            from google import genai
        except ImportError:
            raise ConfigurationError(
                "google-generativeai package not installed.\n"
                "Run: pip install google-generativeai"
            )

        self._genai = genai
        self.client = genai.Client(api_key=self.api_key)
        self._last_request_time = 0.0  # for inter-request throttling

    def _throttle(self):
        """Enforce minimum delay between consecutive API requests."""
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
        seed: int = None,
        negative_prompt: str = None,  # accepted for interface compat; Imagen ignores it
    ) -> dict:
        """
        Generate a single storyboard image.
        Default 1024x576 (16:9) for cinematic framing.
        Returns metadata dict with path, size, dimensions, cost.

        Automatically retries on 429 (RESOURCE_EXHAUSTED) with exponential backoff.
        """
        # Prompt comes pre-structured from prompt_builder with quality directives
        full_prompt = prompt

        # Determine aspect ratio string for the API
        if width > height:
            aspect = "16:9"
        elif height > width:
            aspect = "9:16"
        else:
            aspect = "1:1"

        from google.genai import types

        backoff = self.INITIAL_BACKOFF_SECS
        last_error = None

        for attempt in range(self.MAX_RETRIES + 1):
            self._throttle()
            try:
                response = self.client.models.generate_images(
                    model=self.MODEL,
                    prompt=full_prompt,
                    config=types.GenerateImagesConfig(
                        number_of_images=1,
                        aspect_ratio=aspect,
                    ),
                )

                if not response.generated_images:
                    raise RuntimeError(f"[{self.API_NAME}] No image in response")

                image = response.generated_images[0].image
                image_bytes = image.image_bytes

                output = Path(output_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(image_bytes)

                return {
                    "path": str(output),
                    "size_bytes": len(image_bytes),
                    "width": width,
                    "height": height,
                    "cost_usd": 0.04,
                    "provider": self.API_NAME,
                    "model": self.MODEL,
                }

            except Exception as e:
                last_error = e
                err_str = str(e)
                is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str

                if is_rate_limit and attempt < self.MAX_RETRIES:
                    print(f"    [RATE LIMIT] 429 hit, waiting {backoff}s before retry {attempt + 1}/{self.MAX_RETRIES}...")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self.MAX_BACKOFF_SECS)
                    continue

                raise last_error

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
        from world bible for consistent art direction.
        """
        character_visuals = character_visuals or {}
        self._visual_style = visual_style
        results = []
        estimated_cost = len(shots) * 0.04 * (2 if include_vertical else 1)
        print(f"\n  Storyboard generation: {len(shots)} shots")
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

            # Skip shots that already have generated images (saves quota on re-runs)
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
                # Cinematic (16:9) — 1024x576
                result_16_9 = self.generate_storyboard(
                    prompt=prompt,
                    output_path=str(output_path),
                )
                print(f"    [OK] 16:9 storyboard saved (1024x576)")

                from .prompt_builder import DEFAULT_VISUAL_STYLE as _DEF_STYLE
                result = {
                    "shot_id": shot_id,
                    "status": "generated",
                    "image_16_9": result_16_9["path"],
                    "cost_usd": result_16_9["cost_usd"],
                    "provider": self.API_NAME,
                    "model": self.MODEL,
                    "final_prompt": prompt,
                    "negative_prompt": None,
                    "visual_style": self._visual_style or _DEF_STYLE,
                }

                # Vertical (9:16) — 576x1024
                if include_vertical:
                    vertical_path = str(output_path).replace(
                        "storyboard.png", "storyboard_vertical.png"
                    )
                    # Skip vertical if it already exists too
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
                err_str = str(e)
                is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                if is_rate_limit:
                    print(f"    [QUOTA] Daily quota exhausted after retries. {len(shots) - i - 1} shots remaining.")
                    print(f"    Re-run this stage tomorrow — already-generated shots will be skipped.")
                    results.append({"shot_id": shot_id, "status": "quota_exhausted", "error": err_str})
                    break
                print(f"    [FAIL] Failed: {e}")
                results.append({"shot_id": shot_id, "status": "failed", "error": err_str})

        generated = sum(1 for r in results if r["status"] == "generated")
        total_cost = sum(r.get("cost_usd", 0) for r in results)
        if skipped_existing:
            print(f"\n  Skipped {skipped_existing} shots with existing images")
        print(f"  Generated: {generated}/{len(shots)} storyboards, ${total_cost:.2f} spent")
        return results

    # Legacy _inject_character_tags removed — now uses apis.prompt_builder

    def estimate_cost(self, shot_count: int, include_vertical: bool = True) -> float:
        multiplier = 2 if include_vertical else 1
        return round(shot_count * 0.04 * multiplier, 2)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
