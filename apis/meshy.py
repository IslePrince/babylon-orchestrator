"""
apis/meshy.py
Meshy.ai API client.
Handles:
  - text-to-3D mesh generation (preview + refine pipeline)
  - text-to-animation for background characters and props
  - batch asset processing with manifest updates

Animation decision guide:
  Background characters → Meshy animation (mesh + motion in one pipeline, cheaper)
  Named characters      → Cartwheel (MetaHuman compatible, better quality)
"""

import json
import time
from pathlib import Path
from typing import Optional
from .base import BaseAPIClient, APIError


class MeshyClient(BaseAPIClient):

    API_NAME = "meshy"
    BASE_URL = "https://api.meshy.ai"
    ENV_KEY = "MESHY_API_KEY"

    POLL_INTERVAL = 15
    MAX_POLL_TIME = 600

    COST_MESH = {"hero": 0.50, "medium": 0.25, "low": 0.10}
    COST_ANIM = {"complex": 0.20, "standard": 0.10, "simple": 0.05}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    # ------------------------------------------------------------------
    # Text-to-3D mesh generation
    # ------------------------------------------------------------------

    def submit_preview(
        self,
        prompt: str,
        negative_prompt: str = "",
        art_style: str = "realistic",
        topology: str = "quad",
        target_polycount: int = 10000
    ) -> str:
        payload = {
            "mode": "preview",
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "art_style": art_style,
            "topology": topology,
            "target_polycount": target_polycount,
            "should_remesh": True
        }
        resp = self.post("/v2/text-to-3d", json=payload)
        job_id = resp.json().get("result")
        if not job_id:
            raise APIError("meshy", 0, f"No job ID: {resp.text}")
        print(f"    ↳ preview submitted: {job_id}")
        return job_id

    def submit_refine(
        self,
        preview_job_id: str,
        texture_resolution: int = 2048,
        enable_pbr: bool = True
    ) -> str:
        payload = {
            "mode": "refine",
            "preview_task_id": preview_job_id,
            "texture_resolution": texture_resolution,
            "enable_pbr": enable_pbr
        }
        resp = self.post("/v2/text-to-3d", json=payload)
        job_id = resp.json().get("result")
        if not job_id:
            raise APIError("meshy", 0, f"No refine job ID: {resp.text}")
        print(f"    ↳ refine submitted: {job_id}")
        return job_id

    def poll(self, job_id: str, endpoint: str, label: str = "") -> dict:
        tag = f" [{label}]" if label else ""
        elapsed = 0
        while elapsed < self.MAX_POLL_TIME:
            resp = self.get(f"{endpoint}/{job_id}")
            data = resp.json()
            status = data.get("status", "").lower()
            progress = data.get("progress", 0)
            print(f"    {tag} {status} {progress}%    ", end="\r")
            if status == "succeeded":
                print(f"    ✓{tag} done             ")
                return data
            if status in ("failed", "expired"):
                raise APIError("meshy", 0, f"Job {status}: {data.get('task_error')}")
            time.sleep(self.POLL_INTERVAL)
            elapsed += self.POLL_INTERVAL
        raise APIError("meshy", 0, f"Timed out after {self.MAX_POLL_TIME}s")

    def generate_mesh(
        self,
        prompt: str,
        negative_prompt: str = "",
        output_dir: str = "",
        detail_level: str = "medium",
        output_format: str = "fbx"
    ) -> dict:
        """Full pipeline: preview → refine → download. Returns paths + job IDs."""
        polycount = {"hero": 20000, "medium": 10000, "low": 4000}.get(detail_level, 10000)
        tex_res = {"hero": 2048, "medium": 1024, "low": 512}.get(detail_level, 1024)

        preview_id = self.submit_preview(prompt, negative_prompt, target_polycount=polycount)
        self.poll(preview_id, "/v2/text-to-3d", label="preview")

        refine_id = self.submit_refine(preview_id, texture_resolution=tex_res)
        final = self.poll(refine_id, "/v2/text-to-3d", label="refine")

        result = {
            "preview_job_id": preview_id,
            "refine_job_id": refine_id,
            "cost_usd": self.COST_MESH.get(detail_level, 0.25)
        }

        if output_dir:
            result["files"] = self._download_mesh(final, output_dir, output_format)

        return result

    def _download_mesh(self, job_data: dict, output_dir: str, fmt: str) -> dict:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = {}

        model_urls = job_data.get("model_urls", {})
        url = model_urls.get(fmt) or model_urls.get("fbx") or model_urls.get("glb")
        if url:
            ext = fmt if fmt in url.lower() else "fbx"
            p = out / f"mesh.{ext}"
            self._stream(url, p)
            files["model"] = str(p)
            print(f"      ↳ mesh.{ext}")

        for tex_type, tex_url in job_data.get("texture_urls", {}).items():
            if tex_url:
                p = out / f"texture_{tex_type}.png"
                self._stream(tex_url, p)
                files[f"texture_{tex_type}"] = str(p)
                print(f"      ↳ texture_{tex_type}.png")

        meta_p = out / "meshy_meta.json"
        with open(meta_p, "w") as f:
            json.dump({"job_id": job_data.get("id"), "prompt": job_data.get("prompt"),
                       "files": files}, f, indent=2)
        files["meta"] = str(meta_p)
        return files

    def _stream(self, url: str, path: Path):
        import httpx
        with httpx.stream("GET", url, timeout=180.0) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_bytes(8192):
                    f.write(chunk)

    # ------------------------------------------------------------------
    # Text-to-Animation (background characters only)
    # Named characters use Cartwheel instead — see cartwheel.py
    # ------------------------------------------------------------------

    def submit_animation(
        self,
        mesh_refine_job_id: str,
        prompt: str,
        negative_prompt: str = "twitching, sliding, floating, unnatural, clipping"
    ) -> str:
        """Animate a mesh that Meshy generated. Returns animation job_id."""
        payload = {
            "model_task_id": mesh_refine_job_id,
            "prompt": prompt,
            "negative_prompt": negative_prompt
        }
        resp = self.post("/v2/text-to-animation", json=payload)
        job_id = resp.json().get("result")
        if not job_id:
            raise APIError("meshy", 0, f"No animation job ID: {resp.text}")
        print(f"    ↳ animation submitted: {job_id}")
        return job_id

    def generate_animation(
        self,
        mesh_refine_job_id: str,
        prompt: str,
        output_dir: str,
        complexity: str = "standard"
    ) -> dict:
        """Generate and download animation. Use for background characters only."""
        job_id = self.submit_animation(mesh_refine_job_id, prompt)
        data = self.poll(job_id, "/v2/text-to-animation", label=prompt[:25])

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        files = {}

        anim_url = data.get("animation_url") or data.get("model_url")
        if anim_url:
            p = out / "animation.fbx"
            self._stream(anim_url, p)
            files["animation"] = str(p)
            print(f"      ↳ animation.fbx")

        meta_p = out / "anim_meta.json"
        with open(meta_p, "w") as f:
            json.dump({"job_id": job_id, "prompt": prompt, "files": files}, f, indent=2)
        files["meta"] = str(meta_p)

        return {"job_id": job_id, "files": files,
                "cost_usd": self.COST_ANIM.get(complexity, 0.10)}

    # ------------------------------------------------------------------
    # Background character: mesh + animation together
    # ------------------------------------------------------------------

    def generate_background_character(
        self,
        bg_type: dict,
        motion_prompts: list,
        output_dir: str,
        dry_run: bool = False
    ) -> dict:
        """
        Full pipeline for a background character type.
        Generates one mesh, then one animation per motion prompt.

        bg_type: background_character_type schema dict
        motion_prompts: list of strings, e.g.:
            ["slow walk carrying clay jug, ancient Mesopotamia",
             "idle standing looking at market stalls"]
        """
        type_id = bg_type["type_id"]
        ap = bg_type.get("appearance", {})

        mesh_prompt = (
            f"Ancient Babylonian {ap.get('gender','male')} commoner T-pose, "
            f"{ap.get('age_range','25-45')} years old, {ap.get('skin_tone_range','olive')} skin, "
            f"rough linen tunic belted, game-ready rigged character, photorealistic, 600 BCE"
        )
        neg = "modern clothing, armor, fantasy, greek, roman, cartoon, nudity"

        est_cost = (self.COST_MESH["low"] +
                    self.COST_ANIM["simple"] * len(motion_prompts))
        print(f"\n  Background character: {type_id}")
        print(f"  {len(motion_prompts)} motion(s) to generate")
        print(f"  Estimated: ${est_cost:.2f}")

        if dry_run:
            return {"type_id": type_id, "status": "dry_run", "cost_usd": est_cost}

        try:
            # Mesh
            mesh_out = str(Path(output_dir) / "mesh")
            mesh_result = self.generate_mesh(
                prompt=mesh_prompt, negative_prompt=neg,
                output_dir=mesh_out, detail_level="low"
            )

            # Animations
            anim_results = []
            for i, motion in enumerate(motion_prompts):
                anim_label = motion.replace(" ", "_")[:30]
                anim_out = str(Path(output_dir) / "animations" / f"anim_{i:02d}_{anim_label}")
                anim_result = self.generate_animation(
                    mesh_refine_job_id=mesh_result["refine_job_id"],
                    prompt=motion,
                    output_dir=anim_out,
                    complexity="simple"
                )
                anim_results.append({
                    "prompt": motion,
                    "files": anim_result["files"],
                    "cost_usd": anim_result["cost_usd"]
                })

            total_cost = mesh_result["cost_usd"] + sum(a["cost_usd"] for a in anim_results)
            print(f"  ✓ {type_id} complete — ${total_cost:.2f}")

            return {
                "type_id": type_id,
                "status": "completed",
                "mesh": mesh_result,
                "animations": anim_results,
                "cost_usd": total_cost
            }

        except APIError as e:
            print(f"  ✗ Failed: {e}")
            return {"type_id": type_id, "status": "failed", "error": str(e), "cost_usd": 0}

    # ------------------------------------------------------------------
    # Batch processing from manifest
    # ------------------------------------------------------------------

    def process_manifest_batch(
        self,
        batch: dict,
        manifest: dict,
        project_root: str,
        dry_run: bool = False
    ) -> dict:
        """
        Process a generation_batch entry from asset manifest.
        Updates manifest asset statuses in place.
        """
        batch_id = batch["batch_id"]
        asset_ids = batch["asset_ids"]
        results = []
        total_cost = 0.0

        assets_to_run = []
        for aid in asset_ids:
            asset = self._find_asset(manifest, aid)
            if not asset:
                print(f"  ✗ Not found in manifest: {aid}")
                continue
            if not asset.get("approved_for_generation"):
                print(f"  ⏭ Not approved: {aid}")
                continue
            if asset["meshy"]["status"] == "completed":
                print(f"  ✓ Already done: {aid}")
                continue
            assets_to_run.append(asset)

        est = sum(self.COST_MESH.get(a.get("detail_level","medium"), 0.25)
                  for a in assets_to_run)
        print(f"\n  Batch {batch_id}: {len(assets_to_run)} assets to generate")
        print(f"  Estimated: ${est:.2f}")

        if dry_run:
            return {"batch_id": batch_id, "status": "dry_run", "estimated": est}

        for asset in assets_to_run:
            aid = asset["asset_id"]
            mc = asset["meshy"]
            out_dir = str(Path(project_root) / mc.get("output_ref", f"assets/generated/{aid}"))

            print(f"\n  [{aid}]")
            try:
                result = self.generate_mesh(
                    prompt=mc["prompt"],
                    negative_prompt=mc.get("negative_prompt", ""),
                    output_dir=out_dir,
                    detail_level=asset.get("detail_level", "medium"),
                    output_format=mc.get("output_format", "fbx")
                )
                asset["meshy"]["status"] = "completed"
                asset["meshy"]["job_id"] = result["refine_job_id"]
                asset["meshy"]["cost_usd"] = result["cost_usd"]
                total_cost += result["cost_usd"]
                results.append({"asset_id": aid, "status": "completed",
                                 "cost_usd": result["cost_usd"]})

            except APIError as e:
                asset["meshy"]["status"] = "failed"
                print(f"  ✗ {aid}: {e}")
                results.append({"asset_id": aid, "status": "failed", "error": str(e)})

        done = sum(1 for r in results if r["status"] == "completed")
        print(f"\n  Batch done: {done}/{len(assets_to_run)} — ${total_cost:.2f}")
        return {"batch_id": batch_id, "completed": done,
                "cost_usd": total_cost, "results": results}

    def _find_asset(self, manifest: dict, asset_id: str) -> Optional[dict]:
        for cat in ["environments", "props", "costumes", "vegetation"]:
            for asset in manifest.get("assets", {}).get(cat, []):
                if asset["asset_id"] == asset_id:
                    return asset
        return None

    def estimate_manifest_cost(self, manifest: dict) -> dict:
        by_cat = {}
        total = 0.0
        for cat in ["environments", "props", "costumes", "vegetation"]:
            c = 0.0
            for a in manifest.get("assets", {}).get(cat, []):
                if a.get("meshy", {}).get("status") == "pending":
                    c += self.COST_MESH.get(a.get("detail_level", "medium"), 0.25)
            by_cat[cat] = round(c, 2)
            total += c
        return {"by_category": by_cat, "total": round(total, 2)}
