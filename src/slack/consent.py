"""Write-mode consent flow: the `!write` command, the consent Block Kit buttons,
and the allowlist + TTL gate.

Turning write-mode ON is gated behind an explicit, allowlisted approval (a
threaded Approve/Deny Block Kit prompt) and is TTL-bounded; turning it OFF is
always allowed. The allowlist (WRITE_ALLOWLIST) and TTL (CONSENT_TTL_MIN) are
read LIVE from os.environ so a SIGHUP .env reload takes effect. Moved verbatim
from the former src/app.py.

Kind-B seam: the wall-clock `_now` lives on the app facade (= time.time); the
TTL/expiry call sites here resolve it THROUGH the facade so a test that patches
`app._now` is seen by this module.
"""

from __future__ import annotations

import json
import logging
import os

from src import store

logger = logging.getLogger("peon")

# Block Kit action ids for the write-mode consent buttons. The action handlers in
# build_app_for are registered against these; the buttons carry the (agent,
# thread_ts) target in their `value` so a click can find the right thread.
_WRITE_APPROVE_ACTION = "write_approve"
_WRITE_DENY_ACTION = "write_deny"

# Default consent TTL (minutes) if CONSENT_TTL_MIN is unset/invalid.
_DEFAULT_CONSENT_TTL_MIN = 2880

# The on/off words accepted by !write (case-insensitive). Anything else -> usage.
_WRITE_ON_WORDS = ("on", "true", "yes", "1", "enable")
_WRITE_OFF_WORDS = ("off", "false", "no", "0", "disable")
# The status word accepted by !write: report the current write state, change nothing.
_WRITE_STATUS_WORDS = ("status", "state", "?")


def _handle_write_command(agent, arg, thread_ts, say, user_id, channel_id):
    """Apply `!write on|off|status` for this thread, gating ON behind consent.

    Turning write ON no longer flips the flag directly: an allowlisted requester
    gets a threaded Block Kit consent request (Approve/Deny buttons); the thread
    stays read-only until an allowlisted user approves (see _handle_consent), which
    grants write-mode for CONSENT_TTL_MIN minutes. A NON-allowlisted `!write on` is
    refused outright with no buttons. Turning OFF is always permitted (clears the
    flag immediately). `status` reports the current state, changing nothing. An
    unrecognized arg gets a usage line. Every branch acks into the thread.
    """
    word = arg.lower()
    if word in _WRITE_ON_WORDS:
        if not _is_write_allowed(user_id, channel_id):
            say(
                text=(
                    f"{agent['display_name']}: write-mode is not allowed for you. "
                    f"Ask an operator to add your user or channel to WRITE_ALLOWLIST. "
                    f"This thread stays read-only."
                ),
                thread_ts=thread_ts,
            )
            return
        # Allowlisted: post a consent request. Write-mode is NOT enabled yet; it
        # flips only when an allowlisted user clicks Approve (a TTL-bounded grant).
        ttl = _consent_ttl_min()
        say(
            text=(
                f"{agent['display_name']}: write-mode requested for this thread. "
                f"An allowlisted user must Approve; it then stays on for {ttl} min."
            ),
            thread_ts=thread_ts,
            blocks=_build_consent_blocks(agent, thread_ts, ttl),
        )
        return
    if word in _WRITE_OFF_WORDS:
        store.set_override(agent["name"], thread_ts, "write", False)
        say(
            text=(
                f"{agent['display_name']}: write-mode DISABLED for this thread "
                f"(read-only)."
            ),
            thread_ts=thread_ts,
        )
        return
    if word in _WRITE_STATUS_WORDS:
        _ack_write_status(agent, thread_ts, say)
        return
    say(text="Usage: !write <on|off|status>", thread_ts=thread_ts)


def _ack_write_status(agent, thread_ts, say):
    """Report this thread's current write-mode state (TTL-aware), changing nothing."""
    from src import app as _appfacade

    if store.is_write_active(agent["name"], thread_ts, now=_appfacade._now):
        expiry = store.write_expiry(agent["name"], thread_ts)
        if expiry is not None:
            remaining = max(0, round((expiry - _appfacade._now()) / 60))
            say(
                text=(
                    f"{agent['display_name']}: write-mode is ON for this thread "
                    f"(about {remaining} min left)."
                ),
                thread_ts=thread_ts,
            )
        else:
            say(
                text=f"{agent['display_name']}: write-mode is ON for this thread.",
                thread_ts=thread_ts,
            )
        return
    say(
        text=f"{agent['display_name']}: write-mode is OFF (read-only) for this thread.",
        thread_ts=thread_ts,
    )


def _build_consent_blocks(agent, thread_ts, ttl_min):
    """Block Kit blocks for a write-mode consent request: a prompt + Approve/Deny.

    The two buttons carry the (agent name, thread_ts) target encoded in their
    `value` (JSON) so the action handler can resolve the right thread on a click;
    the action_ids are _WRITE_APPROVE_ACTION / _WRITE_DENY_ACTION. Pure data (no
    Slack call), so it is unit-testable.
    """
    value = json.dumps({"agent": agent["name"], "thread_ts": thread_ts})
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{agent['display_name']}*: write-mode requested for this "
                    f"thread. An allowlisted user must approve; it then stays on "
                    f"for {ttl_min} min, then reverts to read-only."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": _WRITE_APPROVE_ACTION,
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": _WRITE_DENY_ACTION,
                    "value": value,
                },
            ],
        },
    ]


def _handle_consent(
    agent, action_id, clicker_user_id, channel_id, thread_ts, say, now=None
):
    """Resolve a write-mode consent button click. Returns nothing; acks into the thread.

    Gated like `!write on`: ONLY an allowlisted clicker (user OR channel in
    WRITE_ALLOWLIST) can act. A non-allowlisted click is IGNORED silently (no store
    change, no ack), so a random viewer cannot grant or deny. For an allowlisted
    clicker:
      - Approve -> grant write-mode for CONSENT_TTL_MIN minutes (an expiry stored
        with the flag); on/after expiry the thread reverts to read-only.
      - Deny    -> leave the thread read-only (no flag change), with an ack.
    `now` is an injectable zero-arg clock (defaults to the module `_now`) so the
    expiry is deterministic in tests. No real Slack client is needed (say is
    injected), so this is unit-testable.
    """
    from src import app as _appfacade

    if now is None:
        now = _appfacade._now
    if not _is_write_allowed(clicker_user_id, channel_id):
        # A non-allowlisted clicker is ignored: never grants, never denies.
        return
    if action_id == _WRITE_APPROVE_ACTION:
        ttl = _consent_ttl_min()
        store.grant_write_consent(agent["name"], thread_ts, ttl_minutes=ttl, now=now)
        workdir = store.get_workdir(agent["name"], thread_ts)
        say(
            text=(
                f"{agent['display_name']}: write-mode APPROVED for ~{ttl} min. "
                f"Edits are confined to its isolated workdir ({workdir}); the "
                f"thread reverts to read-only when it lapses."
            ),
            thread_ts=thread_ts,
        )
        return
    # Deny (the only other registered action): stays read-only.
    say(
        text=(
            f"{agent['display_name']}: write-mode request DENIED. "
            f"This thread stays read-only."
        ),
        thread_ts=thread_ts,
    )


def _is_write_allowed(user_id, channel_id):
    """Whether this principal may turn write-mode ON (the read-write tool surface).

    Read at CALL time from os.environ (so a SIGHUP .env reload takes effect).
    WRITE_ALLOWLIST is a comma-separated list of Slack user and/or channel IDs;
    the request is allowed iff the requesting user_id OR the channel_id is in it.
    FAIL-CLOSED: an unset/empty allowlist denies everyone, since write-mode removes
    the read-only safety sandbox and must be explicitly opted into.
    """
    raw = os.environ.get("WRITE_ALLOWLIST", "")
    allow = {tok.strip() for tok in raw.split(",") if tok.strip()}
    if not allow:
        return False
    return (user_id in allow) or (channel_id in allow)


def _consent_ttl_min():
    """The write-mode consent TTL in minutes, read LIVE from os.environ.

    CONSENT_TTL_MIN (a SIGHUP .env reload takes effect). Falls back to the default
    (2880, i.e. 2 days) when unset, empty, or non-integer, so a typo never crashes
    a click.
    """
    raw = os.environ.get("CONSENT_TTL_MIN", "").strip()
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_CONSENT_TTL_MIN


def _on_consent_action(agent, body, say):
    """Unpack a Bolt action payload and route it to _handle_consent.

    Pulls the clicker, channel, action id, and the (agent, thread_ts) target the
    button carried in its `value` (JSON), then defers to _handle_consent for the
    allowlist gate + grant/deny. The thread_ts comes from the button value (the
    thread the request was posted in); we fall back to the message's own ts. A
    malformed/foreign payload is ignored (logged) rather than crashing the handler.
    """
    try:
        action = (body.get("actions") or [{}])[0]
        action_id = action.get("action_id")
        clicker = (body.get("user") or {}).get("id")
        channel_id = (body.get("channel") or {}).get("id")
        target = json.loads(action.get("value") or "{}")
        thread_ts = target.get("thread_ts") or (body.get("message") or {}).get("ts")
        if not thread_ts:
            return
    except Exception:  # noqa: BLE001 - a malformed payload must not crash the handler
        logger.warning(
            "ignoring malformed consent action payload for %s", agent["name"]
        )
        return

    def _say(text=None, thread_ts=thread_ts, blocks=None):  # noqa: ARG001
        say(text=text, thread_ts=thread_ts)

    _handle_consent(agent, action_id, clicker, channel_id, thread_ts, _say)
