"""Facade for the Codex runner backend.

Phase-2 split: the Codex-only runner internals now live in `src/runners/codex.py`.
This module is a thin facade that re-exports them so the runner's public surface
(and the test suite's `codex_runner.<name>` references, including the
`subprocess` patch target) keep resolving unchanged.

The bare `import subprocess` below is load-bearing: tests patch
`src.runners.codex_runner.subprocess.run` / `.Popen`. Because `subprocess` is a
process-wide singleton module object, patching it through this name mutates the
same object that `src/runners/codex.py` calls through its own bare import.
"""

from __future__ import annotations

import subprocess  # noqa: F401 - load-bearing: tests patch codex_runner.subprocess.{run,Popen}

# Codex-only runner internals (moved verbatim into src/runners/codex.py).
from .codex import (  # noqa: F401
    _agent_message_text_from_event,
    _cwd_from_overrides,
    _run_codex_streaming,
    _stream_enabled,
    _thread_id_from_stdout,
    _tokens_from_stdout,
    _usage_dict_from_event,
    answer,
    build_command,
    CodexRunError,
    DEFAULT_TIMEOUT_MIN,
    run_codex,
)
