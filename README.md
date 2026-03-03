# Babylon Studio

An AI-orchestrated film production pipeline. Takes a literary source text and
drives it through staged production — story arc, world bible, screenplay, shot
list, storyboard, voice, 3D assets, animation — coordinating multiple AI
services while protecting you from runaway costs.

Built for Unreal Engine 5 output. Produces cinematic 16:9 master and vertical
9:16 for social media from a single production.

> **Status:** Active development. Core pipeline and CLI complete.
> Web UI in progress. UE5 scene assembly stage not yet built.

---

## What It Does

```
Source text (.txt)
    │
    ▼
[Ingest] → chapter outlines + world bible draft        ~$0.12
    │
    ▼
[Screenplay] → per-chapter screenplay                  ~$0.08/chapter
    │
    ▼
[Cinematographer] → shot list with camera specs        ~$0.05/chapter
    │
    ▼
[Storyboard] → placeholder images (16:9 + 9:16)       ~$0.08/shot pair
    │
    ▼  ← GATE: human reviews storyboards
    │
[Audio] → ElevenLabs voice for all dialogue            ~$0.30/1k chars
    │
    ▼
[Assets] → Meshy 3D mesh generation                   $0.10-0.50/asset
    │
[Animate] → Cartwheel motion for named characters     ~$0.15/clip
    │
    ▼  ← GATE: human reviews before UE5 work
    │
[UE5 Assembly] → scene build                          (coming soon)
```

Cost gates prevent automatic spending at every tier boundary.
Every stage supports `--dry-run` to estimate costs before committing.

---

## Requirements

- Python 3.11+
- Git + Git LFS (`git lfs install` once per machine)
- API keys for the services you want to use (see `.env.example`)

---

## Installation

```bash
git clone https://github.com/your-username/babylon-orchestrator
cd babylon-orchestrator
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your API keys
```

Start the web UI:

```bash
python ui/server.py --projects-dir ~/studio/projects
# Open http://localhost:5757
```

Or use the CLI directly:

```bash
python orchestrator.py --project ~/studio/projects/my_project status
```

---

## Creating a Project

Projects are **separate git repos** from the orchestrator. One orchestrator
serves many projects.

**Recommended setup:**

1. Create an empty repo on GitHub for your project
2. Open the UI at `http://localhost:5757`
3. Click **New Project** and paste the clone URL
4. The wizard handles cloning, schema init, LFS config, and first push

**Or via CLI:**

```bash
# Clone your empty project repo
git clone git@github.com:you/my-project ~/studio/projects/my-project

# Initialize as a Babylon Studio project
python orchestrator.py --project ~/studio/projects/my-project init-repo
```

---

## Running Your First Production

```bash
# Point at your project
export PROJECT=~/studio/projects/my-project

# Estimate costs before spending anything
python orchestrator.py --project $PROJECT run ingest \
  --source source/my_book.txt --dry-run

# Run ingest (cheap — Claude only)
python orchestrator.py --project $PROJECT run ingest \
  --source source/my_book.txt

# Chain through to storyboard (all cheap stages)
python orchestrator.py --project $PROJECT run screenplay --chapter ch01
python orchestrator.py --project $PROJECT run cinematographer --chapter ch01
python orchestrator.py --project $PROJECT run storyboard --chapter ch01

# Review storyboards in the UI, then approve the gate
python orchestrator.py --project $PROJECT approve-gate storyboard_to_audio

# Now audio and beyond (costs real money — always dry-run first)
python orchestrator.py --project $PROJECT run audio --chapter ch01 --dry-run
python orchestrator.py --project $PROJECT run audio --chapter ch01
```

---

## Project Structure

```
my-project/                    ← separate git repo, separate from orchestrator
├── project.json               master config: pipeline, gates, budgets
├── world/
│   └── world_bible.json       visual language, locations, rules
├── characters/
│   ├── _index.json
│   └── arkad/
│       ├── character.json     voice, animation, appearance
│       └── notes.md
├── chapters/
│   └── ch01/
│       ├── chapter.json
│       ├── screenplay.md
│       └── shots/
│           └── ch01_sc01_sh001/
│               ├── shot.json
│               └── notes.md
├── assets/
│   └── manifest.json          deduped 3D asset list with Meshy briefs
├── audio/                     ElevenLabs MP3 output
├── meshes/                    Meshy FBX output
└── costs/
    └── ledger.json            every API transaction logged
```

---

## Animation Routing

| Character type | Tool | Why |
|---|---|---|
| Named characters (Arkad, Bansir…) | Cartwheel | MetaHuman compatible, better performance nuance |
| Background / crowd | Meshy animation | Mesh + motion in one pipeline, cheaper |

---

## Services Used

| Service | Purpose | Required |
|---|---|---|
| Anthropic Claude | Story passes, screenplay, shot lists | Yes |
| ElevenLabs | Character voices | For audio stage |
| Meshy.ai | 3D asset generation + bg animation | For asset stage |
| Cartwheel | Named character animation | For animation stage |
| Stability AI | Storyboard placeholder images | For storyboard stage |

Each service can be skipped by not adding its API key. The pipeline runs
up to whichever stage requires a missing key.

---

## Cost Budgets

Set per-API budgets in the New Project wizard or directly in `project.json`.
The orchestrator will stop and ask before exceeding any budget.

Example conservative budget for a 14-chapter project:

| Service | Budget |
|---|---|
| Claude | $50 |
| ElevenLabs | $30 |
| Meshy | $100 |
| Cartwheel | $50 |
| Stability AI | $5 |
| **Total** | **$235** |

---

## License

MIT — free to use, modify, and distribute.
If you build something with this, a mention would be appreciated.

---

## Contributing

Issues and PRs welcome. Please open an issue before a large PR to discuss
the approach first.
