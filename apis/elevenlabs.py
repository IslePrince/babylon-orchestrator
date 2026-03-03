"""
apis/elevenlabs.py
ElevenLabs API client.
Handles voice generation, voice listing, and audio file management.
"""

import os
import json
from pathlib import Path
from typing import Optional
from .base import BaseAPIClient, APIError


class ElevenLabsClient(BaseAPIClient):

    API_NAME = "elevenlabs"
    BASE_URL = "https://api.elevenlabs.io/v1"
    ENV_KEY = "ELEVENLABS_API_KEY"

    def _headers(self) -> dict:
        return {
            "xi-api-key": self.api_key,
            "Accept": "application/json"
        }

    # ------------------------------------------------------------------
    # Voice management
    # ------------------------------------------------------------------

    def list_voices(self) -> list:
        """Return all available voices on the account."""
        response = self.get("/voices")
        return response.json().get("voices", [])

    def get_voice(self, voice_id: str) -> dict:
        response = self.get(f"/voices/{voice_id}")
        return response.json()

    def get_models(self) -> list:
        response = self.get("/models")
        return response.json()

    # ------------------------------------------------------------------
    # Audio generation
    # ------------------------------------------------------------------

    def generate(
        self,
        text: str,
        voice_id: str,
        model: str = "eleven_multilingual_v2",
        stability: float = 0.75,
        similarity_boost: float = 0.85,
        style: float = 0.35,
        use_speaker_boost: bool = True,
        output_format: str = "mp3_44100_128"
    ) -> bytes:
        """
        Generate audio for a dialogue line.
        Returns raw audio bytes.
        """
        payload = {
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
                "style": style,
                "use_speaker_boost": use_speaker_boost
            }
        }

        response = self._request(
            "POST",
            f"/text-to-speech/{voice_id}",
            params={"output_format": output_format},
            json=payload,
            headers={"Accept": "audio/mpeg"}
        )
        return response.content

    def generate_and_save(
        self,
        text: str,
        voice_id: str,
        output_path: str,
        voice_settings: Optional[dict] = None,
        model: str = "eleven_multilingual_v2"
    ) -> dict:
        """
        Generate audio and save to file.
        Returns metadata including estimated duration.
        """
        settings = voice_settings or {}
        audio_bytes = self.generate(
            text=text,
            voice_id=voice_id,
            model=model,
            stability=settings.get("stability", 0.75),
            similarity_boost=settings.get("similarity_boost", 0.85),
            style=settings.get("style", 0.35),
            use_speaker_boost=settings.get("use_speaker_boost", True)
        )

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "wb") as f:
            f.write(audio_bytes)

        # Rough duration estimate: ~150 words/min average speech
        word_count = len(text.split())
        estimated_duration_sec = round((word_count / 150) * 60, 1)

        return {
            "path": str(output),
            "size_bytes": len(audio_bytes),
            "estimated_duration_sec": estimated_duration_sec,
            "word_count": word_count,
            "char_count": len(text)
        }

    def generate_line_batch(
        self,
        lines: list,
        character_map: dict,
        project_root: str,
        dry_run: bool = False
    ) -> list:
        """
        Generate audio for a batch of dialogue lines.

        lines: list of dicts with keys:
            line_id, character_id, text, audio_ref

        character_map: dict of character_id -> character.json data

        Returns list of results with cost tracking data.
        """
        results = []
        total_chars = sum(len(line["text"]) for line in lines)
        estimated_cost = round((total_chars / 1000) * 0.30, 4)

        print(f"\n  ElevenLabs batch: {len(lines)} lines, ~{total_chars} chars")
        print(f"  Estimated cost: ${estimated_cost:.4f}")

        if dry_run:
            print("  DRY RUN — no API calls made")
            return [{"line_id": l["line_id"], "status": "dry_run"} for l in lines]

        for i, line in enumerate(lines):
            char_id = line["character_id"]
            char_data = character_map.get(char_id)
            if not char_data:
                print(f"  ✗ No character data for '{char_id}', skipping {line['line_id']}")
                results.append({"line_id": line["line_id"], "status": "skipped", "reason": "no_character_data"})
                continue

            voice_cfg = char_data.get("voice", {})
            voice_id = voice_cfg.get("voice_id", "")
            if not voice_id:
                print(f"  ✗ No voice_id set for '{char_id}', skipping {line['line_id']}")
                results.append({"line_id": line["line_id"], "status": "skipped", "reason": "no_voice_id"})
                continue

            output_path = Path(project_root) / line["audio_ref"]
            char_cost = round((len(line["text"]) / 1000) * 0.30, 4)

            try:
                print(f"  [{i+1}/{len(lines)}] Generating: {line['line_id']} ({char_id})")
                result = self.generate_and_save(
                    text=line["text"],
                    voice_id=voice_id,
                    output_path=str(output_path),
                    voice_settings=voice_cfg.get("settings", {}),
                    model=voice_cfg.get("model", "eleven_multilingual_v2")
                )
                result.update({
                    "line_id": line["line_id"],
                    "character_id": char_id,
                    "status": "generated",
                    "cost_usd": char_cost
                })
                results.append(result)
                print(f"    ✓ Saved {result['size_bytes']} bytes, ~{result['estimated_duration_sec']}s")

            except APIError as e:
                print(f"    ✗ Failed: {e}")
                results.append({
                    "line_id": line["line_id"],
                    "status": "failed",
                    "error": str(e)
                })

        generated = sum(1 for r in results if r["status"] == "generated")
        actual_cost = sum(r.get("cost_usd", 0) for r in results)
        print(f"\n  Done: {generated}/{len(lines)} lines generated, ${actual_cost:.4f} spent")
        return results

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def estimate_cost(self, text: str) -> float:
        """Estimate cost for a single text string."""
        return round((len(text) / 1000) * 0.30, 4)

    def estimate_batch_cost(self, lines: list) -> float:
        total_chars = sum(len(line.get("text", "")) for line in lines)
        return round((total_chars / 1000) * 0.30, 4)
