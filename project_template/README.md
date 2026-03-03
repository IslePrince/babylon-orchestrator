# {{PROJECT_NAME}}

A film production project built with [Babylon Studio](https://github.com/your-username/babylon-orchestrator).

## About This Repo

This repository contains **production data only** — schemas, audio, meshes,
storyboards, and renders. It does not contain the orchestrator code.

To work with this project you need the orchestrator installed separately.

## Setup

```bash
# 1. Clone the orchestrator (if you haven't already)
git clone https://github.com/your-username/babylon-orchestrator
cd babylon-orchestrator
pip install -r requirements.txt
cp .env.example .env
# edit .env with your API keys

# 2. Clone this project repo into your projects directory
cd ~/studio/projects
git clone {{GIT_REMOTE_URL}}

# 3. Start the UI pointing at your projects directory
cd ~/studio/babylon-orchestrator
python ui/server.py --projects-dir ~/studio/projects
# Open http://localhost:5757
```

## Project Details

- **Period:** {{PERIOD}}
- **Location:** {{LOCATION}}
- **Created:** {{CREATED_AT}}
- **Orchestrator version:** {{ORCHESTRATOR_VERSION}}

## Structure

```
{{PROJECT_SLUG}}/
├── project.json          master config: pipeline state, gates, budgets
├── source/               original source text
├── world/                world bible, visual language, rules
├── characters/           character profiles, voice config, expressions
├── chapters/             chapter schemas → scenes → shots
├── assets/               3D asset manifest and generated meshes
├── audio/                ElevenLabs voice output (git LFS)
├── animation/            Cartwheel motion libraries (git LFS)
├── renders/              UE5 render output (git LFS)
└── costs/                API spend ledger
```

## Current Status

See `project.json` → `pipeline.current_stage` for where production is up to.

Run `python orchestrator.py --project . status` for a full breakdown.
