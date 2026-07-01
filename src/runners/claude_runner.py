"""Facade for the Claude runner backend.

The Claude-only runner internals live in `src/runners/claude.py` and the
cross-vendor dedup in `src/runners/common.py`; the vendor-neutral persistence
stores live in `src/store/`. This module is a thin facade that re-exports all of
those symbols so the runner's public surface (and the test
suite's `claude_runner.<name>` references, including the `setattr` patch targets
`_sessions_path` / `_overrides_path` / `_crons_path` and the `subprocess` patch
target) keep resolving unchanged.

The bare `import subprocess` below is load-bearing: tests patch
`src.runners.claude_runner.subprocess.run` / `.Popen`. Because `subprocess` is a
process-wide singleton module object, patching it through this name mutates the
same object that `src/runners/claude.py` calls through its own bare import.
"""

from __future__ import annotations

import subprocess  # noqa: F401 - load-bearing: tests patch claude_runner.subprocess.{run,Popen}

# Claude-only runner internals (src/runners/claude.py).
from .claude import (  # noqa: F401
    _CLAUDE_MODEL_FALLBACK,
    _CONTEXT_WINDOW_1M,
    _CONTEXT_WINDOW_DEFAULT,
    _context_window_for,
    _cwd_from_overrides,
    _is_block_stop,
    _meta_from_payload,
    _run_claude_streaming,
    _stream_enabled,
    _text_delta_from_stream_event,
    answer,
    build_command,
    ClaudeRunError,
    DEFAULT_TIMEOUT_MIN,
    run_claude,
)

# Cross-vendor idempotency dedup + the run-cancel interrupt token.
from .common import Interrupt, seen_before  # noqa: F401

# Vendor-neutral persistence stores live in src/store/ (sessions, overrides,
# crons, workdir all anchored on store.base). They are re-exported here so the
# runner's public surface (and the test suite's claude_runner.<name> references)
# keep resolving unchanged.
from src.store.base import (  # noqa: F401
    _SESSIONS_LOCK,
    _load_dict_store,
    _save_dict_store,
    _session_key,
    _sessions_path,
    _sibling_store_path,
)
from src.store.crons import (  # noqa: F401
    _crons_path,
    add_cron,
    list_crons,
    remove_cron,
    set_cron_enabled,
)
from src.store.overrides import (  # noqa: F401
    _overrides_path,
    clear_override,
    get_override,
    set_override,
)
from src.store.sessions import (  # noqa: F401
    get_or_create_session,
    get_session,
    set_session,
)
from src.store.workdir import _safe_token, get_workdir  # noqa: F401
