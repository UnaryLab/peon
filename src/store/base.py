"""Vendor-neutral persistence foundation: paths, the shared lock, and the
dict-store load/save plus the composite key.

Single source of truth for the store layout: every backend-agnostic store
(sessions, overrides, crons) anchors here. The JSON stores all live beside
sessions.json (see _sibling_store_path), so SESSIONS_PATH redirects all of them
at once with no extra env var. Nothing here imports slack_bolt or any runner, so
the stores stay importable/testable without Slack and without a backend.
"""

from __future__ import annotations

import json
import os
import threading
import time

# ponytail: session store is a JSON file; fine for one process / modest volume;
# swap for sqlite if it grows or needs multi-process access. The read-modify-write
# is NOT inherently safe here: daemon background threads share this process, so
# concurrent first-touches of the same key could clobber each other. We guard the
# critical section with _SESSIONS_LOCK below (in-process only); sqlite is still the
# upgrade path if it grows or needs concurrency across processes.
# Anchor to the PROJECT ROOT, NOT the package or subpackage dir, so sessions.json
# lives at the repo root regardless of the current working directory (matching
# .gitignore, which ignores sessions.json at the root). This module lives in
# src/store/, so the project root is THREE levels up from __file__
# (src/store/base.py -> src/store -> src -> project root).
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_DEFAULT_SESSIONS_PATH = os.path.join(_PROJECT_ROOT, "sessions.json")


def _sessions_path():
    """Resolve the session-store path, reading SESSIONS_PATH from os.environ LIVE.

    Read lazily (per store access), NOT baked into an import-time constant, so a
    SESSIONS_PATH loaded from .env into os.environ takes effect even though .env
    is loaded after this module is imported. The SESSIONS_PATH env var wins if
    set (and non-empty); otherwise the store lives at <project_root>/sessions.json.
    """
    return os.environ.get("SESSIONS_PATH") or _DEFAULT_SESSIONS_PATH


# Guards the read-modify-write of the JSON session store against the background
# worker threads in app.py racing on the same new key.
_SESSIONS_LOCK = threading.Lock()


def _session_key(agent_name, thread_ts):
    """The independent-context key. Including agent_name guarantees that two
    agents replying in the SAME Slack thread never share a claude session id.
    """
    return f"{agent_name}:{thread_ts}"


def _load_dict_store(path):
    """Load a dict-shaped JSON store (sessions OR overrides); missing/corrupt -> {}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Missing or corrupt store -> start fresh rather than crash.
        return {}


def _save_dict_store(data, path):
    """Persist a dict-shaped JSON store (pretty, deterministic key order)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _sibling_store_path(filename):
    """Resolve a per-thread JSON store that lives beside the sessions store.

    Every secondary store (overrides, crons) sits in the SAME directory as
    sessions.json, so SESSIONS_PATH redirects all of them at once with no extra
    env var. Read lazily (per access) for the same reason _sessions_path is.
    """
    return os.path.join(os.path.dirname(_sessions_path()), filename)


# Default wall-clock for the consent TTL. Module-level so tests inject a fake.
_now = time.time


def _resolve_path(attr, fallback):
    """Resolve a store's default path, honoring a patched runner-level resolver.

    The legacy public seam is the claude_runner module: callers and the test
    suite override the store path by patching claude_runner._sessions_path /
    _overrides_path / _crons_path. The neutral stores moved out of that module,
    so each store's default-path branch routes through here: if the named
    resolver attribute is present on claude_runner (always, since it re-exports
    these), call it; otherwise fall back to the store's own local resolver. The
    import is lazy (per call) because claude_runner imports this package, so a
    top-level import would be circular; by call time claude_runner is fully
    loaded. In production claude_runner.<attr> IS the local resolver (re-exported),
    so this is behavior-identical; a test's setattr(claude_runner, attr, ...) is
    honored because the lookup reads the live attribute.
    """
    try:
        from src.runners import claude_runner
    except Exception:  # noqa: BLE001 - never let the seam crash a store op
        return fallback()
    resolver = getattr(claude_runner, attr, None)
    if resolver is None:
        return fallback()
    return resolver()
