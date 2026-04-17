"""
orchestrator.py — Babylon Studio Production Orchestrator

Usage:
  python orchestrator.py --project ./rmib status
  python orchestrator.py --project ./rmib status --chapter ch01
  python orchestrator.py --project ./rmib init-repo
  python orchestrator.py --project ./rmib run ingest --source source/text.txt
  python orchestrator.py --project ./rmib run ingest --source source/text.txt --dry-run
  python orchestrator.py --project ./rmib run screenplay --chapter ch01
  python orchestrator.py --project ./rmib run cinematographer --chapter ch01
  python orchestrator.py --project ./rmib run cinematographer --chapter ch01 --scene ch01_sc02
  python orchestrator.py --project ./rmib run storyboard --chapter ch01
  python orchestrator.py --project ./rmib run voice-recording --chapter ch01
  python orchestrator.py --project ./rmib run sound-fx --chapter ch01
  python orchestrator.py --project ./rmib run audio-score --chapter ch01
  python orchestrator.py --project ./rmib run assets
  python orchestrator.py --project ./rmib approve-gate screenplay_to_voice_recording
  python orchestrator.py --project ./rmib check-drift
  python orchestrator.py --project ./rmib costs
  python orchestrator.py --project ./rmib git-status
  python orchestrator.py --project ./rmib ready --verbose

Add --dry-run to any run command to preview cost without spending.
"""

import argparse
import sys
from pathlib import Path

from core.project import Project
from core.state_manager import StateManager
from core.cost_manager import CostManager, GateLockError, BudgetExceededError
from core.git_manager import GitManager


def cmd_status(project, args):
    sm = StateManager(project)
    chapter = getattr(args, 'chapter', None)
    if chapter:
        status = sm.get_chapter_status(chapter)
        print(f"\n  Chapter: {status['title']}")
        print(f"  Status:  {status['status']}")
        print(f"  Cost:    ${status['costs']['chapter_total_usd']:.2f}\n")
        for scene in status["scenes"]:
            shots = scene.get("shots", {})
            total = shots.get("total", 0)
            built = shots.get("built", 0)
            audio = shots.get("audio_approved", 0)
            sb = shots.get("storyboard_approved", 0)
            print(f"  {scene['scene_id']}  shots:{total}  sb:{sb}  audio:{audio}  built:{built}")
            for flag in shots.get("flagged", []):
                print(f"    WARNING:{flag}")
    else:
        sm.print_project_status()


def cmd_approve_gate(project, args):
    gate_name = args.gate
    valid = list(project.data["pipeline"]["gates"].keys())
    if gate_name not in valid:
        print(f"  [FAIL] Unknown gate '{gate_name}'")
        print(f"  Valid: {', '.join(valid)}")
        sys.exit(1)
    if project.is_gate_open(gate_name):
        print(f"  [INFO] Gate '{gate_name}' already approved.")
        return
    print(f"\n  Gate: {gate_name}")
    print(f"  This unlocks the next pipeline stage.")
    confirm = input("  Approve? [y/N]: ").strip().lower()
    if confirm == "y":
        project.approve_gate(gate_name)
    else:
        print("  Cancelled.")


def cmd_check_drift(project, args):
    sm = StateManager(project)
    drift = sm.find_version_drift()
    if not drift:
        print("\n  [OK] No version drift. All built shots match current world version.")
        return
    print(f"\n  WARNING:{len(drift)} shots built against old world version:")
    for item in drift:
        print(f"    {item['shot_id']}  world {item['built_against_world']} → {item['current_world']}")


def cmd_costs(project, args):
    cm = CostManager(project)
    cm.print_summary()
    cm.print_stage_summary()


def cmd_git_status(project, args):
    GitManager(project).status_summary()


def cmd_init_repo(project, args):
    gm = GitManager(project)
    gm.init_repo()
    gm.initial_commit()
    print("\n  [OK] Repository ready.")


def cmd_ready(project, args):
    sm = StateManager(project)
    verbose = getattr(args, 'verbose', False)
    storyboard_ready = sm.get_ready_for_storyboard()
    audio_ready = sm.get_ready_for_voice_recording()
    mesh_ready = sm.get_ready_for_mesh_generation()
    print(f"\n  Ready for Storyboard: {len(storyboard_ready)} shots")
    print(f"  Ready for Recording:  {len(audio_ready)} chapters")
    print(f"  Ready for Meshy:      {len(mesh_ready)} assets")
    if verbose:
        for shot_id in storyboard_ready[:10]:
            print(f"    sb:    {shot_id}")
        for line in audio_ready[:10]:
            print(f"    audio: [{line['character_id']}] {line['text'][:55]}...")
        for asset in mesh_ready[:10]:
            print(f"    mesh:  {asset['asset_id']} ({asset['detail_level']})")


def cmd_run(project, args):
    from stages.pipeline import (
        IngestStage, ScreenplayStage, CharacterStage, CinematographerStage,
        StoryboardStage, VoiceRecordingStage, AssetManifestStage
    )
    from stages.character_sheets import CharacterSheetStage, LoRATrainingStage

    stage_name = args.stage
    dry_run = getattr(args, 'dry_run', False)
    chapter = getattr(args, 'chapter', None)
    scene = getattr(args, 'scene', None)

    if dry_run:
        print(f"\n  🔍 DRY RUN — cost estimate only, no API calls")

    try:
        if stage_name == "ingest":
            source = getattr(args, 'source', None)
            if not source:
                print("  [FAIL] --source required for ingest")
                sys.exit(1)
            IngestStage(project).run(source, dry_run=dry_run)

        elif stage_name == "screenplay":
            if not chapter:
                print("  [FAIL] --chapter required")
                sys.exit(1)
            ScreenplayStage(project).run(chapter, dry_run=dry_run)

        elif stage_name == "characters":
            CharacterStage(project).run(dry_run=dry_run)

        elif stage_name == "character-sheets":
            char = getattr(args, 'character', None)
            force = getattr(args, 'force', False)
            CharacterSheetStage(project).run(
                character_id=char, dry_run=dry_run, force=force
            )

        elif stage_name == "lora-train":
            char = getattr(args, 'character', None)
            LoRATrainingStage(project).run(
                character_id=char, dry_run=dry_run
            )

        elif stage_name == "cinematographer":
            if not chapter:
                print("  [FAIL] --chapter required")
                sys.exit(1)
            CinematographerStage(project).run(chapter, scene_id=scene, dry_run=dry_run)

        elif stage_name == "storyboard":
            if not chapter:
                print("  [FAIL] --chapter required")
                sys.exit(1)
            force = getattr(args, 'force', False)
            StoryboardStage(project).run(chapter, dry_run=dry_run, force=force)

        elif stage_name == "voice-recording":
            if not chapter:
                print("  [FAIL] --chapter required")
                sys.exit(1)
            VoiceRecordingStage(project).run(chapter, scene_id=scene, dry_run=dry_run)

        elif stage_name == "sound-fx":
            from stages.pipeline import SoundFXStage
            if not chapter:
                print("  [FAIL] --chapter required")
                sys.exit(1)
            SoundFXStage(project).run(chapter, dry_run=dry_run)

        elif stage_name == "audio-score":
            from stages.pipeline import AudioScoreStage
            if not chapter:
                print("  [FAIL] --chapter required")
                sys.exit(1)
            AudioScoreStage(project).run(chapter, dry_run=dry_run)

        elif stage_name == "assets":
            AssetManifestStage(project).run(chapter_id=chapter, dry_run=dry_run)

        elif stage_name == "meshes":
            from stages.mesh_animation import MeshStage
            batch = getattr(args, 'batch', None)
            MeshStage(project).run(batch_id=batch, dry_run=dry_run)

        elif stage_name == "bg-characters":
            from stages.mesh_animation import MeshStage
            MeshStage(project).run_background_characters(dry_run=dry_run)

        elif stage_name == "animate":
            from stages.mesh_animation import AnimationStage
            AnimationStage(project).run(
                character_id=getattr(args, 'character', None),
                chapter_id=chapter,
                dry_run=dry_run
            )

        elif stage_name == "ue5-notes":
            from stages.mesh_animation import AnimationStage
            char = getattr(args, 'character', None)
            if not char:
                print("  [FAIL] --character required for ue5-notes")
                sys.exit(1)
            AnimationStage(project).print_ue5_notes(char)

        else:
            print(f"  [FAIL] Unknown stage: {stage_name}")
            sys.exit(1)

    except GateLockError as e:
        print(f"\n  🔒 Gate locked: {e}")
        sys.exit(1)
    except BudgetExceededError as e:
        print(f"\n  💸 Budget exceeded: {e}")
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"\n  [FAIL] File not found: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description="Babylon Studio Production Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--project", "-p", required=True,
                        help="Path to project root directory")

    sub = parser.add_subparsers(dest="command")

    # status
    p_status = sub.add_parser("status", help="Project or chapter status")
    p_status.add_argument("--chapter", "-c", help="Chapter ID for detail view")

    # run
    p_run = sub.add_parser("run", help="Run a pipeline stage")
    p_run.add_argument(
        "stage",
        choices=["ingest", "screenplay", "characters", "character-sheets", "lora-train",
                 "voice-recording",
                 "cinematographer", "storyboard",
                 "sound-fx", "audio-score", "assets",
                 "meshes", "bg-characters", "animate", "ue5-notes"]
    )
    p_run.add_argument("--chapter", "-c", help="Chapter ID")
    p_run.add_argument("--scene", "-s", help="Scene ID (optional)")
    p_run.add_argument("--character", help="Character ID (animate/ue5-notes)")
    p_run.add_argument("--batch", help="Batch ID (meshes stage)")
    p_run.add_argument("--source", help="Source text file (ingest only)")
    p_run.add_argument("--dry-run", action="store_true", help="Estimate costs only")
    p_run.add_argument("--force", action="store_true", help="Regenerate even if output exists")

    # approve-gate
    p_gate = sub.add_parser("approve-gate", help="Approve a pipeline gate")
    p_gate.add_argument("gate", help="Gate name")

    # check-drift
    sub.add_parser("check-drift", help="Find shots built against old world version")

    # costs
    sub.add_parser("costs", help="Show cost summary")

    # git-status
    sub.add_parser("git-status", help="Git repository status")

    # init-repo
    sub.add_parser("init-repo", help="Initialize git repo with LFS")

    # ready
    p_ready = sub.add_parser("ready", help="Show what can proceed now")
    p_ready.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        project = Project(args.project)
    except FileNotFoundError as e:
        print(f"\n  [FAIL] {e}")
        sys.exit(1)

    dispatch = {
        "status":       cmd_status,
        "run":          cmd_run,
        "approve-gate": cmd_approve_gate,
        "check-drift":  cmd_check_drift,
        "costs":        cmd_costs,
        "git-status":   cmd_git_status,
        "init-repo":    cmd_init_repo,
        "ready":        cmd_ready,
    }

    dispatch[args.command](project, args)


if __name__ == "__main__":
    main()
