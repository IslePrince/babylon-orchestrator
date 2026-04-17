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
        output_format: str = "mp3_44100_128",
        previous_text: str = "",
        next_text: str = "",
    ) -> bytes:
        """
        Generate audio for a dialogue line.
        Returns raw audio bytes.

        previous_text / next_text provide invisible emotional context
        to ElevenLabs (not spoken, but influences prosody and tone).
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
        if previous_text:
            payload["previous_text"] = previous_text
        if next_text:
            payload["next_text"] = next_text

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
        model: str = "eleven_multilingual_v2",
        previous_text: str = "",
        next_text: str = "",
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
            use_speaker_boost=settings.get("use_speaker_boost", True),
            previous_text=previous_text,
            next_text=next_text,
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

        import hashlib, json as _json

        cached = 0
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
            meta_path = output_path.with_suffix(".meta.json")
            char_cost = round((len(line["text"]) / 1000) * 0.30, 4)
            prev_ctx = line.get("previous_text", "")
            next_ctx = line.get("next_text", "")
            text_hash = hashlib.md5(
                f"{line['text']}:{voice_id}:{prev_ctx}:{next_ctx}".encode()
            ).hexdigest()

            # Cache check: reuse existing audio if text + voice haven't changed
            if output_path.exists() and meta_path.exists():
                try:
                    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                    if meta.get("text_hash") == text_hash:
                        cached += 1
                        size = output_path.stat().st_size
                        results.append({
                            "line_id": line["line_id"],
                            "character_id": char_id,
                            "status": "generated",
                            "cost_usd": 0,
                            "size_bytes": size,
                            "estimated_duration_sec": meta.get("duration_sec", 0),
                        })
                        if cached <= 3:
                            print(f"  [{i+1}/{len(lines)}] Cached: {line['line_id']} ({char_id})")
                        elif cached == 4:
                            print(f"  ... (suppressing further cache hits)")
                        continue
                except Exception:
                    pass  # meta corrupt — regenerate

            try:
                print(f"  [{i+1}/{len(lines)}] Generating: {line['line_id']} ({char_id})")
                result = self.generate_and_save(
                    text=line["text"],
                    voice_id=voice_id,
                    output_path=str(output_path),
                    voice_settings=voice_cfg.get("settings", {}),
                    model=voice_cfg.get("model", "eleven_multilingual_v2"),
                    previous_text=prev_ctx,
                    next_text=next_ctx,
                )
                result.update({
                    "line_id": line["line_id"],
                    "character_id": char_id,
                    "status": "generated",
                    "cost_usd": char_cost
                })
                results.append(result)
                print(f"    ✓ Saved {result['size_bytes']} bytes, ~{result['estimated_duration_sec']}s")

                # Write cache metadata
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta_path.write_text(_json.dumps({
                    "text_hash": text_hash,
                    "text": line["text"],
                    "voice_id": voice_id,
                    "character_id": char_id,
                    "duration_sec": result.get("estimated_duration_sec", 0),
                    "direction": line.get("direction", ""),
                    "previous_text": prev_ctx,
                    "next_text": next_ctx,
                }, indent=2), encoding="utf-8")

            except APIError as e:
                print(f"    ✗ Failed: {e}")
                results.append({
                    "line_id": line["line_id"],
                    "status": "failed",
                    "error": str(e)
                })

        generated = sum(1 for r in results if r["status"] == "generated")
        actual_cost = sum(r.get("cost_usd", 0) for r in results)
        cache_msg = f", {cached} cached" if cached else ""
        print(f"\n  Done: {generated}/{len(lines)} lines generated{cache_msg}, ${actual_cost:.4f} spent")
        return results

    # ------------------------------------------------------------------
    # Shared voice library
    # ------------------------------------------------------------------

    def search_shared_voices(self, params: dict = None) -> list:
        """
        Search the ElevenLabs shared voice library.
        Returns voices from GET /v1/shared-voices.

        Supported params: search, gender, age, accent, language,
        use_cases, category, page_size (max 100, default 25).
        """
        allowed = {
            "search", "gender", "age", "accent", "language",
            "use_cases", "category", "page_size",
        }
        query = {"page_size": 25}
        if params:
            for k, v in params.items():
                if k in allowed and v:
                    query[k] = v
        page_size = int(query.get("page_size", 25))
        query["page_size"] = min(page_size, 100)

        response = self.get("/shared-voices", params=query)
        return response.json().get("voices", [])

    # ------------------------------------------------------------------
    # Sound effects generation
    # ------------------------------------------------------------------

    def generate_sound_effect(
        self,
        prompt: str,
        duration_sec: float = None,
        output_path: str = None,
    ) -> dict:
        """
        Generate a sound effect from a text prompt.
        Uses POST /v1/sound-generation.

        Returns dict with path, size_bytes, duration_sec, cost_usd.
        """
        payload = {
            "text": prompt,
        }
        if duration_sec is not None:
            payload["duration_seconds"] = min(max(0.5, duration_sec), 22.0)

        response = self._request(
            "POST",
            "/sound-generation",
            json=payload,
            headers={"Accept": "audio/mpeg"},
        )
        audio_bytes = response.content

        # ElevenLabs SFX pricing: ~$0.10 per generation (flat rate estimate)
        cost = 0.10

        result = {
            "size_bytes": len(audio_bytes),
            "duration_sec": duration_sec or 5.0,
            "cost_usd": cost,
            "prompt": prompt,
        }

        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as f:
                f.write(audio_bytes)
            result["path"] = str(out)

        return result

    # ------------------------------------------------------------------
    # Music generation
    # ------------------------------------------------------------------

    def generate_music(
        self,
        prompt: str,
        duration_sec: float = 30.0,
        output_path: str = None,
    ) -> dict:
        """
        Generate a music piece from a text prompt.
        Uses POST /v1/sound-generation with a music-oriented prompt.

        Returns dict with path, size_bytes, duration_sec, cost_usd.
        """
        # Music generation uses the same sound-generation endpoint
        # with music-specific prompts
        payload = {
            "text": prompt,
            "duration_seconds": min(max(0.5, duration_sec), 22.0),
        }

        response = self._request(
            "POST",
            "/sound-generation",
            json=payload,
            headers={"Accept": "audio/mpeg"},
        )
        audio_bytes = response.content

        # Music pricing estimate: ~$0.15 per generation
        cost = 0.15

        result = {
            "size_bytes": len(audio_bytes),
            "duration_sec": duration_sec,
            "cost_usd": cost,
            "prompt": prompt,
        }

        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as f:
                f.write(audio_bytes)
            result["path"] = str(out)

        return result

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def estimate_cost(self, text: str) -> float:
        """Estimate cost for a single text string (voice generation)."""
        return round((len(text) / 1000) * 0.30, 4)

    def estimate_batch_cost(self, lines: list) -> float:
        total_chars = sum(len(line.get("text", "")) for line in lines)
        return round((total_chars / 1000) * 0.30, 4)

    def estimate_sfx_cost(self, count: int) -> float:
        """Estimate cost for N sound effect generations."""
        return round(count * 0.10, 4)

    def estimate_music_cost(self, count: int) -> float:
        """Estimate cost for N music piece generations."""
        return round(count * 0.15, 4)
