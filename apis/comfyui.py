"""
apis/comfyui.py
ComfyUI local client for storyboard image generation.

Connects to a locally running ComfyUI instance via its REST API.
Generates images using SDXL checkpoints with zero API cost.

Supports:
  - txt2img: Basic prompt-to-image storyboard generation
  - img2img: Style-guided generation using VAEEncode (denoise-based)
  - IPAdapter: Style transfer using IPAdapter Plus (if installed)

Falls back gracefully:
  IPAdapter (best) → img2img (basic) → txt2img (no style guide)

SDXL resolutions (must be divisible by 8, ~1 megapixel total):
  16:9 → 1344×768    9:16 → 768×1344    1:1 → 1024×1024
"""

import os
import json
import time
import random
import shutil
from pathlib import Path
from typing import Optional

import httpx

# Audio model patterns to skip when auto-detecting SDXL checkpoints
_AUDIO_MODEL_PATTERNS = [
    "ace_step", "stable-audio", "audio", "musicgen", "bark",
]

# Preferred SDXL checkpoint name patterns (searched in order)
_SDXL_CHECKPOINT_PREFERENCES = [
    "juggernaut",
    "dreamshaper",
    "protovision",
    "realvis",
    "copax",
    "animagine",
    "sdxl",
    "sd_xl",
]

# SDXL-native resolutions by aspect ratio
SDXL_RESOLUTIONS = {
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "1:1":  (1024, 1024),
}

# Default sampler settings for SDXL
SDXL_DEFAULTS = {
    "steps": 25,
    "cfg": 7.0,
    "sampler_name": "euler",
    "scheduler": "normal",
}


class ComfyUIError(Exception):
    """Raised when ComfyUI returns an error or is unreachable."""
    def __init__(self, message: str, details: dict = None):
        self.message = message
        self.details = details or {}
        super().__init__(f"[comfyui] {message}")


class ComfyUIClient:
    """
    Local ComfyUI client for storyboard image generation.
    Drop-in replacement for StabilityClient / GoogleImagenClient.

    No API key required — talks to localhost.
    Cost per image: $0.00 (local GPU).
    """

    API_NAME = "comfyui"
    COST_PER_IMAGE = 0.0  # Free! Local generation.
    DEFAULT_PORTS = [8000, 8001, 8002]
    POLL_INTERVAL = 2.0   # seconds between completion polls
    TIMEOUT = 300          # max seconds to wait per image

    def __init__(self, base_url: str = None, checkpoint: str = None):
        self.base_url = (
            base_url
            or os.getenv("COMFYUI_URL", "").strip()
            or self._find_comfyui_url()
        )
        self.client = httpx.Client(timeout=30.0)
        self._verify_connection()

        self.checkpoint = checkpoint or self._detect_checkpoint()
        self._has_ipadapter = self._check_ipadapter()

        if self._has_ipadapter:
            print(f"  ComfyUI: IPAdapter available (style-guided generation enabled)")
        print(f"  ComfyUI: Using checkpoint '{self.checkpoint}'")

    # ------------------------------------------------------------------
    # Connection & setup
    # ------------------------------------------------------------------

    def _verify_connection(self):
        """Ping ComfyUI to ensure it's running."""
        try:
            resp = self.client.get(f"{self.base_url}/system_stats")
            resp.raise_for_status()
            stats = resp.json()
            device = stats.get("devices", [{}])[0]
            gpu_name = device.get("name", "unknown").split(" : ")[0]
            vram_gb = device.get("vram_total", 0) / (1024**3)
            print(f"  ComfyUI: Connected to {self.base_url}")
            print(f"  ComfyUI: GPU = {gpu_name} ({vram_gb:.0f} GB VRAM)")
        except Exception as e:
            raise ComfyUIError(
                f"Cannot connect to ComfyUI at {self.base_url}. "
                f"Is ComfyUI running? Error: {e}"
            )

    def _detect_checkpoint(self) -> str:
        """
        Auto-detect the best SDXL checkpoint from available models.
        Skips audio models, prefers known SDXL checkpoints.
        """
        try:
            resp = self.client.get(
                f"{self.base_url}/object_info/CheckpointLoaderSimple"
            )
            data = resp.json()
            all_ckpts = (
                data.get("CheckpointLoaderSimple", {})
                    .get("input", {})
                    .get("required", {})
                    .get("ckpt_name", [[]])[0]
            )
        except Exception:
            raise ComfyUIError(
                "Cannot query checkpoints from ComfyUI. "
                "Ensure at least one SDXL checkpoint is installed."
            )

        # Filter out audio models
        image_ckpts = []
        for name in all_ckpts:
            name_lower = name.lower()
            if any(pat in name_lower for pat in _AUDIO_MODEL_PATTERNS):
                continue
            image_ckpts.append(name)

        if not image_ckpts:
            raise ComfyUIError(
                "No image generation checkpoints found in ComfyUI. "
                "Download an SDXL checkpoint (e.g. Juggernaut XL) and "
                "place it in your ComfyUI checkpoints directory."
            )

        # Prefer known SDXL checkpoints in order
        for pref in _SDXL_CHECKPOINT_PREFERENCES:
            for ckpt in image_ckpts:
                if pref in ckpt.lower():
                    return ckpt

        # Fall back to first non-audio checkpoint
        return image_ckpts[0]

    def _check_ipadapter(self) -> bool:
        """Check if ComfyUI_IPAdapter_plus nodes are available."""
        try:
            resp = self.client.get(
                f"{self.base_url}/object_info/IPAdapterAdvanced"
            )
            data = resp.json()
            return "IPAdapterAdvanced" in data
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Context manager (matches StabilityClient pattern)
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.client.close()

    # ------------------------------------------------------------------
    # Workflow builders
    # ------------------------------------------------------------------

    def _build_txt2img_workflow(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        seed: int,
        prefix: str = "babylon",
    ) -> dict:
        """Build an SDXL txt2img workflow in ComfyUI API format."""
        return {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": self.checkpoint},
                "_meta": {"title": "Load Checkpoint"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["1", 1]},
                "_meta": {"title": "Positive Prompt"},
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["1", 1]},
                "_meta": {"title": "Negative Prompt"},
            },
            "4": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": width,
                    "height": height,
                    "batch_size": 1,
                },
                "_meta": {"title": "Empty Latent"},
            },
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": SDXL_DEFAULTS["steps"],
                    "cfg": SDXL_DEFAULTS["cfg"],
                    "sampler_name": SDXL_DEFAULTS["sampler_name"],
                    "scheduler": SDXL_DEFAULTS["scheduler"],
                    "denoise": 1.0,
                    "model": ["1", 0],
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "latent_image": ["4", 0],
                },
                "_meta": {"title": "KSampler"},
            },
            "6": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
                "_meta": {"title": "VAE Decode"},
            },
            "7": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": prefix,
                    "images": ["6", 0],
                },
                "_meta": {"title": "Save Image"},
            },
        }

    def _build_img2img_workflow(
        self,
        prompt: str,
        negative_prompt: str,
        style_image_name: str,
        width: int,
        height: int,
        seed: int,
        denoise: float = 0.7,
        prefix: str = "babylon_styled",
    ) -> dict:
        """
        Build an SDXL img2img workflow using VAEEncode.
        style_image_name: filename as returned by POST /upload/image.
        denoise: 0.0 = pure copy, 1.0 = ignore input entirely.
        """
        return {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": self.checkpoint},
                "_meta": {"title": "Load Checkpoint"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["1", 1]},
                "_meta": {"title": "Positive Prompt"},
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["1", 1]},
                "_meta": {"title": "Negative Prompt"},
            },
            "10": {
                "class_type": "LoadImage",
                "inputs": {"image": style_image_name},
                "_meta": {"title": "Style Reference"},
            },
            "11": {
                "class_type": "VAEEncode",
                "inputs": {
                    "pixels": ["10", 0],
                    "vae": ["1", 2],
                },
                "_meta": {"title": "VAE Encode Reference"},
            },
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": SDXL_DEFAULTS["steps"],
                    "cfg": SDXL_DEFAULTS["cfg"],
                    "sampler_name": SDXL_DEFAULTS["sampler_name"],
                    "scheduler": SDXL_DEFAULTS["scheduler"],
                    "denoise": denoise,
                    "model": ["1", 0],
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "latent_image": ["11", 0],
                },
                "_meta": {"title": "KSampler"},
            },
            "6": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
                "_meta": {"title": "VAE Decode"},
            },
            "7": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": prefix,
                    "images": ["6", 0],
                },
                "_meta": {"title": "Save Image"},
            },
        }

    def _build_ipadapter_workflow(
        self,
        prompt: str,
        negative_prompt: str,
        style_image_name: str,
        width: int,
        height: int,
        seed: int,
        ipa_weight: float = 0.6,
        prefix: str = "babylon_ipa",
    ) -> dict:
        """
        Build an SDXL IPAdapter style-transfer workflow.
        Requires ComfyUI_IPAdapter_plus custom nodes.
        ipa_weight: 0.0-1.0, how much the style image influences output.
        """
        return {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": self.checkpoint},
                "_meta": {"title": "Load Checkpoint"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["1", 1]},
                "_meta": {"title": "Positive Prompt"},
            },
            "3": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": negative_prompt, "clip": ["1", 1]},
                "_meta": {"title": "Negative Prompt"},
            },
            "10": {
                "class_type": "IPAdapterUnifiedLoader",
                "inputs": {
                    "preset": "PLUS (high strength)",
                    "model": ["1", 0],
                },
                "_meta": {"title": "IPAdapter Loader"},
            },
            "11": {
                "class_type": "LoadImage",
                "inputs": {"image": style_image_name},
                "_meta": {"title": "Style Reference"},
            },
            "12": {
                "class_type": "IPAdapterAdvanced",
                "inputs": {
                    "weight": ipa_weight,
                    "weight_type": "style transfer precise",
                    "combine_embeds": "concat",
                    "embeds_scaling": "V only",
                    "start_at": 0.0,
                    "end_at": 0.5,
                    "model": ["10", 0],
                    "ipadapter": ["10", 1],
                    "image": ["11", 0],
                },
                "_meta": {"title": "IPAdapter Style Transfer"},
            },
            "4": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": width,
                    "height": height,
                    "batch_size": 1,
                },
                "_meta": {"title": "Empty Latent"},
            },
            "5": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": SDXL_DEFAULTS["steps"],
                    "cfg": SDXL_DEFAULTS["cfg"],
                    "sampler_name": SDXL_DEFAULTS["sampler_name"],
                    "scheduler": SDXL_DEFAULTS["scheduler"],
                    "denoise": 1.0,
                    "model": ["12", 0],   # From IPAdapter, not raw checkpoint
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "latent_image": ["4", 0],
                },
                "_meta": {"title": "KSampler"},
            },
            "6": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
                "_meta": {"title": "VAE Decode"},
            },
            "7": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": prefix,
                    "images": ["6", 0],
                },
                "_meta": {"title": "Save Image"},
            },
        }

    # ------------------------------------------------------------------
    # Execution: queue → poll → retrieve
    # ------------------------------------------------------------------

    def _queue_prompt(self, workflow: dict) -> str:
        """Submit a workflow to ComfyUI's queue. Returns prompt_id."""
        import uuid
        client_id = str(uuid.uuid4())
        payload = {"prompt": workflow, "client_id": client_id}
        try:
            resp = self.client.post(
                f"{self.base_url}/prompt", json=payload
            )
            data = resp.json()
            if "error" in data:
                node_errors = data.get("node_errors", {})
                raise ComfyUIError(
                    f"Workflow rejected: {data['error']}",
                    details=node_errors,
                )
            return data["prompt_id"]
        except httpx.HTTPError as e:
            raise ComfyUIError(f"Failed to queue prompt: {e}")

    def _poll_completion(self, prompt_id: str) -> dict:
        """
        Poll /history/{prompt_id} until execution completes.
        Returns the history entry with output info.
        """
        start = time.time()
        while time.time() - start < self.TIMEOUT:
            try:
                resp = self.client.get(
                    f"{self.base_url}/history/{prompt_id}"
                )
                history = resp.json()
                if prompt_id in history:
                    entry = history[prompt_id]
                    # Check for execution errors
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        error_msg = self._extract_error(status)
                        raise ComfyUIError(error_msg)
                    return entry
            except ComfyUIError:
                raise
            except Exception:
                pass  # Connection hiccup during polling, retry
            time.sleep(self.POLL_INTERVAL)

        raise ComfyUIError(
            f"Timed out waiting for ComfyUI after {self.TIMEOUT}s"
        )

    @staticmethod
    def _extract_error(status: dict) -> str:
        """Extract a clean error message from ComfyUI status messages."""
        messages = status.get("messages", [])
        for msg in messages:
            if isinstance(msg, (list, tuple)) and len(msg) >= 2:
                label, data = msg[0], msg[1]
                if label == "execution_error" and isinstance(data, dict):
                    node_type = data.get("node_type", "unknown")
                    exc_msg = data.get("exception_message", "").strip()
                    exc_type = data.get("exception_type", "")

                    # Provide actionable advice for common errors
                    if "paging file" in exc_msg.lower() or "os error 1455" in exc_msg:
                        return (
                            f"{node_type} failed: {exc_msg}\n"
                            f"  Fix: Close other applications to free RAM, or "
                            f"increase Windows page file size:\n"
                            f"  Settings → System → About → Advanced system settings → "
                            f"Performance → Advanced → Virtual Memory → Change"
                        )
                    if "out of memory" in exc_msg.lower():
                        return (
                            f"{node_type} failed: GPU out of memory.\n"
                            f"  Fix: Free VRAM by calling POST /free on ComfyUI, "
                            f"or close other GPU applications."
                        )
                    return f"{node_type} failed ({exc_type}): {exc_msg}"

        # Fallback: dump raw messages
        return f"Execution failed: {messages}"

    def _get_output_images(self, history_entry: dict) -> list:
        """
        Download output images from a completed prompt.
        Returns list of (filename, image_bytes) tuples.
        """
        outputs = history_entry.get("outputs", {})
        images = []
        for node_id, node_output in outputs.items():
            if "images" not in node_output:
                continue
            for img_info in node_output["images"]:
                resp = self.client.get(
                    f"{self.base_url}/view",
                    params={
                        "filename": img_info["filename"],
                        "subfolder": img_info.get("subfolder", ""),
                        "type": img_info.get("type", "output"),
                    },
                )
                resp.raise_for_status()
                images.append((img_info["filename"], resp.content))
        return images

    def _upload_image(self, image_path: str) -> str:
        """
        Upload an image to ComfyUI's input directory.
        Returns the filename as stored by ComfyUI.
        """
        path = Path(image_path)
        if not path.exists():
            raise ComfyUIError(f"Image not found: {image_path}")

        with open(path, "rb") as f:
            resp = self.client.post(
                f"{self.base_url}/upload/image",
                files={"image": (path.name, f, "image/png")},
                data={"type": "input", "overwrite": "true"},
            )
        resp.raise_for_status()
        return resp.json()["name"]

    def _generate(self, workflow: dict, output_path: str) -> dict:
        """
        Execute a workflow and save the first output image.
        Returns metadata dict with path, size, dimensions.
        """
        prompt_id = self._queue_prompt(workflow)
        history = self._poll_completion(prompt_id)
        images = self._get_output_images(history)

        if not images:
            raise ComfyUIError("ComfyUI produced no output images")

        # Take the first output image
        filename, image_bytes = images[0]

        # Write to project output path
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(image_bytes)

        # Extract dimensions from workflow (we set them)
        width = height = 0
        for node in workflow.values():
            if node.get("class_type") == "EmptyLatentImage":
                width = node["inputs"]["width"]
                height = node["inputs"]["height"]
                break
        if width == 0:
            # img2img mode — dimensions come from input image
            # Just use the output file size
            try:
                import struct
                # Quick PNG dimension read from header
                with open(output_path, "rb") as f:
                    f.read(16)
                    w_bytes = f.read(4)
                    h_bytes = f.read(4)
                    width = struct.unpack(">I", w_bytes)[0]
                    height = struct.unpack(">I", h_bytes)[0]
            except Exception:
                width, height = 1344, 768  # Fallback

        # Extract seed from the KSampler node for reproducibility
        used_seed = None
        for node in workflow.values():
            if node.get("class_type") == "KSampler":
                used_seed = node["inputs"].get("seed")
                break

        return {
            "path": str(out),
            "size_bytes": len(image_bytes),
            "width": width,
            "height": height,
            "cost_usd": self.COST_PER_IMAGE,
            "provider": self.API_NAME,
            "model": self.checkpoint,
            "seed": used_seed,
        }

    # ------------------------------------------------------------------
    # Public API: matches StabilityClient interface
    # ------------------------------------------------------------------

    def generate_storyboard(
        self,
        prompt: str,
        output_path: str,
        width: int = 1344,
        height: int = 768,
        negative_prompt: str = None,
        seed: int = None,
    ) -> dict:
        """
        Generate a storyboard image using SDXL txt2img.
        Returns metadata dict with path, size, dimensions, cost, seed.
        """
        from .prompt_builder import build_negative_prompt

        neg = negative_prompt or build_negative_prompt()
        seed = seed if seed is not None else random.randint(0, 2**31)

        # Snap to nearest SDXL-native resolution
        w, h = self._snap_resolution(width, height)

        workflow = self._build_txt2img_workflow(
            prompt=prompt,
            negative_prompt=neg,
            width=w,
            height=h,
            seed=seed,
            prefix=f"babylon_{int(time.time())}",
        )

        return self._generate(workflow, output_path)

    def generate_character_reference(
        self,
        prompt: str,
        output_path: str,
        negative_prompt: str = None,
        seed: int = None,
    ) -> dict:
        """Generate a 1:1 character reference portrait image."""
        return self.generate_storyboard(
            prompt=prompt,
            output_path=output_path,
            width=1024,
            height=1024,
            negative_prompt=negative_prompt,
            seed=seed,
        )

    def generate_with_style_guide(
        self,
        prompt: str,
        style_image_path: str,
        output_path: str,
        fidelity: float = 0.3,
        width: int = 1344,
        height: int = 768,
        negative_prompt: str = None,
        seed: int = None,
    ) -> dict:
        """
        Generate an image using a style reference for character consistency.

        If IPAdapter Plus is installed: uses IPAdapter style transfer
        (generates new composition with style influence from reference).

        Otherwise: falls back to img2img with VAEEncode
        (denoise controls how much the reference is preserved).

        fidelity: 0.0-1.0 controls style image influence.
          - For IPAdapter: maps directly to weight (0.3 → 0.3 weight)
          - For img2img: maps inversely to denoise (0.3 fidelity → 0.7 denoise)
        """
        from .prompt_builder import build_negative_prompt

        neg = negative_prompt or build_negative_prompt()
        seed = seed if seed is not None else random.randint(0, 2**31)
        w, h = self._snap_resolution(width, height)

        # Upload the style reference image
        style_name = self._upload_image(style_image_path)

        if self._has_ipadapter:
            # Best quality: IPAdapter style transfer (precise mode)
            # New composition from text + appearance influence from image
            workflow = self._build_ipadapter_workflow(
                prompt=prompt,
                negative_prompt=neg,
                style_image_name=style_name,
                width=w,
                height=h,
                seed=seed,
                ipa_weight=max(0.15, min(0.5, fidelity)),
                prefix=f"babylon_ipa_{int(time.time())}",
            )
        else:
            # Fallback: img2img — reference composition with text guidance
            denoise = 1.0 - fidelity  # fidelity 0.3 → denoise 0.7
            workflow = self._build_img2img_workflow(
                prompt=prompt,
                negative_prompt=neg,
                style_image_name=style_name,
                width=w,
                height=h,
                seed=seed,
                denoise=denoise,
                prefix=f"babylon_i2i_{int(time.time())}",
            )

        result = self._generate(workflow, output_path)
        result["style_guide_used"] = True
        result["fidelity"] = fidelity
        result["style_method"] = "ipadapter" if self._has_ipadapter else "img2img"
        return result

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
            width=768,
            height=1344,
            seed=seed,
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
        character_loras: dict = None,
        common_seed: int = None,
    ) -> list:
        """
        Generate storyboard images for a list of shot dicts.
        Matches StabilityClient.generate_shot_boards() interface exactly.

        If character_loras is provided (dict of char_id → lora config with
        'file', 'trigger_word', 'weight'), automatically uses LoRA-enhanced
        generation for shots containing those characters.
        """
        character_visuals = character_visuals or {}
        character_loras = character_loras or {}
        self._visual_style = visual_style
        results = []

        print(f"\n  Storyboard generation (ComfyUI local): {len(shots)} shots")
        print(f"  Cost: $0.00 (local GPU)")
        if character_visuals:
            print(f"  Character visual tags loaded: {list(character_visuals.keys())}")
        if character_loras:
            print(f"  Character LoRAs available: {list(character_loras.keys())}")

        # Verify available LoRAs actually exist in ComfyUI
        if character_loras:
            available = self.get_available_loras()
            verified_loras = {}
            for cid, cfg in character_loras.items():
                lora_file = cfg.get("safetensors_name") or cfg.get("file", "")
                # Extract just the filename if it's a path
                if "/" in lora_file or "\\" in lora_file:
                    lora_file = Path(lora_file).name
                if lora_file in available:
                    verified_loras[cid] = {**cfg, "file": lora_file}
                else:
                    print(f"  [WARN] LoRA not found in ComfyUI: {lora_file} "
                          f"(for {cid})")
            character_loras = verified_loras
            if character_loras:
                print(f"  Verified LoRAs: {list(character_loras.keys())}")

        if common_seed is not None:
            print(f"  Common seed: {common_seed}")

        if dry_run:
            print("  DRY RUN -- no generation")
            return [{"shot_id": s["shot_id"], "status": "dry_run"} for s in shots]

        skipped_existing = 0
        lora_shots = 0

        for i, shot in enumerate(shots):
            shot_id = shot["shot_id"]
            chapter_id = shot["chapter_id"]
            storyboard_cfg = shot.get("storyboard", {})
            prompt = storyboard_cfg.get("storyboard_prompt", "")

            if not prompt:
                print(f"  [FAIL] No storyboard_prompt for {shot_id}, skipping")
                results.append({
                    "shot_id": shot_id,
                    "status": "skipped",
                    "reason": "no_prompt",
                })
                continue

            # Build enriched prompt with visual style + character descriptions
            from .prompt_builder import build_storyboard_prompt
            prompt = build_storyboard_prompt(
                prompt, shot, character_visuals, self._visual_style
            )

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

            # Determine which LoRAs apply to this shot
            shot_loras = self._get_shot_loras(shot, character_loras)

            print(f"  [{i+1}/{len(shots)}] {shot_id}")
            if shot_loras:
                lora_names = [cfg["file"] for cfg in shot_loras]
                print(f"    LoRAs: {', '.join(lora_names)}")
                lora_shots += 1
            print(f"    Prompt: {prompt[:120]}...")

            try:
                # Build gender-aware negative prompt for this shot
                from .prompt_builder import gender_negative_terms
                _gneg = gender_negative_terms(shot, character_visuals)
                from .prompt_builder import build_negative_prompt as _bneg
                _neg = _bneg(_gneg)

                # Use LoRA-enhanced generation if any character LoRAs apply
                if shot_loras:
                    result_16_9 = self.generate_storyboard_with_loras(
                        prompt=prompt,
                        output_path=str(output_path),
                        lora_configs=shot_loras,
                        seed=common_seed,
                        negative_prompt=_neg,
                    )
                else:
                    result_16_9 = self.generate_storyboard(
                        prompt=prompt,
                        output_path=str(output_path),
                        seed=common_seed,
                        negative_prompt=_neg,
                    )
                print(f"    [OK] 16:9 storyboard saved ({result_16_9['width']}x{result_16_9['height']})")

                from .prompt_builder import DEFAULT_VISUAL_STYLE as _DEF_STYLE
                result = {
                    "shot_id": shot_id,
                    "status": "generated",
                    "image_16_9": result_16_9["path"],
                    "cost_usd": result_16_9["cost_usd"],
                    "provider": self.API_NAME,
                    "model": self.checkpoint,
                    "final_prompt": prompt,
                    "negative_prompt": _neg,
                    "visual_style": self._visual_style or _DEF_STYLE,
                    "seed": result_16_9.get("seed"),
                }
                if shot_loras:
                    result["loras_used"] = [cfg["file"] for cfg in shot_loras]

                if include_vertical:
                    vertical_path = str(output_path).replace(
                        "storyboard.png", "storyboard_vertical.png"
                    )
                    if (
                        not force
                        and Path(vertical_path).exists()
                        and Path(vertical_path).stat().st_size > 1000
                    ):
                        result["image_9_16"] = vertical_path
                        print(f"    [SKIP] 9:16 already exists")
                    else:
                        if shot_loras:
                            result_9_16 = self.generate_storyboard_with_loras(
                                prompt=prompt,
                                output_path=vertical_path,
                                lora_configs=shot_loras,
                                width=768,
                                height=1344,
                                seed=common_seed,
                                negative_prompt=_neg,
                            )
                        else:
                            result_9_16 = self.generate_storyboard_vertical(
                                prompt=prompt,
                                output_path=vertical_path,
                                seed=common_seed,
                                negative_prompt=_neg,
                            )
                        result["image_9_16"] = result_9_16["path"]
                        result["cost_usd"] += result_9_16["cost_usd"]
                        print(f"    [OK] 9:16 storyboard saved ({result_9_16['width']}x{result_9_16['height']})")

                results.append(result)

            except Exception as e:
                print(f"    [FAIL] Failed: {e}")
                results.append({
                    "shot_id": shot_id,
                    "status": "failed",
                    "error": str(e),
                })

        generated = sum(1 for r in results if r["status"] == "generated")
        total_cost = sum(r.get("cost_usd", 0) for r in results)
        if skipped_existing:
            print(f"\n  Skipped {skipped_existing} shots with existing images")
        if lora_shots:
            print(f"  LoRA-enhanced: {lora_shots}/{generated} shots")
        print(f"  Generated: {generated}/{len(shots)} storyboards, ${total_cost:.2f} spent")
        return results

    def _get_shot_loras(self, shot: dict, character_loras: dict) -> list:
        """
        Determine which LoRA configs apply to a given shot.

        Checks characters_in_frame against available character_loras.
        Adjusts weights based on character count in frame:
          - 1-2 chars: 0.8 weight (default)
          - 3 chars:   0.7 weight
          - 4+ chars:  0.6 weight

        Returns list of lora config dicts with file, weight, trigger_word.
        """
        if not character_loras:
            return []

        # Get character IDs in this shot
        chars_in_frame = shot.get("characters_in_frame", [])
        char_ids = []
        for entry in chars_in_frame:
            if isinstance(entry, dict):
                cid = entry.get("character_id", "").lower()
            elif isinstance(entry, str):
                cid = entry.lower()
            else:
                continue
            if cid:
                char_ids.append(cid)

        # Find matching LoRAs
        matching = []
        for cid in char_ids:
            if cid in character_loras:
                matching.append(character_loras[cid])

        if not matching:
            return []

        # Adjust weights based on number of LoRAs
        count = len(matching)
        if count <= 2:
            weight = 0.8
        elif count == 3:
            weight = 0.7
        else:
            weight = 0.6

        # Apply weight adjustment (respect per-char weight if already set)
        result = []
        for cfg in matching:
            result.append({
                "file": cfg["file"],
                "trigger_word": cfg.get("trigger_word", ""),
                "weight": cfg.get("weight", weight),
            })

        return result

    def generate_character_sheet(
        self,
        prompt: str,
        output_path: str,
        negative_prompt: str = None,
        seed: int = None,
    ) -> dict:
        """
        Generate a single character sheet training image (1024x1024).

        Used by CharacterSheetStage to produce LoRA training data.
        Identical to generate_character_reference but with a specific
        negative prompt optimized for isolated character images.
        """
        from .prompt_builder import build_character_sheet_negative

        neg = negative_prompt or build_character_sheet_negative()
        seed = seed if seed is not None else random.randint(0, 2**31)

        workflow = self._build_txt2img_workflow(
            prompt=prompt,
            negative_prompt=neg,
            width=1024,
            height=1024,
            seed=seed,
            prefix=f"charsheet_{int(time.time())}",
        )

        return self._generate(workflow, output_path)

    def generate_character_sheet_with_reference(
        self,
        prompt: str,
        output_path: str,
        reference_image_path: str,
        fidelity: float = 0.2,
        negative_prompt: str = None,
        seed: int = None,
    ) -> dict:
        """
        Generate a character sheet training image guided by a reference image.

        When IPAdapter is available: uses "style transfer precise" mode with
        limited step range (0.0-0.5) to transfer appearance (skin tone, hair
        color, facial features) without overriding pose/composition.

        Without IPAdapter: falls back to plain txt2img. img2img from a single
        portrait can't produce varied poses — all outputs look the same.

        Args:
            prompt: Full generation prompt
            output_path: Where to save the output image
            reference_image_path: Path to reference image for style guidance
            fidelity: 0.0-1.0, how strongly the reference influences output.
                      0.2 = light guidance for appearance consistency.
            negative_prompt: Optional override
            seed: Optional seed for reproducibility
        """
        from .prompt_builder import build_character_sheet_negative

        neg = negative_prompt or build_character_sheet_negative()
        seed = seed if seed is not None else random.randint(0, 2**31)

        # Upload the reference image to ComfyUI
        style_name = self._upload_image(reference_image_path)

        if self._has_ipadapter:
            workflow = self._build_ipadapter_workflow(
                prompt=prompt,
                negative_prompt=neg,
                style_image_name=style_name,
                width=1024,
                height=1024,
                seed=seed,
                ipa_weight=max(0.15, min(0.5, fidelity)),
                prefix=f"charsheet_ref_{int(time.time())}",
            )
        else:
            # img2img from a portrait can't produce varied poses —
            # fall back to plain txt2img (character details are in the prompt)
            print(f"    [INFO] No IPAdapter — using txt2img (reference skipped)")
            workflow = self._build_txt2img_workflow(
                prompt=prompt,
                negative_prompt=neg,
                width=1024,
                height=1024,
                seed=seed,
                prefix=f"charsheet_{int(time.time())}",
            )

        result = self._generate(workflow, output_path)
        result["style_guide_used"] = True
        result["fidelity"] = fidelity
        result["style_method"] = "ipadapter" if self._has_ipadapter else "img2img"
        return result

    def generate_character_sheet_batch(
        self,
        sheet_prompts: list,
        output_dir: str,
        dry_run: bool = False,
        progress_callback=None,
        reference_image_path: str = None,
        reference_fidelity: float = 0.2,
        negative_prompt: str = None,
        force: bool = False,
    ) -> list:
        """
        Generate all training images for a character sheet.

        Args:
            sheet_prompts: List of dicts from build_character_sheet_prompts().
                           Each has: prompt, caption, filename, label.
            output_dir: Directory to save images (e.g. characters/{id}/training_images/)
            dry_run: If True, only count and return without generating
            progress_callback: Optional (pct, msg) callback
            reference_image_path: Optional path to reference image (character
                                  reference or first generated training image).
                                  When provided and IPAdapter is available, images
                                  use style transfer for appearance consistency.
                                  Without IPAdapter, reference is skipped (img2img
                                  from a portrait can't produce varied poses).
            reference_fidelity: How strongly the reference influences output
                                (0.2 = light guidance for consistency).
            negative_prompt: Optional override for negative prompt (with gender
                             negatives baked in).

        Returns:
            List of result dicts with status, path, caption_path per image.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        total = len(sheet_prompts)
        ref_label = ""
        if reference_image_path:
            ref_label = f" (reference-guided, fidelity={reference_fidelity})"
        print(f"\n  Character sheet generation: {total} training images{ref_label}")
        print(f"  Output directory: {out}")
        print(f"  Cost: $0.00 (local GPU)")

        if dry_run:
            print("  DRY RUN — no generation")
            return [{"filename": s["filename"], "status": "dry_run"} for s in sheet_prompts]

        # Track the active reference — may be set from first generated image
        active_ref = reference_image_path

        results = []
        for i, spec in enumerate(sheet_prompts):
            pct = int((i / total) * 100)
            if progress_callback:
                progress_callback(pct, f"Generating {spec['label']}...")

            image_path = out / f"{spec['filename']}.png"
            caption_path = out / f"{spec['filename']}.txt"

            # Skip existing images (unless force regeneration requested)
            if not force and image_path.exists() and image_path.stat().st_size > 1000:
                print(f"  [{i+1}/{total}] [SKIP] {spec['filename']} (exists)")
                results.append({
                    "filename": spec["filename"],
                    "status": "already_exists",
                    "image_path": str(image_path),
                    "caption_path": str(caption_path),
                })
                # If no reference yet, use first existing image as reference
                if not active_ref:
                    active_ref = str(image_path)
                    print(f"    Using existing image as reference for remaining")
                continue

            print(f"  [{i+1}/{total}] {spec['label']}"
                  f"{' [REF]' if active_ref else ''}")
            try:
                if active_ref:
                    result = self.generate_character_sheet_with_reference(
                        prompt=spec["prompt"],
                        output_path=str(image_path),
                        reference_image_path=active_ref,
                        fidelity=reference_fidelity,
                        negative_prompt=negative_prompt,
                    )
                else:
                    result = self.generate_character_sheet(
                        prompt=spec["prompt"],
                        output_path=str(image_path),
                        negative_prompt=negative_prompt,
                    )

                # Write caption file alongside the image
                caption_path.write_text(spec["caption"], encoding="utf-8")

                print(f"    [OK] Saved {result['width']}x{result['height']}"
                      f"{' (style: ' + result.get('style_method', '') + ')' if result.get('style_guide_used') else ''}")

                # First generated image becomes the reference for remaining
                if not active_ref:
                    active_ref = str(image_path)
                    print(f"    First image set as reference for remaining")

                results.append({
                    "filename": spec["filename"],
                    "status": "generated",
                    "image_path": str(image_path),
                    "caption_path": str(caption_path),
                    "width": result["width"],
                    "height": result["height"],
                    "style_guide_used": result.get("style_guide_used", False),
                })

            except Exception as e:
                print(f"    [FAIL] {e}")
                results.append({
                    "filename": spec["filename"],
                    "status": "failed",
                    "error": str(e),
                })

        generated = sum(1 for r in results if r["status"] == "generated")
        skipped = sum(1 for r in results if r["status"] == "already_exists")
        styled = sum(1 for r in results if r.get("style_guide_used"))
        print(f"\n  Character sheet: {generated} generated, {skipped} skipped"
              f"{f', {styled} reference-guided' if styled else ''}")
        return results

    # ------------------------------------------------------------------
    # LoRA-enhanced generation
    # ------------------------------------------------------------------

    def _build_lora_txt2img_workflow(
        self,
        prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        seed: int,
        lora_configs: list,
        prefix: str = "babylon_lora",
    ) -> dict:
        """
        Build an SDXL txt2img workflow with chained LoraLoader nodes.

        lora_configs: list of dicts with:
          - file: LoRA filename (e.g. "kobbi_char.safetensors")
          - weight: model weight (0.0-1.0)

        Workflow chain:
          Checkpoint → LoRA1 → LoRA2 → ... → KSampler → VAEDecode → Save

        Each LoraLoader takes model/clip from the previous node.
        """
        if not lora_configs:
            return self._build_txt2img_workflow(
                prompt, negative_prompt, width, height, seed, prefix
            )

        workflow = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": self.checkpoint},
                "_meta": {"title": "Load Checkpoint"},
            },
        }

        # Chain LoraLoader nodes starting at node "20"
        prev_model_ref = ["1", 0]  # model output from checkpoint
        prev_clip_ref = ["1", 1]   # clip output from checkpoint

        for idx, lora_cfg in enumerate(lora_configs):
            node_id = str(20 + idx)
            workflow[node_id] = {
                "class_type": "LoraLoader",
                "inputs": {
                    "lora_name": lora_cfg["file"],
                    "strength_model": lora_cfg.get("weight", 0.8),
                    "strength_clip": lora_cfg.get("weight", 0.8),
                    "model": prev_model_ref,
                    "clip": prev_clip_ref,
                },
                "_meta": {"title": f"LoRA: {lora_cfg['file']}"},
            }
            prev_model_ref = [node_id, 0]
            prev_clip_ref = [node_id, 1]

        # Text encoding uses final clip from LoRA chain
        workflow["2"] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": prev_clip_ref},
            "_meta": {"title": "Positive Prompt"},
        }
        workflow["3"] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": prev_clip_ref},
            "_meta": {"title": "Negative Prompt"},
        }
        workflow["4"] = {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
            "_meta": {"title": "Empty Latent"},
        }
        workflow["5"] = {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": SDXL_DEFAULTS["steps"],
                "cfg": SDXL_DEFAULTS["cfg"],
                "sampler_name": SDXL_DEFAULTS["sampler_name"],
                "scheduler": SDXL_DEFAULTS["scheduler"],
                "denoise": 1.0,
                "model": prev_model_ref,
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
            "_meta": {"title": "KSampler"},
        }
        workflow["6"] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
            "_meta": {"title": "VAE Decode"},
        }
        workflow["7"] = {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": prefix, "images": ["6", 0]},
            "_meta": {"title": "Save Image"},
        }

        return workflow

    def generate_storyboard_with_loras(
        self,
        prompt: str,
        output_path: str,
        lora_configs: list,
        width: int = 1344,
        height: int = 768,
        negative_prompt: str = None,
        seed: int = None,
    ) -> dict:
        """
        Generate a storyboard image using SDXL + character LoRAs.

        lora_configs: list of dicts with:
          - file: LoRA filename in ComfyUI's lora directory
          - weight: strength (0.0-1.0)
          - trigger_word: prepended to prompt automatically

        Falls back to standard txt2img if lora_configs is empty.
        """
        from .prompt_builder import build_negative_prompt, inject_trigger_words

        # Inject trigger words into prompt
        prompt = inject_trigger_words(prompt, lora_configs)

        neg = negative_prompt or build_negative_prompt()
        seed = seed if seed is not None else random.randint(0, 2**31)
        w, h = self._snap_resolution(width, height)

        workflow = self._build_lora_txt2img_workflow(
            prompt=prompt,
            negative_prompt=neg,
            width=w,
            height=h,
            seed=seed,
            lora_configs=lora_configs,
            prefix=f"babylon_lora_{int(time.time())}",
        )

        result = self._generate(workflow, output_path)
        result["loras_used"] = [cfg["file"] for cfg in lora_configs]
        return result

    def get_available_loras(self) -> list:
        """
        Query ComfyUI for available LoRA files.
        Returns list of LoRA filenames.
        """
        try:
            resp = self.client.get(
                f"{self.base_url}/object_info/LoraLoader"
            )
            data = resp.json()
            loras = (
                data.get("LoraLoader", {})
                    .get("input", {})
                    .get("required", {})
                    .get("lora_name", [[]])[0]
            )
            return list(loras)
        except Exception:
            return []

    def estimate_cost(self, shot_count: int, include_vertical: bool = True) -> float:
        """Always $0.00 — local generation."""
        return 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _snap_resolution(width: int, height: int) -> tuple:
        """
        Snap arbitrary width/height to nearest SDXL-native resolution.
        SDXL requires resolutions divisible by 8, ideally ~1 megapixel.
        """
        aspect = width / height if height > 0 else 1.0

        if aspect > 1.5:      # Landscape (16:9 ≈ 1.78)
            return SDXL_RESOLUTIONS["16:9"]
        elif aspect < 0.67:    # Portrait (9:16 ≈ 0.56)
            return SDXL_RESOLUTIONS["9:16"]
        else:                  # Square-ish
            return SDXL_RESOLUTIONS["1:1"]

    @classmethod
    def _find_comfyui_url(cls) -> str:
        """
        Scan default ports (8000, 8001, 8002) for a running ComfyUI instance.
        ComfyUI sometimes opens on a different port after a restart.
        Returns the first reachable URL, or falls back to port 8000.
        """
        for port in cls.DEFAULT_PORTS:
            url = f"http://localhost:{port}"
            try:
                resp = httpx.get(f"{url}/system_stats", timeout=2.0)
                if resp.status_code == 200:
                    return url
            except Exception:
                continue
        return f"http://localhost:{cls.DEFAULT_PORTS[0]}"

    @classmethod
    def is_available(cls, base_url: str = None) -> bool:
        """
        Quick check if ComfyUI is reachable at the given URL.
        If no URL given, scans ports 8000-8002.
        Use this before instantiating the client to avoid errors.
        """
        if base_url:
            urls = [base_url]
        elif os.getenv("COMFYUI_URL", "").strip():
            urls = [os.getenv("COMFYUI_URL").strip()]
        else:
            urls = [f"http://localhost:{p}" for p in cls.DEFAULT_PORTS]

        for url in urls:
            try:
                resp = httpx.get(f"{url}/system_stats", timeout=3.0)
                if resp.status_code == 200:
                    return True
            except Exception:
                continue
        return False
