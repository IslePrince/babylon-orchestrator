# Babylon Studio MCP — Agent Guide

This document is written **for the AI agent** that will drive the
orchestrator from another machine on the LAN. If you are reading it as
an LLM tool-caller, treat the numbered sections as the canonical order
for building a short-form video end-to-end.

---

## 0 · Connection

- Transport: Server-Sent Events (SSE) over HTTP.
- URL: `http://<server-host>:5758/sse`
- **No authentication**. The server trusts anything on the same LAN.
  Don't expose this port to the public internet.

Your MCP client library (e.g. `mcp`, `anthropic`, or LangChain's
MCP connector) handles the SSE framing. Once connected you'll see
the full tool catalogue via `list_tools`.

---

## 1 · Mental model

Everything in Babylon Studio happens inside a **project**. A project
is a self-contained directory on the server holding:

```
<project>/
├── project.json       master config, budgets, gates
├── source/<slug>.txt  original story
├── world/world_bible.json
├── characters/<id>/character.json + reference.png + <id>_char.safetensors
├── chapters/<ch>/
│   ├── chapter.json
│   ├── screenplay.md
│   └── shots/<shot_id>/shot.json + storyboard.png + preview.mp4
├── audio/<ch>/
│   ├── recordings.json
│   ├── lines/<line_id>.mp3
│   └── <shot_id>/sfx_NNN.mp3
└── costs/ledger.json
```

The pipeline walks those files forward stage-by-stage. You, the agent,
are driving that walk. Each tool is a thin wrapper around one pipeline
stage or one file edit.

### Long vs short calls

- **Short (< 30s):** `list_*`, `get_*`, `update_*`, `set_*`,
  `approve_*`, `mix_preview_audio`. Block synchronously.
- **Long (minutes to hours):** storyboard, voice recording, LoRA
  training, preview video. Use the `_async` variants — they return a
  `job_id`. Then call `wait_for_job(job_id, timeout_sec=900)` or poll
  `get_job_status(job_id)`.

If a blocking call takes too long for your transport, your MCP client
may disconnect. Prefer `_async` + `wait_for_job` for anything that
could exceed 60 seconds.

### Gates

Spending is gated. Before certain stages run, a human (or you, via
`approve_gate`) must open the corresponding gate. Current gates:

| Gate | Guards |
|---|---|
| `screenplay_to_voice_recording` | ElevenLabs dialogue generation |
| `cut_to_sound` | ElevenLabs SFX + music score |
| `sound_to_assets` | Meshy 3D asset generation |
| `assets_to_scene` | UE5 scene assembly |
| `preview_to_final` | Final render |

When a stage fails with `GateLockError`, call `approve_gate(slug,
"<gate_name>")` and retry.

### Budgets

`project.json → budgets` caps each API's spend. The CostManager
aborts stages that would exceed the cap. Call `get_cost_ledger(slug)`
to check where you are before committing.

---

## 2 · Recommended sequence for a new project

The tools are designed to be called in roughly this order. You can
skip steps for existing projects (use `get_project_status(slug)` to
know where you are).

```
                   ┌─────────────────────┐
1. create_project  │ slug, display_name, │
                   │ source_text         │
                   └──────────┬──────────┘
                              ▼
2. run_ingest_async     ──►   wait_for_job
                              │ → chapters + world bible exist
                              ▼
3. list_chapters        ──►   pick chapter_id (often "ch01")
                              ▼
4. run_screenplay             (per chapter)
   get_screenplay
   revise_screenplay          (feedback loop as needed)
   approve_screenplay
                              ▼
5. run_characters             (once all screenplays approved)
   list_characters
   update_character            (tweak descriptions, visual_tags)
                              ▼
6. list_elevenlabs_voices
   auto_cast_voices       OR   set_character_voice (one at a time)
                              ▼
7. run_character_sheets_async ──►  wait_for_job
   generate_character_reference_image
   train_character_lora_async ──►  wait_for_job  (optional but improves consistency)
                              ▼
8. approve_gate("screenplay_to_voice_recording")
   run_voice_recording_async ──►  wait_for_job     (COSTS MONEY — dry_run first)
                              ▼
9. run_cinematographer        (breaks screenplay into shots with slices)
   list_shots / get_shot / update_shot
                              ▼
10. run_storyboard_async ──►  wait_for_job
    regenerate_shot_storyboard  (per-shot fixups)
                              ▼
11. editing-room tweaks:
    set_shot_enabled, set_shot_duration
                              ▼
12. approve_gate("cut_to_sound")
    run_sound_fx_async ──►  wait_for_job    (ComfyUI-first; cheap-to-free)
    run_audio_score_async ──►  wait_for_job
                              ▼
13. run_preview_video_async(chapter_id=...) ──►  wait_for_job
    # silent shots auto-use Wan I2V, dialogue shots use InfiniTalk
    # post-mix of SFX into the mp4 is automatic

14. mix_preview_audio(slug, shot_id)  (re-mix on gain/offset tweaks;
                                       no re-render)
```

After step 13 you have `preview.mp4` (and optionally `preview_vertical.mp4`)
on disk for each shot. That's the short-form "animatic" — not the final
UE5 render, but playable and shareable.

---

## 3 · Worked example: short-form video from a .txt story

You receive: a file path on the server with a 2-page short story.

```jsonc
// Step 1: bootstrap
create_project(
  slug="richest_man",
  display_name="The Richest Man in Babylon (test)",
  source_text_path="/nfs/stories/babylon.txt",
  budget_usd={"claude": 20, "elevenlabs": 10, "meshy": 0,
              "stability": 5}
)
// → { "slug": "richest_man", "next_step": "run_ingest" }
```

```jsonc
// Step 2: ingest (~1 min)
job = run_ingest_async(slug="richest_man")
// → { "job_id": "abc123" }
wait_for_job("abc123", 180)
// → { "status": "complete", "result": { "chapters_created": 3, ... } }
```

```jsonc
// Step 3: per-chapter
list_chapters("richest_man")
// → [ { "chapter_id": "ch01", "title": "The Man Who Desired Gold" }, ... ]

run_screenplay("richest_man", "ch01")             // ~2 min, blocks
approve_screenplay("richest_man", "ch01")          // auto-approve
```

```jsonc
// Step 4: characters + voices
run_characters("richest_man")
// → { "created": ["kobbi","bansir"], ... }

auto_cast_voices("richest_man", "ch01")
// → { "cast": [ { "character_id": "kobbi", "assigned": "AmY1pcg..." }, ... ] }

// Optional per-character tweak
update_character(
  "richest_man", "bansir",
  { "voice": { "stability": 0.6, "similarity_boost": 0.85 } }
)
```

```jsonc
// Step 5: character sheets + reference images
job = run_character_sheets_async("richest_man")
wait_for_job(job["job_id"], 1800)

generate_character_reference_image("richest_man", "kobbi")
```

```jsonc
// Step 6: voice recording
approve_gate("richest_man", "screenplay_to_voice_recording")

job = run_voice_recording_async("richest_man", "ch01", dry_run=True)
wait_for_job(job["job_id"], 120)   // ← review cost estimate first

job = run_voice_recording_async("richest_man", "ch01")  // real run
wait_for_job(job["job_id"], 900)
```

```jsonc
// Step 7: shots + storyboards
run_cinematographer("richest_man", "ch01")

job = run_storyboard_async("richest_man", "ch01")
wait_for_job(job["job_id"], 7200)     // up to 2 hours for a big chapter
```

```jsonc
// Step 8: SFX + score + preview video
approve_gate("richest_man", "cut_to_sound")

job = run_sound_fx_async("richest_man", "ch01")
wait_for_job(job["job_id"], 1800)

job = run_audio_score_async("richest_man", "ch01")
wait_for_job(job["job_id"], 1800)

job = run_preview_video_async(
  slug="richest_man",
  chapter_id="ch01",
  orientation="both",
  force=False,
)
wait_for_job(job["job_id"], 14400)    // ~4 hours for a full chapter
```

At the end you have `preview.mp4` + `preview_vertical.mp4` for every
shot under `chapters/ch01/shots/<shot_id>/`. Each one is a talking-
head / silent animatic with the baked mix of dialogue + SFX.

---

## 4 · Tool reference summary

Grouped by pipeline phase. Every tool has a full docstring visible
via your MCP client's `list_tools`/`describe_tool`.

### Project lifecycle
| Tool | Purpose |
|---|---|
| `list_projects` | Enumerate every project on this server. |
| `get_project_status(slug)` | Stage + per-chapter state + gates. |
| `create_project(slug, display_name, source_text=..., source_text_path=..., budget_usd=...)` | Bootstrap a new project. |
| `write_source_text(slug, text)` | Overwrite / set the source story after bootstrap. |

### Ingest
| Tool | Purpose |
|---|---|
| `run_ingest(slug, source_text_path=None, dry_run=False)` | Blocking ingest. |
| `run_ingest_async(...)` | Same, returns job_id. |
| `list_chapters(slug)` | Chapter summaries after ingest. |
| `get_world_bible(slug)` | Period/palette/anachronism rules. |

### Screenplay
| Tool | Purpose |
|---|---|
| `run_screenplay(slug, chapter_id)` | Claude writes screenplay.md. |
| `get_screenplay(slug, chapter_id)` | Read current text + metadata. |
| `revise_screenplay(slug, chapter_id, feedback)` | Feedback-driven revise. |
| `approve_screenplay(slug, chapter_id)` | Mark human-reviewed. |

### Characters + voices
| Tool | Purpose |
|---|---|
| `run_characters(slug)` | Extract named speakers into stubs. |
| `list_characters(slug)` | Current roster + voice/LoRA status. |
| `get_character(slug, character_id)` | Full character.json. |
| `update_character(slug, character_id, updates)` | Shallow-merge edits. |
| `list_elevenlabs_voices()` | Fetch the current voice catalogue. |
| `set_character_voice(slug, character_id, voice_id, ...)` | Manual casting. |
| `auto_cast_voices(slug, chapter_id=None)` | Claude-assisted casting. |

### Character visuals (sheets + LoRAs)
| Tool | Purpose |
|---|---|
| `run_character_sheets(slug, character_id=None, force=False)` | Blocking. |
| `run_character_sheets_async(...)` | Same, returns job_id. |
| `generate_character_reference_image(slug, character_id)` | Single full-body portrait. |
| `train_character_lora_async(slug, character_id, force=False)` | Train a LoRA (30+ min). |

### Cinematographer + shots
| Tool | Purpose |
|---|---|
| `run_cinematographer(slug, chapter_id, scene_id=None)` | Shots + dialogue slices. |
| `list_shots(slug, chapter_id)` | Summaries. |
| `get_shot(slug, chapter_id, shot_id)` | Full shot.json. |
| `update_shot(slug, chapter_id, shot_id, updates)` | Shallow-merge. |

### Storyboard
| Tool | Purpose |
|---|---|
| `run_storyboard_async(slug, chapter_id, force=False)` | Batch render. |
| `regenerate_shot_storyboard(slug, chapter_id, shot_id, feedback=None)` | Single re-roll. |

### Voice recording + editing
| Tool | Purpose |
|---|---|
| `run_voice_recording_async(slug, chapter_id, dry_run=False)` | ElevenLabs dialogue gen. |
| `set_shot_enabled(slug, chapter_id, shot_id, enabled, notes=None)` | Toggle in/out of cut. |
| `set_shot_duration(slug, chapter_id, shot_id, duration_sec)` | Override duration. |
| `approve_gate(slug, gate_name)` | Open a pipeline gate. |

### Sound FX + score
| Tool | Purpose |
|---|---|
| `run_sound_fx_async(slug, chapter_id, provider="auto", dry_run=False)` | Generate SFX; auto router. |
| `run_audio_score_async(slug, chapter_id, dry_run=False)` | Music cues. |
| `mix_preview_audio(slug, shot_id, orientation="horizontal")` | Re-mix SFX into mp4 without re-rendering. |

### Preview video
| Tool | Purpose |
|---|---|
| `run_preview_video_async(slug, shot_id=None, chapter_id=None, orientation="horizontal", force=False)` | Wan 2.1 + InfiniTalk. |

### Costs / gates / jobs
| Tool | Purpose |
|---|---|
| `get_cost_ledger(slug)` | Per-API totals + transactions. |
| `get_gates(slug)` | Gate approval table. |
| `check_drift(slug)` | Shots out-of-date vs current world bible. |
| `get_job_status(job_id)` | One job's state. |
| `wait_for_job(job_id, timeout_sec=900)` | Block until done/timeout. |
| `list_jobs()` | Every job since server start. |

---

## 5 · Conventions, pitfalls, idioms

- **slug names** are lowercase dash/underscore strings, e.g.
  `babylon-film`, `richest_man`. They map directly to the project
  folder name.
- **chapter_id** is always `ch01`, `ch02`, … — never a title.
- **shot_id** format: `ch01_sc01_sh006`. The scene_id is derivable
  as the first two underscored parts; most tools derive it for you.
- Every mutation tool returns the new state so you don't have to
  read-back.
- If a call raises `GateLockError`, approve the named gate and
  retry. You *can* approve all gates up-front, but you lose the
  cost-safety net.
- When a stage times out, check `get_job_status(job_id)` —
  progress keeps updating even when your wait timed out. A single
  long job can be polled indefinitely.
- For speed, prefer `provider="comfyui"` on `run_sound_fx_async` —
  it skips ElevenLabs entirely and uses local Stable Audio Open. The
  default `"auto"` is a smart hybrid.

---

## 6 · Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ComfyUIError: Cannot connect to ComfyUI` | ComfyUI isn't running on the orchestrator host. Start it first (port 8000 by default). |
| `GateLockError: API '...' is locked` | Call `approve_gate(...)` with the gate name the error mentioned. |
| `Missing ComfyUI assets` from a video/SFX tool | Check the model listings; the orchestrator's env vars (`WAN_UNET`, `INFINITETALK_PATCH`, etc.) can override expected filenames. |
| Tool hangs past `wait_for_job` timeout | It's usually still running. Poll `get_job_status` directly. |
| Video looks static even when dialogue plays | Re-run `run_preview_video_async` with `force=True`. Earlier runs pre-dated the prompt-enrichment fix and generate minimal motion. |
| SFX too loud / too quiet | Put a `gain` field (0.0–1.0) on the shot's `audio.sound_effects[i]` (via `update_shot`) and call `mix_preview_audio` — no re-render needed. |
