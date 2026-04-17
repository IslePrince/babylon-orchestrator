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
import re
import json
import time
import anthropic
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Resolve .env relative to this file's parent (orchestrator root)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path, override=True)


class ClaudeClient:

    MODEL = "claude-sonnet-4-20250514"
    MAX_TOKENS = 8192

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise EnvironmentError("Missing ANTHROPIC_API_KEY in .env")
        self.client = anthropic.Anthropic(api_key=api_key)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def _call(
        self,
        system: str,
        user: str,
        max_tokens: int = None,
        temperature: float = 0.7,
        retries: int = 4,
        retry_delay: float = 5.0,
    ) -> str:
        """Base call with retry logic for transient errors.

        Retries on:
        - Cloudflare 403 challenges (HTML instead of JSON)
        - 429 rate limits / 529 overloaded
        - 500+ server errors
        - Connection errors
        """
        last_error = None
        for attempt in range(retries):
            try:
                response = self.client.messages.create(
                    model=self.MODEL,
                    max_tokens=max_tokens or self.MAX_TOKENS,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": user}]
                )
                return response.content[0].text

            except anthropic.APIStatusError as e:
                last_error = e
                status = e.status_code
                body = str(e.body) if e.body else str(e)

                # Cloudflare challenge: 403 with HTML body
                is_cloudflare = (
                    status == 403
                    and ("cloudflare" in body.lower()
                         or "just a moment" in body.lower()
                         or "<!DOCTYPE" in body)
                )

                if is_cloudflare or status in (429, 529) or status >= 500:
                    wait = retry_delay * (2 ** attempt)
                    label = "Cloudflare challenge" if is_cloudflare else f"HTTP {status}"
                    print(f"  [RETRY] Claude {label}, attempt {attempt+1}/{retries}, waiting {wait:.0f}s...")
                    time.sleep(wait)
                    continue

                # Non-retryable API error
                raise

            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
                last_error = e
                wait = retry_delay * (2 ** attempt)
                print(f"  [RETRY] Claude connection error, attempt {attempt+1}/{retries}, waiting {wait:.0f}s...")
                time.sleep(wait)
                continue

        # All retries exhausted
        raise last_error

    def _call_json(self, system: str, user: str, max_tokens: int = None,
                    retries: int = 2) -> dict:
        """Call expecting JSON response. Strips fences, repairs common
        LLM JSON quirks (trailing commas, single quotes, comments),
        and retries on parse failure."""
        last_error = None
        for attempt in range(1 + retries):
            raw = self._call(
                system=system + "\n\nRespond ONLY with valid JSON. No markdown, no explanation.",
                user=user,
                max_tokens=max_tokens,
                temperature=0.3
            )
            clean = raw.strip()
            # Strip markdown fences
            if clean.startswith("```"):
                lines = clean.split("\n")
                clean = "\n".join(lines[1:-1])
            # Repair common LLM JSON issues
            clean = self._repair_json(clean)
            try:
                return json.loads(clean)
            except json.JSONDecodeError as e:
                last_error = e
                print(f"  [WARN] JSON parse attempt {attempt+1} failed: {e}")
                if attempt < retries:
                    time.sleep(1)
        raise last_error

    @staticmethod
    def _repair_json(text: str) -> str:
        """Fix common LLM JSON issues that cause parse failures."""
        # Remove trailing commas before } or ]
        text = re.sub(r',\s*([}\]])', r'\1', text)
        # Remove // single-line comments
        text = re.sub(r'(?m)//.*$', '', text)
        # Remove /* block comments */
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
        return text

    def _estimate_cost(self, input_text: str, output_text: str) -> float:
        input_tokens = len(input_text.split()) * 1.3
        output_tokens = len(output_text.split()) * 1.3
        return round((input_tokens / 1_000_000 * 3.0) + (output_tokens / 1_000_000 * 15.0), 6)

    # ------------------------------------------------------------------
    # World Bible helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unwrap_world_bible(wb: dict) -> dict:
        """Unwrap world_bible wrapper key and normalize field access.

        The loaded JSON is typically {"world_bible": {...actual data...}}.
        Some callers forget to unwrap, causing empty reads on fields like
        setting.time_period, visual_palette, society_structure, etc.
        """
        return wb.get("world_bible", wb)

    @staticmethod
    def _extract_world_context(wb: dict) -> dict:
        """Extract the most useful world bible fields for character prompts.

        Returns a flat dict with period, location, clothing_by_class,
        social_hierarchy, forbidden_clothing, and visual_palette —
        the fields that directly affect how characters should look and dress.
        """
        bible = wb.get("world_bible", wb)
        setting = bible.get("setting", {})
        society = bible.get("society_structure", {})
        anachronisms = bible.get("anachronism_watchlist", {})
        palette = bible.get("visual_palette", {})

        return {
            "period": (
                setting.get("time_period")
                or setting.get("period")
                or "historical"
            ),
            "location": (
                setting.get("location", "")
            ),
            "cultural_context": (
                setting.get("cultural_context", "")
            ),
            "clothing_by_class": society.get("clothing_by_class", {}),
            "social_hierarchy": society.get("social_hierarchy", []),
            "occupations": society.get("occupations", []),
            "forbidden_clothing": anachronisms.get("forbidden_clothing", []),
            "material_textures": palette.get("material_textures", []),
        }

    # ------------------------------------------------------------------
    # Stage 1: Source Ingest
    # ------------------------------------------------------------------

    def ingest_source(self, raw_text: str, project_id: str) -> dict:
        """
        Parse raw source text into chapter outline with metadata.
        Sends up to 100K chars for thorough analysis.
        Returns structured chapter list with detailed summaries.
        """
        system = """You are a story analyst specializing in adapting literary works for film production.
Your task is to analyze the COMPLETE source text and produce a structured chapter breakdown.
Each chapter should have: title, chapter_number, a DETAILED summary, theme, key_characters, key_locations, estimated_runtime_min.
Your summaries must be thorough — they will guide screenplay writers. Include: who the characters are,
what happens in the chapter, key dialogue moments, the lesson or resolution, and character relationships."""

        # Send up to 100K chars — well within Claude's 200K token context
        max_chars = 100_000
        user = f"""Analyze this source text and return a JSON chapter breakdown for the project '{project_id}'.

Source text:
{raw_text[:max_chars]}

Return JSON:
{{
  "project_id": "{project_id}",
  "total_chapters": <number>,
  "chapters": [
    {{
      "chapter_number": 1,
      "chapter_id": "ch01",
      "title": "<exact chapter title as it appears in the text>",
      "summary": "<detailed 4-6 sentence summary: WHO are the characters, WHAT happens, key dialogue, the resolution or lesson>",
      "theme": "<core lesson or theme>",
      "key_characters": ["<character names exactly as they appear in the text>"],
      "key_locations": ["<location descriptions>"],
      "estimated_runtime_min": <8-12>,
      "parable_lesson": "<one sentence lesson>"
    }}
  ]
}}

IMPORTANT: Use character names EXACTLY as they appear in the source text (e.g. "Bansir", "Kobbi", "Arkad").
Summaries must describe what actually happens — not generic descriptions."""

        result = self._call_json(system, user, max_tokens=6000)
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
        existing_characters: list,
        source_text: str = "",
    ) -> dict:
        """
        Generate a full character schema from source description.
        Includes visual_tag and costume_default for image generation consistency.
        """
        system = """You are a casting director and character designer for historical epic films.
Create detailed character profiles that cover appearance, personality, voice direction,
animation requirements, and production notes. Be specific and actionable.

CRITICAL — IMAGE CONSISTENCY:
You MUST include a "visual_tag" field: a SHORT (15-25 word) physical description
that will be prepended VERBATIM to every AI image prompt featuring this character.
Include: age, build, hair color/style, facial features, skin tone, one defining trait.
Do NOT include clothing in visual_tag — put default clothing in "costume_default".
Make each character look DISTINCTLY different from others."""

        wctx = self._extract_world_context(world_bible)
        period = wctx["period"]

        source_section = ""
        if source_text:
            source_section = f"\nOriginal source text context (first 15K chars):\n{source_text[:15000]}"

        clothing_section = ""
        if wctx["clothing_by_class"]:
            clothing_section = f"\nPeriod-accurate clothing by social class:\n{json.dumps(wctx['clothing_by_class'], indent=2)}"
        forbidden_section = ""
        if wctx["forbidden_clothing"]:
            forbidden_section = f"\nFORBIDDEN clothing items (anachronistic):\n{json.dumps(wctx['forbidden_clothing'])}"

        user = f"""Create a character profile for '{character_name}' ({role}) in {period}.
Setting: {wctx['location']}. {wctx['cultural_context']}

Source description:
{source_description}
{source_section}
{clothing_section}{forbidden_section}

Social hierarchy: {json.dumps(wctx['social_hierarchy'])}
Existing characters (for contrast): {[c.get('display_name') for c in existing_characters]}

Return a JSON character profile with these sections:
- character_id (lowercase, no spaces — e.g. "bansir", "arkad")
- display_name (full name as in source text)
- description (age, role, archetype, personality_traits, physical_appearance as detailed text, height_cm)
- visual_tag (15-25 word SPECIFIC physical description for AI image generation — age, build, hair, face, skin, defining trait — NO clothing)
- costume_default (10-15 word default outfit description — MUST be period-accurate to {period})
- narrative (chapters as list of chapter IDs, arc, relationships as dict)
- voice (elevenlabs_voice_id as null, tone, pace, emotion_range, direction_notes)
- animation (motion_style, facial_expression_hints, posture)
- assets (costume_variants as list, signature_props as list)

CRITICAL RULES:
1. visual_tag is THE MOST IMPORTANT FIELD — reused across hundreds of images.
   Be extremely specific: "stocky 42-year-old, broad shoulders, deep brown eyes, thick black curly beard, sun-weathered bronze skin, calloused hands" — NOT "a man".
2. costume_default MUST be period-accurate clothing. Infer social class from the character's role and assign dress from the clothing_by_class data above. Every character MUST be properly dressed.
3. ALWAYS include explicit gender in visual_tag — use "man" or "male" for males, "woman" or "female" for females. NEVER omit gender, even when the name or role seems obvious.
4. The character's role/occupation must come from the source text, not be invented.
5. costume_variants MUST each be a SPECIFIC period-accurate clothing description (10-15 words).
   Each variant must name actual garments (tunic, robe, cloak, wrap, etc.), materials (linen, wool, leather),
   and accessories. NEVER use vague terms like "work clothes", "formal attire", "travel garments",
   or "casual outfit". Use the clothing_by_class data to match the character's social class.
   WRONG: "formal meeting attire"
   RIGHT: "clean brown linen tunic with leather vest, bronze clasp belt, combed sandals"
   WRONG: "work clothes"
   RIGHT: "rough-spun work tunic with patches, leather tool belt, frayed sandals, bare arms" """

        result = self._call_json(system, user, max_tokens=3000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    def revise_character(
        self,
        character: dict,
        feedback: str,
        world_bible: dict,
        scene_context: str = "",
    ) -> dict:
        """
        Revise a character profile based on director feedback.
        Returns the complete updated character JSON.
        """
        system = """You are a casting director and character designer revising a character profile
based on the director's feedback. You have full context about the world, setting, and scenes.

Return the COMPLETE updated character JSON — not just the changed fields.
Preserve all existing fields and only modify what the director's feedback requires.

CRITICAL — visual_tag rules:
- 15-25 word SPECIFIC physical description for AI image generation
- Include: age, build, hair color/style, facial features, skin tone, one defining trait
- Do NOT include clothing in visual_tag — clothing goes in costume_default
- Must be period-accurate to the setting

CRITICAL — costume rules:
- costume_default and every costume_variant MUST be SPECIFIC 10-15 word period-accurate descriptions
- Name actual garments (tunic, robe, cloak, wrap), materials (linen, wool, leather), and accessories
- NEVER use vague terms like "work clothes", "formal attire", or "travel garments"
- WRONG: "casual outfit"  RIGHT: "simple linen tunic, no jewelry, comfortable leather sandals" """

        setting = world_bible.get("setting", {})
        period = setting.get("period", "historical")
        visual_style = world_bible.get("visual_style", "")

        user = f"""Revise this character profile based on the director's feedback.

DIRECTOR'S FEEDBACK:
{feedback}

CURRENT CHARACTER PROFILE:
{json.dumps(character, indent=2)}

WORLD SETTING:
Period: {period}
Location: {setting.get("location", "unknown")}
Visual style: {visual_style}
{f"SCENE CONTEXT (scenes featuring this character):{chr(10)}{scene_context}" if scene_context else ""}

Return the COMPLETE updated character JSON with all fields. Apply the director's
feedback while keeping everything else intact and period-accurate to {period}."""

        result = self._call_json(system, user, max_tokens=4000)
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
        adaptation_notes: str = "",
        source_text: str = ""
    ) -> str:
        """
        Generate a screenplay for one chapter.
        If source_text is provided, adapt it faithfully.
        Returns formatted screenplay text (not JSON).
        """
        if source_text:
            system = """You are an experienced screenwriter adapting literary works for film.
You are given the ORIGINAL SOURCE TEXT for this chapter. Your screenplay MUST faithfully adapt it:
- Keep ALL characters, dialogue, and events from the source
- Use character names exactly as they appear in the source
- Preserve the actual dialogue and conversations (adapt to screenplay format)
- Maintain the story's structure and progression
- Add visual scene descriptions and action lines to bring it to life cinematically
Write in standard screenplay format with INT/EXT headings, action lines, and dialogue.
Do NOT invent new characters or change the story — adapt what is written."""
        else:
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

        # Build the prompt — include source text when available
        source_section = ""
        if source_text:
            # Limit to 60K chars to leave room for context + output
            source_section = f"""
ORIGINAL SOURCE TEXT (adapt this faithfully):
--- BEGIN SOURCE ---
{source_text[:60000]}
--- END SOURCE ---

"""

        user = f"""Write the screenplay for Chapter {chapter_outline['chapter_number']}: {chapter_outline['title']}

{source_section}Chapter outline:
{json.dumps(chapter_outline, indent=2)}

Characters in this chapter:
{chr(10).join(char_summaries) if char_summaries else '(derive from source text)'}

Anachronism watchlist (avoid these):
{chr(10).join(f'- {a}' for a in anachronisms)}

Adaptation notes:
{adaptation_notes or 'Adapt the source text faithfully. Keep all characters, conversations, and events.'}

Write a complete, production-ready screenplay. Target {chapter_outline.get('estimated_runtime_min', 10)} minutes runtime.
Format: standard screenplay format with scene headings, action lines, and dialogue.
{"CRITICAL: Preserve the actual characters, dialogue, and story from the source text above." if source_text else ""}"""

        # Use more tokens when adapting real source text
        max_tokens = 12000 if source_text else 8192
        screenplay = self._call(system, user, max_tokens=max_tokens, temperature=0.7)
        return screenplay

    def revise_screenplay(
        self,
        screenplay_text: str,
        instruction: str,
        chapter_outline: dict = None,
        characters: list = None,
        world_bible: dict = None,
    ) -> dict:
        """
        Revise an existing screenplay based on a director's instruction.
        Returns {"revised": "<full revised screenplay>", "_cost_usd": float}.
        """
        period = ""
        anachronisms = []
        if world_bible:
            period = world_bible.get("setting", {}).get("period", "")
            anachronisms = world_bible.get("rules", {}).get("anachronism_watchlist", [])

        system = f"""You are an experienced screenwriter-editor revising an existing screenplay \
based on the director's feedback.

RULES:
- Maintain standard screenplay format (INT/EXT headings, character names in ALL CAPS, \
parentheticals, transitions)
- Preserve the overall structure and scene count unless the instruction specifically \
requests restructuring
- Keep character names consistent with the original
- Apply ONLY the changes the director requests — do not rewrite sections that don't need changing
- {"Maintain period-appropriate dialogue for " + period + " — no modern slang" if period else "Maintain consistent dialogue tone"}
- Return the COMPLETE revised screenplay, not just the changed sections

{"Anachronism watchlist (avoid these): " + ", ".join(anachronisms) if anachronisms else ""}"""

        char_context = ""
        if characters:
            names = [c.get("display_name", c.get("character_id", "")) for c in characters]
            char_context = f"\nCharacters in this chapter: {', '.join(names)}"

        chapter_context = ""
        if chapter_outline:
            title = chapter_outline.get("title", "")
            summary = chapter_outline.get("narrative", {}).get("logline", "")
            if title:
                chapter_context = f"\nChapter: {title}"
            if summary:
                chapter_context += f"\nLogline: {summary}"

        user = f"""Here is the current screenplay:

--- BEGIN SCREENPLAY ---
{screenplay_text}
--- END SCREENPLAY ---
{chapter_context}{char_context}

DIRECTOR'S INSTRUCTION:
{instruction}

Apply the director's instruction to the screenplay above. Return the COMPLETE revised screenplay."""

        revised = self._call(system, user, max_tokens=12000, temperature=0.7)
        cost = self._estimate_cost(user, revised)
        return {"revised": revised, "_cost_usd": cost}

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
        chapter_id: str,
        character_visuals: dict = None,
        beat_map: str = "",
    ) -> dict:
        """
        Break a screenplay scene into shots.
        Returns list of shot definitions.

        character_visuals: optional dict from generate_character_visuals() with
        stable visual tags per character for image generation consistency.
        """
        character_visuals = character_visuals or {}

        system = """You are an experienced film cinematographer breaking down screenplay scenes into shot lists.
For each shot define: shot type, framing, camera movement, depth of field, characters in frame,
dialogue lines covered, and vertical reframe strategy.
Think both cinematically (16:9 master) and for vertical (9:16 social).
Be specific and technical — your output drives UE5 scene assembly.

ABSOLUTE BAN — NO TEXT IN SHOTS:
- NEVER create title cards, chapter titles, text overlays, credit screens, or typography shots
- NEVER write storyboard_prompt text that describes on-screen text, lettering, or captions
- The AI image generator CANNOT render text — it produces gibberish, misspellings, and artifacts
- Shot 1 must always be a real visual scene shot (establishing wide, character action, etc.)
- If the screenplay has "FADE IN:" or a title, SKIP it — go straight to the first visual moment

CRITICAL — AUDIO-FIRST PIPELINE:
Audio is generated AFTER these storyboards. Each shot's dialogue audio plays sequentially,
then the player advances to the next shot. There is no editing room to adjust.
Rules:
  - Every dialogue shot MUST show the speaking character in characters_in_frame
  - Shots with no dialogue become silent holds (duration_sec only) — minimize these
  - The SHOT BUDGET in the beat map gives your target shot count. Stay close to it.

CRITICAL — BEAT-DRIVEN SHOT DESIGN:
You will receive a SCENE BEAT MAP listing every dialogue turn and action beat in order.
Your shot list MUST cover EVERY beat — nothing should be left unshot.

PACING (Walter Murch principle — cut for emotion, not mechanics):
  - Target ASL: 4-5 seconds. No shot under 2 seconds. No shot over 6 seconds.
  - duration_sec for dialogue = ceil(word_count / 2.5). Clamp to [2, 6].
  - Do NOT mechanically alternate close-up/close-up ("ping-pong"). Vary the angle
    with each cut: close-up, medium, over-shoulder, two-shot at emotional peaks.

Dialogue beats:
- ONE shot per dialogue beat. Speaker MUST be in characters_in_frame.
- CONT'D beats (same speaker continuing a speech) MUST get a DIFFERENT framing
  than the previous beat (e.g., close-up → medium → over-shoulder → insert of hands/props).
  This is critical for visual variety during long speeches.
- Between different speakers: go directly to the next speaker's shot.
  Do NOT insert reaction shots or padding between every dialogue exchange.
  A brief reaction shot is allowed only at major emotional turning points
  (1-2 per scene max, not between every line).
- Use over-the-shoulder shots for variety (list only the SPEAKING character in characters_in_frame)
- Use two-shots sparingly (scene openers, emotional beats, revelations) — NOT for every line

Action beats (non-dialogue / silence):
- 1 shot per action beat, 3-4 seconds. These become silent holds in playback.
- Scene-opening action beats (establishing location): 2-3 shots max.
- Mid-dialogue action beats (stage directions between lines): condense to 1 shot
  or fold the action description into the next dialogue shot's storyboard_prompt.
- Do NOT expand short stage directions into multiple shots.

Shot count: stay within the SHOT BUDGET provided in the beat map.
The beat map already splits long speeches into CONT'D sub-beats.
Do not inflate the shot count beyond the budget with extra padding.

CRITICAL — STORYBOARD PROMPT RULES:
The "storyboard_prompt" field describes the SCENE ACTION and SETTING for AI image generation.
A separate system automatically injects each character's full physical description and costume,
so you must NOT include physical descriptions of characters in the storyboard_prompt.

Instead:
- Refer to characters by their NAME (e.g. "Bansir pacing angrily" not "stocky man pacing angrily")
- Describe the ACTION, EMOTION, SETTING, LIGHTING, and MOOD
- Include environment details (architecture, props, time of day)
- Do NOT describe what characters look like (age, build, hair, eyes, clothing)
- Do NOT use generic references like "a man", "two figures", "the woman"

Good: "Bansir pacing angrily in the workshop, half-completed chariot behind him, morning light through doorway"
Bad: "Stocky 40s man with thick chest hair pacing angrily in workshop"

The character names MUST match the character_ids from characters_in_frame."""

        visual_lang = world_bible.get("visual_language", {})
        lighting_rules = visual_lang.get("lighting", {})
        palette = visual_lang.get("palette", {})

        # Build character reference section (names + IDs for the cinematographer)
        char_visual_section = ""
        if character_visuals:
            lines = ["CHARACTER REFERENCES (use these names in storyboard_prompt — visual descriptions are injected automatically):"]
            for key, cv in character_visuals.items():
                name = cv.get("display_name", key)
                lines.append(f"  character_id: {key} → Name: {name}")
            char_visual_section = "\n".join(lines)
        else:
            char_visual_section = f"Characters: {json.dumps([c.get('display_name') for c in characters_in_scene])}"

        beat_section = ""
        if beat_map:
            beat_section = f"""
{beat_map}

You MUST create at least one shot for EVERY beat listed above.
Dialogue beats → 1 shot each (speaker visible). CONT'D beats → different angle from previous.
Action beats → 1 shot each (tight, don't expand into multiple shots).
Stay within the SHOT BUDGET above. Do NOT skip any beats. Do NOT inflate beyond budget.

"""

        user = f"""Break this screenplay scene into a shot list.

Scene ID: {scene_id}
Chapter: {chapter_id}
Location: {json.dumps(location, indent=2)}

{char_visual_section}

Lighting rules: {json.dumps(lighting_rules, indent=2)}
Palette: {json.dumps(palette, indent=2)}
{beat_section}
Screenplay scene:
{scene_text}

Return COMPACT JSON (no extra whitespace, keep fields short):
{{
  "scene_id": "{scene_id}",
  "shots": [
    {{
      "shot_number": 1,
      "label": "<short label>",
      "shot_type": "<wide_establishing|medium|close_up|over_shoulder|two_shot|insert|pov>",
      "framing": "<full_shot|medium_shot|close_up|extreme_close_up>",
      "lens_mm_equiv": <number>,
      "duration_sec": <number>,
      "characters_in_frame": ["<character_ids>"],
      "dialogue_lines_covered": ["<brief line refs>"],
      "composition_notes": "<brief blocking note>",
      "storyboard_prompt": "<scene action using character NAMES — describe action, setting, mood, lighting — do NOT describe character appearance>"
    }}
  ]
}}

REMINDER:
- NEVER create title cards or text-on-screen shots. Shot 1 = first visual action.
- storyboard_prompt must use character NAMES — never physical descriptions
- storyboard_prompt must NEVER describe text, titles, lettering, or words appearing on screen
- Cover EVERY beat — dialogue AND action. No beat left unshot.
- ONE shot per dialogue beat. CONT'D beats MUST get different angles. Speaker always visible.
- Action beats: 1 shot each, tight. Don't inflate stage directions into multiple shots.
- Target ASL 4-5 sec. No shot under 2s or over 6s. Stay within the SHOT BUDGET.
- characters_in_frame: only who is VISIBLE (speaker for dialogue, nobody for pure establishing)"""

        result = self._call_json(system, user, max_tokens=16000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    # ------------------------------------------------------------------
    # Voice Casting (match voices to character profiles)
    # ------------------------------------------------------------------

    def match_voices(self, character: dict, voices: list,
                     setting: dict = None, top_n: int = 8) -> dict:
        """
        Rank voice actors from ElevenLabs library against a character profile.
        Returns a list of voice_ids sorted by fit, with reasoning.

        setting: world bible setting dict with period, location, etc.
        """
        setting = setting or {}
        # Build compact character summary
        char_name = character.get("display_name", "Unknown")
        char_desc = character.get("description", {})
        voice_info = character.get("voice", {})
        voice_desc = voice_info.get("description", "")
        personality = char_desc.get("personality", "")
        age = char_desc.get("age", "")
        role = character.get("role", "")

        # Build compact voice list (only include useful fields)
        voice_summaries = []
        for v in voices:
            labels = v.get("labels", {})
            summary = {
                "id": v.get("voice_id"),
                "name": v.get("name"),
                "desc": (v.get("description") or "")[:200],
                "labels": labels,
            }
            voice_summaries.append(summary)

        period = setting.get("period", "")
        location = setting.get("location", "")
        setting_context = ""
        if period or location:
            setting_context = f"""
PRODUCTION SETTING:
  Period: {period}
  Location: {location}
  Voice direction: Prioritise VOCAL QUALITY (tone, timbre, age, energy) over accent.
  Cast a DIVERSE range of voices — vary accent, pitch, cadence and texture across
  characters so each one is instantly distinguishable. Avoid clustering everyone into
  the same accent family. A wise elder, a young merchant, and a street urchin should
  each sound completely different from one another.
  Match social class: royalty/scholars sound refined, laborers/traders sound earthier.
  For historical or fantasy settings, any accent that evokes the right tone is fine —
  British, Mediterranean, South Asian, African, or neutral — as long as the voice
  FITS THE CHARACTER'S personality, age, and status. Avoid defaulting to a single
  accent for all characters."""

        system = """You are an expert film casting director matching voice actors to characters.
Given a character profile, production setting, and a list of available voices,
select the top voices that best match the character.

PRIORITIES (in order):
1. Vocal quality & personality fit — the voice must SOUND like this character
   (authoritative vs gentle, gruff vs refined, warm vs cold)
2. Age match — a 68-year-old must sound mature, a 19-year-old must sound young
3. Gender match from labels
4. DIVERSITY — pick voices with DIFFERENT accents, timbres, and styles from
   one another so the cast doesn't all sound the same
5. Social class and character background (royalty vs working class)
6. Accent — use accent to reinforce character identity, not to match a region.
   Avoid recommending only one accent family.
7. Voice description if provided in the character profile"""

        user = f"""Match voices for this character:

CHARACTER:
  Name: {char_name}
  Age: {age}
  Role: {role}
  Personality: {personality}
  Voice description: {voice_desc}
{setting_context}

AVAILABLE VOICES:
{json.dumps(voice_summaries, indent=1)}

Return JSON — pick the top {top_n} best matches:
{{
  "matches": [
    {{"voice_id": "<id>", "name": "<name>", "reason": "<1 sentence why this voice fits>"}}
  ]
}}"""

        result = self._call_json(system, user, max_tokens=2000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    # ------------------------------------------------------------------
    # Shot Prompt Rewriting (director feedback → updated prompt + notes)
    # ------------------------------------------------------------------

    def rewrite_shot_prompt(self, shot: dict, feedback: str,
                            character_names: list = None) -> dict:
        """
        Rewrite a shot's storyboard_prompt and composition_notes based on
        director feedback. Returns dict with updated fields.

        character_names: list of display names for characters in frame
        (visual descriptions are injected by prompt_builder, not here)
        """
        character_names = character_names or []
        chars_str = ", ".join(character_names) if character_names else "no named characters"

        system = """You are an experienced film cinematographer revising a shot based on the director's feedback.
You will receive the current shot data and the director's notes. Your job is to update the
storyboard_prompt and composition_notes to incorporate the feedback.

RULES:
- Refer to characters by NAME only — never describe their physical appearance, age, build, hair, eyes, or clothing. A separate system handles visual descriptions automatically.
- Focus on: action, body language, emotion, setting/environment, props, lighting, mood, atmosphere
- Keep the storyboard_prompt concise (1-3 sentences) and vivid
- Preserve any good elements from the original prompt that don't conflict with the feedback
- The composition_notes should describe framing and blocking, not character appearance
- Write for AI image generation — be specific and visual"""

        current_prompt = shot.get("storyboard", {}).get("storyboard_prompt", "")
        current_comp = shot.get("cinematic", {}).get("composition_notes", "")
        shot_type = shot.get("cinematic", {}).get("shot_type", "")
        framing = shot.get("cinematic", {}).get("framing", "")
        label = shot.get("label", "")

        user = f"""Revise this shot based on the director's feedback.

DIRECTOR'S FEEDBACK:
{feedback}

CURRENT SHOT:
  Label: {label}
  Shot type: {shot_type}, Framing: {framing}
  Characters in frame: {chars_str}
  Current storyboard_prompt: {current_prompt}
  Current composition_notes: {current_comp}

Return JSON:
{{
  "storyboard_prompt": "<revised prompt using character NAMES, describing action/setting/mood>",
  "composition_notes": "<revised blocking and framing notes>"
}}"""

        result = self._call_json(system, user, max_tokens=1000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    # ------------------------------------------------------------------
    # Character Visual Consistency
    # ------------------------------------------------------------------

    def generate_character_visuals(
        self,
        chapter_outline: dict,
        screenplay_text: str = "",
        source_text: str = "",
        world_bible: dict = None,
        existing_visuals: dict = None,
    ) -> dict:
        """
        Generate stable visual casting tags for every named character in a chapter.
        These short (15-25 word) tags are prepended to every image prompt featuring
        that character, ensuring visual consistency across shots.

        If existing_visuals is provided, the model must keep those descriptions
        unchanged and only add new characters.
        """
        world_bible = world_bible or {}
        existing_visuals = existing_visuals or {}
        period = world_bible.get("setting", {}).get("period", "ancient Babylon, 600 BCE")
        palette = world_bible.get("visual_language", {}).get("palette", {})

        system = """You are a film casting director creating precise visual references for AI image generation.
For each character, create a SHORT visual tag (15-25 words) that will be prepended to EVERY image prompt
featuring that character. These tags must be highly specific, vivid, and unchanging.

Include: approximate age, body build, hair color/style, facial features (eyes, nose, jaw, beard),
skin tone, and one defining visual trait (scar, jewelry, posture, etc.).
Do NOT include clothing in the visual_tag — put default clothing in costume_default instead.

These tags will be used VERBATIM in hundreds of image prompts. They must be:
- Specific enough that Imagen always generates the same person
- Short enough to leave room for scene-specific prompt content
- Period-appropriate (no modern features)"""

        existing_section = ""
        if existing_visuals:
            existing_section = f"""
EXISTING CHARACTER VISUALS (keep these EXACTLY as-is, only add new characters):
{json.dumps(existing_visuals, indent=2)}
"""

        # Use source text if available, fallback to screenplay
        text_context = ""
        if source_text:
            text_context = f"Source text (first 30K chars):\n{source_text[:30000]}"
        elif screenplay_text:
            text_context = f"Screenplay:\n{screenplay_text[:20000]}"

        user = f"""Create visual casting tags for all named characters in this chapter.

Period: {period}
Color palette: {json.dumps(palette, indent=2)}

Chapter: {chapter_outline.get('title', 'Unknown')}
Characters mentioned: {json.dumps(chapter_outline.get('key_characters', []))}
Summary: {chapter_outline.get('summary', '')}
{existing_section}
{text_context}

Return JSON:
{{
  "characters": {{
    "<character_name_lowercase>": {{
      "display_name": "<Full Name as in source>",
      "visual_tag": "<15-25 word physical description — age, build, hair, face, skin, defining trait>",
      "costume_default": "<10-15 word default outfit for this character>"
    }}
  }}
}}

IMPORTANT:
- Use character names exactly as they appear in the source text (lowercase keys)
- Be VERY specific with physical features — vague descriptions like "handsome man" cause inconsistency
- Each character must look distinctly different from others
- visual_tag should NOT include clothing (that goes in costume_default)
- These tags will be reused across 20+ shots — make them rock-solid"""

        result = self._call_json(system, user, max_tokens=3000)
        result["_cost_usd"] = self._estimate_cost(user, str(result))

        # Merge with existing visuals (existing take priority)
        characters = result.get("characters", {})
        for key, val in existing_visuals.items():
            characters[key] = val  # preserve existing
        result["characters"] = characters

        return result

    def generate_stub_character_visuals(
        self,
        stub_characters: list[dict],
        world_bible: dict = None,
        existing_visuals: dict = None,
        source_text: str = "",
        screenplay_context: dict = None,
    ) -> dict:
        """
        Generate visual_tag, costume_default, and full description fields for
        stub characters that were auto-created (e.g. during VoiceRecordingStage) and
        lack visual information.

        Args:
            stub_characters: List of stub character dicts (character_id, display_name, description.role)
            world_bible: Project world bible for period/setting context
            existing_visuals: Already-cast characters for visual contrast
            source_text: Source text for inferring character details
            screenplay_context: Dict mapping character_id -> screenplay dialogue/actions text

        Returns:
            {"characters": {"char_id": {visual_tag, costume_default, description, ...}}}
        """
        world_bible = world_bible or {}
        existing_visuals = existing_visuals or {}
        screenplay_context = screenplay_context or {}
        wctx = self._extract_world_context(world_bible)
        period = wctx["period"]

        system = """You are a film casting director creating character profiles for characters
that were discovered in a screenplay but lack visual descriptions.

For each character, you must infer their likely appearance from:
1. Their SCREENPLAY DIALOGUE (most important — reveals gender, age, social class, occupation)
2. Their name and role
3. The world setting, period, and social hierarchy
4. Context clues from the source text

Be creative but PERIOD-ACCURATE. Every character must be properly dressed for their social class.

CRITICAL — IMAGE CONSISTENCY:
- "visual_tag": SHORT (15-25 word) physical description for AI image prompts.
  Include: age, build, hair color/style, facial features, skin tone, one defining trait.
  Gender MUST be clear from the tag (e.g., "young woman" not just "young person").
  Do NOT include clothing — that goes in "costume_default".
- "costume_default": 10-15 word period-accurate outfit. Use the clothing_by_class data below
  to assign appropriate dress based on the character's social standing.
- "costume_variants": Each variant MUST be a SPECIFIC 10-15 word period-accurate description
  naming actual garments (tunic, robe, cloak, wrap), materials (linen, wool, leather), and accessories.
  NEVER use vague terms like "work clothes", "formal attire", or "travel garments".
  WRONG: "formal meeting attire"  RIGHT: "clean brown linen tunic with leather vest, bronze clasp belt"
- Each character must look DISTINCTLY different from existing cast members.
- Be extremely specific — vague tags like "a man" cause visual inconsistency."""

        stub_info = []
        for ch in stub_characters:
            desc = ch.get("description", {})
            role = desc.get("role", "") if isinstance(desc, dict) else str(desc)
            cid = ch.get("character_id", ch.get("character_id", "unknown"))
            entry = {
                "character_id": cid,
                "display_name": ch.get("display_name", cid),
                "known_role": role,
                "chapters": ch.get("narrative", {}).get("chapters", []),
            }
            # Attach screenplay dialogue if available
            if cid in screenplay_context:
                entry["screenplay_dialogue"] = screenplay_context[cid]
            stub_info.append(entry)

        existing_section = ""
        if existing_visuals:
            existing_section = f"""
EXISTING CAST (keep these characters visually distinct from them):
{json.dumps(existing_visuals, indent=2)}
"""

        source_section = ""
        if source_text:
            source_section = f"\nSource text (first 30K chars, use for context clues):\n{source_text[:30000]}"

        clothing_section = ""
        if wctx["clothing_by_class"]:
            clothing_section = f"\nPeriod-accurate clothing by social class:\n{json.dumps(wctx['clothing_by_class'], indent=2)}"
        forbidden_section = ""
        if wctx["forbidden_clothing"]:
            forbidden_section = f"\nFORBIDDEN clothing (anachronistic — never use):\n{json.dumps(wctx['forbidden_clothing'])}"

        user = f"""Generate visual profiles for these stub characters that lack visual descriptions.

Period: {period}
Location: {wctx['location']}
Cultural context: {wctx['cultural_context']}
Social hierarchy: {json.dumps(wctx['social_hierarchy'])}
Known occupations in this world: {json.dumps(wctx['occupations'])}
{clothing_section}{forbidden_section}

CHARACTERS NEEDING VISUALS:
{json.dumps(stub_info, indent=2)}
{existing_section}{source_section}

For EACH character, return:
{{
  "characters": {{
    "<character_id>": {{
      "display_name": "<name>",
      "visual_tag": "<15-25 word physical description — age, gender, build, hair, face, skin, defining trait>",
      "costume_default": "<10-15 word period-accurate outfit matching their social class>",
      "description": {{
        "role": "<their actual role — inferred from dialogue and actions, NOT 'Speaking character'>",
        "archetype": "<character archetype>",
        "physical_appearance": "<2-3 sentence detailed physical description>",
        "age": <estimated_age_number>,
        "personality_traits": ["trait1", "trait2", "trait3"]
      }},
      "animation": {{
        "motion_style": "<how they move>",
        "posture": "<posture description>",
        "facial_expression_hints": "<expression tendencies>"
      }}
    }}
  }}
}}

CRITICAL RULES:
1. READ each character's screenplay_dialogue carefully — it reveals who they ARE
2. Infer gender from dialogue content, name, and context (e.g., "young_woman" = female)
3. Infer social class from their role/occupation and assign clothing accordingly
4. costume_default must use period-accurate {period} dress — use the clothing_by_class data
5. The role field must describe their ACTUAL role, NOT "Speaking character (auto-created)"
6. visual_tag must make gender unmistakable
7. Be period-accurate to {period} — no anachronistic items"""

        # Scale max_tokens: ~300 tokens per character, minimum 4000
        max_tokens = max(4000, len(stub_characters) * 350)
        result = self._call_json(system, user, max_tokens=min(max_tokens, 8192))
        result["_cost_usd"] = self._estimate_cost(user, str(result))
        return result

    def regenerate_character_visuals(
        self,
        characters: list[dict],
        world_bible: dict = None,
        source_text: str = "",
        screenplay_context: dict = None,
    ) -> dict:
        """
        Regenerate visual_tag, costume_default, and description for characters
        that ALREADY HAVE these fields but need them corrected to be
        world-accurate and screenplay-aligned.

        Unlike generate_stub_character_visuals which fills blanks, this method
        REPLACES existing values using the screenplay as ground truth.

        Args:
            characters: List of full character dicts (with existing visual_tag, etc.)
            world_bible: Project world bible for period/setting context
            source_text: Source text for context
            screenplay_context: Dict mapping character_id -> screenplay dialogue/actions text

        Returns:
            {"characters": {"char_id": {visual_tag, costume_default, description, ...}}}
        """
        world_bible = world_bible or {}
        screenplay_context = screenplay_context or {}
        wctx = self._extract_world_context(world_bible)
        period = wctx["period"]

        system = """You are a film casting director REVISING character profiles to be
accurate to the screenplay and world setting.

The existing visual descriptions may have errors:
- Wrong occupation (e.g., described as "craftsman" when screenplay shows they're a musician)
- Missing or wrong gender
- Clothing that doesn't match the period or the character's social class
- Generic descriptions that lack specificity

Your job is to RE-DERIVE each character's visual identity from their SCREENPLAY DIALOGUE
and ACTIONS. The screenplay is the DEFINITIVE source of truth — override any existing
descriptions that contradict what the character actually says and does.

CRITICAL — IMAGE CONSISTENCY:
- "visual_tag": SHORT (15-25 word) physical description for AI image prompts.
  Include: age, gender, build, hair color/style, facial features, skin tone, one defining trait.
  Gender MUST be clear. Do NOT include clothing — that goes in "costume_default".
- "costume_default": 10-15 word period-accurate outfit matching their social class.
- "costume_variants": Each variant MUST be a SPECIFIC 10-15 word period-accurate description
  naming actual garments (tunic, robe, cloak, wrap), materials (linen, wool, leather), and accessories.
  NEVER use vague terms like "work clothes", "formal attire", or "travel garments".
  WRONG: "casual outfit"  RIGHT: "simple linen tunic, no jewelry, comfortable leather sandals"
- Be extremely specific — vague tags cause visual inconsistency across hundreds of images."""

        char_info = []
        for ch in characters:
            cid = ch.get("character_id", "unknown")
            desc = ch.get("description", {})
            role = desc.get("role", "") if isinstance(desc, dict) else str(desc)
            entry = {
                "character_id": cid,
                "display_name": ch.get("display_name", cid),
                "current_role": role,
                "current_visual_tag": ch.get("visual_tag", ""),
                "current_costume": ch.get("costume_default", ""),
                "chapters": ch.get("narrative", {}).get("chapters", []),
            }
            if cid in screenplay_context:
                entry["screenplay_dialogue"] = screenplay_context[cid]
            char_info.append(entry)

        clothing_section = ""
        if wctx["clothing_by_class"]:
            clothing_section = f"\nPeriod-accurate clothing by social class:\n{json.dumps(wctx['clothing_by_class'], indent=2)}"
        forbidden_section = ""
        if wctx["forbidden_clothing"]:
            forbidden_section = f"\nFORBIDDEN clothing (anachronistic — never use):\n{json.dumps(wctx['forbidden_clothing'])}"

        source_section = ""
        if source_text:
            source_section = f"\nSource text (first 30K chars):\n{source_text[:30000]}"

        user = f"""REGENERATE visual profiles for these characters. Their current descriptions
may be wrong or incomplete. Use the screenplay dialogue to determine who they really are.

Period: {period}
Location: {wctx['location']}
Cultural context: {wctx['cultural_context']}
Social hierarchy: {json.dumps(wctx['social_hierarchy'])}
Known occupations: {json.dumps(wctx['occupations'])}
{clothing_section}{forbidden_section}

CHARACTERS TO REGENERATE (with current possibly-wrong data):
{json.dumps(char_info, indent=2)}
{source_section}

For EACH character, return CORRECTED data:
{{
  "characters": {{
    "<character_id>": {{
      "display_name": "<name>",
      "visual_tag": "<15-25 word CORRECTED physical description — age, gender, build, hair, face, skin, defining trait>",
      "costume_default": "<10-15 word period-accurate outfit matching their social class>",
      "description": {{
        "role": "<CORRECTED role — derived from screenplay dialogue and actions>",
        "archetype": "<character archetype>",
        "physical_appearance": "<2-3 sentence detailed physical description>",
        "age": <estimated_age_number>,
        "personality_traits": ["trait1", "trait2", "trait3"]
      }}
    }}
  }}
}}

CRITICAL RULES:
1. READ each character's screenplay_dialogue — it is the SOURCE OF TRUTH
2. If the screenplay reveals they are a musician, say musician — not craftsman
3. If the screenplay reveals gender (e.g., "young_woman", feminine dialogue), reflect that
4. Infer social class from their role and assign clothing from the clothing_by_class data
5. costume_default must be period-accurate {period} dress
6. visual_tag must make gender UNMISTAKABLE
7. If current_visual_tag is accurate, you may keep it — but VERIFY against screenplay first
8. The role field must reflect their ACTUAL role from the story, not generic labels"""

        # Scale max_tokens based on character count
        max_tokens = max(4000, len(characters) * 400)
        result = self._call_json(system, user, max_tokens=min(max_tokens, 8000))
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
