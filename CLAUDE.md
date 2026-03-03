# Babylon Studio Orchestrator — Claude Code Instructions

This file tells Claude Code how to work with this repository.
Read this before making any changes.

## What This Is

A Python orchestrator that drives AI-assisted film production through a staged
pipeline. It coordinates Claude, ElevenLabs, Meshy, Cartwheel, and Stability AI
to take a literary source text from ingest through UE5 scene assembly.

The orchestrator is a TOOL. It operates on PROJECT FOLDERS which are separate
git repos. Never mix orchestrator code with project data.

## Repository Structure

```
babylon-orchestrator/
├── orchestrator.py          CLI entry point — all commands start here
├── core/                    Foundation modules (load once, used everywhere)
│   ├── project.py           Schema loader/writer — the only place JSON is read/written
│   ├── cost_manager.py      Budget enforcement + ledger — ALWAYS use this for API calls
│   ├── state_manager.py     Status queries + drift detection
│   └── git_manager.py       Project repo git operations
├── apis/                    One file per external service
│   ├── base.py              BaseAPIClient — all API clients inherit this
│   ├── claude_client.py     All Claude stage passes
│   ├── elevenlabs.py        Voice generation
│   ├── meshy.py             Mesh + animation (background chars)
│   ├── cartwheel.py         Named character animation
│   └── stability.py         Storyboard images
├── stages/                  Pipeline stage orchestrators
│   ├── pipeline.py          Ingest → screenplay → cine → storyboard → audio → assets
│   └── mesh_animation.py    Mesh batches + Cartwheel motion libraries
├── ui/                      Flask dashboard (localhost:5757)
│   ├── server.py            App factory + blueprint registration
│   ├── routes/              One blueprint file per concern
│   ├── templates/           Jinja2 HTML templates
│   └── static/              JS + CSS
├── project_template/        Copied into every new project on init
│   └── (schema stubs)
├── CLAUDE.md                This file
├── README.md                User-facing install + quickstart
├── requirements.txt         Pinned Python dependencies
└── .env.example             API key template (never commit .env)
```

## Key Rules — Read Before Editing Anything

### Never write JSON directly
All schema reads and writes go through `core/project.py`. Never use `open()`
on a JSON file directly in stage or UI code. The Project class handles paths,
validation, and saves atomically.

### Always use CostManager before API calls
Every external API call must go through cost_manager.check_api_allowed() and
cost_manager.check_budget() first. Never call an API client directly from a
route handler or stage without these checks.

### Gates are sacred
The gate system (storyboard_to_audio, audio_to_assets, etc.) prevents
unintended spending. Never call check_api_allowed() with a gate parameter
set to None unless the stage genuinely has no gate. When in doubt, add a gate.

### Progress callbacks are required for UI stages
Every stage run() method signature must be:
```python
def run(self, ..., dry_run=False, progress_callback=None):
```
The UI uses SSE streaming via progress_callback. Stages that don't accept it
will block the UI without feedback.

### Project paths via Project class only
Never hardcode paths like `project_root + "/chapters/"`. Always use:
```python
self.project._path("chapters", chapter_id, "chapter.json")
```

### No global state in Flask
Every route handler reloads what it needs from the Project instance.
No module-level caches of project data. The JSON files are the state.

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure API keys
cp .env.example .env
# edit .env

# Start the UI (point at your projects directory)
python ui/server.py --projects-dir ~/studio/projects

# Or use the CLI directly
python orchestrator.py --project ~/studio/projects/my_project status
```

## Running Tests

```bash
# There are no automated tests yet.
# Test approach: use --dry-run on all stages to verify cost estimates
# without making real API calls.
python orchestrator.py --project ~/studio/projects/test_project run ingest \
  --source test_project/source.txt --dry-run
```

## Common Tasks for Claude Code

### Adding a new stage
1. Create a class in stages/ inheriting from PipelineStage
2. Add run() with dry_run=False and progress_callback=None parameters
3. Wire into orchestrator.py cmd_run() dispatch
4. Add the stage to project_template/project.json pipeline.stages array
5. Add a route in ui/routes/stage_routes.py

### Adding a new API client
1. Create apis/yournewapi.py inheriting from BaseAPIClient
2. Set API_NAME, BASE_URL, ENV_KEY class attributes
3. Implement _headers() returning auth headers
4. Add cost estimate method following existing pattern
5. Add to .env.example with comments

### Debugging a stage that fails silently
Check in order:
1. Is the gate open? Run: orchestrator.py status and check gates section
2. Is the API key in .env? Check: python -c "from apis.yournewapi import YourClient; YourClient()"
3. Is the budget exceeded? Check: orchestrator.py costs
4. Is the schema missing? Check: orchestrator.py status --chapter ch01

### Adding a new UI page
1. Add route in ui/routes/project_routes.py
2. Create template extending base.html
3. Add JSON API endpoint in ui/routes/api_routes.py
4. Add nav link in ui/templates/base.html sidebar
5. Register blueprint in ui/server.py if new blueprint

## Project Template

The project_template/ directory contains schema stubs that are copied into
every new project on init. If you change a schema structure, update BOTH:
- The corresponding template file in project_template/
- The Project class loader/writer in core/project.py

## Animation Routing — Important

Meshy animation: background characters ONLY (mesh + motion in one pipeline)
Cartwheel: named characters ONLY (MetaHuman compatible, better quality)

This is enforced by checking character.json animation.cartwheel.api_enabled.
Named characters have this set to true. Background character types do not have
a character.json — they use background_character_type.json instead.

## Schema Version Tracking

Every shot.json has world_version_built_against. When the world bible is
updated, run: orchestrator.py check-drift to find shots that need rebuild.
Never skip this check before a UE5 scene assembly stage.
