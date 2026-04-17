"""
apis/sfx_router.py
Decide which generator to use for each SFX prompt.

Stable Audio Open (local, free via ComfyUI) handles broadband foley —
footsteps, fabric, wood creak, wind, crowd ambience, water, animals —
really well, but slips into synth/drum territory on tonal sources
(lyres, bells, chimes, horns, singing, whistles) and struggles with
specific named voices or complex layered scenes.

ElevenLabs' sound-generation is the opposite: better on tonal/musical
instrumental SFX and named character vocalizations, but costs money
per clip.

The router picks a provider per prompt with simple keyword heuristics
biased toward ComfyUI (free) when the prompt looks like broadband
foley. Callers can override with ``force_provider``.
"""

from __future__ import annotations

import re
from typing import Literal

Provider = Literal["comfyui", "elevenlabs"]


# Keywords that push us toward ElevenLabs. These are sources Stable
# Audio Open tends to misrender as music — pitched instruments,
# ceremonial/ritual sounds, explicit singing/chanting — or complex
# scenes where quality matters more than cost.
_ELEVENLABS_KEYWORDS = {
    # pitched instruments
    "lyre", "harp", "flute", "pipe", "panpipe", "horn", "trumpet",
    "bell", "bells", "chime", "chimes", "gong", "cymbal", "drum solo",
    # ceremonial / religious
    "temple bell", "ceremonial", "ritual", "prayer chant", "hymn",
    # voiced / vocalised
    "chant", "chanting", "sing", "singing", "song", "melody",
    "humming", "whistling", "whistle", "scream", "shout", "laugh",
    "cry", "wail", "weeping", "moan", "gasp",
    # specific animal vocalizations that need character
    "lion roar", "wolf howl", "eagle cry",
    # music / orchestral
    "orchestral", "choir", "string quartet", "music",
    # crowd dialogue (words, not just murmur)
    "marketplace haggling", "merchants shouting", "vendors calling",
    "argument", "shouting match",
}

# Keywords that confirm broadband foley — ComfyUI handles these well.
_COMFYUI_FRIENDLY_KEYWORDS = {
    "footstep", "footsteps", "sandal", "sandals", "boots", "shoe",
    "cloth", "fabric", "robe", "rustle", "rustling",
    "wood", "wooden", "creak", "creaking", "crack",
    "leather", "rope",
    "wind", "breeze", "gust", "sandstorm",
    "dust", "dirt", "earth", "sand", "stone",
    "water", "pour", "splash", "drip", "flow", "stream",
    "crowd", "chatter", "murmur", "hubbub", "ambience", "ambient",
    "market", "street", "bustle", "distant",
    "horse", "hoof", "hooves", "donkey", "camel", "goat", "cattle",
    "chariot wheels", "wheel rumbling", "cart", "wagon",
    "door", "slam", "latch",
    "fire", "crackle", "torch", "flame",
    "workshop", "tools", "hammer",
}


def route(prompt: str, force_provider: Provider | None = None) -> Provider:
    """Pick a provider for a single SFX prompt.

    Returns ``"comfyui"`` or ``"elevenlabs"``. Defaults to comfyui when
    no keywords match, on the theory that most SFX are broadband foley
    and cost-free generation is the right default.
    """
    if force_provider in ("comfyui", "elevenlabs"):
        return force_provider

    p = (prompt or "").lower()
    tokens = set(re.findall(r"[a-z]+(?:\s+[a-z]+)?", p))

    def _any(keys: set[str]) -> bool:
        return any(k in p for k in keys)

    # Strong push to ElevenLabs first — these are the failure modes we
    # actually observed (lyre => drums, bells => synth pads).
    if _any(_ELEVENLABS_KEYWORDS):
        return "elevenlabs"

    # Friendly foley keyword → ComfyUI (explicit confirmation so the
    # default isn't just "whatever I couldn't classify").
    if _any(_COMFYUI_FRIENDLY_KEYWORDS):
        return "comfyui"

    # Unknown — default to the cheap provider.
    return "comfyui"


def explain(prompt: str) -> tuple[Provider, str]:
    """Return (provider, reason) for logging / UI display."""
    p = (prompt or "").lower()
    hits_el = sorted(k for k in _ELEVENLABS_KEYWORDS if k in p)
    if hits_el:
        return "elevenlabs", f"matched tonal keywords: {', '.join(hits_el[:3])}"
    hits_cu = sorted(k for k in _COMFYUI_FRIENDLY_KEYWORDS if k in p)
    if hits_cu:
        return "comfyui", f"matched foley keywords: {', '.join(hits_cu[:3])}"
    return "comfyui", "no keyword match (default)"
