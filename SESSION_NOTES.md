# Session notes — 2026-04-16 → 2026-04-17

A rolling log of substantive work, non-obvious decisions, and lessons
learned per session. Future sessions should append rather than
rewrite. Keep entries narrative-length — the goal is a future agent
(human or AI) pulling this up and understanding *why* the code is
the way it is, not just *what* changed.

---

## 2026-04-17 — Wan InfiniTalk preview pipeline + safety tooling

Starting state: orchestrator had CLI + Flask UI + MCP server and was
producing SFX and dialogue mp3s cleanly. Goal this session: make
ch01 render as a full short-form animatic end-to-end, and add the
guardrails we discovered we needed along the way.

### Features shipped

- **Wan 2.1 + InfiniTalk preview video pipeline**
  - `apis/comfyui_video.py` with `generate_preview_video()` (talking
    heads via WanInfiniteTalkToVideo) and `generate_silent_video()`
    (plain Wan I2V for non-dialogue shots)
  - `PreviewVideoStage` in `stages/pipeline.py` dispatches
    single-shot vs chapter-batch and talking vs silent automatically
  - Programmatic workflow construction (not JSON templating) so we
    can switch `single_speaker` ↔ `two_speakers` per shot
  - Post-mix step auto-layers SFX into the rendered mp4 via ffmpeg;
    keeps a pristine `preview_raw.mp4` alongside so re-mixes are
    free-and-instant instead of a 10-minute Wan re-render

- **Editing Room got three batch buttons** (Sound FX / All Previews
  / Diversify Split Storyboards) with proper confirmations and
  progress streaming

- **Shot-splitting + recovery tooling**
  - `utils/split_long_shots.py` — word-boundary-aware split of any
    shot whose dialogue slice exceeds a cap (default 10s)
  - `utils/recover_lost_shot_slices.py` — detects coverage gaps in
    each recording and reconstructs the missing shot
  - `utils/diversify_split_storyboards.py` — asks Claude for an
    alternate camera angle per continuation + re-renders the
    storyboard through ComfyUI with parent seed + LoRAs, so cuts
    between a shot and its continuation don't show the same frame

- **Hard cap in PreviewVideoStage** (`PREVIEW_VIDEO_MAX_SEC`, 12s
  default) refuses to dispatch shots longer than the cap and names
  the split utility in the error message

- **GPU serialization** via `core/gpu_lock.py` — an RLock-backed
  `gpu_exclusive(...)` context that every GPU-heavy stage acquires
  on entry. Reentrant so batches that acquire once can call into
  per-shot workers. Exposed as `gpu_status()` MCP tool for agents
  to introspect who's holding it

- **ComfyUI unstick** — `utils/comfyui_unstick.py` + MCP tool
  sending `/interrupt` + `/queue clear` and optionally hard-killing
  the ComfyUI process when the sampler wedges

- **MCP server** — 43 tools covering the full pipeline, with
  background-job management (`_spawn_job` / `get_job_status` /
  `wait_for_job`). Long-running stages get `_async` variants.
  Written up for agents in `MCP_GUIDE.md`

### Lessons learned

1. **Wan/InfiniTalk can wedge on long clips.** A 22.5s shot ran
   for 300+ minutes without progress. Root cause: sampler hang on
   windows far outside training distribution (model is happy up to
   ~10s, unreliable past 15s). Interrupt via `/interrupt` doesn't
   always unstick it — sometimes the Python process itself needs a
   hard kill. Hence `PREVIEW_VIDEO_MAX_SEC` + `split_long_shots.py`.
   **Never dispatch a shot over 12s.**

2. **Split utilities need naming collision protection.** Pass 1
   produced `sh024b` for the 22.5s-shot's tail. Pass 2 of the same
   utility was called to split the now-12s-first-half, also
   produced `sh024b`, overwriting the disk content of pass-1's
   `sh024b`. The index ended up with 108 entries (4 dupes). Lesson:
   `_new_shot_id` must walk existing suffixes (`b, c, d, ...`) and
   avoid any already-on-disk name. `_insert_after` must refuse to
   insert a duplicate shot_id. The recovery tool exists because of
   this bug — it detects recording-coverage gaps left by overwrites
   and reconstructs the missing shot.

3. **Claude's SFX suggestions are only as good as the context we
   give it.** Started with just `{shot_id, label, shot_type,
   camera_movement}` → got "soft footsteps on wooden floor" and
   "room tone with subtle background hum" for an ancient Babylonian
   street scene. Enriching with world bible period + anachronism
   watchlist got period-appropriate prompts. Enriching with the
   action lines *immediately preceding the shot's dialogue* (not
   just the chapter's opening action) got hyper-specific ones like
   "lyre strings gentle pluck" for the scripted "unexpected
   TWANGING of lyre strings" beat. **Put the shot-local action text
   first in the Claude prompt. Keep it short and concrete.**

4. **Claude's action descriptions need to be wrapped in the same
   style prefix the main pipeline uses, or SDXL drops the style.**
   My first diversify implementation passed Claude's raw action
   prompt straight to ComfyUI and the rendered continuation lost
   the pen-and-ink look entirely. Fix: route through
   `apis/prompt_builder.build_storyboard_prompt` — the same helper
   StoryboardStage uses. It prepends the `STORYBOARD_MEDIUM` string,
   the world bible's adapted `visual_style`, and the character
   inline descriptions; then appends composition / camera / quality
   directives. **Always go through the shared prompt builder.**

5. **Splits need to inherit the parent's seed.** Using a fresh
   random seed per diversify render meant character identity and
   lighting could drift visibly across the cut. Inheriting
   `shot.storyboard.generation_meta.seed` from the parent (with a
   `seed_source` stamp in the child's generation_meta so we can
   audit later) + the same LoRA keeps everything on-model across
   split halves.

6. **LoRA paths matter.** Hard-coding
   `characters/<cid>/<cid>_char.safetensors` was wrong — the real
   layout is `characters/<cid>/lora/<cid>_char.safetensors`. Silent
   failure: `generate_storyboard_with_loras` got an empty config
   list and fell back to LoRA-less generation, which broke identity
   on the diversified continuations. Sign of this happening:
   `generation_meta.loras_used == []` on a shot whose parent has
   `loras_used == ['<cid>_char.safetensors']`. Always verify.

7. **`setTimeout` chains are a terrible way to drive visual
   playback.** Our Play Cut chained `setTimeout(playNext,
   visualDurMs)` per shot. On long runs, tab focus changes or a
   long style reflow would push a timer late; the cut audio had
   already clamped itself at slice end; and the user would
   perceive "moved on too early". Rewrote to a single 100ms poll
   loop that checks `performance.now() >= deadline`. Self-corrects
   on tab de-throttle, can't double-fire, immune to drift.

8. **Idempotency on all re-runnable stages.** Split, Diversify,
   Generate All Previews were all initially "re-run the whole
   set". A restart mid-run + re-click meant re-doing completed
   work. Fixes: skip if `preview.video_ref` already set (previews),
   skip if `generation_meta.diversified_from` is set (diversify),
   force flag opts-in to a fresh re-roll. **Every GPU-expensive
   stage must have a skip-unless-force behavior.**

9. **Confusing confirm dialog nearly burned a GPU-hour.** Had
   "OK = skip / Cancel = force regenerate" on the Generate All
   Previews confirm. A user hitting Cancel to abort silently
   triggered force-regen across the whole chapter. **Cancel must
   always mean abort. Force goes behind its own separate
   confirm.**

10. **Windows stdout encoding will bite you.** PowerShell's cp1252
    default can't encode `→` / em-dash / bullets. A `print("...→...")`
    inside a stage callback crashes the whole job with a cryptic
    `charmap` error. Two-layer fix: avoid unicode in new
    `print()` calls, and reconfigure `sys.stdout` / `sys.stderr`
    to UTF-8 with `errors="replace"` at the top of every server
    entry point (`ui/server.py`, `mcp_server.py`).

11. **`importlib.import_module` + Werkzeug debug reloader is
    flaky.** Stages are loaded via `importlib.import_module()` in
    `stage_routes._import_stage` and then cached in `sys.modules`.
    Werkzeug's file watcher reloads the process on source change,
    but subtle cases (new file added, parallel thread holding old
    module) don't always catch it. After any stage code change,
    hard-kill port 5757 and restart rather than relying on debug
    reload.

12. **Wan I2V produces minimal motion without a prompt that
    describes action.** First-pass prompts were generic ("Character
    speaking on camera, subtle body motion"). Wan happily produced
    visually-still videos. Enriching the Wan prompt with the same
    `storyboard_prompt` used for the still (which already describes
    pose + gesture + context) + the preceding action lines gave us
    walking, gesturing, turning motion. **Tell the I2V model what
    the character is doing.**

13. **ComfyUI API gotchas**
    - `CheckpointLoaderSimple` on Stable Audio Open doesn't produce
      a usable CLIP output; Stable Audio needs a separate
      `CLIPLoader` with `type: "stable_audio"` and a T5 encoder
      file (`t5-base.safetensors`)
    - Node option schemas come in two shapes: legacy
      `[["opt_a","opt_b"], {}]` and newer COMBO
      `["COMBO", {"options": ["opt_a","opt_b"]}]`. Both must be
      handled in any discovery helper
    - `/upload/image` accepts any file type despite the name — mp3
      audio uploads work through the same endpoint
    - `LoadAudio`'s runtime slicing isn't reliable; pre-slice with
      ffmpeg and upload the exact clip you want
    - `/view` serves files range-capable, and `send_file(...,
      conditional=True)` is required on our side for browser
      `<audio>` / `<video>` seek to work

### Architectural patterns that crystallized

- **Stage class per pipeline phase.** Every long-running thing is a
  `PipelineStage` with `.run(..., dry_run=False, progress_callback=None)`.
  Flask stage_routes dispatches via STAGE_MAP. MCP exposes both
  sync and `_async` variants.

- **Core/project.py is the only writer.** No `open(*.json, "w")` in
  stages or UI code — everything goes through `project.save_X()`
  helpers. Atomic writes, path resolution, and schema consistency
  all live there.

- **Background jobs by job_id.** Long stages return immediately
  with a job_id. The MCP tool `wait_for_job` polls internally.
  Flask's SSE stream does the same for the UI. Progress callbacks
  are threaded through unchanged.

- **GPU lock is cooperative.** Any stage that uses the GPU
  explicitly acquires `gpu_exclusive(label)`. Non-GPU stages don't
  contend. Lock is reentrant so batch stages don't deadlock
  themselves.

- **Utility scripts are the source of truth for one-off
  workflows.** split_long_shots, recover_lost_shot_slices,
  diversify_split_storyboards, mix_preview_audio — each is a
  standalone CLI + importable function, and most are also exposed
  via stage wrapper + MCP tool. Lets us re-run surgically.

- **Skip-by-default, force-by-flag.** Every stage that could
  re-do existing work has a skip check (preview.mp4 exists,
  generation_meta.diversified_from is set, recordings.json is
  fresh, etc.) and a `force=True` opt-in.

### Current project state (end of session)

`D:/babylon-orchestrator/projects/babylon-film/chapters/ch01/`:

- **104 shots total** (72 original + 27 splits + 4 recovered +
  re-splits of the big 2)
- **All shots ≤ 10s** — hard cap safety net active
- **All 104 shots have both 16:9 and 9:16 storyboards**
- **All 32 continuations diversified** with alternate-angle
  storyboards + parent seed + character LoRAs
- **Generate All Previews running** — ~14 previews done, ~90
  pending, estimated 15-20 hours remaining on the 4090
  (diversified + bridge-tiled slices + sequential ComfyUI loads)
- **ch01 dialogue fully recorded** — 38 lines in
  `audio/ch01/lines/*.mp3` + `recordings.json`
- **Sound FX** generated chapter-wide (`audio.sound_effects` on
  most shots); routed per-prompt via `apis.sfx_router`

### Known pending / next session

- Let the Generate All Previews batch finish overnight.
- When it completes, audition the cut via Play Cut and the detail
  panel's per-shot Preview Mix. Expect the Clock-anchored driver
  to advance shots reliably; if anything still drifts, check
  `_cutState.deadline` vs `performance.now()` in DevTools.
- Remaining chapters (ch02–ch10) need the same treatment:
  screenplay → voice_recording → cinematographer → storyboard →
  split_long_shots → diversify → sound_fx → preview_video.
  Budget perspective: each chapter adds roughly the same ~$2.50
  ElevenLabs + whatever SFX tonal-route chooses.
- Character sheets + LoRAs aren't current for every speaker; the
  diversify + preview quality will be noticeably better for
  characters that have both a visual_tag and a trained LoRA.
  `run_character_sheets` + `train_character_lora_async` per
  character before the big render.
- The MCP `gpu_status` tool tells an agent whether a GPU stage is
  live. Agents should call it before dispatching preview_video /
  storyboard / diversify / sound_fx — if it's not free, poll
  instead of fail-fast.
