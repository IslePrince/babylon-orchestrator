"""Recover a wedged ComfyUI server.

ComfyUI's sampler occasionally hangs on very long / very demanding
workflows. This utility:

  1. Dumps current /queue state so you can see what was running /
     pending.
  2. Sends /interrupt to ask the current job to stop at its next
     sampler step.
  3. Clears the pending queue with /queue {"clear": True}.
  4. If --force, kills the ComfyUI Python process holding the port.
     Use only when /interrupt has no effect (sampler truly hung).

Usage:
    python3 utils/comfyui_unstick.py                 # soft: interrupt + clear
    python3 utils/comfyui_unstick.py --force         # hard kill after soft fails
    python3 utils/comfyui_unstick.py --force --wait 10   # wait longer for soft
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx


def _queue(client: httpx.Client, base_url: str) -> dict:
    return client.get(f"{base_url}/queue").json()


def _find_pid_on_port(port: int) -> int | None:
    """Windows-first helper. Returns the PID holding ``port`` or None."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    marker = f":{port} "
    for line in out.splitlines():
        if "LISTENING" in line and marker in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def _kill_pid(pid: int) -> bool:
    """Platform-agnostic force kill. Returns True on success."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True)
        else:
            os.kill(pid, 9)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"kill failed: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--url", default=os.getenv("COMFYUI_URL", "http://localhost:8000"),
        help="ComfyUI base URL (default: $COMFYUI_URL or http://localhost:8000)",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="If the soft interrupt doesn't free the current slot "
             "within --wait seconds, hard-kill the ComfyUI process.",
    )
    ap.add_argument(
        "--wait", type=float, default=5.0,
        help="Seconds to wait after /interrupt before checking / "
             "escalating to --force.",
    )
    args = ap.parse_args()

    base = args.url.rstrip("/")

    with httpx.Client(timeout=15) as c:
        try:
            before = _queue(c, base)
        except httpx.HTTPError as e:
            print(f"Cannot reach ComfyUI at {base}: {e}")
            return 2

        running = before.get("queue_running", [])
        pending = before.get("queue_pending", [])
        print(f"Before: running={len(running)} pending={len(pending)}")
        for r in running:
            print(f"  running prompt_id: {r[1] if len(r) > 1 else r}")

        c.post(f"{base}/interrupt")
        c.post(f"{base}/queue", json={"clear": True})
        print("Sent /interrupt + /queue clear.")

        time.sleep(args.wait)
        after = _queue(c, base)
        still_running = len(after.get("queue_running", []))
        print(f"After wait {args.wait:.0f}s: "
              f"running={still_running} pending={len(after.get('queue_pending', []))}")

        if still_running == 0:
            print("ComfyUI recovered via soft interrupt.")
            return 0

        if not args.force:
            print(
                f"Current job still running after {args.wait:.0f}s. "
                "Re-run with --force to hard-kill the ComfyUI process "
                "(you'll need to restart ComfyUI manually after)."
            )
            return 1

    # Hard kill.
    from urllib.parse import urlparse
    port = urlparse(base).port or 8000
    pid = _find_pid_on_port(port)
    if pid is None:
        print(f"No process found listening on port {port}.")
        return 1
    print(f"Hard-killing ComfyUI (pid={pid}) on port {port}...")
    ok = _kill_pid(pid)
    if not ok:
        return 1
    print("ComfyUI killed. Restart it yourself, then rerun your batch "
          "(shots that already have preview_*.mp4 will be skipped "
          "automatically).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
