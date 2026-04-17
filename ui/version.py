"""
ui/version.py — Version detection for Babylon Studio.

Reads the current version from git tags and checks the remote for updates.
Falls back to a hardcoded version if git is unavailable.
"""

import subprocess
import time
from pathlib import Path

FALLBACK_VERSION = "0.1.0"
ORCHESTRATOR_ROOT = str(Path(__file__).resolve().parent.parent)

# Cache: avoid fetching remote tags on every page load
_last_fetch_time = 0
_FETCH_INTERVAL = 300  # seconds (5 minutes)


def _run_git(*args: str) -> str | None:
    """Run a git command in the orchestrator repo. Returns stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ORCHESTRATOR_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def _parse_version(tag: str) -> tuple:
    """Parse 'v1.2.3' or '1.2.3' into a comparable tuple (1, 2, 3)."""
    v = tag.lstrip("v")
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def get_current_version() -> str:
    """Return the current version from the latest git tag on HEAD, or fallback."""
    desc = _run_git("describe", "--tags", "--abbrev=0")
    if desc:
        return desc.lstrip("v")
    return FALLBACK_VERSION


def get_all_remote_tags() -> list[str]:
    """Fetch tags from origin (if stale) and return all version tags sorted."""
    global _last_fetch_time
    now = time.time()
    if now - _last_fetch_time > _FETCH_INTERVAL:
        _run_git("fetch", "--tags", "--quiet")
        _last_fetch_time = now

    output = _run_git("tag", "--list", "v*")
    if not output:
        return []
    tags = [t.strip() for t in output.splitlines() if t.strip()]
    tags.sort(key=_parse_version)
    return tags


def get_latest_version() -> str:
    """Return the highest version tag known (local + fetched remote)."""
    tags = get_all_remote_tags()
    if tags:
        return tags[-1].lstrip("v")
    return get_current_version()


def check_for_update() -> dict:
    """Return version info dict for the API."""
    current = get_current_version()
    latest = get_latest_version()
    return {
        "current": current,
        "latest": latest,
        "update_available": _parse_version(latest) > _parse_version(current),
    }


def pull_latest() -> dict:
    """Pull latest code from origin/main. Returns {ok, message}."""
    result = _run_git("pull", "origin", "main", "--ff-only")
    if result is not None:
        return {"ok": True, "message": result}
    # Try without --ff-only constraint info
    err = _run_git("pull", "origin", "main")
    if err is not None:
        return {"ok": True, "message": err}
    return {"ok": False, "message": "git pull failed — resolve conflicts manually"}
