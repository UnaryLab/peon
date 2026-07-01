"""Per-thread run-interrupt registry + the interrupt-phrase matcher.

The Ctrl-C analog for Slack: while a run is in flight, the worker registers its
Interrupt token here under the run's (agent, thread) key; a "!stop"/"stop"/
"ctrl-c" control phrase looks the token up and signals it (SIGINT to the live CLI
plus a graceful-settle flag the runner reads). See src.runners.common.Interrupt.

In-memory only (subprocess handles are not serializable) and single-process, like
the seen_before dedup. Thread-safe. One run per (agent, thread): the message path
claims the slot atomically with try_register (the busy guard) and declines a
second concurrent message, so it never overwrites a live token. Only the cron path
still uses register (last-writer-wins) if a run somehow overlaps.
"""

from __future__ import annotations

import threading

from src.runners.common import Interrupt

_LOCK = threading.Lock()
_RUNNING: dict[tuple[str, str], Interrupt] = {}

# A de-mentioned message equal (case-insensitively, stripped) to one of these is an
# interrupt request, not a prompt. Both "!"-prefixed and bare forms, since a user
# reaching for "stop" mid-run will not prefix it. Mirrors claude-wormhole's matcher.
# ponytail: exact whole-message match keeps the false-positive risk near zero (a
# real prompt is rarely the single word "stop"), and when nothing is running the
# handler just replies "nothing to interrupt", so even a stray match is harmless.
_INTERRUPT_PHRASES = frozenset(
    {
        "!stop",
        "stop",
        "!interrupt",
        "interrupt",
        "/interrupt",
        "ctrl+c",
        "ctrl-c",
        "ctrlc",
        "control+c",
        "control-c",
        "^c",
    }
)


def is_interrupt_phrase(text):
    """True if `text` (a de-mentioned message) is an interrupt request."""
    return (text or "").strip().lower() in _INTERRUPT_PHRASES


def register(agent_name, thread_ts):
    """Create + register an Interrupt token for this run; return it."""
    token = Interrupt()
    with _LOCK:
        _RUNNING[(agent_name, thread_ts)] = token
    return token


def try_register(agent_name, thread_ts):
    """Register a token ONLY if this thread has no run in flight (the busy guard).

    Returns the new token, or None if a run is already registered for this
    (agent, thread). Atomic under _LOCK, so two near-simultaneous messages to one
    thread cannot both pass: the first claims the slot, the second gets None and
    the caller declines to start a competing run (which would --resume the same
    session id concurrently). Unlike register(), this never overwrites a live
    token. The caller passes the returned token into the run and unregister()s it
    when done. register() (unconditional) is still used by the cron path.
    """
    token = Interrupt()
    with _LOCK:
        if (agent_name, thread_ts) in _RUNNING:
            return None
        _RUNNING[(agent_name, thread_ts)] = token
    return token


def unregister(agent_name, thread_ts, token):
    """Drop this run's token, but only if a newer run has not replaced it."""
    with _LOCK:
        if _RUNNING.get((agent_name, thread_ts)) is token:
            del _RUNNING[(agent_name, thread_ts)]


def request(agent_name, thread_ts):
    """Signal the in-flight run for this thread. Return True if one was running."""
    with _LOCK:
        token = _RUNNING.get((agent_name, thread_ts))
    if token is None:
        return False
    token.request()
    return True
