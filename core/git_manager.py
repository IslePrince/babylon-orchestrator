"""
core/git_manager.py
Handles git operations for the production repository.
Each pipeline stage commits its artifacts on a predictable branch.
Tracks versions and enables rollback of any entity to any prior state.
"""

import subprocess
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List
from .project import Project


class GitManager:

    def __init__(self, project: Project):
        self.project = project
        self.root = project.root

    def _git(self, *args, check=True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=False
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed:\n{result.stderr.strip()}"
            )
        return result

    # ------------------------------------------------------------------
    # Repo setup
    # ------------------------------------------------------------------

    def init_repo(self):
        """Initialize git repo and configure LFS for binary assets."""
        self._git("init", check=False)
        self._git("lfs", "install", check=False)

        # Write .gitattributes for LFS tracking
        gitattributes = self.root / ".gitattributes"
        lfs_patterns = self.project.data["git"]["lfs_patterns"]
        lines = [f"{pattern} filter=lfs diff=lfs merge=lfs -text\n" for pattern in lfs_patterns]
        with open(gitattributes, "w") as f:
            f.writelines(lines)

        # Write .gitignore
        gitignore = self.root / ".gitignore"
        ignored = [
            "__pycache__/\n",
            "*.pyc\n",
            ".env\n",
            ".orchestrator_cache/\n",
            "renders/final/\n",  # final renders excluded — too large
            "*.tmp\n"
        ]
        with open(gitignore, "w") as f:
            f.writelines(ignored)

        print(f"  ✓ Git repo initialized with LFS at {self.root}")

    def initial_commit(self):
        """Commit the initial schema tree."""
        self._git("add", ".")
        self._git("commit", "-m", "init: project schema tree created")
        print("  ✓ Initial commit: project schema tree")

    # ------------------------------------------------------------------
    # Branch strategy
    # Each entity gets its own branch for a pipeline stage.
    # branch format: stage/entity-id
    # e.g. screenplay/ch01, storyboard/ch01_sc01, audio/ch01_sc01
    # ------------------------------------------------------------------

    def branch_name(self, stage: str, entity_id: str) -> str:
        return f"{stage}/{entity_id}"

    def create_stage_branch(self, stage: str, entity_id: str, from_branch: str = "main"):
        """Create a new branch for a stage/entity combination."""
        branch = self.branch_name(stage, entity_id)
        # Check if branch already exists
        result = self._git("branch", "--list", branch, check=False)
        if result.stdout.strip():
            print(f"  ℹ Branch '{branch}' already exists, checking out")
            self._git("checkout", branch)
        else:
            self._git("checkout", "-b", branch, from_branch)
            print(f"  ✓ Created branch '{branch}' from '{from_branch}'")

    def checkout(self, branch: str):
        self._git("checkout", branch)

    def checkout_main(self):
        self._git("checkout", "main")

    def current_branch(self) -> str:
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip()

    # ------------------------------------------------------------------
    # Commit helpers for each stage
    # ------------------------------------------------------------------

    def commit_stage_artifacts(
        self,
        stage: str,
        entity_id: str,
        message: Optional[str] = None,
        paths: Optional[List[str]] = None
    ):
        """
        Stage and commit artifacts for a pipeline stage.
        If paths is None, stages all changes under the entity's directory.
        """
        if paths:
            for path in paths:
                self._git("add", path)
        else:
            self._git("add", ".")

        msg = message or f"{stage}: {entity_id} artifacts committed"
        result = self._git("commit", "-m", msg, "--allow-empty", check=False)
        if result.returncode == 0:
            print(f"  ✓ Committed: {msg}")
        else:
            if "nothing to commit" in result.stdout:
                print(f"  ℹ Nothing new to commit for {entity_id}")
            else:
                raise RuntimeError(f"Commit failed: {result.stderr}")

    def merge_to_main(self, stage: str, entity_id: str):
        """Merge a stage branch back to main after approval."""
        branch = self.branch_name(stage, entity_id)
        self.checkout_main()
        self._git("merge", "--no-ff", branch, "-m", f"merge: {branch} approved and merged")
        print(f"  ✓ Merged '{branch}' into main")

    # ------------------------------------------------------------------
    # Version tagging
    # ------------------------------------------------------------------

    def tag_world_version(self, version: str):
        tag = f"world-v{version}"
        self._git("tag", "-a", tag, "-m", f"World bible version {version}")
        print(f"  ✓ Tagged world version: {tag}")

    def tag_chapter_complete(self, chapter_id: str):
        timestamp = datetime.now().strftime("%Y%m%d")
        tag = f"{chapter_id}-complete-{timestamp}"
        self._git("tag", "-a", tag, "-m", f"Chapter {chapter_id} final render complete")
        print(f"  ✓ Tagged chapter complete: {tag}")

    # ------------------------------------------------------------------
    # History and rollback
    # ------------------------------------------------------------------

    def get_shot_history(self, chapter_id: str, shot_id: str) -> List[dict]:
        """Return git log for a specific shot's notes file."""
        notes_path = f"chapters/{chapter_id}/shots/{shot_id}/notes.md"
        result = self._git(
            "log", "--oneline", "--follow", "--", notes_path,
            check=False
        )
        commits = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split(" ", 1)
                commits.append({"hash": parts[0], "message": parts[1] if len(parts) > 1 else ""})
        return commits

    def show_file_at_commit(self, commit_hash: str, filepath: str) -> str:
        """Return file content at a specific commit — for rollback review."""
        result = self._git("show", f"{commit_hash}:{filepath}", check=False)
        return result.stdout

    def list_branches(self, filter_prefix: Optional[str] = None) -> List[str]:
        result = self._git("branch", "--list", check=False)
        branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]
        if filter_prefix:
            branches = [b for b in branches if b.startswith(filter_prefix)]
        return branches

    def get_uncommitted_changes(self) -> List[str]:
        result = self._git("status", "--short", check=False)
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

    def status_summary(self):
        branch = self.current_branch()
        changes = self.get_uncommitted_changes()
        stage_branches = self.list_branches(filter_prefix="screenplay/") + \
                         self.list_branches(filter_prefix="storyboard/") + \
                         self.list_branches(filter_prefix="audio/")

        print(f"\n  Git Status")
        print(f"  Current branch: {branch}")
        print(f"  Uncommitted changes: {len(changes)}")
        if changes:
            for c in changes[:5]:
                print(f"    {c}")
            if len(changes) > 5:
                print(f"    ... and {len(changes)-5} more")
        print(f"  Active stage branches: {len(stage_branches)}")
        for b in stage_branches[:8]:
            print(f"    {b}")
