"""Genuinely cross-vendor runtime shared by both runner backends.

Holds the symbols both the claude and codex runners truly share: the idempotency
dedup (`seen_before`), the run-interrupt handle (`Interrupt`), and the
incremental-update helper (`safe_on_update`). All are Slack-agnostic, so this is
importable without slack_bolt and is unit-testable. The vendor facades
(claude_runner / codex_runner) re-export `seen_before` and `Interrupt` from here.
"""

from __future__ import annotations

import collections
import signal
import threading
from typing import Any


# ---------------------------------------------------------------------------
# Idempotency dedup (Slack-agnostic: dedups opaque string message ids)
# ---------------------------------------------------------------------------
# Lives here (not in app.py) so it is importable without slack_bolt and is unit-
# testable. app.py calls seen_before(msg_id) at the top of its handler so a
# message delivered as BOTH an app_mention and a message.* event is handled once.
#
# ponytail: in-memory, bounded (deque maxlen) + set, single-process. Resets on
# restart and does not dedup across processes; that is fine for this one always-on
# process. No external cache, no TTL.

_TOKEN_LIMIT_MARKERS = (
    "token limit",
    "context length",
    "context limit",
    "too many tokens",
    "maximum context",
    "exceeds context",
    "exceeded context",
    "prompt is too long",
)


def _clean_process_output(text: str | None) -> str:
    return " ".join((text or "").split())


def _looks_like_token_limit(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _TOKEN_LIMIT_MARKERS)


def format_process_failure(
    command_name: str,
    returncode: int,
    stderr: str | None = "",
    stdout: str | None = "",
    limit: int = 1000,
) -> str:
    detail = _clean_process_output(stderr) or _clean_process_output(stdout)
    if not detail:
        detail = "no stderr/stdout captured"
    detail = detail[:limit]
    if _looks_like_token_limit(detail):
        detail = f"likely token/context limit: {detail}"
    return f"{command_name} exited with code {returncode}: {detail}"


_SEEN_MAXLEN = 512
_SEEN_LOCK = threading.Lock()
_SEEN_IDS = set()
_SEEN_ORDER = collections.deque(maxlen=_SEEN_MAXLEN)


def seen_before(msg_id):
    """Record msg_id and return whether it had already been seen.

    Returns False the first time a given id is presented (and records it), True
    on any subsequent presentation. Bounded to the last _SEEN_MAXLEN ids: once an
    id ages out of the deque it is forgotten (acceptable for dedup of near-
    simultaneous duplicate Slack deliveries). Thread-safe.
    """
    with _SEEN_LOCK:
        if msg_id in _SEEN_IDS:
            return True
        if len(_SEEN_ORDER) == _SEEN_ORDER.maxlen:
            evicted = _SEEN_ORDER[0]  # deque drops the left item on append
            _SEEN_IDS.discard(evicted)
        _SEEN_ORDER.append(msg_id)
        _SEEN_IDS.add(msg_id)
        return False


# ----------------------------------------------------------------------------
# Cooperative run interrupt (the Slack Ctrl-C analog)
# ----------------------------------------------------------------------------
# Created by the Slack worker, passed into runner.answer(cancel=...). The runner
# stores the live streaming subprocess on `.proc` right after spawning it. A
# control-phrase handler on ANOTHER thread calls .request() to signal a user
# interrupt: it sets the flag and sends SIGINT (mimics a terminal Ctrl-C, giving
# the CLI a chance to flush its own session state). The runner, on a nonzero exit,
# checks `.requested` and settles GRACEFULLY (returns the partial output) instead
# of raising, so the (agent, thread) conversation stays resumable.
#
# ponytail: only the STREAMING (Popen) path is interruptible; the legacy
# STREAM_OUTPUT=0 path blocks inside subprocess.run with no exposed handle, so
# .proc stays None and .request() only sets the flag (the run finishes/timeouts on
# its own). Escalate SIGINT -> SIGTERM/kill only if a CLI is ever seen to ignore it.


class Interrupt:
    """A one-shot, thread-safe cancel handle for a single in-flight run."""

    def __init__(self):
        self._requested = threading.Event()
        # The live Popen, set by the runner once it spawns; Any since we only
        # duck-type .poll()/.send_signal() (a real Popen at runtime, a fake in tests).
        self.proc: Any = None

    @property
    def requested(self):
        """True once a user interrupt has been signalled for this run."""
        return self._requested.is_set()

    def request(self):
        """Signal a user interrupt: set the flag, then SIGINT the live proc."""
        self._requested.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass  # already exited between the poll and the signal


def safe_on_update(on_update, text, force=False):
    """Push one incremental update through `on_update`, swallowing any error.

    Both streaming runners feed the cumulative reply text here as it grows. A
    transient Slack failure (or a None callback on the non-stream path) must never
    abort the run, so every error is swallowed.

    `force=True` asks the updater to bypass its ~1/sec throttle; the runners set it
    when a unit of output COMPLETES (claude: a content_block_stop; codex: a
    completed item) so a finished block shows in FULL instead of the mid-sentence
    fragment the throttle last posted. A plain single-arg callback that predates
    the force kwarg (e.g. a bare list.append in tests) still works via the
    fallback call.
    """
    if on_update is None:
        return
    try:
        on_update(text, force=force)
    except TypeError:
        # Callback without a force= parameter: retry the plain single-arg form.
        try:
            on_update(text)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 - a bad update must not abort the run
        pass
