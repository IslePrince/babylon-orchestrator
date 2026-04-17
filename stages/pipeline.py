"""
stages/pipeline.py
Pipeline stage orchestrators.
Each stage: loads context → calls API → updates schemas → commits to git.
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from core.project import Project
from core.cost_manager import CostManager, GateLockError, BudgetExceededError
from core.git_manager import GitManager
from apis.claude_client import ClaudeClient
from apis.elevenlabs import ElevenLabsClient
from apis.meshy import MeshyClient
from apis.google_imagen import GoogleImagenClient
from apis.stability import StabilityClient
from apis.comfyui import ComfyUIClient

import re

# Gender keywords used to detect whether a visual_tag includes explicit gender.
_MALE_KEYWORDS = re.compile(
    r"\b(man|male|boy|father|grandfather|he)\b", re.IGNORECASE
)
_FEMALE_KEYWORDS = re.compile(
    r"\b(woman|female|girl|mother|grandmother|she)\b", re.IGNORECASE
)


def _ensure_gender_in_visual_tag(visual_tag: str, character: dict) -> str:
    """Return *visual_tag* with explicit gender if it's missing.

    Uses the character's role / description to infer gender.  Falls back to
    ``male`` when inference is ambiguous (all named characters in the current
    project are male except those explicitly described as female).
    """
    if not visual_tag:
        return visual_tag

    if _MALE_KEYWORDS.search(visual_tag) or _FEMALE_KEYWORDS.search(visual_tag):
        return visual_tag  # already has gender

    # Try to infer gender from character metadata
    role = character.get("role", "")
    desc_text = ""
    desc = character.get("description", {})
    if isinstance(desc, dict):
        desc_text = " ".join(str(v) for v in desc.values())
    elif isinstance(desc, str):
        desc_text = desc
    combined = f"{role} {desc_text}".lower()

    if _FEMALE_KEYWORDS.search(combined):
        gender = "female"
    else:
        gender = "male"

    # Insert gender after the age phrase, e.g. "lean 35-year-old" → "lean male 35-year-old"
    # or prepend if no age phrase found
    age_pat = re.compile(r"(\d{1,3}-year-old)")
    m = age_pat.search(visual_tag)
    if m:
        insert_pos = m.start()
        visual_tag = visual_tag[:insert_pos] + f"{gender} " + visual_tag[insert_pos:]
    else:
        visual_tag = f"{gender} {visual_tag}"

    return visual_tag


class PipelineStage:
    """Base class for all pipeline stages."""

    def __init__(self, project: Project):
        self.project = project
        self.costs = CostManager(project)
        self.git = GitManager(project)

    def _git_commit(self, stage: str, entity_id: str, message: str,
                    paths: list = None, merge: bool = False):
        """Attempt git operations; skip gracefully if not a git repo."""
        try:
            self.git.create_stage_branch(stage, entity_id)
            self.git.commit_stage_artifacts(stage, entity_id, message, paths=paths)
            if merge:
                self.git.merge_to_main(stage, entity_id)
        except (RuntimeError, Exception) as e:
            print(f"  [WARN] Git skipped (not a repo?): {e}")

    def _load_characters_for_chapter(self, chapter_id: str) -> list:
        chapter = self.project.load_chapter(chapter_id)
        character_ids = chapter.get("characters", {}).get("featured", [])
        characters = []
        for cid in character_ids:
            try:
                characters.append(self.project.load_character(cid))
            except FileNotFoundError:
                pass
        return characters

    def _get_scene_ids(self, chapter_id: str) -> list:
        chapter = self.project.load_chapter(chapter_id)
        scene_ids = []
        for act in chapter.get("structure", {}).get("acts", []):
            scene_ids.extend(act.get("scene_ids", []))
        return scene_ids


# ------------------------------------------------------------------
# Stage: Ingest + World Bible
# ------------------------------------------------------------------

class IngestStage(PipelineStage):

    def run(self, source_text_path: str, dry_run: bool = False,
            progress_callback=None) -> dict:
        """
        Ingest raw source text and generate:
        - Chapter index
        - Per-chapter source text files (for faithful screenplay adaptation)
        - World bible (draft)
        """
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Ingest + World Bible")
        print(f"{'-'*55}")

        source_text = Path(source_text_path).read_text(encoding="utf-8")
        print(f"  Source: {len(source_text)} characters, {len(source_text.split())} words")
        _progress(5, f"Source loaded: {len(source_text.split())} words")

        # Copy source text into the project for future reference
        source_dir = self.project._path("source")
        source_dir.mkdir(parents=True, exist_ok=True)
        source_copy = source_dir / Path(source_text_path).name
        source_copy.write_text(source_text, encoding="utf-8")
        print(f"  Source text copied to project: {source_copy.name}")

        estimated = self.costs.estimate_claude(
            input_tokens=int(len(source_text.split()) * 1.3),
            output_tokens=3000
        )
        print(f"  Estimated Claude cost: ${estimated:.4f}")
        _progress(10, f"Estimated cost: ${estimated:.4f}")

        if dry_run:
            print("  DRY RUN — no API calls")
            _progress(100, "Dry run complete", estimated)
            return {"status": "dry_run"}

        self.costs.check_api_allowed("claude")
        self.costs.check_budget("claude", estimated)

        with ClaudeClient() as claude:
            # Ingest — send up to 100K chars for better chapter analysis
            print("\n  Running source ingest...")
            _progress(15, "Running source ingest...")
            ingest_result = claude.ingest_source(source_text, self.project.id)
            self.costs.record("claude", ingest_result.pop("_cost_usd", 0), "ingest",
                              "Source ingest pass")

            # Save chapter index
            chapter_index = {
                "$schema": "babylon-studio/schemas/chapter-index/v1",
                "project_id": self.project.id,
                "total_chapters": ingest_result["total_chapters"],
                "chapters": [
                    {
                        "chapter_id": c["chapter_id"],
                        "chapter_number": c["chapter_number"],
                        "title": c["title"],
                        "status": "pending",
                        "approved": False
                    }
                    for c in ingest_result["chapters"]
                ]
            }
            self.project._save(
                self.project._path("chapters", "_index.json"),
                chapter_index
            )

            # Save individual chapter stubs + split source text per chapter
            chapter_titles = [c["title"] for c in ingest_result["chapters"]]
            chapter_texts = self._split_source_by_chapters(source_text, chapter_titles)

            for ch_data in ingest_result["chapters"]:
                chapter_stub = self._build_chapter_stub(ch_data)
                ch_dir = self.project._path("chapters", ch_data["chapter_id"])
                ch_dir.mkdir(parents=True, exist_ok=True)
                self.project.save_chapter(ch_data["chapter_id"], chapter_stub)

                # Save per-chapter source text for faithful screenplay adaptation
                ch_idx = ch_data["chapter_number"] - 1
                if ch_idx < len(chapter_texts) and chapter_texts[ch_idx]:
                    source_path = ch_dir / "source_text.txt"
                    source_path.write_text(chapter_texts[ch_idx], encoding="utf-8")
                    print(f"    {ch_data['chapter_id']}: {len(chapter_texts[ch_idx])} chars of source text saved")

            print(f"  [OK] {ingest_result['total_chapters']} chapters indexed")
            _progress(45, f"{ingest_result['total_chapters']} chapters indexed")

            # World bible
            print("\n  Generating world bible...")
            _progress(50, "Generating world bible...")
            source_summary = "\n".join([
                f"Ch{c['chapter_number']}: {c['title']} — {c['summary']}"
                for c in ingest_result["chapters"]
            ])
            world_result = claude.generate_world_bible(source_summary, self.project.data)
            cost = world_result.pop("_cost_usd", 0)
            self.costs.record("claude", cost, "world_bible", "World bible generation")

            # Merge into existing world bible template if it exists
            wb_path = self.project._path("world", "world_bible.json")
            wb_path.parent.mkdir(parents=True, exist_ok=True)
            world_result["project_id"] = self.project.id
            world_result["schema_version"] = "1.0"
            world_result["meta"] = {"approved": False, "cinematographer_reviewed": False}
            self.project._save(wb_path, world_result)
            print("  [OK] World bible generated (draft — needs human review)")
            _progress(80, "World bible generated")

        _progress(90, "Committing to git...")
        self._git_commit("ingest", self.project.id,
                         "ingest: source parsed and world bible drafted", merge=True)

        self.project.set_pipeline_stage("world_bible_review")
        print(f"\n  [OK] Ingest complete. Review world_bible.json before proceeding.")
        _progress(100, "Ingest complete")
        return {"status": "complete", "chapters": ingest_result["total_chapters"]}

    @staticmethod
    def _split_source_by_chapters(source_text: str, chapter_titles: list) -> list:
        """
        Split source text into per-chapter chunks using chapter titles as delimiters.
        Returns a list of strings, one per chapter title (in order).
        """
        import re
        lines = source_text.split("\n")
        # Find the line index where each chapter title appears as a heading
        # (standalone line matching the title, not in a TOC with page numbers)
        title_positions = []
        for title in chapter_titles:
            pattern = re.compile(re.escape(title.strip()) + r"\s*$", re.IGNORECASE)
            found = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                # Skip TOC entries (they have page numbers after the title)
                if re.search(r'\d+\s*$', stripped) and stripped != title.strip():
                    continue
                if pattern.match(stripped):
                    title_positions.append(i)
                    found = True
                    break
            if not found:
                title_positions.append(-1)

        # Extract text between consecutive chapter positions
        chapter_texts = []
        for idx, pos in enumerate(title_positions):
            if pos == -1:
                chapter_texts.append("")
                continue
            # Find end: next chapter start or end of file
            end = len(lines)
            for next_pos in title_positions[idx + 1:]:
                if next_pos > pos:
                    end = next_pos
                    break
            chapter_texts.append("\n".join(lines[pos:end]).strip())

        return chapter_texts

    def _build_chapter_stub(self, ch_data: dict) -> dict:
        """Build a chapter.json stub from ingest data."""
        return {
            "$schema": "babylon-studio/schemas/chapter/v1",
            "project_id": self.project.id,
            "chapter_id": ch_data["chapter_id"],
            "chapter_number": ch_data["chapter_number"],
            "title": ch_data["title"],
            "status": "pending",
            "source": {
                "adaptation_notes": ch_data.get("summary", "")
            },
            "narrative": {
                "logline": ch_data.get("summary", ""),
                "theme": ch_data.get("theme", ""),
                "parable_lesson": ch_data.get("parable_lesson", ""),
                "tone": "reflective_hopeful"
            },
            "structure": {"acts": []},
            "characters": {
                "featured": ch_data.get("key_characters", []),
                "background": []
            },
            "locations": {"used": ch_data.get("key_locations", [])},
            "screenplay": {"ref": f"chapters/{ch_data['chapter_id']}/screenplay.md",
                           "status": "pending", "approved": False},
            "production": {
                "total_shots": 0, "shots_approved": 0,
                "audio_lines_total": 0, "audio_lines_generated": 0,
                "scenes_built_in_ue5": 0, "preview_renders_complete": 0,
                "final_renders_complete": 0
            },
            "costs": {
                "stage_totals_usd": {
                    "screenplay": 0.0, "cinematographer": 0.0,
                    "storyboard": 0.0, "audio": 0.0,
                    "mesh_generation": 0.0, "scene_assembly": 0.0,
                    "preview_render": 0.0, "final_render": 0.0
                },
                "chapter_total_usd": 0.0
            },
            "meta": {"schema_version": "1.0", "created": datetime.now().isoformat(),
                     "approved": False}
        }


# ------------------------------------------------------------------
# Stage: Screenplay
# ------------------------------------------------------------------

class ScreenplayStage(PipelineStage):

    def run(self, chapter_id: str, dry_run: bool = False,
            progress_callback=None) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Screenplay — {chapter_id}")
        print(f"{'-'*55}")

        chapter = self.project.load_chapter(chapter_id)
        world_bible = self.project.load_world_bible()
        characters = self._load_characters_for_chapter(chapter_id)
        _progress(10, "Context loaded")

        estimated = self.costs.estimate_claude(input_tokens=3000, output_tokens=5000)
        print(f"  Characters: {[c.get('display_name') for c in characters]}")
        print(f"  Estimated cost: ${estimated:.4f}")

        if dry_run:
            print("  DRY RUN")
            _progress(100, "Dry run complete", estimated)
            return {"status": "dry_run"}

        self.costs.check_api_allowed("claude")
        self.costs.check_budget("claude", estimated)

        with ClaudeClient() as claude:
            print(f"  Writing screenplay...")
            _progress(20, "Writing screenplay...")
            adaptation_notes = chapter.get("source", {}).get("adaptation_notes", "")

            # Load per-chapter source text if available (for faithful adaptation)
            source_text = ""
            source_path = self.project._path("chapters", chapter_id, "source_text.txt")
            if source_path.exists():
                source_text = source_path.read_text(encoding="utf-8")
                print(f"  Source text: {len(source_text)} chars loaded for faithful adaptation")
            else:
                print(f"  WARNING: No source_text.txt found — screenplay will be based on summary only")
                print(f"  To fix: re-run ingest, or place chapter source in {source_path}")

            screenplay = claude.generate_screenplay(
                chapter_outline=chapter,
                world_bible=world_bible,
                characters=characters,
                adaptation_notes=adaptation_notes,
                source_text=source_text
            )
            input_words = len(adaptation_notes.split()) + len(source_text.split())
            actual_cost = self.costs.estimate_claude(
                int(input_words * 1.3),
                int(len(screenplay.split()) * 1.3)
            )
            self.costs.record("claude", actual_cost, "screenplay",
                              f"Screenplay {chapter_id}", entity_id=chapter_id)

        _progress(70, "Screenplay generated, saving...")

        # Save screenplay
        screenplay_path = self.project._path("chapters", chapter_id, "screenplay.md")
        screenplay_path.parent.mkdir(parents=True, exist_ok=True)
        screenplay_path.write_text(screenplay, encoding="utf-8")

        # Update chapter
        chapter["screenplay"]["status"] = "draft"
        chapter["status"] = "screenplay"
        self.project.save_chapter(chapter_id, chapter)

        self._git_commit("screenplay", chapter_id,
                         f"screenplay: {chapter_id} draft complete",
                         paths=[f"chapters/{chapter_id}/"])

        self.project.append_chapter_note(
            chapter_id,
            f"Screenplay draft generated. Word count: {len(screenplay.split())}. Needs human review.",
            author="screenplay_stage"
        )

        self.project.set_pipeline_stage("screenplay")
        print(f"  [OK] Screenplay saved: {screenplay_path}")
        print(f"  WARNING: Review screenplay before running cinematographer pass.")
        _progress(100, "Screenplay complete")
        return {"status": "complete", "path": str(screenplay_path)}


# ------------------------------------------------------------------
# Stage: Character Generation
# ------------------------------------------------------------------

class CharacterStage(PipelineStage):
    """
    Generate character profiles for ALL named characters across the project.
    Scans every chapter for featured character names, deduplicates, reads
    source text for context, and generates full profiles with visual_tag
    and costume_default for cross-chapter image consistency.

    Saves: characters/{character_id}/character.json + characters/_index.json
    """

    def run(self, dry_run: bool = False, progress_callback=None) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Character Generation")
        print(f"{'-'*55}")

        world_bible = self.project.load_world_bible()
        _progress(5, "Scanning chapters for characters...")

        # Collect all unique character names across every chapter,
        # along with which chapters they appear in and source text context
        char_map = {}  # name_lower -> {display_name, chapters, source_snippets, role}
        for chapter_id in self.project.get_all_chapter_ids():
            try:
                chapter = self.project.load_chapter(chapter_id)
            except FileNotFoundError:
                continue
            featured = chapter.get("characters", {}).get("featured", [])
            summary = chapter.get("narrative", {}).get("logline", "")

            # Load source text for richer character context
            source_path = self.project._path("chapters", chapter_id, "source_text.txt")
            source_text = ""
            if source_path.exists():
                source_text = source_path.read_text(encoding="utf-8")

            for name in featured:
                # Skip generic/group names
                name_stripped = name.strip()
                if self._is_generic_name(name_stripped):
                    continue
                key = name_stripped.lower().replace(" ", "_")
                if key not in char_map:
                    char_map[key] = {
                        "display_name": name_stripped,
                        "chapters": [],
                        "source_snippets": [],
                        "role": "featured",
                    }
                char_map[key]["chapters"].append(chapter_id)
                if source_text:
                    # Extract sentences mentioning this character (first 500 chars)
                    snippet = self._extract_character_mentions(
                        source_text, name_stripped, max_chars=500
                    )
                    if snippet:
                        char_map[key]["source_snippets"].append(snippet)

            # Also scan screenplay for speaking characters not in featured list
            screenplay_path = self.project._path("chapters", chapter_id, "screenplay.md")
            if screenplay_path.exists():
                screenplay_text = screenplay_path.read_text(encoding="utf-8")
                screenplay_chars = self._parse_screenplay_characters(screenplay_text)
                for name in screenplay_chars:
                    name_stripped = name.strip()
                    if self._is_generic_name(name_stripped):
                        continue
                    key = name_stripped.lower().replace(" ", "_")
                    if key not in char_map:
                        char_map[key] = {
                            "display_name": name_stripped,
                            "chapters": [chapter_id],
                            "source_snippets": [],
                            "role": "speaking",
                        }
                    elif chapter_id not in char_map[key]["chapters"]:
                        char_map[key]["chapters"].append(chapter_id)

        if not char_map:
            print("  No named characters found across chapters.")
            _progress(100, "No characters to generate")
            return {"status": "no_characters"}

        print(f"  Found {len(char_map)} unique characters: {list(char_map.keys())}")

        # Load existing character profiles to avoid regenerating
        existing_ids = set()
        existing_characters = []
        for cid in self.project.get_all_character_ids():
            try:
                char_data = self.project.load_character(cid)
                existing_ids.add(cid)
                existing_characters.append(char_data)
            except FileNotFoundError:
                pass

        new_chars = {k: v for k, v in char_map.items() if k not in existing_ids}
        if not new_chars:
            print(f"  All {len(char_map)} characters already have profiles.")
            _progress(100, "All characters already generated")
            return {"status": "complete", "characters": len(char_map), "new": 0}

        print(f"  New characters to generate: {len(new_chars)}")
        estimated_per = self.costs.estimate_claude(input_tokens=2000, output_tokens=2000)
        total_estimated = estimated_per * len(new_chars)
        print(f"  Estimated cost: ${total_estimated:.4f}")

        if dry_run:
            print("  DRY RUN")
            _progress(100, f"Dry run complete: {len(new_chars)} characters", total_estimated)
            return {"status": "dry_run", "characters": len(new_chars)}

        self.costs.check_api_allowed("claude")
        self.costs.check_budget("claude", total_estimated)

        _progress(10, f"Generating {len(new_chars)} character profiles...")
        total_cost = 0.0
        generated = []

        with ClaudeClient() as claude:
            for i, (char_key, char_info) in enumerate(new_chars.items()):
                pct = int(10 + (i / len(new_chars)) * 80)
                _progress(pct, f"Generating {char_info['display_name']}...")
                print(f"\n  [{i+1}/{len(new_chars)}] {char_info['display_name']}")

                # Build source description from summaries and snippets
                source_desc = "\n".join(char_info["source_snippets"][:3])
                if not source_desc:
                    source_desc = f"Character '{char_info['display_name']}' appears in chapters: {', '.join(char_info['chapters'])}"

                # Get full source text from first chapter for richer context
                first_chapter_source = ""
                for ch_id in char_info["chapters"]:
                    sp = self.project._path("chapters", ch_id, "source_text.txt")
                    if sp.exists():
                        first_chapter_source = sp.read_text(encoding="utf-8")
                        break

                result = claude.generate_character(
                    character_name=char_info["display_name"],
                    role=char_info["role"],
                    source_description=source_desc,
                    world_bible=world_bible,
                    existing_characters=existing_characters,
                    source_text=first_chapter_source,
                )
                cost = result.pop("_cost_usd", estimated_per)
                self.costs.record("claude", cost, "character_generation",
                                  f"Character profile: {char_info['display_name']}",
                                  entity_id=char_key)
                total_cost += cost

                # Normalize character_id
                character_id = result.get("character_id", char_key)
                result["character_id"] = character_id
                result["display_name"] = result.get("display_name", char_info["display_name"])

                # Ensure visual fields exist
                if "visual_tag" not in result:
                    desc = result.get("description", {})
                    result["visual_tag"] = desc.get("physical_appearance", "")[:100]
                if "costume_default" not in result:
                    assets = result.get("assets", {})
                    variants = assets.get("costume_variants", [])
                    result["costume_default"] = variants[0] if variants else ""

                # Ensure explicit gender in visual_tag
                result["visual_tag"] = _ensure_gender_in_visual_tag(
                    result.get("visual_tag", ""), result
                )

                # Save character profile
                self.project.save_character(character_id, result)
                existing_characters.append(result)
                generated.append(character_id)
                print(f"    [OK] visual_tag: {result.get('visual_tag', '')[:60]}...")

        # Update character index
        index = self.project.load_character_index()
        for cid in generated:
            if cid not in [c.get("character_id") for c in index.get("characters", [])]:
                try:
                    char_data = self.project.load_character(cid)
                    index.setdefault("characters", []).append({
                        "character_id": cid,
                        "display_name": char_data.get("display_name", cid),
                        "role": char_data.get("description", {}).get("role", "featured"),
                        "visual_tag": char_data.get("visual_tag", ""),
                    })
                except FileNotFoundError:
                    pass
        index["total_named"] = len(index.get("characters", []))
        index["last_updated"] = datetime.now().isoformat()
        self.project._save(self.project._path("characters", "_index.json"), index)

        # Also save project-level visual_tags.json for quick access
        visual_tags = {}
        for cid in self.project.get_all_character_ids():
            try:
                char_data = self.project.load_character(cid)
                visual_tags[cid] = {
                    "display_name": char_data.get("display_name", cid),
                    "visual_tag": char_data.get("visual_tag", ""),
                    "costume_default": char_data.get("costume_default", ""),
                }
            except FileNotFoundError:
                pass
        self.project._save(
            self.project._path("characters", "visual_tags.json"),
            {"characters": visual_tags}
        )

        self._git_commit("characters", self.project.id,
                         f"characters: {len(generated)} profiles generated")

        self.project.set_pipeline_stage("characters")
        print(f"\n  [OK] {len(generated)} character profiles generated, ${total_cost:.4f}")
        print(f"  Visual tags saved to characters/visual_tags.json")
        _progress(100, f"Characters complete: {len(generated)} profiles")
        return {"status": "complete", "characters": len(generated), "cost": total_cost}

    @staticmethod
    def _is_generic_name(name: str) -> bool:
        """Filter out group/generic character references that aren't real characters."""
        generic_patterns = [
            "narrator", "citizens", "crowd", "merchants", "traders",
            "students", "defenders", "forces", "borrowers", "people",
            "various", "enemy", "slave masters", "creditors", "guards",
            "servants", "workers", "audience", "modern",
        ]
        name_lower = name.lower()
        return any(pat in name_lower for pat in generic_patterns)

    @staticmethod
    def _extract_character_mentions(text: str, character_name: str,
                                     max_chars: int = 500) -> str:
        """Extract sentences mentioning a character from source text."""
        import re
        sentences = re.split(r'[.!?]+', text)
        mentions = []
        total = 0
        for s in sentences:
            if character_name.lower() in s.lower():
                s_clean = s.strip()
                if s_clean and total + len(s_clean) < max_chars:
                    mentions.append(s_clean)
                    total += len(s_clean)
                if total >= max_chars:
                    break
        return ". ".join(mentions)

    @staticmethod
    def _parse_screenplay_characters(text: str) -> list:
        """Extract ALL CAPS character names from screenplay markdown.
        Uses the same regex pattern as VoiceRecordingStage._parse_screenplay_dialogue.
        """
        import re
        char_pattern = re.compile(
            r'^([A-Z][A-Z\s\'.\d]+?)\s*(?:\(CONT\'?D\)|\(V\.O\.\)|\(O\.S\.\))?\s*$'
        )
        names = set()
        for line in text.split("\n"):
            clean = line.strip()
            if clean.startswith("**") and clean.endswith("**"):
                clean = clean[2:-2].strip()
            if clean.startswith("FADE") or clean.startswith("CUT TO"):
                continue
            if clean.startswith("INT.") or clean.startswith("EXT."):
                continue
            match = char_pattern.match(clean)
            if match:
                names.add(match.group(1).strip())
        return sorted(names)


# ------------------------------------------------------------------
# Stage: Cinematographer Pass
# ------------------------------------------------------------------

class CinematographerStage(PipelineStage):

    def run(self, chapter_id: str, scene_id: Optional[str] = None,
            dry_run: bool = False, progress_callback=None) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Cinematographer — {chapter_id}")
        print(f"{'-'*55}")

        world_bible = self.project.load_world_bible()
        chapter = self.project.load_chapter(chapter_id)
        _progress(5, "Context loaded")

        screenplay_path = self.project._path("chapters", chapter_id, "screenplay.md")
        if not screenplay_path.exists():
            raise FileNotFoundError(f"Screenplay not found: {screenplay_path}")

        screenplay_text = screenplay_path.read_text(encoding="utf-8")

        # Load audio recordings for actual durations (if voice recording has run)
        recordings_map = {}  # text[:100] -> {recording_id, duration_sec, character_id, audio_ref}
        try:
            recs = self.project.load_recordings(chapter_id)
            for rec in recs.get("recordings", []):
                recordings_map[rec["text"][:100]] = rec
            print(f"  Audio recordings loaded: {len(recordings_map)} lines")
        except FileNotFoundError:
            print(f"  [INFO] No recordings.json — using word-count duration estimates")

        # Load source text for character visual generation
        source_text = ""
        source_path = self.project._path("chapters", chapter_id, "source_text.txt")
        if source_path.exists():
            source_text = source_path.read_text(encoding="utf-8")

        # Process one scene or all scenes
        scene_ids = [scene_id] if scene_id else self._get_scene_ids(chapter_id)
        if not scene_ids:
            print("  WARNING: No scenes defined in chapter structure yet.")
            print("  Running full-chapter shot breakdown...")
            scene_ids = [f"{chapter_id}_sc01"]

        all_shots = []
        total_cost = 0.0

        with ClaudeClient() as claude:
            # Load character visual tags for consistent storyboard imagery
            character_visuals = self._load_character_visuals(chapter_id, dry_run)

            for scene_i, sid in enumerate(scene_ids):
                scene_pct = int(15 + (scene_i / max(len(scene_ids), 1)) * 70)
                _progress(scene_pct, f"Processing scene {sid}")
                print(f"\n  Scene: {sid}")

                # Try to load scene schema, build minimal one if missing
                try:
                    scene = self.project.load_scene(chapter_id, sid)
                    location = scene.get("location", {})
                    chars_in_scene = scene.get("characters", {}).get("present", [])
                    char_ids = [c["character_id"] for c in chars_in_scene]
                except FileNotFoundError:
                    location = {}
                    char_ids = chapter["characters"]["featured"]

                characters = []
                for cid in char_ids:
                    try:
                        characters.append(self.project.load_character(cid))
                    except FileNotFoundError:
                        pass

                # Extract scene text from screenplay
                scene_text = self._extract_scene_from_screenplay(screenplay_text, sid)

                # Parse dialogue + action beats for the cinematographer
                beats = self._parse_screenplay_beats(scene_text)
                beat_map = self._format_beats_for_prompt(beats, recordings_map)
                if beats:
                    dialogue_beats = sum(1 for b in beats if b["type"] == "dialogue")
                    action_beats = sum(1 for b in beats if b["type"] == "action")
                    print(f"  Beat map: {len(beats)} beats ({dialogue_beats} dialogue, {action_beats} action)")

                estimated = self.costs.estimate_claude(input_tokens=2000, output_tokens=3000)
                if dry_run:
                    print(f"  DRY RUN — estimated ${estimated:.4f}")
                    continue

                self.costs.check_budget("claude", estimated)

                result = claude.cinematographer_pass(
                    scene_text=scene_text,
                    scene_id=sid,
                    location=location,
                    characters_in_scene=characters,
                    world_bible=world_bible,
                    chapter_id=chapter_id,
                    character_visuals=character_visuals,
                    beat_map=beat_map,
                )
                cost = result.pop("_cost_usd", estimated)
                self.costs.record("claude", cost, "cinematographer",
                                  f"Cinematographer pass {sid}", entity_id=chapter_id)
                total_cost += cost

                shots = result.get("shots", [])
                print(f"  [OK] {len(shots)} shots from cinematographer for {sid}")

                # Post-process: ensure 1 shot per dialogue beat.
                # The LLM often under-produces shots. Fill gaps
                # with programmatic close-ups so every dialogue
                # line has a dedicated shot for audio sync.
                shots = self._ensure_dialogue_coverage(shots, beats)
                print(f"  [OK] {len(shots)} shots after dialogue coverage pass")

                # Save shot files with recording references
                self._save_shots(chapter_id, sid, shots, recordings_map)
                all_shots.extend(shots)

        # Update chapter production count
        chapter["production"]["total_shots"] = len(all_shots)

        # Populate chapter structure with scene_ids so downstream stages
        # (storyboard, audio, assets) can discover shots via _get_scene_ids()
        if not chapter["structure"]["acts"]:
            chapter["structure"]["acts"] = [{
                "act": 1,
                "scene_ids": list(dict.fromkeys(scene_ids))  # dedupe, preserve order
            }]
        else:
            # Merge new scene_ids into existing acts
            existing = set()
            for act in chapter["structure"]["acts"]:
                existing.update(act.get("scene_ids", []))
            new_ids = [sid for sid in scene_ids if sid not in existing]
            if new_ids:
                chapter["structure"]["acts"][-1].setdefault("scene_ids", []).extend(new_ids)

        self.project.save_chapter(chapter_id, chapter)

        self._git_commit("cinematographer", chapter_id,
                         f"cinematographer: {chapter_id} shot list complete ({len(all_shots)} shots)")

        _progress(95, "Committing to git...")
        self.project.set_pipeline_stage("cinematographer")
        print(f"\n  [OK] Cinematographer pass complete: {len(all_shots)} shots, ${total_cost:.4f}")
        _progress(100, f"Cinematographer complete: {len(all_shots)} shots")
        return {"status": "complete", "shots": len(all_shots), "cost": total_cost}

    def _load_character_visuals(self, chapter_id, dry_run) -> dict:
        """
        Load character visual tags for image generation consistency.

        Priority order:
        1. Project-level characters/visual_tags.json (from CharacterStage)
        2. Per-chapter chapters/{ch}/character_visuals.json (legacy fallback)
        3. Empty dict (no visual consistency)

        Returns dict keyed by character_id with {visual_tag, costume_default}.
        """
        # 1. Try project-level visual tags (preferred — from CharacterStage)
        project_visuals_path = self.project._path("characters", "visual_tags.json")
        if project_visuals_path.exists():
            try:
                data = self.project._load(project_visuals_path)
                characters = data.get("characters", {})
                if characters:
                    print(f"  Character visuals (project): {list(characters.keys())}")
                    return characters
            except Exception:
                pass

        # 2. Try per-chapter visuals (legacy fallback)
        visuals_path = self.project._path("chapters", chapter_id, "character_visuals.json")
        if visuals_path.exists():
            try:
                data = self.project._load(visuals_path)
                characters = data.get("characters", {})
                if characters:
                    print(f"  Character visuals (chapter): {list(characters.keys())}")
                    return characters
            except Exception:
                pass

        # 3. No visual tags available
        if not dry_run:
            print("  WARNING: No character visual tags found.")
            print("  Run 'characters' stage first for image consistency.")
        return {}

    @staticmethod
    def _parse_screenplay_beats(scene_text: str) -> list:
        """Parse screenplay text into ordered beats (dialogue + action).

        Returns list of dicts:
          {"type": "dialogue", "character": "BANSIR", "character_id": "bansir",
           "text": "...", "words": 28}
          {"type": "action", "text": "Bansir paces angrily...", "words": 15}

        This gives the cinematographer an exact map of the scene's rhythm
        so it can create one shot per beat (or group short action beats).
        """
        import re
        lines = scene_text.split("\n")
        beats = []
        i = 0
        char_pattern = re.compile(
            r'^([A-Z][A-Z\s\'.\d]+?)\s*(?:\(CONT\'?D\)|\(V\.O\.\)|\(O\.S\.\))?\s*$'
        )
        action_buf = []

        def _strip_bold(s):
            """Strip markdown bold markers: **TEXT** → TEXT"""
            s = s.strip()
            if s.startswith("**") and s.endswith("**"):
                return s[2:-2].strip()
            return s

        def flush_action():
            text = " ".join(action_buf).strip()
            if text:
                beats.append({
                    "type": "action",
                    "text": text,
                    "words": len(text.split()),
                })
            action_buf.clear()

        while i < len(lines):
            line = lines[i].rstrip()

            # Skip empty lines (but they may separate action from dialogue)
            if not line:
                i += 1
                continue

            # Skip scene headings / transitions
            stripped = line.strip()
            bare = _strip_bold(stripped)
            if bare.startswith("FADE") or bare.startswith("CUT TO"):
                i += 1
                continue

            # Scene heading (INT./EXT.) — treat as action beat
            if bare.startswith("INT.") or bare.startswith("EXT."):
                flush_action()
                action_buf.append(bare)
                flush_action()
                i += 1
                continue

            # Check for character cue (ALL CAPS, with bold stripped)
            match = char_pattern.match(bare)
            if match:
                flush_action()
                raw_name = match.group(1).strip()
                character_id = raw_name.lower().replace(" ", "_").replace("'", "")
                i += 1

                # Skip parenthetical
                if i < len(lines) and lines[i].strip().startswith("("):
                    i += 1

                # Collect dialogue text
                text_parts = []
                while i < len(lines) and lines[i].strip():
                    dl = lines[i].strip()
                    dl_clean = _strip_bold(dl)
                    if char_pattern.match(dl_clean) or dl_clean.startswith("INT.") \
                            or dl_clean.startswith("EXT."):
                        break
                    text_parts.append(dl)
                    i += 1

                if text_parts:
                    text = " ".join(text_parts)
                    # Split long speeches into sub-beats so each gets
                    # its own shot with a different framing.
                    sub_beats = CinematographerStage._split_long_dialogue(
                        raw_name, character_id, text
                    )
                    beats.extend(sub_beats)
            else:
                # Markdown heading (## Scene...) — skip as structural
                if stripped.startswith("#"):
                    i += 1
                    continue
                # Regular prose line → action beat
                action_buf.append(stripped)
                i += 1

        flush_action()
        return beats

    @staticmethod
    def _split_long_dialogue(
        character_name: str,
        character_id: str,
        text: str,
        max_words: int = 50,
    ) -> list:
        """Split a long dialogue turn into sub-beats at sentence boundaries.

        Short dialogue (≤ max_words) returns a single beat.
        Long dialogue is split at sentence-ending punctuation (.!?) into
        chunks of ~25-40 words. Continuation beats are marked (CONT'D)
        so the cinematographer uses different framings for each.

        Returns list of dialogue beat dicts.
        """
        import re

        words = text.split()
        if len(words) <= max_words:
            return [{
                "type": "dialogue",
                "character": character_name,
                "character_id": character_id,
                "text": text,
                "words": len(words),
            }]

        # Split text into sentences
        sentences = re.split(r'(?<=[.!?])\s+', text)
        if len(sentences) <= 1:
            # Can't split — single run-on sentence
            return [{
                "type": "dialogue",
                "character": character_name,
                "character_id": character_id,
                "text": text,
                "words": len(words),
            }]

        # Group sentences into chunks of ~25-40 words
        chunks = []
        current_chunk = []
        current_words = 0

        for sentence in sentences:
            s_words = len(sentence.split())
            # If adding this sentence would push well past target, start new chunk
            # (unless current chunk is empty — always take at least one sentence)
            if current_words > 0 and current_words + s_words > max_words * 0.7:
                chunks.append(" ".join(current_chunk))
                current_chunk = [sentence]
                current_words = s_words
            else:
                current_chunk.append(sentence)
                current_words += s_words

        if current_chunk:
            chunks.append(" ".join(current_chunk))

        # Build beat list — first chunk is the original character name,
        # subsequent chunks are marked CONT'D for different framing
        beats = []
        for ci, chunk in enumerate(chunks):
            name = character_name if ci == 0 else f"{character_name} (CONT'D)"
            beats.append({
                "type": "dialogue",
                "character": name,
                "character_id": character_id,
                "text": chunk,
                "words": len(chunk.split()),
            })

        return beats

    @staticmethod
    def _format_beats_for_prompt(beats: list, recordings_map: dict = None) -> str:
        """Format beats into a numbered list for the cinematographer prompt,
        plus a concrete SHOT BUDGET to guide Claude's output.
        When recordings_map is available, uses real audio durations."""
        if not beats:
            return ""
        lines = []
        for idx, b in enumerate(beats, 1):
            if b["type"] == "dialogue":
                key = b["text"][:100]
                rec = (recordings_map or {}).get(key)
                dur_str = f", {rec['duration_sec']:.1f}s recorded" if rec else f", ~{b['words'] / 2.5:.1f}s est."
                lines.append(
                    f"  {idx}. [DIALOGUE] {b['character']} ({b['words']} words{dur_str}): "
                    f"\"{b['text'][:80]}{'...' if len(b['text']) > 80 else ''}\""
                )
            else:
                lines.append(
                    f"  {idx}. [ACTION] {b['text'][:100]}{'...' if len(b['text']) > 100 else ''}"
                )

        dialogue_count = sum(1 for b in beats if b["type"] == "dialogue")
        action_count = sum(1 for b in beats if b["type"] == "action")
        # Use real durations when available, fall back to word-count estimate
        dialogue_secs = 0
        for b in beats:
            if b["type"] == "dialogue":
                key = b["text"][:100]
                rec = (recordings_map or {}).get(key)
                if rec:
                    dialogue_secs += rec["duration_sec"]
                else:
                    dialogue_secs += b["words"] / 2.5
        dialogue_secs = round(dialogue_secs)
        dialogue_words = sum(b["words"] for b in beats if b["type"] == "dialogue")
        dur_source = "recorded" if recordings_map else "estimated"
        # Budget: 1 shot per dialogue beat + minimal action shots
        action_shots = max(action_count, 2) if action_count > 0 else 2
        target_total = dialogue_count + action_shots

        header = (
            f"SCENE BEAT MAP ({len(beats)} beats total: "
            f"{dialogue_count} dialogue, {action_count} action/silence):\n"
        )

        budget = (
            f"\n\nSHOT BUDGET (audio-driven — stay close to these targets):\n"
            f"  Dialogue shots: {dialogue_count} (1:1 with dialogue beats, speaker always visible)\n"
            f"  Action/transition shots: {action_shots} (establishing + breathing room)\n"
            f"  Target total: ~{target_total} shots\n"
            f"  Dialogue audio: ~{dialogue_words} words ≈ {dialogue_secs}s ({dur_source})\n"
            f"  Target ASL: 4-5 seconds\n"
        )

        return header + "\n".join(lines) + budget

    @staticmethod
    def _ensure_dialogue_coverage(shots: list, beats: list) -> list:
        """Ensure every dialogue beat has its own shot.

        Walks through dialogue beats in order and maps them to shots by
        character. When a beat has no matching unused shot, a new close-up
        shot is injected at the right position. This makes the shot list
        deterministically cover every line, regardless of how many shots
        the LLM produced.
        """
        dialogue_beats = [b for b in beats if b["type"] == "dialogue"]
        if not dialogue_beats:
            return shots

        # Build a map: which shots cover which characters
        # Track which shots have been "used" by a dialogue beat
        used = set()
        result = list(shots)  # we'll insert into this
        shot_ptr = 0
        insert_offset = 0  # track how many we've inserted

        for beat in dialogue_beats:
            char_id = beat["character_id"]

            # Search forward from shot_ptr for an unused shot with this character
            found_idx = None
            for i in range(shot_ptr, len(result)):
                s = result[i]
                s_chars = [c.lower() if isinstance(c, str) else c
                           for c in s.get("characters_in_frame", [])]
                if char_id in s_chars and i not in used:
                    found_idx = i
                    break

            if found_idx is not None:
                used.add(found_idx)
                shot_ptr = found_idx + 1
            else:
                # No shot for this beat — inject a close-up
                # Find the last shot with this character (for context)
                last_prompt = ""
                for i in range(min(shot_ptr, len(result)) - 1, -1, -1):
                    s = result[i]
                    s_chars = [c.lower() if isinstance(c, str) else c
                               for c in s.get("characters_in_frame", [])]
                    if char_id in s_chars:
                        last_prompt = s.get("storyboard_prompt", "")
                        break

                # Build a simple close-up shot for this dialogue line
                char_name = beat.get("character", char_id.upper())
                text_preview = beat["text"][:60]
                new_shot = {
                    "shot_number": 0,  # renumbered below
                    "label": f"{char_name} speaks",
                    "shot_type": "close_up",
                    "framing": "close_up",
                    "lens_mm_equiv": 85,
                    "duration_sec": max(2, beat["words"] // 3),
                    "characters_in_frame": [char_id],
                    "dialogue_lines_covered": [text_preview],
                    "composition_notes": f"Close-up on {char_name} speaking",
                    "storyboard_prompt": (
                        f"{char_name} speaking, close-up portrait, "
                        f"dramatic lighting, emotional expression"
                    ),
                }

                # Insert at current pointer position
                insert_pos = min(shot_ptr, len(result))
                result.insert(insert_pos, new_shot)
                used.add(insert_pos)
                # Shift all used indices >= insert_pos
                used = {(u + 1 if u >= insert_pos and u != insert_pos else u) for u in used}
                shot_ptr = insert_pos + 1

        # Renumber all shots sequentially
        for i, s in enumerate(result):
            s["shot_number"] = i + 1

        added = len(result) - len(shots)
        if added > 0:
            print(f"  [+] Injected {added} close-up shots for uncovered dialogue lines")

        # Second pass: split overloaded shots (>3 dialogue lines per shot)
        MAX_DIALOGUE_PER_SHOT = 3
        split_result = []
        split_count = 0
        for shot in result:
            dialogue_covered = shot.get("dialogue_lines_covered", [])
            if len(dialogue_covered) <= MAX_DIALOGUE_PER_SHOT:
                split_result.append(shot)
                continue

            # Split into chunks of MAX_DIALOGUE_PER_SHOT
            chunks = [dialogue_covered[i:i + MAX_DIALOGUE_PER_SHOT]
                      for i in range(0, len(dialogue_covered), MAX_DIALOGUE_PER_SHOT)]
            for ci, chunk in enumerate(chunks):
                new_shot = dict(shot)
                new_shot["dialogue_lines_covered"] = chunk
                new_shot["shot_number"] = 0  # renumbered below
                if ci > 0:
                    new_shot["label"] = f"{shot.get('label', '')} (cont.)"
                    new_shot["shot_type"] = "medium_close_up"
                    new_shot["framing"] = "medium_close_up"
                    split_count += 1
                split_result.append(new_shot)

        if split_count > 0:
            for i, s in enumerate(split_result):
                s["shot_number"] = i + 1
            print(f"  [+] Split {split_count} overloaded shots (max {MAX_DIALOGUE_PER_SHOT} dialogue lines each)")
            result = split_result

        return result

    def _extract_scene_from_screenplay(self, screenplay: str, scene_id: str) -> str:
        """
        Attempt to extract a specific scene from screenplay text.
        Falls back to returning full screenplay if scene markers not found.
        """
        lines = screenplay.split("\n")
        scene_num = scene_id.split("sc")[-1].lstrip("0") if "sc" in scene_id else "1"
        markers = [f"SCENE {scene_num}", f"SC{scene_num.zfill(2)}", f"SC {scene_num}"]

        for i, line in enumerate(lines):
            if any(m in line.upper() for m in markers):
                end = len(lines)
                for j in range(i + 1, len(lines)):
                    if any(m in lines[j].upper() for m in ["INT.", "EXT.", "SCENE "]):
                        end = j
                        break
                return "\n".join(lines[i:end])

        # No scene markers found — return the full screenplay
        return screenplay

    def _save_shots(self, chapter_id: str, scene_id: str, shots: list,
                    recordings_map: dict = None):
        """Save shot schemas and index for a scene.
        When recordings_map is provided, populates audio.lines with recording references."""
        shots_dir = self.project._path("chapters", chapter_id, "shots")
        shots_dir.mkdir(parents=True, exist_ok=True)

        index_entries = []
        for shot_data in shots:
            shot_number = shot_data.get("shot_number", 1)
            shot_id = f"{scene_id}_sh{str(shot_number).zfill(3)}"

            # Build audio recording references from dialogue lines
            audio_lines = []
            if recordings_map:
                for dialogue_text in shot_data.get("dialogue_lines_covered", []):
                    key = dialogue_text[:100]
                    rec = recordings_map.get(key)
                    if rec:
                        audio_lines.append({
                            "recording_id": rec["recording_id"],
                            "character_id": rec["character_id"],
                            "audio_ref": rec["audio_ref"],
                            "text": rec["text"],
                            "start_time_sec": 0,
                            "end_time_sec": rec.get("duration_sec", 0),
                        })

            # Build full shot schema
            shot = {
                "$schema": "babylon-studio/schemas/shot/v1",
                "project_id": self.project.id,
                "chapter_id": chapter_id,
                "scene_id": scene_id,
                "shot_id": shot_id,
                "shot_number": shot_number,
                "label": shot_data.get("label", ""),
                "status": "generated",
                "cinematic": {
                    "shot_type": shot_data.get("shot_type", "medium"),
                    "framing": shot_data.get("framing", "medium_shot"),
                    "lens_mm_equiv": shot_data.get("lens_mm_equiv", 35),
                    "camera_movement": shot_data.get("camera_movement", {}),
                    "composition_notes": shot_data.get("composition_notes", ""),
                    "vertical_reframe": shot_data.get("vertical_reframe", {})
                },
                "characters_in_frame": [
                    {"character_id": cid}
                    for cid in shot_data.get("characters_in_frame", [])
                ],
                "dialogue_in_shot": shot_data.get("dialogue_lines_covered", []),
                "audio": {"lines": audio_lines, "sound_design": []},
                "assets_required": {
                    "assets_needed_raw": shot_data.get("assets_needed", [])
                },
                "storyboard": {
                    "image_ref": f"chapters/{chapter_id}/shots/{shot_id}/storyboard.png",
                    "generated": False,
                    "approved": False,
                    "storyboard_prompt": shot_data.get("storyboard_prompt", "")
                },
                "build_status": {
                    "world_version_built_against": None,
                    "assets_placed": False,
                    "lighting_set": False,
                    "animation_keyed": False,
                    "audio_synced": False,
                    "preview_rendered": False,
                    "final_rendered": False
                },
                "version_notes": [{
                    "version": "1.0",
                    "date": datetime.now().isoformat(),
                    "author": "ai_cinematographer_pass",
                    "note": "Initial shot generated from screenplay"
                }],
                "meta": {
                    "schema_version": "1.0",
                    "created": datetime.now().isoformat(),
                    "approved": False,
                    "flags": shot_data.get("flags", [])
                }
            }

            shot_dir = shots_dir / shot_id
            shot_dir.mkdir(parents=True, exist_ok=True)
            self.project._save(shot_dir / "shot.json", shot)

            # Create empty notes file
            notes_path = shot_dir / "notes.md"
            if not notes_path.exists():
                notes_path.write_text(
                    f"# Shot Notes: {shot_id}\n## {shot_data.get('label', '')}\n\n"
                    f"## Version History\n\n### v1.0 — {datetime.now().strftime('%Y-%m-%d')} — ai\n"
                    f"Initial shot generated from cinematographer pass.\n"
                )

            index_entries.append({
                "shot_id": shot_id,
                "label": shot_data.get("label", ""),
                "shot_type": shot_data.get("shot_type", ""),
                "duration_sec": shot_data.get("duration_sec", 0),
                "dialogue_lines": len(shot_data.get("dialogue_lines_covered", [])),
                "status": "generated",
                "storyboard_approved": False,
                "audio_approved": False,
                "built": False,
                "preview_rendered": False,
                "final_rendered": False,
                "world_version": None,
                "flags": shot_data.get("flags", [])
            })

        # Save shot index
        self.project._save(shots_dir / "_index.json", {
            "scene_id": scene_id,
            "total_shots": len(index_entries),
            "shots": index_entries
        })
        print(f"    Saved {len(index_entries)} shot files")


# ------------------------------------------------------------------
# Stage: Storyboard
# ------------------------------------------------------------------

class StoryboardStage(PipelineStage):

    def run(self, chapter_id: str, dry_run: bool = False,
            force: bool = False, progress_callback=None) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Storyboard — {chapter_id}")
        print(f"{'-'*55}")

        # Gather all shots for chapter
        all_shots = []
        for scene_id in self._get_scene_ids(chapter_id):
            try:
                index = self.project.load_shot_index(chapter_id, scene_id)
                for shot_summary in index["shots"]:
                    shot = self.project.load_shot(chapter_id, scene_id, shot_summary["shot_id"])
                    shot["chapter_id"] = chapter_id
                    all_shots.append(shot)
            except FileNotFoundError:
                pass

        if not all_shots:
            print("  No shots found. Run cinematographer pass first.")
            _progress(100, "No shots found")
            return {"status": "no_shots"}

        # Load character visual data for structured image prompts.
        # Start with visual_tags.json, then enrich from full character.json
        # files to get age, signature_props, costume_variants, etc.
        character_visuals = {}
        project_visuals = self.project._path("characters", "visual_tags.json")
        if project_visuals.exists():
            try:
                character_visuals = self.project._load(project_visuals).get("characters", {})
            except Exception:
                pass
        if not character_visuals:
            visuals_path = self.project._path("chapters", chapter_id, "character_visuals.json")
            if visuals_path.exists():
                try:
                    character_visuals = self.project._load(visuals_path).get("characters", {})
                except Exception:
                    pass

        # Enrich with full character data (age, props, costume variants)
        for cid in list(character_visuals.keys()):
            try:
                full_char = self.project.load_character(cid)
                desc = full_char.get("description", {})
                assets = full_char.get("assets", {})
                if desc.get("age"):
                    character_visuals[cid]["age"] = desc["age"]
                if assets.get("signature_props"):
                    character_visuals[cid]["signature_props"] = assets["signature_props"]
                if assets.get("costume_variants"):
                    character_visuals[cid]["costume_variants"] = assets["costume_variants"]
            except (FileNotFoundError, KeyError):
                pass

        # Ensure every visual_tag has explicit gender
        for cid, vis in character_visuals.items():
            tag = vis.get("visual_tag", "")
            try:
                char_data = self.project.load_character(cid)
            except FileNotFoundError:
                char_data = vis
            fixed = _ensure_gender_in_visual_tag(tag, char_data)
            if fixed != tag:
                vis["visual_tag"] = fixed
                print(f"  Gender fix: {cid} → {fixed[:60]}")

        if character_visuals:
            print(f"  Character visuals loaded: {list(character_visuals.keys())}")

        # Load visual style from world bible, then adapt for storyboard:
        # strip photorealistic medium terms, prepend pen/ink/marker medium,
        # keep scene elements (lighting, palette, production design).
        from apis.prompt_builder import adapt_style_for_storyboard
        visual_style = ""
        try:
            wb = self.project.load_world_bible()
            bible = wb.get("world_bible", wb)
            visual_style = bible.get("visual_style", "")
        except FileNotFoundError:
            pass
        visual_style = adapt_style_for_storyboard(visual_style)
        print(f"  Visual style: {visual_style[:100]}...")

        _progress(10, f"Found {len(all_shots)} shots to storyboard")

        # Pick image provider: prefer comfyui (local, free) → stabilityai → google_imagen
        use_comfyui = (
            self.project.is_api_enabled("comfyui")
            and ComfyUIClient.is_available(
                self.project.get_api_config("comfyui").get("url")
            )
        )
        use_stability = self.project.is_api_enabled("stabilityai")
        use_imagen = self.project.is_api_enabled("google_imagen")

        if use_comfyui:
            api_name = "comfyui"
            estimated = 0.0  # Local generation is free
        elif use_stability:
            api_name = "stabilityai"
            estimated = self.costs.estimate_stability(len(all_shots) * 2)
        elif use_imagen:
            api_name = "google_imagen"
            estimated = self.costs.estimate_imagen(len(all_shots) * 2)
        else:
            raise GateLockError("No image generation API enabled (comfyui, stabilityai, or google_imagen)")

        print(f"  Image provider: {api_name}")
        print(f"  Shots to storyboard: {len(all_shots)}")
        print(f"  Estimated cost (16:9 + 9:16): ${estimated:.2f}")

        if dry_run:
            print("  DRY RUN")
            _progress(100, "Dry run complete", estimated)
            return {"status": "dry_run"}

        if api_name != "comfyui":
            # ComfyUI is local — no gate or budget checks needed
            self.costs.check_api_allowed(api_name)
            self.costs.check_budget(api_name, estimated)

        if api_name == "comfyui":
            comfyui_cfg = self.project.get_api_config("comfyui")
            client_cls = lambda: ComfyUIClient(
                base_url=comfyui_cfg.get("url"),
                checkpoint=comfyui_cfg.get("checkpoint"),
            )
        elif api_name == "stabilityai":
            client_cls = StabilityClient
        else:
            client_cls = GoogleImagenClient

        # Load character LoRA data for ComfyUI LoRA-enhanced generation
        character_loras = {}
        if api_name == "comfyui":
            character_loras = self._load_character_loras()
            if character_loras:
                print(f"  Character LoRAs detected: {list(character_loras.keys())}")

        with client_cls() as img_client:
            gen_kwargs = dict(
                shots=all_shots,
                project_root=str(self.project.root),
                include_vertical=True,
                character_visuals=character_visuals,
                visual_style=visual_style,
                force=force,
            )
            # Pass character LoRAs and common seed to ComfyUI client
            if api_name == "comfyui":
                if character_loras:
                    gen_kwargs["character_loras"] = character_loras
                comfyui_cfg = self.project.get_api_config("comfyui")
                common_seed = comfyui_cfg.get("common_seed")
                if common_seed is not None:
                    gen_kwargs["common_seed"] = common_seed
            results = img_client.generate_shot_boards(**gen_kwargs)

        _progress(60, "Updating shot schemas...")
        # Update shot schemas
        generated_count = 0
        for result in results:
            if result["status"] == "generated":
                shot_id = result["shot_id"]
                chapter_id_local = chapter_id
                scene_id = "_".join(shot_id.split("_")[:2])
                try:
                    shot = self.project.load_shot(chapter_id_local, scene_id, shot_id)
                    shot["storyboard"]["generated"] = True
                    shot["storyboard"]["generation_meta"] = {
                        "provider": result.get("provider", api_name),
                        "model": result.get("model", ""),
                        "final_prompt": result.get("final_prompt", ""),
                        "negative_prompt": result.get("negative_prompt"),
                        "visual_style": result.get("visual_style", ""),
                        "generated_at": datetime.now().isoformat(),
                        "cost_usd": result.get("cost_usd", 0.08),
                    }
                    if result.get("seed") is not None:
                        shot["storyboard"]["generation_meta"]["seed"] = result["seed"]
                    if result.get("loras_used"):
                        shot["storyboard"]["generation_meta"]["loras_used"] = result["loras_used"]
                    self.project.save_shot(chapter_id_local, scene_id, shot_id, shot)
                    cost = result.get("cost_usd", 0.0)
                    if cost > 0:  # Skip ledger for free local generation (ComfyUI)
                        self.costs.record(api_name, cost,
                                          "storyboard", f"Storyboard {shot_id}",
                                          entity_id=chapter_id)
                    generated_count += 1
                except FileNotFoundError:
                    pass

        self._git_commit("storyboard", chapter_id,
                         f"storyboard: {chapter_id} {generated_count} images generated")

        self.project.set_pipeline_stage("storyboard")
        print(f"\n  [OK] {generated_count}/{len(all_shots)} storyboards generated")
        _progress(100, f"Storyboard complete: {generated_count} images")
        return {"status": "complete", "generated": generated_count}

    def _load_character_loras(self) -> dict:
        """
        Load character LoRA configurations from character.json files.

        Scans all characters for assets.lora data. Returns dict of:
          { char_id: { file, safetensors_name, trigger_word, weight } }

        Only includes characters whose LoRA safetensors file exists.
        """
        loras = {}
        default_weight = (
            self.project.get_api_config("comfyui")
            .get("lora_default_weight", 0.8)
        )

        for cid in self.project.get_all_character_ids():
            try:
                char_data = self.project.load_character(cid)
            except FileNotFoundError:
                continue

            lora_info = char_data.get("assets", {}).get("lora", {})
            lora_file = lora_info.get("file", "")
            safetensors_name = lora_info.get("safetensors_name", "")

            if not lora_file and not safetensors_name:
                continue

            # Check if the LoRA safetensors file exists on disk
            if lora_file:
                lora_path = self.project._path(lora_file)
                if not lora_path.exists():
                    continue

            loras[cid] = {
                "file": safetensors_name or Path(lora_file).name,
                "safetensors_name": safetensors_name or Path(lora_file).name,
                "trigger_word": lora_info.get("trigger_word", f"{cid}_char"),
                "weight": lora_info.get("weight", default_weight),
            }

        return loras


# ------------------------------------------------------------------
# Stage: Audio
# ------------------------------------------------------------------

class VoiceRecordingStage(PipelineStage):

    def run(self, chapter_id: str, scene_id: Optional[str] = None,
            dry_run: bool = False, progress_callback=None) -> dict:
        """
        Generate audio for dialogue lines from a chapter's screenplay.
        Recordings are independent of shots — stored per screenplay line at
        audio/{chapter_id}/lines/{line_id}.mp3 with a recordings.json manifest.
        """
        import hashlib

        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Voice Recording — {chapter_id}")
        print(f"{'-'*55}")

        _progress(5, "Loading context...")

        # Build character map (gracefully handle missing index)
        character_map = {}
        try:
            for cid in self.project.get_all_character_ids():
                try:
                    character_map[cid] = self.project.load_character(cid)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            print("  [INFO] No character index found — using chapter character names")

        # Parse dialogue directly from screenplay (no shots needed)
        screenplay_path = self.project._path("chapters", chapter_id, "screenplay.md")
        if not screenplay_path.exists():
            raise FileNotFoundError(f"Screenplay not found: {screenplay_path}")

        screenplay_lines = self._parse_screenplay_dialogue(
            screenplay_path.read_text(encoding="utf-8")
        )
        print(f"  Parsed {len(screenplay_lines)} dialogue lines from screenplay")

        # Build previous_text / next_text context for emotional delivery
        for idx, dl in enumerate(screenplay_lines):
            parts = []
            if dl.get("direction"):
                parts.append(f"[Direction: {dl['direction']}]")
            if dl.get("preceding_action"):
                parts.append(dl["preceding_action"])
            if idx > 0:
                parts.append(screenplay_lines[idx - 1]["text"])
            dl["previous_text"] = " ".join(parts)[:500] if parts else ""

            if idx < len(screenplay_lines) - 1:
                dl["next_text"] = screenplay_lines[idx + 1]["text"][:500]
            else:
                dl["next_text"] = ""

        if not screenplay_lines:
            print("  No dialogue found in screenplay — chapter is silent/narration-only.")
            _progress(100, "No dialogue in screenplay")
            return {"status": "nothing_to_do", "reason": "no_dialogue_in_screenplay"}

        # Build pending_lines directly from screenplay — no shot matching needed
        pending_lines = []
        for i, dl in enumerate(screenplay_lines):
            line_id = f"{chapter_id}_line{str(i + 1).zfill(3)}"
            pending_lines.append({
                "line_id": line_id,
                "character_id": dl["character_id"],
                "text": dl["text"],
                "audio_ref": f"audio/{chapter_id}/lines/{line_id}.mp3",
                "direction": dl.get("direction", ""),
                "previous_text": dl.get("previous_text", ""),
                "next_text": dl.get("next_text", ""),
            })

        # Auto-create stub characters for speakers not in the character index
        speaking_ids = {l["character_id"] for l in pending_lines}
        missing_ids = speaking_ids - set(character_map.keys())
        if missing_ids:
            print(f"  [AUTO] Creating stub profiles for {len(missing_ids)} missing speaker(s): {sorted(missing_ids)}")
            try:
                index = self.project.load_character_index()
            except FileNotFoundError:
                index = {"characters": [], "background_types": [], "total_named": 0,
                         "total_background_types": 0}
            for cid in sorted(missing_ids):
                display = cid.replace("_", " ").title()
                stub = {
                    "character_id": cid,
                    "display_name": display,
                    "description": {"role": "Speaking character (auto-created from screenplay)"},
                    "visual_tag": "",
                    "costume_default": "",
                    "narrative": {"chapters": [chapter_id]},
                    "voice": {},
                    "assets": {},
                }
                self.project.save_character(cid, stub)
                character_map[cid] = stub
                if not any(c["character_id"] == cid for c in index["characters"]):
                    index["characters"].append({
                        "character_id": cid,
                        "display_name": display,
                        "role": "Speaking character (auto-created)",
                        "visual_tag": "",
                    })
                print(f"    + {cid} ({display})")
            index["total_named"] = len(index["characters"])
            index["last_updated"] = datetime.now().isoformat()
            self.project._save(self.project._path("characters", "_index.json"), index)
            _progress(12, f"Created {len(missing_ids)} missing character(s): {', '.join(sorted(missing_ids))}")

        _progress(15, f"Found {len(pending_lines)} lines to generate")
        estimated = self.costs.estimate_elevenlabs(
            " ".join(l["text"] for l in pending_lines)
        )
        print(f"  Lines to generate: {len(pending_lines)}")
        print(f"  Estimated cost: ${estimated:.4f}")

        if dry_run:
            print("  DRY RUN")
            _progress(100, "Dry run complete", estimated)
            return {"status": "dry_run", "lines": len(pending_lines)}

        # Gate and budget checks only for real runs (after dry_run bail-out)
        self.costs.check_api_allowed("elevenlabs", required_gate="screenplay_to_voice_recording")
        self.costs.check_budget("elevenlabs", estimated)
        _progress(20, "Generating audio...")

        with ElevenLabsClient() as el:
            results = el.generate_line_batch(
                lines=pending_lines,
                character_map=character_map,
                project_root=str(self.project.root),
                dry_run=dry_run
            )

        # Build and save recordings.json manifest
        recordings = []
        for result in results:
            if result["status"] != "generated":
                continue
            line = next((l for l in pending_lines if l["line_id"] == result["line_id"]), None)
            if line:
                recordings.append({
                    "recording_id": result["line_id"],
                    "character_id": line["character_id"],
                    "text": line["text"],
                    "text_hash": hashlib.md5(line["text"].encode()).hexdigest(),
                    "audio_ref": line["audio_ref"],
                    "duration_sec": result.get("estimated_duration_sec", 0),
                    "direction": line.get("direction", ""),
                    "recorded_at": datetime.now().isoformat(),
                })
                self.costs.record("elevenlabs", result.get("cost_usd", 0),
                                  "audio", f"Audio {result['line_id']}",
                                  entity_id=chapter_id)

        self.project.save_recordings(chapter_id, {
            "chapter_id": chapter_id,
            "recordings": recordings,
        })
        print(f"  Saved recordings.json with {len(recordings)} entries")

        # Update chapter audio count
        chapter = self.project.load_chapter(chapter_id)
        chapter.setdefault("production", {})["audio_lines_total"] = len(pending_lines)
        chapter["production"]["audio_lines_generated"] = len(recordings)
        self.project.save_chapter(chapter_id, chapter)

        generated = len(recordings)
        skipped = [r for r in results if r["status"] == "skipped"]
        self._git_commit("voice_recording", chapter_id,
                         f"voice_recording: {chapter_id} {generated} lines generated")

        self.project.set_pipeline_stage("voice_recording")
        print(f"\n  [OK] {generated}/{len(pending_lines)} audio lines generated")

        # Report skipped lines with reasons
        if skipped:
            skip_reasons = {}
            for r in skipped:
                reason = r.get("reason", "unknown")
                skip_reasons.setdefault(reason, []).append(r["line_id"])
            for reason, line_ids in skip_reasons.items():
                char_ids = set()
                for lid in line_ids:
                    line = next((l for l in pending_lines if l["line_id"] == lid), None)
                    if line:
                        char_ids.add(line["character_id"])
                print(f"  [SKIP] {len(line_ids)} lines skipped ({reason}): characters {sorted(char_ids)}")

            skip_chars = set()
            for r in skipped:
                line = next((l for l in pending_lines if l["line_id"] == r["line_id"]), None)
                if line:
                    skip_chars.add(line["character_id"])
            skip_msg = f" | {len(skipped)} skipped (no voice: {', '.join(sorted(skip_chars))})"
            _progress(100, f"Audio: {generated} generated{skip_msg}")
        else:
            _progress(100, f"Audio complete: {generated} lines")

        return {"status": "complete", "generated": generated,
                "skipped": len(skipped),
                "skipped_characters": sorted(set(
                    next((l["character_id"] for l in pending_lines if l["line_id"] == r["line_id"]), "")
                    for r in skipped
                )) if skipped else []}

    @staticmethod
    def _parse_screenplay_dialogue(text: str) -> list:
        """Parse screenplay text and extract dialogue lines.

        Screenplay format (plain or markdown bold):
            CHARACTER_NAME          or   **CHARACTER_NAME**
            (optional parenthetical)
            Dialogue text that may span
            multiple lines...

        Returns list of dicts with keys:
            character_id, text, direction, preceding_action
        """
        import re
        lines = text.split("\n")
        dialogue = []
        i = 0
        last_action = ""
        # Pattern: ALL CAPS name, optionally followed by (CONT'D), (V.O.), (O.S.)
        char_pattern = re.compile(
            r'^([A-Z][A-Z\s\'.\d]+?)\s*(?:\(CONT\'?D\)|\(V\.O\.\)|\(O\.S\.\))?\s*$'
        )

        while i < len(lines):
            line = lines[i].rstrip()

            # Skip empty lines and markdown headings
            if not line or line.startswith("#"):
                i += 1
                continue

            # Strip markdown bold markers: **CHARACTER** → CHARACTER
            clean = line.strip()
            if clean.startswith("**") and clean.endswith("**"):
                clean = clean[2:-2].strip()

            # Skip scene headings and transitions (plain or bold)
            if clean.startswith("FADE") or clean.startswith("CUT TO"):
                i += 1
                continue

            # Check for character name (ALL CAPS, possibly with CONT'D/V.O./O.S.)
            match = char_pattern.match(clean)
            if match:
                raw_name = match.group(1).strip()
                # Clean character_id: lowercase, underscores
                character_id = raw_name.lower().replace(" ", "_").replace("'", "")
                i += 1

                # Capture optional parenthetical like (gloomily)
                direction = ""
                if i < len(lines) and lines[i].strip().startswith("("):
                    paren = lines[i].strip()
                    direction = paren.strip("()").strip()
                    i += 1

                # Collect dialogue text (non-empty lines until next blank or heading)
                text_parts = []
                while i < len(lines) and lines[i].strip():
                    dl = lines[i].strip()
                    # Strip bold for character-cue and heading detection
                    dl_clean = dl[2:-2].strip() if dl.startswith("**") and dl.endswith("**") else dl
                    # Stop if we hit another character name or scene heading
                    if char_pattern.match(dl_clean) or dl_clean.startswith("INT.") \
                            or dl_clean.startswith("EXT.") or dl_clean.startswith("FADE"):
                        break
                    text_parts.append(dl)
                    i += 1

                if text_parts:
                    dialogue.append({
                        "character_id": character_id,
                        "text": " ".join(text_parts),
                        "direction": direction,
                        "preceding_action": last_action,
                    })
                    last_action = ""
            else:
                # Track narrative/action lines for emotional context
                stripped = line.strip()
                if stripped and not stripped.startswith("EXT.") \
                        and not stripped.startswith("INT."):
                    last_action = stripped
                i += 1

        return dialogue


# ------------------------------------------------------------------
# Stage: Sound FX
# ------------------------------------------------------------------

class SoundFXStage(PipelineStage):
    """Generate sound effects for enabled shots using ElevenLabs.

    Uses Claude to suggest SFX per shot based on label and cinematic
    context, then generates them via the ElevenLabs sound generation API.
    Gate: cut_to_sound must be approved.
    """

    def run(self, chapter_id: str, scene_id: Optional[str] = None,
            dry_run: bool = False, progress_callback=None) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Sound FX — {chapter_id}")
        print(f"{'-'*55}")

        _progress(5, "Loading shots...")

        # Gather enabled shots
        scene_ids = [scene_id] if scene_id else self._get_scene_ids(chapter_id)
        shots_to_process = []
        for sid in scene_ids:
            try:
                index = self.project.load_shot_index(chapter_id, sid)
                for shot_summary in index.get("shots", []):
                    shot_id = shot_summary["shot_id"]
                    try:
                        shot = self.project.load_shot(chapter_id, sid, shot_id)
                        # Skip disabled shots from the editing room cut
                        if not shot.get("edit", {}).get("enabled", True):
                            print(f"  [SKIP] {shot_id} — disabled in editing room")
                            continue
                        shots_to_process.append((sid, shot_id, shot))
                    except FileNotFoundError:
                        continue
            except FileNotFoundError:
                continue

        if not shots_to_process:
            _progress(100, "No enabled shots found")
            return {"status": "complete", "generated": 0}

        _progress(10, f"Analyzing {len(shots_to_process)} shots for SFX...")

        # Claude pass: suggest SFX for each shot. Stream per-shot
        # progress from 10% → 25% across the suggestion loop so the UI
        # doesn't appear stuck at 10% during the minutes of sequential
        # Claude calls.
        claude = ClaudeClient()
        sfx_suggestions = []
        shot_count = max(1, len(shots_to_process))
        for loop_i, (sid, shot_id, shot) in enumerate(shots_to_process):
            pct = 10 + int(15 * loop_i / shot_count)
            _progress(pct, f"Analyzing {loop_i+1}/{shot_count}: {shot_id}")

            existing_sfx = shot.get("audio", {}).get("sound_effects", [])
            if existing_sfx:
                print(f"  [CACHE] {shot_id} — already has {len(existing_sfx)} SFX")
                continue

            label = shot.get("label", "")
            cin = shot.get("cinematic", {})
            movement = cin.get("camera_movement", {})
            sound_design = shot.get("audio", {}).get("sound_design", [])

            # World + scene context keeps Claude's suggestions period-
            # appropriate (otherwise it defaults to modern interiors).
            from apis.prompt_builder import build_sfx_context_block
            context_block = build_sfx_context_block(
                self.project, chapter_id, shot=shot
            )

            # Build prompt for Claude to suggest SFX
            prompt_parts = []
            if context_block:
                prompt_parts.append(context_block)
            prompt_parts.append(
                f"Shot: {shot_id}\n"
                f"Label: {label}\n"
                f"Shot type: {cin.get('shot_type', 'unknown')}\n"
                f"Camera movement: {movement.get('type', 'static')}\n"
                f"Existing sound_design hints: {sound_design}"
            )
            prompt_parts.append(
                "Suggest 1-3 sound effects for this shot. Each prompt must be "
                "a SHORT, CONCRETE foley description — ideally 3-8 words "
                "naming ONE specific sound source and surface (e.g., "
                "'sandals scuffing on dry dirt', 'donkey braying distant', "
                "'cloth robe rustle', 'marketplace chatter'). "
                "DO NOT write flowery scene descriptions or stack multiple "
                "sounds in one prompt. Each prompt generates exactly one "
                "sound — keep it physical and specific. Avoid modern/"
                "electronic sources. No dialogue, no voices speaking words, "
                "no music.\n\n"
                "For each SFX also decide offset_sec — how many seconds "
                "INTO the shot the sound should start. 0 for sounds that "
                "fire at the opening beat or continuous ambience; small "
                "positive (0.5-2.0) for sounds that arrive slightly later. "
                "Stagger offsets when multiple sounds would overlap.\n\n"
                "Return JSON array: "
                "[{\"prompt\": \"...\", \"duration_sec\": N, \"offset_sec\": N}]"
            )
            prompt = "\n\n".join(prompt_parts)

            try:
                parsed = claude._call_json(
                    system=(
                        "You are a film sound designer. For each shot, suggest "
                        "1-3 concise SFX generation prompts."
                    ),
                    user=prompt,
                    max_tokens=300,
                )
                # _call_json returns whatever json.loads produces — bare array
                # or an object wrapping one. Handle both.
                suggestions = (
                    parsed if isinstance(parsed, list)
                    else parsed.get("sfx") or parsed.get("effects") or []
                )
                for s in suggestions:
                    if not isinstance(s, dict):
                        continue
                    sfx_suggestions.append({
                        "scene_id": sid,
                        "shot_id": shot_id,
                        "prompt": s.get("prompt", ""),
                        "duration_sec": s.get("duration_sec", 5),
                        "offset_sec": float(s.get("offset_sec", 0) or 0),
                    })
            except Exception as e:
                print(f"  [WARN] Claude SFX suggestion failed for {shot_id}: {e}")

        # Provider routing: ComfyUI (free, local) for broadband foley,
        # ElevenLabs ($0.10/clip) for tonal instruments and voiced SFX.
        # The default can be overridden per project via SFX_PROVIDER env
        # var; set to 'elevenlabs' or 'comfyui' to force, or 'auto' for
        # keyword-based routing.
        from apis.sfx_router import route as route_sfx
        provider_override = os.getenv("SFX_PROVIDER", "auto").strip().lower()
        force = provider_override if provider_override in ("comfyui", "elevenlabs") else None
        for sfx in sfx_suggestions:
            sfx["provider"] = route_sfx(sfx["prompt"], force_provider=force)

        el_count = sum(1 for s in sfx_suggestions if s["provider"] == "elevenlabs")
        cu_count = sum(1 for s in sfx_suggestions if s["provider"] == "comfyui")
        estimated_cost = el_count * 0.10
        print(f"\n  SFX to generate: {len(sfx_suggestions)} "
              f"(ComfyUI: {cu_count}, ElevenLabs: {el_count})")
        print(f"  Estimated cost: ${estimated_cost:.2f}")

        if dry_run:
            print("  DRY RUN — no API calls made")
            for s in sfx_suggestions:
                print(f"    {s['shot_id']}: {s['prompt'][:60]}")
            _progress(100, f"Dry run: {len(sfx_suggestions)} SFX estimated, ${estimated_cost:.2f}")
            return {"status": "dry_run", "sfx_count": len(sfx_suggestions),
                    "estimated_cost": estimated_cost}

        # Gate + budget check — only for the ElevenLabs slice of the run.
        if el_count > 0:
            self.costs.check_api_allowed("elevenlabs", required_gate="cut_to_sound")
            self.costs.check_budget("elevenlabs", estimated_cost)

        _progress(30, f"Generating {len(sfx_suggestions)} sound effects...")

        # Generate SFX — lazy-init each client so we don't connect to a
        # provider we won't use.
        from apis.comfyui_audio import ComfyUIAudioClient
        el = None
        cu = None
        generated = 0
        total_cost = 0.0

        for i, sfx in enumerate(sfx_suggestions):
            output_path = str(self.project._path(
                "audio", chapter_id, sfx["shot_id"],
                f"sfx_{i:03d}.mp3"
            ))

            try:
                if sfx["provider"] == "elevenlabs":
                    if el is None:
                        el = ElevenLabsClient()
                    result = el.generate_sound_effect(
                        prompt=sfx["prompt"],
                        duration_sec=sfx["duration_sec"],
                        output_path=output_path,
                    )
                    cost = result.get("cost_usd", 0.10)
                    self.costs.record("elevenlabs", cost, "sound_fx",
                                      f"SFX: {sfx['prompt'][:40]}",
                                      sfx["shot_id"])
                else:
                    if cu is None:
                        cu = ComfyUIAudioClient()
                    result = cu.generate_sound_effect(
                        prompt=sfx["prompt"],
                        duration_sec=sfx["duration_sec"],
                        output_path=output_path,
                    )
                    cost = 0.0
                total_cost += cost
                generated += 1

                # Update shot schema
                shot = self.project.load_shot(chapter_id, sfx["scene_id"], sfx["shot_id"])
                audio = shot.setdefault("audio", {})
                effects = audio.setdefault("sound_effects", [])
                effects.append({
                    "sfx_id": f"{sfx['shot_id']}_sfx_{i:03d}",
                    "prompt": sfx["prompt"],
                    "audio_ref": f"audio/{chapter_id}/{sfx['shot_id']}/sfx_{i:03d}.mp3",
                    "duration_sec": sfx["duration_sec"],
                    "offset_sec": float(sfx.get("offset_sec", 0) or 0),
                    "provider": sfx["provider"],
                    "generated_at": datetime.now().isoformat(),
                })
                self.project.save_shot(chapter_id, sfx["scene_id"], sfx["shot_id"], shot)

                pct = 30 + int(60 * (i + 1) / len(sfx_suggestions))
                _progress(pct, f"SFX {i+1}/{len(sfx_suggestions)}: {sfx['prompt'][:40]}", cost)
                print(f"  [{i+1}/{len(sfx_suggestions)}] {sfx['shot_id']}: {sfx['prompt'][:50]}")

            except Exception as e:
                print(f"  [FAIL] {sfx['shot_id']}: {e}")

        self._git_commit("sound_fx", chapter_id,
                         f"sound_fx: {chapter_id} {generated} effects generated")
        self.project.set_pipeline_stage("sound_fx")
        _progress(100, f"Sound FX: {generated} generated, ${total_cost:.2f}")

        return {"status": "complete", "generated": generated,
                "cost": total_cost}


# ------------------------------------------------------------------
# Stage: Audio Score
# ------------------------------------------------------------------

class AudioScoreStage(PipelineStage):
    """Generate a library of music pieces for a chapter.

    Uses Claude to analyze the chapter's emotional arc and suggest
    music pieces, then generates them via ElevenLabs.
    Gate: cut_to_sound must be approved.
    """

    def run(self, chapter_id: str, dry_run: bool = False,
            progress_callback=None) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Audio Score — {chapter_id}")
        print(f"{'-'*55}")

        _progress(5, "Loading chapter context...")

        # Load chapter context
        chapter = self.project.load_chapter(chapter_id)
        title = chapter.get("title", chapter_id)

        # Load screenplay for emotional analysis
        try:
            screenplay = self.project.load_screenplay(chapter_id)
        except (FileNotFoundError, AttributeError):
            screenplay = ""

        # Check for existing score manifest
        try:
            existing = self.project._load(
                self.project._path("chapters", chapter_id, "score_manifest.json")
            )
            if existing.get("pieces"):
                print(f"  Score manifest already exists with {len(existing['pieces'])} pieces")
                _progress(100, f"Score already generated: {len(existing['pieces'])} pieces")
                return {"status": "cached", "pieces": len(existing["pieces"])}
        except FileNotFoundError:
            pass

        _progress(10, "Analyzing emotional arc...")

        # Claude pass: analyze chapter and suggest music pieces
        claude = ClaudeClient()
        prompt = (
            f"Chapter: {title}\n\n"
            f"Screenplay excerpt:\n{screenplay[:3000] if isinstance(screenplay, str) else ''}\n\n"
            "Analyze the emotional arc of this chapter and suggest 3-6 music pieces "
            "that could underscore different scenes. For each piece provide:\n"
            "- piece_id: kebab-case identifier\n"
            "- title: evocative name\n"
            "- mood: emotional quality\n"
            "- tempo: slow/medium/fast\n"
            "- instruments: suggested instrumentation\n"
            "- prompt: a detailed text prompt for AI music generation (describe the "
            "  sound, mood, instruments, tempo in a way suitable for an AI generator)\n"
            "- duration_sec: suggested duration (15-30 seconds)\n"
            "- suggested_scenes: which scene IDs this could underscore\n\n"
            "Return JSON: {\"pieces\": [...]}"
        )

        pieces = []
        try:
            parsed = claude._call_json(
                system=(
                    "You are a film composer analyzing a chapter to plan an "
                    "underscore. Suggest distinct music pieces."
                ),
                user=prompt,
                max_tokens=1500,
            )
            if isinstance(parsed, list):
                pieces = parsed
            elif isinstance(parsed, dict):
                pieces = parsed.get("pieces") or []
        except Exception as e:
            print(f"  [WARN] Claude music analysis failed: {e}")
            _progress(100, "Failed to analyze chapter for score")
            return {"status": "failed", "error": str(e)}

        if not pieces:
            print("  No music pieces suggested")
            _progress(100, "No music pieces suggested")
            return {"status": "complete", "generated": 0}

        # Cost estimation
        estimated_cost = len(pieces) * 0.15
        print(f"\n  Music pieces to generate: {len(pieces)}")
        print(f"  Estimated cost: ${estimated_cost:.2f}")

        for p in pieces:
            print(f"    {p.get('piece_id', '?')}: {p.get('title', '?')} ({p.get('mood', '?')})")

        if dry_run:
            print("  DRY RUN — no API calls made")
            _progress(100, f"Dry run: {len(pieces)} pieces estimated, ${estimated_cost:.2f}")
            return {"status": "dry_run", "pieces": len(pieces),
                    "estimated_cost": estimated_cost}

        # Gate + budget check
        self.costs.check_api_allowed("elevenlabs", required_gate="cut_to_sound")
        self.costs.check_budget("elevenlabs", estimated_cost)

        _progress(30, f"Generating {len(pieces)} music pieces...")

        # Generate music
        el = ElevenLabsClient()
        generated = 0
        total_cost = 0.0
        manifest_pieces = []

        for i, piece in enumerate(pieces):
            piece_id = piece.get("piece_id", f"piece_{i:02d}")
            output_path = str(self.project._path(
                "audio", chapter_id, "score", f"{piece_id}.mp3"
            ))

            try:
                result = el.generate_music(
                    prompt=piece.get("prompt", piece.get("title", "")),
                    duration_sec=piece.get("duration_sec", 30),
                    output_path=output_path,
                )
                cost = result.get("cost_usd", 0.15)
                total_cost += cost
                self.costs.record("elevenlabs", cost, "audio_score",
                                  f"Score: {piece.get('title', piece_id)[:40]}",
                                  piece_id)
                generated += 1

                manifest_pieces.append({
                    "piece_id": piece_id,
                    "title": piece.get("title", ""),
                    "mood": piece.get("mood", ""),
                    "tempo": piece.get("tempo", ""),
                    "instruments": piece.get("instruments", ""),
                    "prompt": piece.get("prompt", ""),
                    "duration_sec": piece.get("duration_sec", 30),
                    "audio_ref": f"audio/{chapter_id}/score/{piece_id}.mp3",
                    "suggested_scenes": piece.get("suggested_scenes", []),
                    "generated_at": datetime.now().isoformat(),
                })

                pct = 30 + int(60 * (i + 1) / len(pieces))
                _progress(pct, f"Score {i+1}/{len(pieces)}: {piece.get('title', '')[:40]}", cost)
                print(f"  [{i+1}/{len(pieces)}] {piece_id}: {piece.get('title', '')}")

            except Exception as e:
                print(f"  [FAIL] {piece_id}: {e}")

        # Save score manifest
        manifest = {"pieces": manifest_pieces}
        self.project._save(
            self.project._path("chapters", chapter_id, "score_manifest.json"),
            manifest
        )

        self._git_commit("audio_score", chapter_id,
                         f"audio_score: {chapter_id} {generated} pieces generated")
        self.project.set_pipeline_stage("audio_score")
        _progress(100, f"Score: {generated} pieces, ${total_cost:.2f}")

        return {"status": "complete", "generated": generated,
                "cost": total_cost}


# ------------------------------------------------------------------
# Stage: Asset Manifest Builder
# ------------------------------------------------------------------

class AssetManifestStage(PipelineStage):

    def run(self, chapter_id: Optional[str] = None, dry_run: bool = False,
            progress_callback=None) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        print(f"\n{'-'*55}")
        print(f"  STAGE: Asset Manifest Builder")
        print(f"{'-'*55}")

        world_bible = self.project.load_world_bible()

        # Load existing manifest or start fresh
        try:
            manifest = self.project.load_asset_manifest()
        except FileNotFoundError:
            manifest = {
                "$schema": "babylon-studio/schemas/asset-manifest/v1",
                "project_id": self.project.id,
                "assets": {"environments": [], "props": [], "costumes": [], "vegetation": []},
                "deduplication_log": {"entries": []},
                "generation_batches": []
            }

        # Collect all shots
        chapter_ids = [chapter_id] if chapter_id else self.project.get_all_chapter_ids()
        all_shots = []
        for cid in chapter_ids:
            for sid in self._get_scene_ids(cid):
                try:
                    index = self.project.load_shot_index(cid, sid)
                    for shot_s in index["shots"]:
                        shot = self.project.load_shot(cid, sid, shot_s["shot_id"])
                        all_shots.append(shot)
                except FileNotFoundError:
                    pass

        _progress(15, f"Scanning {len(all_shots)} shots...")
        print(f"  Scanning {len(all_shots)} shots for asset requirements...")
        estimated = self.costs.estimate_claude(input_tokens=3000, output_tokens=4000)
        print(f"  Estimated Claude cost: ${estimated:.4f}")

        if dry_run:
            print("  DRY RUN")
            _progress(100, "Dry run complete", estimated)
            return {"status": "dry_run"}

        self.costs.check_api_allowed("claude")
        self.costs.check_budget("claude", estimated)

        _progress(25, "Building asset entries with Claude...")
        with ClaudeClient() as claude:
            new_assets = claude.build_asset_entries(
                shots=all_shots,
                existing_manifest=manifest,
                world_bible=world_bible,
                chapter_id=chapter_id or "all"
            )
            cost = new_assets.pop("_cost_usd", estimated)
            self.costs.record("claude", cost, "asset_manifest",
                              "Asset manifest build pass")

        # Merge new assets into manifest
        for category in ["environments", "props", "costumes", "vegetation"]:
            new = new_assets.get("new_assets", {}).get(category, [])
            manifest["assets"].setdefault(category, []).extend(new)
            if new:
                print(f"  + {len(new)} new {category}")

        dedup = new_assets.get("deduplication_log", [])
        manifest["deduplication_log"]["entries"].extend(dedup)
        if dedup:
            print(f"  [OK] {len(dedup)} assets deduplicated")

        # Recalculate summary
        total = sum(
            len(manifest["assets"].get(cat, []))
            for cat in ["environments", "props", "costumes", "vegetation"]
        )
        manifest["summary"] = {
            "total_assets": total,
            "generated": sum(
                1 for cat in ["environments", "props", "costumes", "vegetation"]
                for a in manifest["assets"].get(cat, [])
                if a.get("meshy", {}).get("status") == "completed"
            ),
            "pending": sum(
                1 for cat in ["environments", "props", "costumes", "vegetation"]
                for a in manifest["assets"].get(cat, [])
                if a.get("meshy", {}).get("status") == "pending"
            )
        }

        self.project.save_asset_manifest(manifest)

        self._git_commit("assets", "manifest",
                         f"assets: manifest updated, {total} total assets")

        self.project.set_pipeline_stage("asset_manifest")
        print(f"\n  [OK] Manifest updated: {total} total assets")
        print(f"  Review manifest and set approved_for_generation=true on priority assets.")
        print(f"  Then run: approve-gate sound_to_assets")
        _progress(100, f"Asset manifest complete: {total} assets")
        return {"status": "complete", "total_assets": total}


class PropsAndStagingStage(PipelineStage):
    """
    Props & Staging — binds assets to shots, selects costumes per scene,
    builds per-shot prop lists, and updates build_status.

    Runs between asset_manifest and mesh_generation.
    """

    def run(self, chapter_id: str = None, dry_run: bool = False,
            progress_callback=None) -> dict:
        raise NotImplementedError(
            "Props & Staging stage is planned but not yet implemented. "
            "It will bind generated assets to shots, select costumes per scene, "
            "and build per-shot prop lists."
        )


# ------------------------------------------------------------------
# Stage: Preview Video (Wan 2.1 + InfiniTalk)
# ------------------------------------------------------------------

class PreviewVideoStage(PipelineStage):
    """Render a talking-storyboard mp4 for one shot (or all shots in
    a chapter) using the local ComfyUI + Wan 2.1 + InfiniTalk stack.
    Free per clip, runs on the host GPU, ~5-15s of wall time per
    second of output on a 4090.

    Inputs (via ``run(...)`` kwargs):
        shot_id      - "ch01_sc01_sh006" — single-shot mode
        chapter_id   - "ch01" — batch mode, iterates all shots with a
                       storyboard. Mutually exclusive with shot_id.
        orientation  - "horizontal" (default), "vertical", or "both"
        force        - batch only; regenerate shots that already have
                       the target orientation's preview mp4
        seed         - int, optional; random if omitted (single only)

    Writes per rendered shot:
        chapters/<ch>/shots/<shot_id>/preview.mp4       (horizontal)
        chapters/<ch>/shots/<shot_id>/preview_vertical.mp4
        shot.preview.video_ref / video_ref_vertical
    """

    def run(self, shot_id: Optional[str] = None,
            chapter_id: Optional[str] = None,
            orientation: str = "horizontal",
            force: bool = False,
            seed: Optional[int] = None,
            dry_run: bool = False, progress_callback=None, **_: object) -> dict:
        def _progress(pct, msg, cost=0.0):
            if progress_callback:
                progress_callback(pct, msg, cost)

        if orientation not in ("horizontal", "vertical", "both"):
            raise ValueError("orientation must be 'horizontal', 'vertical', or 'both'")

        if shot_id and chapter_id:
            raise ValueError("pass either shot_id or chapter_id, not both")
        if not shot_id and not chapter_id:
            raise ValueError("shot_id or chapter_id is required")

        orientations = (["horizontal", "vertical"]
                        if orientation == "both" else [orientation])

        # ---- Batch (chapter) mode ----
        if chapter_id:
            return self._run_batch(
                chapter_id=chapter_id,
                orientations=orientations,
                force=force,
                dry_run=dry_run,
                _progress=_progress,
            )

        # ---- Single-shot mode ----
        results = []
        for i, orient in enumerate(orientations):
            total = len(orientations)
            base = int(100 * i / total)
            def _single_progress(pct, msg, cost=0.0, _b=base, _t=total):
                _progress(_b + int(pct / total), msg, cost)
            results.append(self._render_single(
                shot_id=shot_id,
                orientation=orient,
                seed=seed,
                dry_run=dry_run,
                _progress=_single_progress,
            ))
        return results[0] if len(results) == 1 else {
            "status": "complete", "shot_id": shot_id, "renders": results,
        }

    # ------------------------------------------------------------------
    # Batch driver
    # ------------------------------------------------------------------

    def _run_batch(self, *, chapter_id: str, orientations: list[str],
                   force: bool, dry_run: bool, _progress) -> dict:
        print(f"\n{'-'*55}")
        print(f"  STAGE: Preview Video (batch) — {chapter_id} "
              f"orientations={orientations} force={force}")
        print(f"{'-'*55}")

        # Gather every shot in the chapter that has a storyboard for at
        # least one requested orientation.
        shots_dir = self.project._path("chapters", chapter_id, "shots")
        if not shots_dir.exists():
            raise FileNotFoundError(f"Chapter dir missing: {shots_dir}")

        tasks: list[tuple[str, str]] = []  # (shot_id, orientation)
        skipped_existing = 0
        skipped_no_storyboard = 0
        for sd in sorted(shots_dir.iterdir()):
            if not sd.is_dir():
                continue
            shot_id = sd.name
            shot_path = sd / "shot.json"
            if not shot_path.exists():
                continue
            try:
                shot = self.project.load_shot(chapter_id, "_".join(shot_id.split("_")[:2]), shot_id)
            except FileNotFoundError:
                continue
            preview_refs = shot.get("preview", {}) or {}
            for orient in orientations:
                story_name = ("storyboard.png" if orient == "horizontal"
                              else "storyboard_vertical.png")
                if not (sd / story_name).exists():
                    skipped_no_storyboard += 1
                    continue
                ref_key = "video_ref" if orient == "horizontal" else "video_ref_vertical"
                if not force and preview_refs.get(ref_key):
                    # Already rendered — skip unless force
                    skipped_existing += 1
                    continue
                tasks.append((shot_id, orient))

        total = len(tasks)
        print(f"  {total} render(s) queued. "
              f"Skipped: {skipped_existing} already-rendered, "
              f"{skipped_no_storyboard} no-storyboard.")

        if dry_run:
            _progress(100, f"Dry run: would render {total} clip(s)")
            return {
                "status": "dry_run",
                "chapter_id": chapter_id,
                "to_render": total,
                "skipped_existing": skipped_existing,
                "skipped_no_storyboard": skipped_no_storyboard,
                "tasks": [
                    {"shot_id": sid, "orientation": o} for sid, o in tasks
                ],
            }

        rendered = 0
        failed: list[dict] = []
        results: list[dict] = []
        for i, (shot_id, orient) in enumerate(tasks):
            prefix = int(100 * i / max(total, 1))
            span = int(100 / max(total, 1))
            def _per_shot_progress(pct, msg, cost=0.0, _b=prefix, _s=span, _sid=shot_id):
                _progress(_b + int(pct * _s / 100),
                          f"[{i+1}/{total}] {_sid} {orient}: {msg}", cost)
            try:
                res = self._render_single(
                    shot_id=shot_id, orientation=orient, seed=None,
                    dry_run=False, _progress=_per_shot_progress,
                )
                results.append(res)
                rendered += 1
            except Exception as e:  # noqa: BLE001
                print(f"  [FAIL] {shot_id} {orient}: {e}")
                failed.append({"shot_id": shot_id, "orientation": orient,
                               "error": str(e)})

        _progress(100, f"Batch done: rendered {rendered}/{total}, "
                       f"failed {len(failed)}")
        return {
            "status": "complete",
            "chapter_id": chapter_id,
            "rendered": rendered,
            "failed": failed,
            "skipped_existing": skipped_existing,
            "skipped_no_storyboard": skipped_no_storyboard,
        }

    # ------------------------------------------------------------------
    # Single-shot worker — what run() originally did, now reusable.
    # ------------------------------------------------------------------

    def _render_single(self, *, shot_id: str, orientation: str,
                       seed: Optional[int], dry_run: bool,
                       _progress) -> dict:
        print(f"\n{'-'*55}")
        print(f"  STAGE: Preview Video — {shot_id} ({orientation})")
        print(f"{'-'*55}")

        parts = shot_id.split("_")
        if len(parts) < 3:
            raise ValueError(f"Bad shot_id: {shot_id!r}")
        chapter_id = parts[0]
        scene_id = "_".join(parts[:2])

        shot = self.project.load_shot(chapter_id, scene_id, shot_id)
        shot_dir = self.project._path("chapters", chapter_id, "shots", shot_id)

        storyboard_name = ("storyboard.png" if orientation == "horizontal"
                           else "storyboard_vertical.png")
        storyboard = shot_dir / storyboard_name
        if not storyboard.exists():
            raise FileNotFoundError(f"Storyboard missing: {storyboard}")

        out_name = ("preview.mp4" if orientation == "horizontal"
                    else "preview_vertical.mp4")
        output_path = shot_dir / out_name

        # Group audio.lines into 1 or 2 speakers in dialogue order.
        # If there are no lines (silent shot), we'll fall through to the
        # Wan I2V path instead of InfiniTalk.
        lines = (shot.get("audio") or {}).get("lines") or []
        speakers: list[dict] = []
        seen: set[str] = set()
        project_root = Path(self.project.root).resolve()
        for line in lines:
            cid = line.get("character_id") or ""
            if cid in seen:
                speakers[-1]["end_time_sec"] = max(
                    speakers[-1]["end_time_sec"],
                    float(line.get("end_time_sec") or 0),
                )
                continue
            seen.add(cid)
            audio_ref = line.get("audio_ref") or ""
            if not audio_ref:
                continue
            speakers.append({
                "character_id": cid,
                "audio_source": str(project_root / audio_ref),
                "start_time_sec": float(line.get("start_time_sec") or 0),
                "end_time_sec": float(line.get("end_time_sec") or 0),
            })
            if len(speakers) == 2:
                break

        silent_mode = len(speakers) == 0
        if silent_mode:
            # Silent shots use shot.duration_sec (or a sensible default
            # if unset) rather than summed dialogue slice.
            total_dur = float(shot.get("duration_sec") or 3.0)
        else:
            total_dur = sum(s["end_time_sec"] - s["start_time_sec"] for s in speakers)
        dims = {"horizontal": (832, 480), "vertical": (480, 832)}[orientation]
        width, height = dims

        mode_label = "silent" if silent_mode else f"{len(speakers)}-speaker talking"
        print(f"  {mode_label}, total {total_dur:.2f}s, {width}x{height}")

        if dry_run:
            _progress(100, f"Dry run: would render {total_dur:.2f}s video")
            return {
                "status": "dry_run",
                "speakers": len(speakers),
                "silent": silent_mode,
                "duration_sec": total_dur,
                "orientation": orientation,
            }

        _progress(5, "Connecting to ComfyUI...")

        # Build a richer prompt so Wan knows what *motion* is in the
        # shot, not just that the mouth should move. Generic prompts
        # tend to produce very still videos; feeding the same action
        # description used to generate the storyboard, plus the
        # surrounding screenplay action, gives Wan the cues it needs
        # to animate pose, walking, gestures, etc.
        label = (shot.get("label") or "").strip()
        char_list = [
            c.get("character_id", c) if isinstance(c, dict) else c
            for c in (shot.get("characters_in_frame") or [])
        ]
        who = ", ".join(c for c in char_list if c) or "scene"
        movement = (shot.get("cinematic", {}) or {}).get(
            "camera_movement", {}).get("type", "static")

        # Pull the storyboard's original action description — this is
        # the prompt the storyboard artist (or AI) used to draw the
        # frame, so it already describes pose, gesture, and activity.
        story_prompt = ((shot.get("storyboard") or {})
                        .get("storyboard_prompt") or "").strip()

        # Fall back to / augment with surrounding screenplay action so
        # we also capture beats that happen in the shot but aren't in
        # the storyboard prompt itself.
        action_note = ""
        try:
            from apis.prompt_builder import _locate_dialogue_action
            sp_path = self.project._path("chapters", chapter_id, "screenplay.md")
            if sp_path.exists():
                needle = (label or
                          ((shot.get("dialogue_in_shot") or [""])[0]))
                _heading, action = _locate_dialogue_action(
                    sp_path.read_text(encoding="utf-8"), needle
                )
                if action:
                    # Keep the tail — most relevant beat lives closest
                    # to the dialogue.
                    action_note = action[-350:]
        except Exception:  # noqa: BLE001
            pass

        # Compose — action description FIRST (Wan weighs the start of
        # the prompt more heavily), then speaking/motion cue, then
        # camera hint. Clip long story_prompts so we don't blow past
        # the text encoder context.
        action_bits = []
        if story_prompt:
            action_bits.append(story_prompt[:400])
        if action_note and action_note not in " ".join(action_bits):
            action_bits.append(f"Context: {action_note}")

        action_lead = ". ".join(action_bits).strip()
        speaking_cue = (
            "Character speaks naturally on camera, lips synced to audio, "
            "subtle body motion consistent with pose and action"
            if not silent_mode
            else "Subtle natural motion consistent with the action"
        )
        camera_cue = (
            f"{movement} camera" if movement and movement != "static"
            else "handheld subtle camera drift"
        )

        parts = []
        if action_lead:
            parts.append(action_lead)
        parts.append(speaking_cue)
        parts.append(camera_cue)
        if label:
            parts.append(label)
        prompt = ". ".join(p for p in parts if p).strip(" .,")

        import tempfile
        from apis.comfyui_video import ComfyUIVideoClient

        _progress(10, f"Rendering {mode_label} video...")
        with tempfile.TemporaryDirectory(prefix="babylon_video_") as tmp:
            with ComfyUIVideoClient() as client:
                if silent_mode:
                    result = client.generate_silent_video(
                        shot_id=shot_id,
                        storyboard_path=storyboard,
                        duration_sec=total_dur,
                        width=width,
                        height=height,
                        prompt=prompt,
                        output_path=output_path,
                        seed=seed,
                    )
                else:
                    result = client.generate_preview_video(
                        shot_id=shot_id,
                        storyboard_path=storyboard,
                        speakers=speakers,
                        width=width,
                        height=height,
                        prompt=prompt,
                        output_path=output_path,
                        seed=seed,
                        scratch_dir=tmp,
                    )

        # Persist reference on the shot for the UI to pick up.
        preview = shot.setdefault("preview", {})
        key = "video_ref_vertical" if orientation == "vertical" else "video_ref"
        preview[key] = f"chapters/{chapter_id}/shots/{shot_id}/{out_name}"
        preview["generated_at"] = datetime.now().isoformat()
        self.project.save_shot(chapter_id, scene_id, shot_id, shot)

        # Post-mix SFX into the mp4 so the exported clip plays the full
        # mix (Wan only bakes dialogue). Cheap (~10s of ffmpeg) and
        # keeps the pristine Wan render alongside as preview_raw.mp4 so
        # future volume/offset tweaks re-mix from clean source.
        sfx_list = (shot.get("audio") or {}).get("sound_effects") or []
        if sfx_list:
            try:
                _progress(97, f"Post-mixing {len(sfx_list)} SFX into video...")
                from utils.mix_preview_audio import mix_preview, _raw_for
                raw_path = _raw_for(output_path)
                mix_preview(
                    preview_path=output_path,
                    raw_path=raw_path,
                    sound_effects=sfx_list,
                    project_root=Path(self.project.root).resolve(),
                )
                print(f"  Post-mixed {len(sfx_list)} SFX into {output_path.name}")
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] Post-mix failed (keeping dialogue-only mp4): {e}")

        _progress(100, f"Done — {result['frames']} frames in {result['wall_sec']}s",
                  cost=0.0)

        return {
            "status": "complete",
            "shot_id": shot_id,
            "path": result.get("path"),
            "frames": result["frames"],
            "duration_sec": result["duration_sec"],
            "speakers": result["speakers"],
            "wall_sec": result["wall_sec"],
        }
