"""
apis/prompt_builder.py — Storyboard & character sheet prompt construction.

Builds natural-language image generation prompts with:
  - Storyboard drawing medium (pen, ink & marker — always colorful)
  - Scene elements from world bible (lighting, palette, production design)
  - Scene description with named characters (not generic "two men")
  - Inline character descriptions (parenthetical) to avoid reference-sheet look
  - Composition/blocking from cinematic data
  - Camera and quality directives
  - Character sheet training images (poses, expressions, costumes)
  - LoRA trigger word injection for character consistency

SD3.5 Large Turbo supports ~10,000 char prompts via T5 encoder,
so we can be detailed without truncation.

Used by ComfyUIClient, StabilityClient, and GoogleImagenClient.
"""

import re
from typing import Optional

# Storyboard drawing medium — pen/ink/marker produces colorful concept art,
# not B&W like pencil & charcoal.  This is the rendering technique prefix.
STORYBOARD_MEDIUM = (
    "Pen, ink, and marker storyboard illustration, bold confident linework, "
    "rich saturated color washes, cinematic composition, "
    "film pre-production concept art style"
)

# Legacy alias — external code may import this name
DEFAULT_VISUAL_STYLE = STORYBOARD_MEDIUM

# Terms to strip from world bible visual_style before combining with
# the storyboard medium.  These describe the FINAL rendered look (e.g.
# "photorealistic cinematic film still") not the storyboard medium.
_PHOTOREALISTIC_TERMS = [
    "photorealistic cinematic film still",
    "cinematic film still",
    "photorealistic",
    "photo-realistic",
    "photo realistic",
    "film still",
    "color photograph",
    "photograph",
]


def adapt_style_for_storyboard(visual_style: str) -> str:
    """
    Combine the storyboard drawing medium with scene elements from the
    world bible visual_style.

    Strips photorealistic medium descriptors (which conflict with the
    storyboard negative prompt) while preserving scene-grounding elements
    like lighting, production design, palette, and composition.

    Example:
        Input:  "Photorealistic cinematic film still, warm golden-hour
                 lighting, epic ancient Mesopotamian production design,
                 dramatic composition, rich earth-tone color palette"
        Output: "Pen, ink, and marker storyboard illustration, bold
                 confident linework, rich saturated color washes, cinematic
                 composition, film pre-production concept art style, warm
                 golden-hour lighting, epic ancient Mesopotamian production
                 design, dramatic composition, rich earth-tone color palette"
    """
    if not visual_style or not visual_style.strip():
        return STORYBOARD_MEDIUM

    # Strip photorealistic medium terms (case-insensitive)
    adapted = visual_style
    for term in _PHOTOREALISTIC_TERMS:
        adapted = re.sub(re.escape(term), "", adapted, flags=re.IGNORECASE)

    # Clean up leftover commas and whitespace
    adapted = re.sub(r",\s*,", ",", adapted)
    adapted = adapted.strip(", \t\n")

    if adapted:
        return f"{STORYBOARD_MEDIUM}, {adapted}"
    else:
        return STORYBOARD_MEDIUM


def build_storyboard_prompt(
    scene_prompt: str,
    shot: dict,
    character_visuals: dict,
    visual_style: str = "",
) -> str:
    """
    Build a natural-language storyboard prompt from shot data.

    Args:
        scene_prompt: The storyboard_prompt from shot.json
        shot: Full shot dict (has cinematic, characters_in_frame, etc.)
        character_visuals: Dict of {char_id: {display_name, visual_tag, costume_default, age, ...}}
        visual_style: Art direction string from world_bible (e.g. "oil painting, warm tones")

    Supports costume_override per character in characters_in_frame:
        {"character_id": "kobbi", "costume_override": "dusty workshop attire"}
    """
    if not scene_prompt:
        return ""

    # Callers are responsible for adapting the world bible visual_style
    # via adapt_style_for_storyboard() before passing it here.
    # User-chosen style overrides (from dropdown) are passed directly.
    style = visual_style.strip() or STORYBOARD_MEDIUM
    parts = []

    # 1. Visual style (storyboard medium + scene elements)
    parts.append(style)

    # 2. Character descriptions FIRST — SD3 weights early tokens more heavily,
    #    so canonical appearance must come before scene action to avoid
    #    any competing descriptions in the base prompt overriding them.
    char_descs = _build_inline_characters(shot, character_visuals)
    parts.extend(char_descs)

    # 3. Scene action (storyboard_prompt from cinematographer)
    #    Inject character names in case the prompt still uses generic refs
    char_names = _get_character_names(shot, character_visuals)
    action = _inject_names_into_action(scene_prompt.rstrip(". "), char_names)
    parts.append(action)

    # 4. Blocking from composition_notes
    comp_notes = shot.get("cinematic", {}).get("composition_notes", "")
    if comp_notes:
        parts.append(comp_notes.rstrip(". "))

    # 5. Camera info
    camera = _build_camera_string(shot)
    if camera:
        parts.append(camera)

    # 6. Quality directives
    parts.append("Storyboard quality, expressive ink linework, vivid marker color")

    return ". ".join(parts)


def build_character_reference_prompt(
    character: dict,
    visual_style: str = "",
    world_context: dict = None,
) -> str:
    """
    Build a portrait prompt for generating a character reference image.

    Always uses pen/ink/marker storyboard medium. The visual_style from the
    world bible is adapted (photorealistic terms stripped) to preserve
    world-specific elements (lighting, palette, setting) while keeping
    the pen/ink/marker medium.

    Args:
        character: Full character.json dict
        visual_style: Art direction string from world_bible (will be adapted)
        world_context: Dict with period, location, clothing_by_class, etc.
    """
    # Always use pen/ink/marker — adapt the visual_style to strip photorealistic terms
    if visual_style.strip():
        style = adapt_style_for_storyboard(visual_style)
    else:
        style = STORYBOARD_MEDIUM
    parts = [style, "Character portrait, three-quarter view"]

    # World setting context for period-accurate imagery
    wctx = world_context or {}
    period = wctx.get("period", "")
    if period:
        parts.append(f"{period} setting")

    visual_tag = character.get("visual_tag", "")
    if visual_tag:
        parts.append(visual_tag)

    physical = character.get("description", {}).get("physical_appearance", "")
    if physical:
        parts.append(physical.rstrip(". "))

    costume = character.get("costume_default", "")
    if costume:
        parts.append(f"Wearing {costume}")
    elif wctx.get("clothing_by_class"):
        # Fallback: infer basic period clothing so character is never undressed
        parts.append(f"Wearing period-appropriate {period} clothing")

    parts.append("Detailed face, expressive ink linework, vivid marker color, single character")
    return ". ".join(parts)


def build_negative_prompt(gender_negatives: str = "") -> str:
    """
    Standard negative prompt for storyboard generation.
    Includes anti-deformation and anachronism terms.

    Args:
        gender_negatives: Optional counter-gender terms from
            ``gender_negative_terms()`` to enforce correct character gender.
    """
    base = (
        "photorealistic, 3D render, CGI, watermark, text, logo, signature, "
        "deformed, disfigured, extra limbs, extra fingers, missing fingers, "
        "mutated hands, bad anatomy, bad proportions, ugly, duplicate, "
        "morbid, malformed, fused fingers, too many fingers, long neck, "
        "character reference sheet, character turnaround, multiple views, "
        "t-pose, a-pose, white background, black and white, monochrome, "
        "modern clothing, contemporary objects, plastic, rubber"
    )
    if gender_negatives:
        return f"{gender_negatives}, {base}"
    return base


_MALE_KW = re.compile(r"\b(man|male|boy|father|grandfather)\b", re.IGNORECASE)
_FEMALE_KW = re.compile(r"\b(woman|female|girl|mother|grandmother)\b", re.IGNORECASE)


def _gender_negatives_for(gender_code: str) -> str:
    """Return counter-gender negative terms for a single gender code ('m' or 'f')."""
    if gender_code == "m":
        return "woman, female, feminine, breasts, cleavage, long eyelashes"
    if gender_code == "f":
        return "man, male, masculine, beard, stubble, adam's apple"
    return ""


def detect_character_gender(character: dict) -> str:
    """Detect gender from a character's visual_tag or physical_appearance.

    Returns 'm', 'f', or '' if undetermined.
    """
    tag = character.get("visual_tag", "")
    if not tag:
        tag = character.get("description", {}).get("physical_appearance", "")
    if _FEMALE_KW.search(tag):
        return "f"
    if _MALE_KW.search(tag):
        return "m"
    return ""


def character_gender_negatives(character: dict) -> str:
    """Return counter-gender negative prompt terms for a single character.

    Prevents the model from generating the wrong gender during training
    image creation (e.g. a male character rendered as female).
    """
    return _gender_negatives_for(detect_character_gender(character))


def gender_negative_terms(
    shot: dict,
    character_visuals: dict,
) -> str:
    """Return counter-gender negative prompt terms based on characters in frame.

    When all characters in a shot are male, returns feminine exclusion terms
    (and vice versa) so the image model doesn't default to the wrong gender.
    Returns an empty string when genders are mixed or unknown.
    """
    if not character_visuals:
        return ""

    genders = set()
    for entry in shot.get("characters_in_frame", []):
        cid = entry.get("character_id", "").lower() if isinstance(entry, dict) else str(entry).lower()
        vis = character_visuals.get(cid)
        if not vis:
            continue
        tag = vis.get("visual_tag", "")
        if _FEMALE_KW.search(tag):
            genders.add("f")
        elif _MALE_KW.search(tag):
            genders.add("m")

    if genders == {"m"}:
        return _gender_negatives_for("m")
    if genders == {"f"}:
        return _gender_negatives_for("f")
    return ""


# ── Character helpers ─────────────────────────────────────────

def _get_character_names(shot: dict, character_visuals: dict) -> list:
    """Extract display names for characters in frame."""
    names = []
    for entry in shot.get("characters_in_frame", []):
        cid = entry.get("character_id", "").lower() if isinstance(entry, dict) else str(entry).lower()
        vis = character_visuals.get(cid)
        if vis and vis.get("visual_tag"):
            names.append(vis.get("display_name", cid.title()))
    return names


def _build_inline_characters(shot: dict, character_visuals: dict) -> list:
    """
    Build inline character descriptions like:
      "Kobbi (lean 35yo craftsman, sandy hair, brown eyes, wearing brown tunic)"

    This natural-language format tells SD3 what each named person looks like
    without triggering character-sheet generation.
    """
    if not character_visuals:
        return []

    descriptions = []
    for entry in shot.get("characters_in_frame", []):
        if isinstance(entry, dict):
            cid = entry.get("character_id", "").lower()
            costume_override = entry.get("costume_override", "")
        elif isinstance(entry, str):
            cid = entry.lower()
            costume_override = ""
        else:
            continue

        vis = character_visuals.get(cid)
        if not vis:
            continue

        name = vis.get("display_name", cid.title())
        visual_tag = vis.get("visual_tag", "")
        if not visual_tag:
            continue

        # Build parenthetical description
        costume = costume_override or vis.get("costume_default", "")
        inner_parts = [visual_tag]
        if costume:
            inner_parts.append(f"wearing {costume}")

        descriptions.append(f"{name} ({', '.join(inner_parts)})")

    return descriptions


def _build_camera_string(shot: dict) -> str:
    """Build camera description from cinematic block."""
    cinematic = shot.get("cinematic", {})
    parts = []

    framing = cinematic.get("framing", "")
    if framing:
        parts.append(framing.replace("_", " "))

    shot_type = cinematic.get("shot_type", "")
    if shot_type and shot_type != framing:
        parts.append(shot_type.replace("_", " "))

    lens = cinematic.get("lens_mm_equiv")
    if lens:
        parts.append(f"{lens}mm lens")

    cam_move = cinematic.get("camera_movement", {})
    if cam_move.get("type") and cam_move["type"] != "static":
        parts.append(cam_move["type"].replace("_", " "))

    return ", ".join(parts) if parts else ""


# ── Name injection ────────────────────────────────────────────

_GENERIC_REFS = {
    1: [
        ("A man", "{0}"), ("a man", "{0}"),
        ("A woman", "{0}"), ("a woman", "{0}"),
        ("A person", "{0}"), ("a person", "{0}"),
        ("The man", "{0}"), ("the man", "{0}"),
        ("The woman", "{0}"), ("the woman", "{0}"),
        ("A figure", "{0}"), ("a figure", "{0}"),
        ("Stocky man", "{0}"), ("stocky man", "{0}"),
    ],
    2: [
        ("Two men", "{0} and {1}"), ("two men", "{0} and {1}"),
        ("Two women", "{0} and {1}"), ("two women", "{0} and {1}"),
        ("Two people", "{0} and {1}"), ("two people", "{0} and {1}"),
        ("Two figures", "{0} and {1}"), ("two figures", "{0} and {1}"),
        ("A man and a woman", "{0} and {1}"),
    ],
    3: [
        ("Three men", "{0}, {1}, and {2}"),
        ("three men", "{0}, {1}, and {2}"),
        ("Three people", "{0}, {1}, and {2}"),
        ("three people", "{0}, {1}, and {2}"),
    ],
}


def _inject_names_into_action(action: str, char_names: list) -> str:
    """Replace generic character references with actual character names."""
    if not char_names:
        return action

    count = len(char_names)
    replacements = _GENERIC_REFS.get(count, [])

    for pattern, template in replacements:
        if pattern in action:
            try:
                named = template.format(*char_names[:count])
                action = action.replace(pattern, named, 1)
            except (IndexError, KeyError):
                pass

    return action


# ── Character sheet training image prompts ────────────────────

# Pose/angle definitions for full-body training images
_SHEET_POSES = [
    {
        "label": "full_body_front",
        "pose_desc": "full body front view, standing naturally, facing the viewer",
        "framing": "full body shot",
    },
    {
        "label": "full_body_three_quarter",
        "pose_desc": "full body three-quarter view, slight turn, relaxed stance",
        "framing": "full body shot",
    },
    {
        "label": "full_body_profile",
        "pose_desc": "full body side profile view, standing upright",
        "framing": "full body shot, side view",
    },
    {
        "label": "full_body_back",
        "pose_desc": "full body rear view, looking away from viewer",
        "framing": "full body shot from behind",
    },
]

# Expression variations for close-up training images
_SHEET_EXPRESSIONS = [
    {
        "label": "closeup_neutral",
        "expr_desc": "neutral calm expression, direct gaze",
        "framing": "close-up portrait, head and shoulders",
    },
    {
        "label": "closeup_happy",
        "expr_desc": "warm genuine smile, eyes crinkling",
        "framing": "close-up portrait, head and shoulders",
    },
    {
        "label": "closeup_determined",
        "expr_desc": "determined resolute expression, focused intense gaze",
        "framing": "close-up portrait, head and shoulders",
    },
    {
        "label": "closeup_angry",
        "expr_desc": "fierce angry expression, furrowed brow, clenched jaw",
        "framing": "close-up portrait, head and shoulders",
    },
]

# Medium shots with action/context for variety
_SHEET_MEDIUM_SHOTS = [
    {
        "label": "medium_action",
        "action_desc": "in a dynamic action pose, moving with purpose",
        "framing": "medium shot, waist up",
    },
    {
        "label": "medium_sitting",
        "action_desc": "sitting casually, relaxed posture",
        "framing": "medium shot, upper body",
    },
    {
        "label": "medium_walking",
        "action_desc": "walking confidently, mid-stride",
        "framing": "medium full shot, knees up",
    },
    {
        "label": "medium_gesture",
        "action_desc": "gesturing expressively while speaking, animated hands",
        "framing": "medium shot, waist up",
    },
]

# Style tag appended to every training caption
_TRAINING_STYLE_TAG = "pen ink and marker illustration style"


def make_trigger_word(character_id: str) -> str:
    """
    Generate a unique trigger word for a character LoRA.

    Uses the pattern {character_id}_char to create a rare token
    that won't collide with normal vocabulary.

    Example: "kobbi" → "kobbi_char"
    """
    clean_id = character_id.lower().strip().replace(" ", "_")
    return f"{clean_id}_char"


def build_character_sheet_prompts(
    character: dict,
    world_context: dict = None,
) -> list:
    """
    Build all training image prompts for a character sheet.

    Returns a list of dicts, each containing:
      - prompt:   Full generation prompt for ComfyUI
      - caption:  Caption text to write to the .txt file (for LoRA training)
      - filename: Output filename stem (e.g. "kobbi_full_body_front_default")
      - label:    Short human-readable label

    Args:
        character: Full character.json dict with visual_tag, costume_default, etc.
        world_context: Dict with period, location, clothing_by_class, etc.
                       Used to inject setting-appropriate backdrop and ensure
                       characters are properly dressed in period clothing.
    """
    char_id = character.get("character_id", "unknown")
    display_name = character.get("display_name", char_id.title())
    visual_tag = character.get("visual_tag", "")
    costume_default = character.get("costume_default", "")
    physical = character.get("description", {}).get("physical_appearance", "")
    costume_variants = character.get("assets", {}).get("costume_variants", [])
    trigger = make_trigger_word(char_id)

    # Use visual_tag as the primary appearance description;
    # fall back to physical_appearance if visual_tag is sparse
    appearance = visual_tag or physical or display_name

    # Extract world context for period-appropriate imagery
    wctx = world_context or {}
    period = wctx.get("period", "")
    location = wctx.get("location", "")
    setting_hint = ""
    if period and location:
        setting_hint = f"{period}, {location}"
    elif period:
        setting_hint = period

    # Build a short period clothing qualifier to inject into vague costume descriptions.
    # This keeps "ancient Babylonian" right next to the clothing tokens in the prompt,
    # preventing SD from defaulting to modern clothes.
    clothing_by_class = wctx.get("clothing_by_class", {})
    period_qualifier = ""
    if period:
        # Extract a short cultural tag from the period string
        # e.g. "605-562 BCE, Neo-Babylonian Empire" → "ancient Babylonian"
        period_lower = period.lower()
        if "babylon" in period_lower:
            period_qualifier = "ancient Babylonian"
        elif "egypt" in period_lower:
            period_qualifier = "ancient Egyptian"
        elif "rome" in period_lower or "roman" in period_lower:
            period_qualifier = "ancient Roman"
        elif "greek" in period_lower:
            period_qualifier = "ancient Greek"
        else:
            # Generic: use the first meaningful word from the period
            period_qualifier = period.split(",")[0].strip()

    def _enrich_costume(costume_desc: str) -> str:
        """Inject period-specific detail into vague costume descriptions."""
        if not costume_desc or not period_qualifier:
            return costume_desc
        # Only count actual garment types as "specific" — not accessories
        # like belt, sash, or materials alone without a garment
        garment_types = {"tunic", "robe", "cloak", "toga", "sari", "chiton",
                         "kilt", "wrap", "shawl", "dress", "gown", "mantle",
                         "stola", "himation", "peplos", "dhoti", "kaftan"}
        desc_lower = costume_desc.lower()
        has_garment = any(w in desc_lower for w in garment_types)
        if has_garment:
            # Already has a specific garment — just prepend period qualifier
            return f"{period_qualifier} {costume_desc}"
        # Vague description like "work clothes" or "better outfit" —
        # expand with period-appropriate common clothing
        common_clothing = clothing_by_class.get("common", "rough linen tunic, rope belt, simple sandals")
        return f"{period_qualifier} {costume_desc} ({common_clothing})"

    # Build the costume list: default + variants
    costumes = []
    if costume_default:
        costumes.append(("default", _enrich_costume(costume_default)))
    else:
        # If no costume_default, try to derive from world clothing_by_class
        fallback_costume = ""
        if clothing_by_class:
            fallback_costume = clothing_by_class.get("common", "")
        costumes.append(("default", _enrich_costume(fallback_costume)))

    for i, variant in enumerate(costume_variants[:3]):
        # costume_variants can be strings or dicts
        if isinstance(variant, str):
            costumes.append((f"costume{i+1}", _enrich_costume(variant)))
        elif isinstance(variant, dict):
            costumes.append((
                variant.get("id", f"costume{i+1}"),
                _enrich_costume(variant.get("description", str(variant))),
            ))

    results = []

    # --- Full-body poses (each costume) ---
    for costume_id, costume_desc in costumes:
        costume_clause = f"wearing {costume_desc}" if costume_desc else ""
        for pose in _SHEET_POSES:
            filename = f"{char_id}_{pose['label']}_{costume_id}"
            caption_parts = [trigger, pose["pose_desc"], appearance]
            if costume_clause:
                caption_parts.append(costume_clause)
            if setting_hint:
                caption_parts.append(setting_hint)
            caption_parts.append(_TRAINING_STYLE_TAG)
            caption = ", ".join(p for p in caption_parts if p)

            prompt_parts = [
                STORYBOARD_MEDIUM,
                f"{display_name}, {pose['pose_desc']}",
                appearance,
            ]
            if costume_clause:
                prompt_parts.append(costume_clause)
            prompt_parts.append(pose["framing"])
            bg = "Single character"
            if setting_hint:
                bg += f", {setting_hint} architectural background, warm earth tones"
            else:
                bg += " on simple background"
            prompt_parts.append(
                f"{bg}, expressive ink linework, vivid marker color"
            )
            prompt = ". ".join(prompt_parts)

            results.append({
                "prompt": prompt,
                "caption": caption,
                "filename": filename,
                "label": f"{display_name} - {pose['label']} ({costume_id})",
            })

    # --- Close-up expressions (default costume only) ---
    costume_clause = f"wearing {costume_default}" if costume_default else ""
    if not costume_clause and wctx.get("clothing_by_class", {}).get("common"):
        costume_clause = f"wearing {wctx['clothing_by_class']['common']}"
    for expr in _SHEET_EXPRESSIONS:
        filename = f"{char_id}_{expr['label']}"
        caption_parts = [trigger, expr["expr_desc"], appearance]
        if costume_clause:
            caption_parts.append(costume_clause)
        if setting_hint:
            caption_parts.append(setting_hint)
        caption_parts.append(_TRAINING_STYLE_TAG)
        caption = ", ".join(p for p in caption_parts if p)

        prompt_parts = [
            STORYBOARD_MEDIUM,
            f"{display_name}, {expr['expr_desc']}",
            appearance,
        ]
        if costume_clause:
            prompt_parts.append(costume_clause)
        prompt_parts.append(expr["framing"])
        prompt_parts.append(
            "Detailed face, single character, simple background, "
            "expressive ink linework, vivid marker color"
        )
        prompt = ". ".join(prompt_parts)

        results.append({
            "prompt": prompt,
            "caption": caption,
            "filename": filename,
            "label": f"{display_name} - {expr['label']}",
        })

    # --- Medium shots (default costume only) ---
    for shot in _SHEET_MEDIUM_SHOTS:
        filename = f"{char_id}_{shot['label']}"
        caption_parts = [trigger, shot["action_desc"], appearance]
        if costume_clause:
            caption_parts.append(costume_clause)
        if setting_hint:
            caption_parts.append(setting_hint)
        caption_parts.append(_TRAINING_STYLE_TAG)
        caption = ", ".join(p for p in caption_parts if p)

        prompt_parts = [
            STORYBOARD_MEDIUM,
            f"{display_name}, {shot['action_desc']}",
            appearance,
        ]
        if costume_clause:
            prompt_parts.append(costume_clause)
        prompt_parts.append(shot["framing"])
        bg = "Single character"
        if setting_hint:
            bg += f", {setting_hint} environmental background, warm earth tones"
        else:
            bg += ", environmental background"
        prompt_parts.append(
            f"{bg}, expressive ink linework, vivid marker color"
        )
        prompt = ". ".join(prompt_parts)

        results.append({
            "prompt": prompt,
            "caption": caption,
            "filename": filename,
            "label": f"{display_name} - {shot['label']}",
        })

    return results


def build_character_sheet_negative(gender_negatives: str = "") -> str:
    """
    Negative prompt for character sheet training images.

    Optimized for clean, isolated character images suitable for LoRA training.
    Suppresses multi-character, turnaround sheet, and photorealistic outputs.

    Args:
        gender_negatives: Counter-gender terms from character_gender_negatives().
                          Prepended to prevent gender drift during generation.
    """
    base = (
        "photorealistic, 3D render, CGI, photograph, watermark, text, logo, "
        "signature, deformed, disfigured, extra limbs, extra fingers, "
        "missing fingers, mutated hands, bad anatomy, bad proportions, "
        "ugly, duplicate, morbid, malformed, fused fingers, too many fingers, "
        "long neck, multiple characters, crowd, group, two people, "
        "character reference sheet, character turnaround, multiple views, "
        "t-pose, a-pose, split screen, comic panels, collage, "
        "black and white, monochrome, grayscale"
    )
    if gender_negatives:
        return f"{gender_negatives}, {base}"
    return base


def inject_trigger_words(prompt: str, character_loras: list) -> str:
    """
    Prepend LoRA trigger words to a storyboard prompt.

    Args:
        prompt: The original storyboard generation prompt.
        character_loras: List of dicts with 'trigger_word' key, one per
                         character in the shot that has a trained LoRA.

    Returns:
        Prompt with trigger words prepended, e.g.:
        "kobbi_char, nina_char, <original prompt>"
    """
    if not character_loras:
        return prompt

    triggers = []
    for lora in character_loras:
        tw = lora.get("trigger_word", "")
        if tw and tw not in triggers:
            triggers.append(tw)

    if not triggers:
        return prompt

    trigger_prefix = ", ".join(triggers)
    return f"{trigger_prefix}, {prompt}"


# ----------------------------------------------------------------------
# Sound-FX context
# ----------------------------------------------------------------------

def _scan_screenplay_for_scene(screenplay_text: str,
                               scene_index: int = 0) -> tuple[str, str]:
    """Walk a screenplay looking for the Nth INT./EXT. scene heading.

    Returns (heading, first_action_block). ``scene_index`` is 0-based.
    """
    lines = screenplay_text.split("\n")
    found = -1
    heading = ""
    action_lines: list[str] = []
    collecting = False
    for raw in lines:
        line = raw.strip()
        clean = line[2:-2].strip() if line.startswith("**") and line.endswith("**") else line
        is_heading = clean.startswith("INT.") or clean.startswith("EXT.")
        if is_heading:
            if collecting:
                break  # next scene
            found += 1
            if found == scene_index:
                heading = clean
                collecting = True
            continue
        if collecting and line:
            # Stop at the first character cue (ALL CAPS) or transition.
            if clean.startswith("FADE") or clean.startswith("CUT TO"):
                break
            if re.fullmatch(r"[A-Z][A-Z\s'.\d]+(?:\s*\([A-Z'. ]+\))?", clean):
                break
            action_lines.append(clean)
            if sum(len(l) for l in action_lines) > 600:
                break
    return heading, " ".join(action_lines)


def _locate_dialogue_action(screenplay_text: str, needle: str) -> tuple[str, str]:
    """Find ``needle`` (a dialogue excerpt) in the screenplay and return
    the scene heading that owns it plus the action lines immediately
    preceding the dialogue.

    The action block is the narrative text between the most-recent
    INT./EXT. heading (or the prior dialogue line, whichever is closer)
    and the matched dialogue. That's the part of the screenplay that
    describes what's visible/audible when the shot fires — which is
    exactly the context Claude needs for SFX.
    """
    if not needle:
        return "", ""
    idx = screenplay_text.lower().find(needle.strip().lower()[:60])
    if idx < 0:
        return "", ""

    lines = screenplay_text[:idx].split("\n")
    heading = ""
    action: list[str] = []
    # Walk backward until we hit a scene heading. Cap at ~1500 chars so
    # we comfortably reach the scene heading on long scenes with
    # preamble, while still keeping the payload tight.
    for raw in reversed(lines):
        line = raw.strip()
        clean = line[2:-2].strip() if line.startswith("**") and line.endswith("**") else line
        if not clean:
            continue
        if clean.startswith("INT.") or clean.startswith("EXT."):
            heading = clean
            break
        if clean.startswith("FADE") or clean.startswith("CUT TO"):
            break
        # Skip ALL-CAPS character cues and parentheticals
        if re.fullmatch(r"[A-Z][A-Z\s'.\d]+(?:\s*\([A-Z'. ]+\))?", clean):
            continue
        if clean.startswith("(") and clean.endswith(")"):
            continue
        action.append(clean)
        if sum(len(l) for l in action) > 1500:
            break
    action.reverse()
    # Keep the newest ~800 chars — those are the action beats closest
    # to the dialogue, which carry the most relevant SFX cues (e.g.
    # "the unexpected TWANGING of lyre strings"). Walking forward from
    # the tail lets us cut on a word boundary.
    joined = " ".join(action)
    if len(joined) > 800:
        tail = joined[-800:]
        sp = tail.find(" ")
        if sp >= 0:
            tail = tail[sp + 1:]
        joined = tail
    return heading, joined


def build_sfx_context_block(project, chapter_id: str,
                            shot: Optional[dict] = None,
                            scene_index: int = 0) -> str:
    """Return a compact scene-first context block for Claude's SFX pass.

    Stable Audio Open (and other foley generators) work best with
    short, concrete prompts. Heavy world-bible context tends to make
    Claude write flowery descriptions that the generator can't render
    faithfully. So this block is deliberately lean:

    - A single-line era+location stamp (prevents "modern hum" drift)
    - The scene heading
    - A compressed opening-action snippet

    We drop weather, daily rhythms, and the anachronism watchlist —
    those are useful for storyboards but noise for SFX prompting.
    """
    parts: list[str] = []

    try:
        wb = project.load_world_bible().get("world_bible", {})
    except Exception:  # noqa: BLE001
        wb = {}

    setting = wb.get("setting", {}) or {}
    period = (setting.get("time_period") or "").strip()
    location = (setting.get("location") or "").strip()
    if period or location:
        stamp = " — ".join(b for b in (period, location) if b)
        parts.append(f"Era/Location: {stamp}")

    # Scene context. If the shot has dialogue_in_shot, locate THAT
    # dialogue in the screenplay and grab the immediately-preceding
    # action block — that's what's happening on screen when the shot
    # fires. Fall back to the first scene's heading/action if no
    # dialogue or no match.
    try:
        sp_path = project._path("chapters", chapter_id, "screenplay.md")
        if sp_path.exists():
            sp_text = sp_path.read_text(encoding="utf-8")
            heading, action = "", ""
            dialogue_in_shot = (shot or {}).get("dialogue_in_shot") or []
            if dialogue_in_shot:
                heading, action = _locate_dialogue_action(sp_text, dialogue_in_shot[0])
            if not (heading or action):
                if shot is not None and scene_index == 0:
                    scene_id = shot.get("scene_id") or ""
                    m = re.search(r"sc(\d+)", scene_id)
                    if m:
                        scene_index = max(0, int(m.group(1)) - 1)
                heading, action = _scan_screenplay_for_scene(sp_text, scene_index)
            if heading:
                parts.append(f"Scene heading: {heading}")
            if action:
                # _locate_dialogue_action already caps/tail-trims to the
                # most recent action; use it whole so the lyre-twang /
                # door-slam / hoof-beats right before dialogue survive.
                parts.append(f"Action on screen when shot fires: {action}")
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(parts)
