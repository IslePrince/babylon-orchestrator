"""
apis/claude_client.py
Anthropic Claude client for all AI pipeline stage passes.

Stage passes:
  - ingest_source      : parse raw text into chapter outlines
  - generate_world     : create world bible from story ingest
  - generate_character : create character profile from description
  - generate_screenplay: write screenplay for a chapter
  - cinematographer    : break screenplay scenes into shots
  - build_asset_manifest: scan shot list and build/update asset manifest
  - generate_storyboard_prompt: write image gen prompt per shot
"""

import os
import json
import anthropic
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class ClaudeClient:

    MODEL = "claude-opus-4-5"
    MAX_TOKENS = 8192

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise EnvironmentError("Missing ANTHROPIC_API_KEY in .env")
        self.client = anthropic.Anthropic(api_key=api_key)

    def _call(
        self,
        system: str,
        user: str,
        max_tokens: int = None,
        temperature: float = 0.7
    ) -> str:
        """Base call. Returns text response."""
        response = self.client.messages.create(
            model=self.MODEL,
            max_tokens=max_tokens or self.MAX_TOKENS,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        return response.content[0].text

    def _call_json(self, system: str, user: str, max_tokens: int = None) -> dict:
        """Call expecting JSON response. Strips markdown fences."""
        raw = self._call(
            system=system + "\n\nRespond ONLY with valid JSON. No markdown, no explanation.",
            user=user,
            max_tokens=max_tokens,
            temperature=0.3
        )
        clean = raw.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            clean = "\n".join(lines[1:-1])
        return json.loads(clean)

    def _estimate_cost(self, input_text: str, output_text: str) -> float:
        input_tokens = len(input_text.split()) * 1.3
        output_tokens = len(output_text.split()) * 1.3
        return round((input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0), 6)

    # ------------------------------------------------------------------
    # Stage 1: Source Ingest
    # ------------------------------------------------------------------

    def ingest_source(self, raw_text: str, project_id: str) -> dict:
        """
        Parse raw source text into chapter outline with metadata.
        Returns structured chapter list.
        """
        system = """You are a story analyst specializing in adapting literary works for film production.
Your task is to analyze source text and produce a structured chapter breakdown.
Each chapter should have: title, chapter_number, summary, theme, key_characters, key_locations, estimated_runtime_min."""

        user = f"""Analyze this source text and return a JSON chapter breakdown for the project '{project_id}'.

Source text (first 8000 chars):
{raw_text[:8000]}

Return JSON:
{{
  "project_id": "{project_id}",
  "total_chapters": <number>,
  "chapters": [
    {{
      "chapter_number": 1,
      "chapter_id": "ch01",
      "title": "<chapter title>",
      "summary": "<2-3 sentence summary>",
      "theme": "<core lesson or theme>",
      "key_characters": ["<character names>"],
      "key_locations": ["<location descriptions>"],
      "estimated_runtime_min": <8-12>,
      "parable_lesson": "<one sentence lesson>"
    }}
  ]
}}"""

        result = self._call_json(system, user, max_tokens=4096)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    # ------------------------------------------------------------------
    # Stage 2: World Bible Generation
    # ------------------------------------------------------------------

    def generate_world_bible(self, source_summary: str, project_schema: dict) -> dict:
        """Generate world bible from source analysis."""
        system = """You are a production designer and world-builder for epic film productions.
Create a comprehensive world bible that will guide all visual and narrative decisions.
Focus on: visual palette, architecture, society, recurring locations, and consistency rules."""

        period = project_schema.get("format", {}).get("period", "historical")
        user = f"""Create a world bible for a film adaptation set in {period}.

Source summary:
{source_summary}

Return a JSON world bible following this structure — populate all fields with specific, actionable detail.
The cinematographer, art director, and AI asset generation system will all use this document.

Focus especially on:
1. Visual palette (specific colors, avoid vague terms)
2. Lighting rules (time of day, sources, mood)
3. Architecture (materials, scale, specific structures)
4. Anachronism watchlist (what to avoid for period accuracy)
5. At least 6 key locations with reuse_rating

Return as JSON matching the world_bible schema structure."""

        result = self._call_json(system, user, max_tokens=6000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    # ------------------------------------------------------------------
    # Stage 3: Character Generation
    # ------------------------------------------------------------------

    def generate_character(
        self,
        character_name: str,
        role: str,
        source_description: str,
        world_bible: dict,
        existing_characters: list
    ) -> dict:
        """Generate a full character schema from source description."""
        system = """You are a casting director and character designer for historical epic films.
Create detailed character profiles that cover appearance, personality, voice direction,
animation requirements, and production notes. Be specific and actionable."""

        world_palette = world_bible.get("visual_language", {}).get("palette", {})
        period = world_bible.get("setting", {}).get("period", "historical")

        user = f"""Create a character profile for '{character_name}' ({role}) in {period}.

Source description:
{source_description}

World palette: {json.dumps(world_palette, indent=2)}
Existing characters (for contrast): {[c.get('display_name') for c in existing_characters]}

Return a JSON character profile with these sections:
- description (age, role, archetype, personality_traits, physical appearance)
- narrative (chapters, arc, relationships)
- voice (ElevenLabs settings, direction notes — specific tone, pace, emotion range)
- animation (Cartwheel motion styles, facial expression hints)
- assets (costume variants, signature props)

Be very specific about voice direction — this will guide ElevenLabs voice selection and direction."""

        result = self._call_json(system, user, max_tokens=3000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    # ------------------------------------------------------------------
    # Stage 4: Screenplay
    # ------------------------------------------------------------------

    def generate_screenplay(
        self,
        chapter_outline: dict,
        world_bible: dict,
        characters: list,
        adaptation_notes: str = ""
    ) -> str:
        """
        Generate a screenplay for one chapter.
        Returns formatted screenplay text (not JSON).
        """
        system = """You are an experienced screenwriter adapting literary works for film.
Write in standard screenplay format. Focus on:
- Authentic period dialogue (no modern slang)
- Visual storytelling — show don't tell
- Clear scene headings (INT/EXT, LOCATION, TIME)
- Concise action lines
- Character voice consistency with their profiles
- Parable lessons delivered naturally through dialogue, not exposition"""

        char_summaries = [
            f"{c.get('display_name')}: {c.get('description', {}).get('summary', '')}"
            for c in characters
        ]

        anachronisms = world_bible.get("rules", {}).get("anachronism_watchlist", [])

        user = f"""Write the screenplay for Chapter {chapter_outline['chapter_number']}: {chapter_outline['title']}

Chapter outline:
{json.dumps(chapter_outline, indent=2)}

Characters in this chapter:
{chr(10).join(char_summaries)}

Anachronism watchlist (avoid these):
{chr(10).join(f'- {a}' for a in anachronisms)}

Adaptation notes:
{adaptation_notes or 'None'}

Write a complete, production-ready screenplay. Target {chapter_outline.get('estimated_runtime_min', 10)} minutes runtime.
Format: standard screenplay format with scene headings, action lines, and dialogue."""

        screenplay = self._call(system, user, max_tokens=8192, temperature=0.8)
        return screenplay

    # ------------------------------------------------------------------
    # Stage 5: Cinematographer Pass
    # ------------------------------------------------------------------

    def cinematographer_pass(
        self,
        scene_text: str,
        scene_id: str,
        location: dict,
        characters_in_scene: list,
        world_bible: dict,
        chapter_id: str
    ) -> dict:
        """
        Break a screenplay scene into shots.
        Returns list of shot definitions.
        """
        system = """You are an experienced film cinematographer breaking down screenplay scenes into shot lists.
For each shot define: shot type, framing, camera movement, depth of field, characters in frame,
dialogue lines covered, and vertical reframe strategy.
Think both cinematically (16:9 master) and for vertical (9:16 social).
Be specific and technical — your output drives UE5 scene assembly."""

        visual_lang = world_bible.get("visual_language", {})
        lighting_rules = visual_lang.get("lighting", {})
        palette = visual_lang.get("palette", {})

        user = f"""Break this screenplay scene into a shot list.

Scene ID: {scene_id}
Chapter: {chapter_id}
Location: {json.dumps(location, indent=2)}
Characters: {json.dumps([c.get('display_name') for c in characters_in_scene])}

Lighting rules: {json.dumps(lighting_rules, indent=2)}
Palette: {json.dumps(palette, indent=2)}

Screenplay scene:
{scene_text}

Return JSON:
{{
  "scene_id": "{scene_id}",
  "shots": [
    {{
      "shot_number": 1,
      "label": "<descriptive label>",
      "shot_type": "<wide_establishing|medium|close_up|over_shoulder|two_shot|insert|pov>",
      "framing": "<full_shot|medium_shot|close_up|extreme_close_up>",
      "lens_mm_equiv": <number>,
      "camera_movement": {{
        "type": "<static|slow_push_in|pan|tilt|crane|handheld>",
        "description": "<what the camera does>"
      }},
      "duration_sec": <number>,
      "characters_in_frame": ["<character_ids>"],
      "dialogue_lines_covered": ["<line descriptions>"],
      "composition_notes": "<what makes this shot work>",
      "vertical_reframe": {{
        "strategy": "<follow_speaker|center_crop|separate_shot>",
        "acceptable": <true|false>,
        "notes": "<reframe direction>"
      }},
      "storyboard_prompt": "<detailed image generation prompt for this exact shot>",
      "assets_needed": ["<brief asset descriptions>"],
      "lighting_notes": "<any shot-specific lighting>",
      "flags": ["<any concerns or warnings>"]
    }}
  ]
}}"""

        result = self._call_json(system, user, max_tokens=6000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    # ------------------------------------------------------------------
    # Stage 6: Asset Manifest Builder
    # ------------------------------------------------------------------

    def build_asset_entries(
        self,
        shots: list,
        existing_manifest: dict,
        world_bible: dict,
        chapter_id: str
    ) -> dict:
        """
        Scan shot list and generate asset manifest entries.
        Deduplicates against existing manifest.
        Returns new/updated entries only.
        """
        system = """You are a production asset manager for a historical epic film.
Your job is to extract every unique asset needed from a shot list and create
Meshy.ai generation briefs for each. Be specific with prompts — vague prompts
produce generic assets. Include anachronism guards in negative prompts.
Deduplicate aggressively — one asset should serve multiple shots."""

        existing_ids = set()
        for category in ["environments", "props", "costumes", "vegetation"]:
            for asset in existing_manifest.get("assets", {}).get(category, []):
                existing_ids.add(asset["asset_id"])

        anachronisms = world_bible.get("rules", {}).get("anachronism_watchlist", [])
        palette = world_bible.get("visual_language", {}).get("palette", {})
        period = world_bible.get("setting", {}).get("period", "ancient Babylon 600 BCE")

        user = f"""Analyze these shots and create asset manifest entries for all unique assets needed.

Chapter: {chapter_id}
Period: {period}
Palette: {json.dumps(palette)}
Anachronism guards: {json.dumps(anachronisms)}

Already in manifest (do not duplicate): {json.dumps(list(existing_ids))}

Shot asset requirements:
{json.dumps([{{"shot_id": s.get("shot_id"), "assets_needed": s.get("assets_needed", [])}} for s in shots], indent=2)}

For each NEW asset not already in the manifest, return:
{{
  "new_assets": {{
    "environments": [...],
    "props": [...],
    "costumes": [...],
    "vegetation": [...]
  }},
  "deduplication_log": [
    {{"merged_from": "<description>", "into": "<asset_id>", "reason": "<why>"}}
  ]
}}

Each asset entry needs: asset_id, label, type, description, style_notes,
meshy.prompt, meshy.negative_prompt, meshy.style_preset, meshy.texture_resolution,
ue5 settings, usage.scene_count, usage.chapters."""

        result = self._call_json(system, user, max_tokens=6000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    # ------------------------------------------------------------------
    # Utility: Generate short-form cut concepts
    # ------------------------------------------------------------------

    def generate_shorts_concepts(
        self,
        chapter_data: dict,
        shot_list: list
    ) -> list:
        """Identify the best 3-4 moments from a chapter for vertical shorts."""
        system = """You are a social media editor identifying the most compelling moments
from film chapters to cut into 60-90 second vertical shorts.
Focus on: emotional peaks, key wisdom moments, dramatic reveals, visual spectacle."""

        user = f"""Identify 3-4 short-form cut concepts for this chapter.

Chapter: {chapter_data['title']}
Theme: {chapter_data['narrative']['theme']}
Shots available: {json.dumps([{{'shot_id': s.get('shot_id'), 'label': s.get('label'), 'duration': s.get('duration_sec')}} for s in shot_list], indent=2)}

Return JSON list:
[
  {{
    "short_id": "<chapter_id>_short_01",
    "concept": "<what this short is about>",
    "hook_line": "<opening line or visual hook>",
    "source_shots": ["<shot_ids>"],
    "duration_target_sec": <60-90>,
    "platform_note": "<why this works for social>"
  }}
]"""

        return self._call_json(system, user, max_tokens=2000)
