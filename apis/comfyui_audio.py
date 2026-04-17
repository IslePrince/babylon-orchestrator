"""
apis/comfyui_audio.py
ComfyUI local client for sound-effect generation via Stable Audio Open.

Runs the same queue/poll/fetch flow as apis.comfyui.ComfyUIClient but
with a text-to-audio workflow built around the locally-installed
stable-audio-open-1.0.safetensors checkpoint. Zero API cost — all
inference runs on the host GPU.

Nodes used (all present in standard ComfyUI as of this writing):
  CheckpointLoaderSimple, CLIPTextEncode, ConditioningStableAudio,
  EmptyLatentAudio, KSampler, VAEDecodeAudio, SaveAudioMP3.

Output files are written to ComfyUI's output dir with a random prefix,
then downloaded via /view and copied to the caller's requested path.
"""

import os
import random
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx


DEFAULT_CHECKPOINT = "stable-audio-open-1.0.safetensors"
# Stable Audio Open ships its UNet + VAE in the checkpoint but relies on
# an external T5 text encoder. t5-base is what the official ComfyUI
# workflow ships with.
DEFAULT_TEXT_ENCODER = "t5-base.safetensors"
# Stable Audio Open is trained on a music+SFX mix and easily slips into
# musical interpretations of tonal/repetitive prompts ("hum" becomes a
# synth pad, repeated footsteps become drums). Heavy anti-music negatives
# are required to keep it on the foley side of its training distribution.
DEFAULT_NEGATIVE = (
    "music, song, melody, rhythm, beat, drums, percussion, kick, snare, "
    "synthesizer, synth, bass, pad, tonal, pitched, melodic, harmonic, "
    "electronic, edm, techno, sci-fi, futuristic, spacey, ambient pad, "
    "low quality, artifacts, distortion, clipping, muddy, lo-fi"
)
# Foley framing stabilizes Stable Audio's output toward SFX textures.
FOLEY_PREFIX = "Clean foley sound effect recording, realistic, no music, no melody: "

# Stable Audio Open hyperparameters. Defaults tuned for SFX on an RTX
# 4090: ~100 steps gives solid quality in ~8-12s per 5s clip.
DEFAULT_STEPS = 100
DEFAULT_CFG = 6.0
DEFAULT_SAMPLER = "dpmpp_2m"
DEFAULT_SCHEDULER = "simple"

# Stable Audio Open's training window caps around 47s.
MAX_DURATION_SEC = 47.0
MIN_DURATION_SEC = 1.0

POLL_INTERVAL = 0.75
TIMEOUT = 240.0


class ComfyUIAudioError(Exception):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.details = details or {}


class ComfyUIAudioClient:
    """Text-to-SFX client backed by a local ComfyUI + Stable Audio Open."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        checkpoint: Optional[str] = None,
        text_encoder: Optional[str] = None,
    ):
        self.base_url = (
            base_url
            or os.getenv("COMFYUI_URL", "").strip()
            or "http://localhost:8000"
        ).rstrip("/")
        self.checkpoint = checkpoint or os.getenv(
            "COMFYUI_AUDIO_CHECKPOINT", DEFAULT_CHECKPOINT
        )
        self.text_encoder = text_encoder or os.getenv(
            "COMFYUI_AUDIO_TEXT_ENCODER", DEFAULT_TEXT_ENCODER
        )
        self.client = httpx.Client(timeout=30.0)
        self._verify_checkpoint()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.close()

    def close(self):
        self.client.close()

    # ------------------------------------------------------------------
    # Setup / validation
    # ------------------------------------------------------------------

    def _verify_checkpoint(self):
        """Fail fast if the audio checkpoint isn't installed."""
        try:
            resp = self.client.get(
                f"{self.base_url}/object_info/CheckpointLoaderSimple"
            )
            resp.raise_for_status()
            data = resp.json()
            available = (
                data.get("CheckpointLoaderSimple", {})
                    .get("input", {})
                    .get("required", {})
                    .get("ckpt_name", [[]])[0]
            )
            if self.checkpoint not in available:
                candidates = [c for c in available if "audio" in c.lower()]
                raise ComfyUIAudioError(
                    f"Audio checkpoint '{self.checkpoint}' not found in ComfyUI. "
                    f"Available audio checkpoints: {candidates}",
                )
        except httpx.HTTPError as e:
            raise ComfyUIAudioError(
                f"Cannot reach ComfyUI at {self.base_url}: {e}. "
                f"Start ComfyUI or set COMFYUI_URL in .env."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_sound_effect(
        self,
        prompt: str,
        duration_sec: float = 5.0,
        output_path: Optional[str] = None,
        negative_prompt: str = DEFAULT_NEGATIVE,
        steps: int = DEFAULT_STEPS,
        cfg: float = DEFAULT_CFG,
        seed: Optional[int] = None,
        foley_framing: bool = True,
    ) -> dict:
        """Generate one SFX clip. Returns dict mirroring
        ElevenLabsClient.generate_sound_effect for drop-in compatibility
        (path, size_bytes, duration_sec, cost_usd=0.0).

        ``foley_framing`` prefixes the positive prompt with a
        foley-recording cue so Stable Audio Open doesn't slip into its
        music-training mode. Disable only when the caller has already
        crafted a heavily-framed prompt.
        """
        if not prompt or not prompt.strip():
            raise ValueError("prompt is required")

        duration_sec = max(MIN_DURATION_SEC, min(MAX_DURATION_SEC, float(duration_sec)))
        if seed is None:
            seed = random.randint(1, 2**31 - 1)

        framed_prompt = (FOLEY_PREFIX + prompt.strip()) if foley_framing else prompt

        prefix = f"babylon_sfx_{uuid.uuid4().hex[:10]}"
        workflow = self._build_workflow(
            prompt=framed_prompt,
            negative_prompt=negative_prompt,
            duration_sec=duration_sec,
            steps=steps,
            cfg=cfg,
            seed=seed,
            filename_prefix=prefix,
        )

        prompt_id = self._queue_prompt(workflow)
        history = self._poll_completion(prompt_id)
        audio_files = self._collect_outputs(history)
        if not audio_files:
            raise ComfyUIAudioError(
                "ComfyUI completed but produced no audio output.",
                details={"history": history},
            )

        filename, blob = audio_files[0]
        result = {
            "prompt": prompt,
            "duration_sec": duration_sec,
            "size_bytes": len(blob),
            "cost_usd": 0.0,
            "provider": "comfyui",
            "model": self.checkpoint,
            "seed": seed,
            "steps": steps,
        }
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as f:
                f.write(blob)
            result["path"] = str(out)
        return result

    # ------------------------------------------------------------------
    # Workflow graph
    # ------------------------------------------------------------------

    def _build_workflow(
        self,
        prompt: str,
        negative_prompt: str,
        duration_sec: float,
        steps: int,
        cfg: float,
        seed: int,
        filename_prefix: str,
    ) -> dict:
        return {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": self.checkpoint},
                "_meta": {"title": "Load Stable Audio Open"},
            },
            "9": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": self.text_encoder,
                    "type": "stable_audio",
                },
                "_meta": {"title": "Load T5 text encoder"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["9", 0]},
                "_meta": {"title": "Positive prompt"},
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["9", 0]},
                "_meta": {"title": "Negative prompt"},
            },
            "4": {
                "class_type": "ConditioningStableAudio",
                "inputs": {
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "seconds_start": 0.0,
                    "seconds_total": float(duration_sec),
                },
                "_meta": {"title": "Stable Audio conditioning"},
            },
            "5": {
                "class_type": "EmptyLatentAudio",
                "inputs": {
                    "seconds": float(duration_sec),
                    "batch_size": 1,
                },
                "_meta": {"title": "Empty latent"},
            },
            "6": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["1", 0],
                    "seed": int(seed),
                    "steps": int(steps),
                    "cfg": float(cfg),
                    "sampler_name": DEFAULT_SAMPLER,
                    "scheduler": DEFAULT_SCHEDULER,
                    "positive": ["4", 0],
                    "negative": ["4", 1],
                    "latent_image": ["5", 0],
                    "denoise": 1.0,
                },
                "_meta": {"title": "KSampler"},
            },
            "7": {
                "class_type": "VAEDecodeAudio",
                "inputs": {"samples": ["6", 0], "vae": ["1", 2]},
                "_meta": {"title": "VAE Decode Audio"},
            },
            "8": {
                "class_type": "SaveAudioMP3",
                "inputs": {
                    "audio": ["7", 0],
                    "filename_prefix": filename_prefix,
                    "quality": "V0",
                },
                "_meta": {"title": "Save MP3"},
            },
        }

    # ------------------------------------------------------------------
    # Execution / retrieval
    # ------------------------------------------------------------------

    def _queue_prompt(self, workflow: dict) -> str:
        client_id = str(uuid.uuid4())
        try:
            resp = self.client.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
            )
            data = resp.json()
            if "error" in data:
                raise ComfyUIAudioError(
                    f"ComfyUI rejected workflow: {data['error']}",
                    details=data.get("node_errors", {}),
                )
            return data["prompt_id"]
        except httpx.HTTPError as e:
            raise ComfyUIAudioError(f"Failed to queue prompt: {e}")

    def _poll_completion(self, prompt_id: str) -> dict:
        start = time.time()
        while time.time() - start < TIMEOUT:
            try:
                resp = self.client.get(f"{self.base_url}/history/{prompt_id}")
                history = resp.json()
                if prompt_id in history:
                    entry = history[prompt_id]
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        raise ComfyUIAudioError(
                            self._extract_error(status),
                            details=status,
                        )
                    return entry
            except ComfyUIAudioError:
                raise
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
        raise ComfyUIAudioError(
            f"Timed out waiting for ComfyUI after {TIMEOUT}s"
        )

    @staticmethod
    def _extract_error(status: dict) -> str:
        for msg in status.get("messages", []):
            if isinstance(msg, (list, tuple)) and len(msg) >= 2 and msg[0] == "execution_error":
                data = msg[1] if isinstance(msg[1], dict) else {}
                node = data.get("node_type", "?")
                exc = (data.get("exception_message") or "").strip()
                return f"{node} failed: {exc}"
        return f"Execution failed: {status.get('messages')}"

    def _collect_outputs(self, history_entry: dict) -> list:
        """Download audio files from the completed prompt. The SaveAudio*
        family stores results under the ``audio`` key with the usual
        {filename, subfolder, type} shape that /view serves."""
        outputs = history_entry.get("outputs", {})
        files = []
        for _node_id, node_output in outputs.items():
            for key in ("audio", "audios"):
                for info in node_output.get(key, []) or []:
                    name = info.get("filename")
                    if not name:
                        continue
                    resp = self.client.get(
                        f"{self.base_url}/view",
                        params={
                            "filename": name,
                            "subfolder": info.get("subfolder", ""),
                            "type": info.get("type", "output"),
                        },
                    )
                    resp.raise_for_status()
                    files.append((name, resp.content))
        return files
