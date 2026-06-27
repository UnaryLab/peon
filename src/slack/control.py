"""Per-thread control phrases (typed AFTER the mention is stripped).

A message starting with "!" is a standalone command: it updates this thread's
model/effort override, manages crons, or resets, and acks, WITHOUT running the
agent. Anchored at the start so a normal prompt that merely contains "!" later is
unaffected.

Cross-module dispatch (cron -> scheduler) is NOT a patched-on-facade seam, so it
is wired with direct package imports.
"""

from __future__ import annotations

import re

from src import agents, store
from src.runners import claude_runner

from . import interrupt, scheduler

CONTROL_RE = re.compile(r"^!(model|effort|reset|cron)\b\s*(.*)$", re.IGNORECASE)

# The reasoning-effort levels accepted by !effort. Anything else is rejected.
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _effective_config(agent, thread_ts):
    """The model + effort that WILL be used for this (agent, thread).

    The per-thread override wins per field; otherwise the agents.json resolution
    (with claude's pinned model fallback / codex's omit). Returned as a (model,
    effort) pair of display strings for the control-phrase ack. For codex with no
    model set the resolved model may be empty; agents.resolve renders the
    agents.json value (every shipped codex entry sets one).
    """
    override = store.get_override(agent["name"], thread_ts) or {}
    if agent.get("backend", "claude") == "claude":
        model = agents.resolve(agent, "model", claude_runner._CLAUDE_MODEL_FALLBACK)
    else:
        model = agents.resolve(agent, "model")
    effort = agents.resolve(agent, "effort")
    if override.get("model"):
        model = override["model"]
    if override.get("effort"):
        effort = override["effort"]
    return model or "(backend default)", effort or "(default)"


def _ack_effective(agent, thread_ts, say):
    """Post the resulting effective config into the thread (used after set/reset)."""
    model, effort = _effective_config(agent, thread_ts)
    say(
        text=(f"{agent['display_name']} (this thread): model={model}, effort={effort}"),
        thread_ts=thread_ts,
    )


def _handle_control_phrase(agent, text, thread_ts, say, channel_id=None):
    """Handle a "!"-prefixed control phrase. Returns True if it was one (handled).

    Recognizes (case-insensitive, anchored at start):
      !model <model-id>   -> set this thread's model override (any non-empty id)
      !effort <level>     -> set this thread's effort override (validated)
      !cron <sub>         -> manage Slack-native scheduled runs in this thread
                             (add "<expr>" <prompt> | list | remove <id> |
                             on <id> | off <id>); see _handle_cron_command
      !reset              -> clear this thread's overrides (back to defaults)
    Any other "!..." -> a one-line help ack. A non-"!" message returns False so
    the caller runs the agent normally. On every handled case this acks into the
    thread and the agent is NOT run. `channel_id` identifies the thread's channel
    for !cron. No real Slack client is needed (say is injected), so this is
    unit-testable.
    """
    # Interrupt (the Ctrl-C analog): a !stop / stop / interrupt / ctrl-c message
    # signals the in-flight run for THIS thread and starts no new run. Checked
    # before the "!"-gate since the bare forms ("stop", "ctrl-c") carry no "!".
    if interrupt.is_interrupt_phrase(text):
        if interrupt.request(agent["name"], thread_ts):
            say(text="🛑 Interrupting the current run…", thread_ts=thread_ts)
        else:
            say(
                text="Nothing is running in this thread to interrupt.",
                thread_ts=thread_ts,
            )
        return True

    if not text.startswith("!"):
        return False

    match = CONTROL_RE.match(text)
    if not match:
        _ack_control_help(agent, thread_ts, say)
        return True

    command = match.group(1).lower()
    arg = match.group(2).strip()

    if command == "reset":
        store.clear_override(agent["name"], thread_ts)
        model, effort = _effective_config(agent, thread_ts)
        say(
            text=(
                f"{agent['display_name']}: overrides cleared. Back to defaults: "
                f"model={model}, effort={effort}"
            ),
            thread_ts=thread_ts,
        )
        return True

    if command == "model":
        if not arg:
            say(text="Usage: !model <model-id>", thread_ts=thread_ts)
            return True
        store.set_override(agent["name"], thread_ts, "model", arg)
        _ack_effective(agent, thread_ts, say)
        return True

    if command == "cron":
        scheduler._handle_cron_command(agent, arg, thread_ts, say, channel_id)
        return True

    # command == "effort"
    level = arg.lower()
    if level not in VALID_EFFORTS:
        say(
            text=(f"Unknown effort {arg!r}. Valid values: {', '.join(VALID_EFFORTS)}."),
            thread_ts=thread_ts,
        )
        return True
    store.set_override(agent["name"], thread_ts, "effort", level)
    _ack_effective(agent, thread_ts, say)
    return True


def _ack_control_help(agent, thread_ts, say):
    """One-line help listing the control commands."""
    say(
        text=(
            f"Commands: `!model <id>`, `!effort <{'|'.join(VALID_EFFORTS)}>`, "
            f"`!cron <add|list|remove|on|off>`, `!stop`, `!reset`"
        ),
        thread_ts=thread_ts,
    )
