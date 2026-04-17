"""
core/gpu_lock.py
Serialize GPU-heavy pipeline work so two stages don't thrash the 4090.

One 4090 + simultaneous SDXL storyboard renders + Wan video gen +
LoRA training = OOM, thrashing, or both. This module gives us a
single RLock that every GPU-bound stage grabs before running, with:

- **Reentrancy**: a batch stage can acquire once and call into sub-
  workers (per-shot renders) without deadlocking itself.
- **Fail-fast by default**: when another GPU job is already running,
  the new caller gets a structured ``GPUBusyError`` that names the
  current holder — callers can choose to wait or surface a message.
- **Introspection**: ``gpu_status()`` returns who holds the lock,
  since when, and from which thread. Exposed via the MCP tool and
  could also power a UI badge.

The lock is cooperative — it only blocks callers that opt in via
``gpu_exclusive(...)``. CPU-bound stages (ingest, screenplay,
voice recording via ElevenLabs, SFX routed to ElevenLabs, ffmpeg
post-mix) don't touch it and can run in parallel with each other
or with a GPU job.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Optional


_GPU_LOCK = threading.RLock()
_HOLDER: dict = {}


class GPUBusyError(RuntimeError):
    """Raised when ``gpu_exclusive(blocking=False)`` can't acquire the
    lock because another GPU job is running. ``holder`` describes who."""

    def __init__(self, message: str, holder: dict):
        super().__init__(message)
        self.holder = holder


@contextmanager
def gpu_exclusive(
    label: str,
    *,
    blocking: bool = False,
    timeout: Optional[float] = None,
    job_id: Optional[str] = None,
):
    """Context manager guarding GPU-heavy work.

    Usage::

        with gpu_exclusive("preview_video:ch01_sc01_sh006"):
            # ... Wan render here

    ``blocking=False`` (the default) raises :class:`GPUBusyError`
    immediately when the GPU is in use. Pass ``blocking=True`` +
    ``timeout=<seconds>`` to wait. ``timeout=None`` with
    ``blocking=True`` waits forever — use sparingly.

    Reentrant: the same thread can re-enter, so a batch stage that
    acquires once and then calls a per-shot helper which also tries
    to acquire won't deadlock.
    """
    acquire_timeout = -1 if timeout is None else float(timeout)
    acquired = _GPU_LOCK.acquire(blocking=blocking, timeout=acquire_timeout)
    if not acquired:
        holder_copy = dict(_HOLDER)
        raise GPUBusyError(
            f"GPU busy: {holder_copy.get('label', 'unknown')} "
            f"(held for {_holder_age(holder_copy):.1f}s)",
            holder=holder_copy,
        )

    # Reentrant re-acquires don't overwrite the outer label.
    outer_holder = bool(_HOLDER)
    if not outer_holder:
        _HOLDER.update({
            "label": label,
            "job_id": job_id,
            "acquired_at": time.time(),
            "thread_id": threading.get_ident(),
            "depth": 1,
        })
    else:
        _HOLDER["depth"] = int(_HOLDER.get("depth", 1)) + 1
    try:
        yield
    finally:
        if _HOLDER.get("depth", 1) <= 1:
            _HOLDER.clear()
        else:
            _HOLDER["depth"] = int(_HOLDER.get("depth", 1)) - 1
        _GPU_LOCK.release()


def gpu_status() -> Optional[dict]:
    """Return the current holder's label/job_id/elapsed, or None if
    the GPU is free. Intended for a badge in the UI and the
    ``gpu_status`` MCP tool."""
    if not _HOLDER:
        return None
    h = dict(_HOLDER)
    h["held_for_sec"] = round(_holder_age(h), 1)
    return h


def _holder_age(h: dict) -> float:
    return max(0.0, time.time() - float(h.get("acquired_at") or time.time()))
