"""
test_dry_run.py -- Exercises all pipeline stages in dry_run mode.
Catches import errors, missing data, schema mismatches, and runtime bugs
without making any API calls.

Usage:
    cd orchestrator
    python3 test_dry_run.py --projects-dir D:/babylon-orchestrator/projects
"""

import sys
import os
import traceback
from pathlib import Path

# Add orchestrator root to sys.path so core/ and stages/ resolve
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.project import Project
from core.cost_manager import CostManager
from core.state_manager import StateManager


def green(text):
    return f"\033[92m{text}\033[0m"


def red(text):
    return f"\033[91m{text}\033[0m"


def yellow(text):
    return f"\033[93m{text}\033[0m"


def test_imports():
    """Test that all stage modules import cleanly."""
    print("\n" + "=" * 55)
    print("  TEST: Module Imports")
    print("=" * 55)

    modules = [
        ("stages.pipeline", ["IngestStage", "ScreenplayStage", "CinematographerStage",
                             "StoryboardStage", "VoiceRecordingStage", "AssetManifestStage"]),
        ("stages.mesh_animation", ["MeshStage", "AnimationStage"]),
        ("core.project", ["Project"]),
        ("core.cost_manager", ["CostManager"]),
        ("core.state_manager", ["StateManager"]),
        ("core.git_manager", ["GitManager"]),
    ]

    all_ok = True
    for mod_path, classes in modules:
        try:
            mod = __import__(mod_path, fromlist=classes)
            for cls_name in classes:
                getattr(mod, cls_name)
            print(f"  {green('[OK]')} {mod_path}: {', '.join(classes)}")
        except Exception as e:
            print(f"  {red('[FAIL]')} {mod_path}: {e}")
            all_ok = False

    return all_ok


def test_project_loading(project_path):
    """Test that project loads and key methods work."""
    print("\n" + "=" * 55)
    print("  TEST: Project Loading")
    print("=" * 55)

    try:
        project = Project(project_path)
        print(f"  {green('[OK]')} Project loaded: {project.id}")
    except Exception as e:
        print(f"  {red('[FAIL]')} Could not load project: {e}")
        return None

    # Test key accessors
    tests = [
        ("get_pipeline_stage()", lambda: project.get_pipeline_stage()),
        ("get_all_chapter_ids()", lambda: project.get_all_chapter_ids()),
        ("get_all_character_ids()", lambda: project.get_all_character_ids()),
        ("load_world_bible()", lambda: project.load_world_bible()),
        ("load_cost_ledger()", lambda: project.load_cost_ledger()),
        ("load_chapter_index()", lambda: project.load_chapter_index()),
        ("is_api_enabled('claude')", lambda: project.is_api_enabled("claude")),
        ("get_budget_remaining('claude')", lambda: project.get_budget_remaining("claude")),
        ("is_gate_open('screenplay_to_voice_recording')", lambda: project.is_gate_open("screenplay_to_voice_recording")),
    ]

    for name, fn in tests:
        try:
            result = fn()
            display = str(result)[:60]
            print(f"  {green('[OK]')} {name} ->{display}")
        except FileNotFoundError as e:
            print(f"  {yellow('[WARN]')} {name} ->FileNotFoundError (expected if not yet created)")
        except Exception as e:
            print(f"  {red('[FAIL]')} {name} ->{e}")

    return project


def test_state_manager(project):
    """Test StateManager queries."""
    print("\n" + "=" * 55)
    print("  TEST: StateManager")
    print("=" * 55)

    sm = StateManager(project)

    tests = [
        ("get_project_status()", lambda: sm.get_project_status()),
        ("get_chapter_status('ch01')", lambda: sm.get_chapter_status("ch01")),
    ]

    for name, fn in tests:
        try:
            result = fn()
            # Verify key fields exist
            if "chapters" in str(name):
                if "get_project_status" in name:
                    chapters = result.get("chapters", [])
                    print(f"  {green('[OK]')} {name} ->{len(chapters)} chapters, stage={result.get('pipeline_stage')}")
                else:
                    scenes = result.get("scenes", [])
                    shot_info = []
                    for sc in scenes:
                        shots = sc.get("shots", {})
                        shot_info.append(f"{sc['scene_id']}: {shots.get('total', 0)} shots, ids={len(shots.get('shot_ids', []))}")
                    print(f"  {green('[OK]')} {name} ->{len(scenes)} scenes: {', '.join(shot_info) or 'none'}")
            else:
                print(f"  {green('[OK]')} {name}")
        except Exception as e:
            print(f"  {red('[FAIL]')} {name} ->{e}")
            traceback.print_exc()


def test_stage_dry_runs(project):
    """Run every stage in dry_run mode."""
    print("\n" + "=" * 55)
    print("  TEST: Stage Dry Runs")
    print("=" * 55)

    from stages.pipeline import (
        IngestStage, ScreenplayStage, CinematographerStage,
        StoryboardStage, VoiceRecordingStage, AssetManifestStage
    )
    from stages.mesh_animation import MeshStage, AnimationStage

    chapter_ids = project.get_all_chapter_ids()
    ch1 = chapter_ids[0] if chapter_ids else None
    source_path = str(project.root / "source" / "source.txt")
    has_source = Path(source_path).exists()

    progress_log = []

    def progress_callback(pct, msg, cost=0.0):
        progress_log.append((pct, msg, cost))

    # List of (name, stage_class, kwargs)
    stages_to_test = []

    if has_source:
        stages_to_test.append(
            ("IngestStage", IngestStage, {"source_text_path": source_path})
        )
    else:
        print(f"  {yellow('[SKIP]')} IngestStage -- no source.txt")

    if ch1:
        stages_to_test.extend([
            ("ScreenplayStage", ScreenplayStage, {"chapter_id": ch1}),
            ("CinematographerStage", CinematographerStage, {"chapter_id": ch1}),
            ("StoryboardStage", StoryboardStage, {"chapter_id": ch1}),
            ("VoiceRecordingStage", VoiceRecordingStage, {"chapter_id": ch1}),
        ])
    else:
        print(f"  {yellow('[SKIP]')} Chapter stages -- no chapters yet")

    stages_to_test.extend([
        ("AssetManifestStage", AssetManifestStage, {}),
        ("MeshStage", MeshStage, {}),
        ("AnimationStage", AnimationStage, {}),
    ])

    for name, StageClass, extra_kwargs in stages_to_test:
        progress_log.clear()
        try:
            stage = StageClass(project)
            kwargs = {"dry_run": True, "progress_callback": progress_callback, **extra_kwargs}
            result = stage.run(**kwargs)
            status = result.get("status", "?")
            pct_reached = max((p[0] for p in progress_log), default=0)
            print(f"  {green('[OK]')} {name:<25} status={status:<12} progress={pct_reached}%")
        except Exception as e:
            print(f"  {red('[FAIL]')} {name:<25} {type(e).__name__}: {e}")
            traceback.print_exc()


def test_cost_manager(project):
    """Test CostManager methods."""
    print("\n" + "=" * 55)
    print("  TEST: CostManager")
    print("=" * 55)

    cm = CostManager(project)

    tests = [
        ("check_api_allowed('claude')", lambda: cm.check_api_allowed("claude")),
        ("estimate_claude(1000, 500)", lambda: cm.estimate_claude(1000, 500)),
        ("estimate_elevenlabs('test')", lambda: cm.estimate_elevenlabs("test text")),
        ("estimate_meshy('medium')", lambda: cm.estimate_meshy("medium")),
        ("estimate_imagen(1)", lambda: cm.estimate_imagen(1)),
        ("check_budget('claude', 0.01)", lambda: cm.check_budget("claude", 0.01)),
    ]

    for name, fn in tests:
        try:
            result = fn()
            if result is not None:
                print(f"  {green('[OK]')} {name} ->{result}")
            else:
                print(f"  {green('[OK]')} {name}")
        except Exception as e:
            print(f"  {red('[FAIL]')} {name} ->{type(e).__name__}: {e}")


def test_api_routes_data(project):
    """Test that API response data shapes are correct."""
    print("\n" + "=" * 55)
    print("  TEST: API Data Shapes")
    print("=" * 55)

    # Simulate what the costs endpoint returns
    ledger = project.load_cost_ledger()
    totals = ledger.get("totals", {})
    total_spent = totals.get("total", 0)
    print(f"  {green('[OK]')} Ledger totals.total = ${total_spent:.4f}")

    # Check transaction field names
    txs = ledger.get("transactions", [])
    if txs:
        tx = txs[0]
        if "cost_usd" in tx:
            print(f"  {green('[OK]')} Transactions use 'cost_usd' field (correct)")
        elif "amount_usd" in tx:
            print(f"  {red('[FAIL]')} Transactions use 'amount_usd' -- should be 'cost_usd'")
        else:
            print(f"  {yellow('[WARN]')} Transaction fields: {list(tx.keys())}")
    else:
        print(f"  {yellow('[WARN]')} No transactions in ledger")

    # Check chapter structure for scene_ids
    for cid in project.get_all_chapter_ids()[:3]:
        ch = project.load_chapter(cid)
        acts = ch.get("structure", {}).get("acts", [])
        scene_ids = []
        for act in acts:
            scene_ids.extend(act.get("scene_ids", []))
        shots_dir = project._path("chapters", cid, "shots")
        has_shots = shots_dir.exists()
        if has_shots and not scene_ids:
            print(f"  {red('[FAIL]')} {cid}: has shots/ dir but structure.acts is empty!")
        elif scene_ids:
            print(f"  {green('[OK]')} {cid}: structure.acts has scene_ids: {scene_ids}")
        else:
            print(f"  {green('[OK]')} {cid}: no shots yet (structure.acts empty, expected)")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--projects-dir", required=True)
    args = parser.parse_args()

    projects_dir = Path(args.projects_dir)
    if not projects_dir.exists():
        print(f"Projects dir not found: {projects_dir}")
        sys.exit(1)

    # Find first project
    project_path = None
    for item in projects_dir.iterdir():
        if (item / "project.json").exists():
            project_path = str(item)
            break

    if not project_path:
        print(f"No projects found in {projects_dir}")
        sys.exit(1)

    print(f"\n{'#' * 55}")
    print(f"  Babylon Studio -- Dry Run Test Suite")
    print(f"  Project: {project_path}")
    print(f"{'#' * 55}")

    # Run all tests
    imports_ok = test_imports()
    if not imports_ok:
        print(f"\n{red('ABORT: Import errors found. Fix these first.')}")
        sys.exit(1)

    project = test_project_loading(project_path)
    if not project:
        print(f"\n{red('ABORT: Cannot load project.')}")
        sys.exit(1)

    test_state_manager(project)
    test_cost_manager(project)
    test_api_routes_data(project)
    test_stage_dry_runs(project)

    print(f"\n{'=' * 55}")
    print(f"  All tests complete.")
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    main()
