"""
apis/comfyui_video.py
Local talking-head preview video via Wan 2.1 + InfiniTalk.

Takes a still storyboard image and one or two dialogue audio slices,
runs ComfyUI's WanInfiniteTalkToVideo workflow, and saves an mp4 that
we can layer into the Editing Room's per-shot preview / Play Cut.

The workflow graph is built programmatically rather than templated
from the user's exported JSON because we need to switch between
single_speaker and two_speakers node shapes on the fly. The user's
exported workflow lives at workflows/wan_infinitetalk_reference.json
as a reference for the intended configuration (LightX2V 4-step LoRA,
umt5 clip, Wan2.1 VAE, wav2vec2 encoder, etc.).

Inputs we rewrite per shot:
  - storyboard.png  -> uploaded to ComfyUI input/ as a one-off name
  - audio slice mp3 -> sliced via ffmpeg, uploaded alongside
  - prompt text     -> caller-provided (usually shot label + mood)
  - width / height  -> derived from storyboard aspect
  - length          -> ceil(duration * fps / 4) * 4 + 1
  - speaker_count   -> 1 or 2
"""

from __future__ import annotations

import math
import os
import random
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx


# Wan 2.1 InfiniTalk asset names. Matched to the user's reference
# workflow; override via env vars if you swap models.
WAN_UNET = os.getenv(
    "WAN_UNET",
    "Wan2_1-I2V-14B-480p_fp8_e4m3fn_scaled_KJ.safetensors",
)
WAN_VAE = os.getenv("WAN_VAE", "Wan2_1_VAE_bf16.safetensors")
WAN_CLIP = os.getenv("WAN_CLIP", "umt5_xxl_fp8_e4m3fn_scaled.safetensors")
WAN_LIGHTX2V_LORA = os.getenv(
    "WAN_LIGHTX2V_LORA",
    "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
)
INFINITETALK_PATCH = os.getenv(
    "INFINITETALK_PATCH",
    "wan2.1_infiniteTalk_multi_fp16.safetensors",
)
WAV2VEC_AUDIO_ENCODER = os.getenv(
    "WAV2VEC_AUDIO_ENCODER",
    "wav2vec2-chinese-base_fp16.safetensors",
)

FPS = 25
POLL_INTERVAL = 1.5
TIMEOUT = 1200.0  # up to 20 minutes per clip — long shots can be slow

DEFAULT_NEGATIVE = (
    "bad quality, blurry, static, still image, deformed face, "
    "extra limbs, watermark, text overlay"
)

DEFAULT_PROMPT = "A character speaks naturally on camera, lips synced to the dialogue."


class ComfyUIVideoError(Exception):
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.details = details or {}


class ComfyUIVideoClient:
    """WanInfiniteTalkToVideo client. Free per-clip, runs on local GPU."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (
            base_url
            or os.getenv("COMFYUI_URL", "").strip()
            or "http://localhost:8000"
        ).rstrip("/")
        self.client = httpx.Client(timeout=30.0)
        self._verify_assets()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.client.close()

    def close(self):
        self.client.close()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _verify_assets(self):
        """Fail fast if any required model is missing from ComfyUI."""
        try:
            resp = self.client.get(f"{self.base_url}/object_info")
            resp.raise_for_status()
            info = resp.json()
        except httpx.HTTPError as e:
            raise ComfyUIVideoError(
                f"Cannot reach ComfyUI at {self.base_url}: {e}"
            )

        def _opts(node: str, key: str) -> list:
            """ComfyUI exposes option lists two ways depending on node
            vintage: the legacy shape ``[["a","b"], {}]`` and the newer
            COMBO shape ``["COMBO", {"options": ["a","b"]}]``. Handle
            both so we don't falsely fail on newer nodes."""
            raw = (
                info.get(node, {}).get("input", {})
                .get("required", {}).get(key)
            )
            if not raw:
                return []
            if isinstance(raw[0], list):
                return raw[0]
            if isinstance(raw[0], str) and raw[0].upper() == "COMBO":
                return (raw[1] or {}).get("options", [])
            return []

        checks = [
            ("UNet",      WAN_UNET,               _opts("UNETLoader", "unet_name")),
            ("VAE",       WAN_VAE,                _opts("VAELoader", "vae_name")),
            ("CLIP",      WAN_CLIP,               _opts("CLIPLoader", "clip_name")),
            ("LoRA",      WAN_LIGHTX2V_LORA,      _opts("LoraLoaderModelOnly", "lora_name")),
            ("Patch",     INFINITETALK_PATCH,     _opts("ModelPatchLoader", "name")),
            ("AudioEnc",  WAV2VEC_AUDIO_ENCODER,  _opts("AudioEncoderLoader", "audio_encoder_name")),
        ]
        missing = [label for label, name, avail in checks if name not in avail]
        if missing:
            raise ComfyUIVideoError(
                f"Missing ComfyUI assets: {missing}. "
                f"Override with env vars WAN_UNET / WAN_VAE / WAN_CLIP / "
                f"WAN_LIGHTX2V_LORA / INFINITETALK_PATCH / "
                f"WAV2VEC_AUDIO_ENCODER."
            )

    # ------------------------------------------------------------------
    # Input staging
    # ------------------------------------------------------------------

    def _slice_audio(self, source_mp3: Path, start_sec: float,
                     end_sec: float, out_path: Path) -> Path:
        """Slice ``source_mp3`` from start_sec to end_sec into out_path.

        Re-encodes to a CBR mp3 for the audio encoder's benefit — stream
        copy sometimes loses a frame at the start if the boundary lands
        mid-frame.
        """
        duration = max(0.05, end_sec - start_sec)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-loglevel", "error", "-y",
            "-ss", f"{start_sec:.3f}",
            "-i", str(source_mp3),
            "-t", f"{duration:.3f}",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "128k",
            str(out_path),
        ]
        subprocess.run(cmd, check=True)
        return out_path

    def _upload(self, src: Path, dest_name: str) -> str:
        """Upload a file to ComfyUI's input/ directory. /upload/image
        happily accepts any file type despite the name; ComfyUI serves
        it back through LoadImage / LoadAudio as long as the filename
        passed into those nodes matches."""
        with open(src, "rb") as f:
            resp = self.client.post(
                f"{self.base_url}/upload/image",
                files={"image": (dest_name, f, "application/octet-stream")},
                data={"type": "input", "overwrite": "true"},
            )
        resp.raise_for_status()
        return resp.json()["name"]

    # ------------------------------------------------------------------
    # Workflow graph
    # ------------------------------------------------------------------

    @staticmethod
    def _frames_for_duration(duration_sec: float, fps: int = FPS) -> int:
        """Wan expects ``length`` in the shape 4n+1 at the native fps.
        Round up so we always cover the audio."""
        raw = max(5, int(math.ceil(duration_sec * fps)))
        # snap to next 4k+1
        return ((raw - 1) // 4 + 1) * 4 + 1

    def _build_workflow(
        self,
        *,
        image_filename: str,
        audio1_filename: str,
        audio2_filename: Optional[str],
        width: int,
        height: int,
        frames: int,
        prompt: str,
        negative_prompt: str,
        seed: int,
        output_prefix: str,
    ) -> dict:
        """Return a ComfyUI API-format workflow graph.

        Two-speaker graphs add a second LoadAudio + AudioEncoderEncode
        plus an AudioConcat so the final mp4 carries both voices in
        sequence. Single-speaker graphs drop those nodes entirely.
        """
        two = audio2_filename is not None
        mode = "two_speakers" if two else "single_speaker"

        wf: dict = {
            "13": {"class_type": "UNETLoader",
                   "inputs": {"unet_name": WAN_UNET,
                              "weight_dtype": "default"}},
            "16": {"class_type": "CLIPLoader",
                   "inputs": {"clip_name": WAN_CLIP,
                              "type": "wan",
                              "device": "default"}},
            "14": {"class_type": "CLIPTextEncode",
                   "inputs": {"text": prompt, "clip": ["16", 0]}},
            "15": {"class_type": "CLIPTextEncode",
                   "inputs": {"text": negative_prompt, "clip": ["16", 0]}},
            "17": {"class_type": "ConditioningZeroOut",
                   "inputs": {"conditioning": ["15", 0]}},
            "29": {"class_type": "VAELoader",
                   "inputs": {"vae_name": WAN_VAE}},
            "32": {"class_type": "LoadImage",
                   "inputs": {"image": image_filename}},
            "33": {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"lora_name": WAN_LIGHTX2V_LORA,
                              "strength_model": 1.0,
                              "model": ["13", 0]}},
            "112": {"class_type": "ModelPatchLoader",
                    "inputs": {"name": INFINITETALK_PATCH}},
            "24": {"class_type": "LoadAudio",
                   "inputs": {"audio": audio1_filename}},
            "26": {"class_type": "AudioEncoderLoader",
                   "inputs": {"audio_encoder_name": WAV2VEC_AUDIO_ENCODER}},
            "25": {"class_type": "AudioEncoderEncode",
                   "inputs": {"audio_encoder": ["26", 0],
                              "audio": ["24", 0]}},
            "149": {"class_type": "PrimitiveInt", "inputs": {"value": width}},
            "150": {"class_type": "PrimitiveInt", "inputs": {"value": height}},
        }

        # Talking node — inputs differ by mode.
        talk_inputs = {
            "mode": mode,
            "width": ["149", 0],
            "height": ["150", 0],
            "length": frames,
            "motion_frame_count": 9,
            "audio_scale": 1.0,
            "model": ["33", 0],
            "model_patch": ["112", 0],
            "positive": ["14", 0],
            "negative": ["17", 0],
            "vae": ["29", 0],
            "audio_encoder_output_1": ["25", 0],
            "start_image": ["32", 0],
        }

        if two:
            wf["90"] = {"class_type": "LoadAudio",
                        "inputs": {"audio": audio2_filename}}
            wf["93"] = {"class_type": "AudioEncoderEncode",
                        "inputs": {"audio_encoder": ["26", 0],
                                   "audio": ["90", 0]}}
            wf["113"] = {"class_type": "AudioConcat",
                         "inputs": {"direction": "after",
                                    "audio1": ["24", 0],
                                    "audio2": ["90", 0]}}
            # Two-speaker mode adds optional 2nd audio + face masks.
            # ComfyUI's dynamic-combo format nests these under a
            # ``mode.`` prefix in the api JSON.
            talk_inputs["mode.audio_encoder_output_2"] = ["93", 0]
            talk_inputs["mode.mask_1"] = ["32", 1]
            talk_inputs["mode.mask_2"] = ["32", 1]

        wf["129"] = {"class_type": "WanInfiniteTalkToVideo",
                     "inputs": talk_inputs}

        # Sampling. The reference workflow uses SamplerCustomAdvanced
        # with BasicScheduler + CFGGuider + RandomNoise + KSamplerSelect.
        wf["200"] = {"class_type": "KSamplerSelect",
                     "inputs": {"sampler_name": "euler"}}
        wf["201"] = {"class_type": "BasicScheduler",
                     "inputs": {"scheduler": "normal", "steps": 4,
                                "denoise": 1.0, "model": ["129", 0]}}
        wf["202"] = {"class_type": "CFGGuider",
                     "inputs": {"cfg": 1.0, "model": ["129", 0],
                                "positive": ["129", 1],
                                "negative": ["129", 2]}}
        wf["203"] = {"class_type": "RandomNoise",
                     "inputs": {"noise_seed": int(seed)}}
        wf["204"] = {"class_type": "SamplerCustomAdvanced",
                     "inputs": {"noise": ["203", 0], "guider": ["202", 0],
                                "sampler": ["200", 0], "sigmas": ["201", 0],
                                "latent_image": ["129", 3]}}
        wf["205"] = {"class_type": "VAEDecode",
                     "inputs": {"samples": ["204", 0], "vae": ["29", 0]}}

        # Final audio track: if two-speaker, concat voices so the video
        # sync makes sense. Otherwise feed the single audio.
        final_audio_ref = ["113", 0] if two else ["24", 0]
        wf["206"] = {"class_type": "CreateVideo",
                     "inputs": {"fps": FPS,
                                "images": ["205", 0],
                                "audio": final_audio_ref}}
        wf["207"] = {"class_type": "SaveVideo",
                     "inputs": {"filename_prefix": output_prefix,
                                "format": "auto", "codec": "auto",
                                "video": ["206", 0]}}
        return wf

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _queue(self, workflow: dict) -> str:
        client_id = str(uuid.uuid4())
        try:
            resp = self.client.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
            )
            data = resp.json()
            if "error" in data:
                raise ComfyUIVideoError(
                    f"ComfyUI rejected workflow: {data['error']}",
                    details=data.get("node_errors", {}),
                )
            return data["prompt_id"]
        except httpx.HTTPError as e:
            raise ComfyUIVideoError(f"Failed to queue prompt: {e}")

    def _poll(self, prompt_id: str) -> dict:
        start = time.time()
        while time.time() - start < TIMEOUT:
            try:
                resp = self.client.get(f"{self.base_url}/history/{prompt_id}")
                history = resp.json()
                if prompt_id in history:
                    entry = history[prompt_id]
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        raise ComfyUIVideoError(
                            self._extract_error(status),
                            details=status,
                        )
                    return entry
            except ComfyUIVideoError:
                raise
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
        raise ComfyUIVideoError(
            f"Timed out waiting for ComfyUI after {TIMEOUT}s"
        )

    @staticmethod
    def _extract_error(status: dict) -> str:
        for msg in status.get("messages", []):
            if (isinstance(msg, (list, tuple)) and len(msg) >= 2
                    and msg[0] == "execution_error"):
                d = msg[1] if isinstance(msg[1], dict) else {}
                return (
                    f"{d.get('node_type', '?')} failed: "
                    f"{(d.get('exception_message') or '').strip()}"
                )
        return f"Execution failed: {status.get('messages')}"

    def _collect_videos(self, history_entry: dict) -> list[tuple[str, bytes]]:
        outputs = history_entry.get("outputs", {})
        files = []
        for _node_id, node_out in outputs.items():
            for key in ("videos", "video", "gifs", "images"):
                for info in node_out.get(key, []) or []:
                    name = info.get("filename")
                    if not name:
                        continue
                    if not name.lower().endswith((".mp4", ".webm", ".mov", ".gif")):
                        continue
                    r = self.client.get(
                        f"{self.base_url}/view",
                        params={
                            "filename": name,
                            "subfolder": info.get("subfolder", ""),
                            "type": info.get("type", "output"),
                        },
                    )
                    r.raise_for_status()
                    files.append((name, r.content))
        return files

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _build_silent_workflow(
        self,
        *,
        image_filename: str,
        width: int,
        height: int,
        frames: int,
        prompt: str,
        negative_prompt: str,
        seed: int,
        output_prefix: str,
    ) -> dict:
        """Workflow graph for a silent shot — same Wan 2.1 base + LoRA
        but no InfiniTalk patch and no audio path. Uses plain
        ``WanImageToVideo`` which animates a still with subtle motion
        consistent with the prompt."""
        wf: dict = {
            "13": {"class_type": "UNETLoader",
                   "inputs": {"unet_name": WAN_UNET,
                              "weight_dtype": "default"}},
            "16": {"class_type": "CLIPLoader",
                   "inputs": {"clip_name": WAN_CLIP,
                              "type": "wan",
                              "device": "default"}},
            "14": {"class_type": "CLIPTextEncode",
                   "inputs": {"text": prompt, "clip": ["16", 0]}},
            "15": {"class_type": "CLIPTextEncode",
                   "inputs": {"text": negative_prompt, "clip": ["16", 0]}},
            "17": {"class_type": "ConditioningZeroOut",
                   "inputs": {"conditioning": ["15", 0]}},
            "29": {"class_type": "VAELoader",
                   "inputs": {"vae_name": WAN_VAE}},
            "32": {"class_type": "LoadImage",
                   "inputs": {"image": image_filename}},
            "33": {"class_type": "LoraLoaderModelOnly",
                   "inputs": {"lora_name": WAN_LIGHTX2V_LORA,
                              "strength_model": 1.0,
                              "model": ["13", 0]}},
            "149": {"class_type": "PrimitiveInt", "inputs": {"value": width}},
            "150": {"class_type": "PrimitiveInt", "inputs": {"value": height}},
            # Plain I2V — no audio conditioning.
            "129": {"class_type": "WanImageToVideo",
                    "inputs": {
                        "positive": ["14", 0],
                        "negative": ["17", 0],
                        "vae": ["29", 0],
                        "width": ["149", 0],
                        "height": ["150", 0],
                        "length": frames,
                        "batch_size": 1,
                        "start_image": ["32", 0],
                    }},
            "200": {"class_type": "KSamplerSelect",
                    "inputs": {"sampler_name": "euler"}},
            "201": {"class_type": "BasicScheduler",
                    "inputs": {"scheduler": "normal", "steps": 4,
                               "denoise": 1.0, "model": ["33", 0]}},
            "202": {"class_type": "CFGGuider",
                    "inputs": {"cfg": 1.0, "model": ["33", 0],
                               "positive": ["129", 0],
                               "negative": ["129", 1]}},
            "203": {"class_type": "RandomNoise",
                    "inputs": {"noise_seed": int(seed)}},
            "204": {"class_type": "SamplerCustomAdvanced",
                    "inputs": {"noise": ["203", 0], "guider": ["202", 0],
                               "sampler": ["200", 0], "sigmas": ["201", 0],
                               "latent_image": ["129", 2]}},
            "205": {"class_type": "VAEDecode",
                    "inputs": {"samples": ["204", 0], "vae": ["29", 0]}},
            "206": {"class_type": "CreateVideo",
                    "inputs": {"fps": FPS, "images": ["205", 0]}},
            "207": {"class_type": "SaveVideo",
                    "inputs": {"filename_prefix": output_prefix,
                               "format": "auto", "codec": "auto",
                               "video": ["206", 0]}},
        }
        return wf

    def generate_silent_video(
        self,
        *,
        shot_id: str,
        storyboard_path: Path | str,
        duration_sec: float,
        width: int,
        height: int,
        prompt: str = DEFAULT_PROMPT,
        negative_prompt: str = DEFAULT_NEGATIVE,
        output_path: Optional[Path | str] = None,
        seed: Optional[int] = None,
    ) -> dict:
        """Render a silent shot — no audio, subtle motion from Wan I2V.

        Used for action/reaction shots with no dialogue (establishing
        views, scenery, character reactions). Same Wan 2.1 base as the
        talking path so the aesthetic matches when the cut stitches
        silent + talking shots together.
        """
        storyboard = Path(storyboard_path)
        if not storyboard.exists():
            raise ComfyUIVideoError(f"Storyboard missing: {storyboard}")

        if seed is None:
            seed = random.randint(1, 2**31 - 1)

        img_name = f"babylon_{shot_id}_silent_{uuid.uuid4().hex[:6]}{storyboard.suffix}"
        self._upload(storyboard, img_name)

        frames = self._frames_for_duration(max(1.0, duration_sec), FPS)
        output_prefix = f"babylon/{shot_id}_silent_{uuid.uuid4().hex[:6]}"

        workflow = self._build_silent_workflow(
            image_filename=img_name,
            width=width,
            height=height,
            frames=frames,
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            output_prefix=output_prefix,
        )

        t0 = time.time()
        prompt_id = self._queue(workflow)
        history = self._poll(prompt_id)
        wall = time.time() - t0

        videos = self._collect_videos(history)
        if not videos:
            raise ComfyUIVideoError(
                "ComfyUI returned no video output",
                details={"history": history},
            )
        filename, blob = videos[0]

        result = {
            "shot_id": shot_id,
            "filename": filename,
            "bytes": len(blob),
            "duration_sec": float(duration_sec),
            "frames": frames,
            "speakers": 0,
            "seed": seed,
            "wall_sec": round(wall, 1),
            "cost_usd": 0.0,
            "silent": True,
        }
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as f:
                f.write(blob)
            result["path"] = str(out)
        return result

    def generate_preview_video(
        self,
        *,
        shot_id: str,
        storyboard_path: Path | str,
        speakers: list[dict],
        width: int,
        height: int,
        prompt: str = DEFAULT_PROMPT,
        negative_prompt: str = DEFAULT_NEGATIVE,
        output_path: Optional[Path | str] = None,
        seed: Optional[int] = None,
        scratch_dir: Optional[Path | str] = None,
    ) -> dict:
        """Render a talking-head video for one shot.

        ``speakers`` is 1 or 2 dicts, each with keys:
            character_id    - str, used in the uploaded filename
            audio_source    - absolute path to the recording's mp3
            start_time_sec  - float, slice start within the recording
            end_time_sec    - float, slice end within the recording

        For a single-speaker shot, pass a list of length 1. For a
        two-shot with both characters speaking (e.g. greeting exchange),
        pass both in dialogue order — the audio will be concatenated
        and their slice durations summed for ``length``.
        """
        if not (1 <= len(speakers) <= 2):
            raise ValueError("speakers must have 1 or 2 entries")
        storyboard = Path(storyboard_path)
        if not storyboard.exists():
            raise ComfyUIVideoError(f"Storyboard missing: {storyboard}")

        scratch = Path(scratch_dir) if scratch_dir else Path(os.getenv("TEMP", "."))
        scratch.mkdir(parents=True, exist_ok=True)

        # Slice + upload each speaker's audio
        uploaded_audios: list[str] = []
        total_duration = 0.0
        for i, sp in enumerate(speakers):
            src = Path(sp["audio_source"])
            if not src.exists():
                raise ComfyUIVideoError(f"Audio source missing: {src}")
            slice_name = f"babylon_{shot_id}_spk{i+1}_{uuid.uuid4().hex[:6]}.mp3"
            slice_path = scratch / slice_name
            start = float(sp["start_time_sec"])
            end = float(sp["end_time_sec"])
            self._slice_audio(src, start, end, slice_path)
            self._upload(slice_path, slice_name)
            uploaded_audios.append(slice_name)
            total_duration += max(0.05, end - start)

        # Upload the storyboard with a shot-unique name so caches don't
        # collide across shots.
        img_name = f"babylon_{shot_id}_{uuid.uuid4().hex[:6]}{storyboard.suffix}"
        self._upload(storyboard, img_name)

        frames = self._frames_for_duration(total_duration, FPS)
        if seed is None:
            seed = random.randint(1, 2**31 - 1)
        output_prefix = f"babylon/{shot_id}_{uuid.uuid4().hex[:6]}"

        workflow = self._build_workflow(
            image_filename=img_name,
            audio1_filename=uploaded_audios[0],
            audio2_filename=uploaded_audios[1] if len(uploaded_audios) == 2 else None,
            width=width,
            height=height,
            frames=frames,
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            output_prefix=output_prefix,
        )

        t0 = time.time()
        prompt_id = self._queue(workflow)
        history = self._poll(prompt_id)
        wall = time.time() - t0

        videos = self._collect_videos(history)
        if not videos:
            raise ComfyUIVideoError(
                "ComfyUI returned no video output",
                details={"history": history},
            )
        filename, blob = videos[0]

        result = {
            "shot_id": shot_id,
            "filename": filename,
            "bytes": len(blob),
            "duration_sec": total_duration,
            "frames": frames,
            "speakers": len(speakers),
            "seed": seed,
            "wall_sec": round(wall, 1),
            "cost_usd": 0.0,
        }
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as f:
                f.write(blob)
            result["path"] = str(out)
        return result
