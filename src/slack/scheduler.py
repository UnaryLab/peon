"""Cron scheduling (Slack-native, self-contained; no new dependency).

A daemon thread (started from main()) ticks once a minute and fires any enabled
cron whose 5-field expression matches the current minute, synthesizing a run
through the SAME _run_and_update seam as a live mention and posting into the
cron's target thread. The store (crons.json, a sibling of sessions.json) is the
shared lock-guarded store; the scheduler re-reads it from disk every tick, so a
SIGHUP that changes crons.json is picked up with no extra wiring. Control phrases
(!cron add/list/remove/on/off) mutate the store.

NOTE: Claude Code has its OWN /schedule (cloud routines). This is the
in-process Slack-native equivalent the user asked for: it runs inside this
always-on process and posts back into Slack threads.

ponytail: a manual 5-field cron matcher (no croniter/APScheduler dependency)
and a 60s tick. Fields are minute hour day-of-month month day-of-week, each
either "*" or a comma list of items, where an item is N, A-B, */S, or A-B/S.
day-of-week is 0-6 with 0 (and 7) = Sunday. dom/dow use OR semantics only when
both are restricted (standard cron); here we keep it simple: every field must
match (AND), which is the common case for the schedules this supports.

Moved verbatim from the former src/app.py. Kind-B seams: _fire_cron resolves
_run_and_update and _scheduler_loop resolves _scheduler_tick THROUGH the app
facade so a test's monkeypatch on the facade is seen by these call sites.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from src import agents, store

logger = logging.getLogger("peon")

# Per-field allowed numeric ranges (inclusive), in field order.
_CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _cron_field_values(field, low, high):
    """Expand ONE cron field token into the set of integers it matches.

    Supports "*", comma lists, "A-B" ranges, "*/S" and "A-B/S" steps. Returns the
    matching ints clamped to [low, high]; raises ValueError on anything malformed
    (an out-of-range or non-numeric piece) so the caller can treat the whole
    expression as non-matching rather than guessing.
    """
    result: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field part")
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("cron step must be positive")
        else:
            base = part
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_s, end_s = base.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(base)
        if start < low or end > high or start > end:
            raise ValueError(f"cron field {base!r} out of range [{low},{high}]")
        result.update(range(start, end + 1, step))
    return result


def cron_matches(expr, now):
    """Whether the 5-field cron `expr` fires at the datetime `now`.

    Fields: minute hour day-of-month month day-of-week. Each field is "*" or a
    comma list of N / A-B / */S / A-B/S items. day-of-week is 0-6 (Sun=0); 7 also
    means Sunday. Every field must match (AND). A malformed expression (wrong field
    count, out-of-range piece, non-numeric) safely returns False, never raises, so
    a bad stored entry can never crash the scheduler tick. `now` is injected (a
    datetime) so tests are hermetic.
    """
    fields = (expr or "").split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    try:
        if now.minute not in _cron_field_values(minute, 0, 59):
            return False
        if now.hour not in _cron_field_values(hour, 0, 23):
            return False
        if now.day not in _cron_field_values(dom, 1, 31):
            return False
        if now.month not in _cron_field_values(month, 1, 12):
            return False
        # Python weekday(): Mon=0..Sun=6. Cron dow: Sun=0..Sat=6 (and 7=Sun).
        cron_dow = (now.weekday() + 1) % 7  # Mon->1, Sun->0
        allowed_dow = _cron_field_values(dow, 0, 7)
        if 7 in allowed_dow:
            allowed_dow.add(0)
        if cron_dow not in allowed_dow:
            return False
    except ValueError:
        return False
    return True


# Quoted-schedule extractor for `!cron add "<expr>" <prompt>`: the schedule is in
# double quotes, the rest (trimmed) is the prompt.
_CRON_ADD_RE = re.compile(r'^"([^"]+)"\s+(.+)$', re.DOTALL)


def _handle_cron_command(agent, arg, thread_ts, say, channel_id):
    """Handle `!cron <sub>` for this thread. Posts a confirmation; never runs the agent.

    Subcommands (the store is the lock-guarded crons.json):
      add "<5-field expr>" <prompt> -> schedule a recurring run in THIS thread
      list                          -> list this thread's crons (or all? -> all here)
      remove <id>                   -> delete a cron by id
      off <id> / on <id>            -> disable / enable a cron by id
    `channel_id` is the channel the new cron should post into (the current one).
    """
    sub, _, rest = arg.partition(" ")
    sub = sub.lower()
    rest = rest.strip()

    if sub == "add":
        m = _CRON_ADD_RE.match(rest)
        if not m:
            say(
                text='Usage: !cron add "<min hour dom month dow>" <prompt>',
                thread_ts=thread_ts,
            )
            return
        schedule, prompt = m.group(1).strip(), m.group(2).strip()
        if not _cron_expr_valid(schedule):
            say(
                text=(
                    f"Invalid cron expression {schedule!r}. Use 5 fields: "
                    f"min hour day-of-month month day-of-week."
                ),
                thread_ts=thread_ts,
            )
            return
        entry = store.add_cron(schedule, agent["name"], channel_id, thread_ts, prompt)
        say(
            text=(
                f"{agent['display_name']}: scheduled cron `{entry['id']}` "
                f"(`{schedule}`) in this thread: {prompt}"
            ),
            thread_ts=thread_ts,
        )
        return

    if sub == "list":
        crons = store.list_crons()
        if not crons:
            say(
                text=f"{agent['display_name']}: no crons scheduled.",
                thread_ts=thread_ts,
            )
            return
        lines = [f"{agent['display_name']}: scheduled crons:"]
        for c in crons:
            state = "on" if c.get("enabled") else "off"
            lines.append(
                f"- `{c.get('id')}` [{state}] `{c.get('schedule')}` "
                f"({c.get('agent')}): {c.get('prompt')}"
            )
        say(text="\n".join(lines), thread_ts=thread_ts)
        return

    if sub == "remove":
        cron_id = rest
        if store.remove_cron(cron_id):
            say(
                text=f"{agent['display_name']}: removed cron `{cron_id}`.",
                thread_ts=thread_ts,
            )
        else:
            say(
                text=f"{agent['display_name']}: no cron with id `{cron_id}` (not found).",
                thread_ts=thread_ts,
            )
        return

    if sub in ("on", "off"):
        cron_id = rest
        enabled = sub == "on"
        if store.set_cron_enabled(cron_id, enabled):
            say(
                text=(
                    f"{agent['display_name']}: cron `{cron_id}` is now "
                    f"{'enabled' if enabled else 'disabled'}."
                ),
                thread_ts=thread_ts,
            )
        else:
            say(
                text=f"{agent['display_name']}: no cron with id `{cron_id}` (not found).",
                thread_ts=thread_ts,
            )
        return

    say(
        text=(
            'Usage: !cron add "<expr>" <prompt> | !cron list | '
            "!cron remove <id> | !cron on <id> | !cron off <id>"
        ),
        thread_ts=thread_ts,
    )


def _cron_expr_valid(expr):
    """Whether `expr` is a well-formed 5-field cron expression (parses without error).

    Probed by trying cron_matches against a fixed datetime: a malformed expr makes
    cron_matches return False for ALL times, so we instead validate by parsing each
    field directly. Returns True iff all 5 fields parse within their ranges.
    """
    fields = (expr or "").split()
    if len(fields) != 5:
        return False
    try:
        for field, (low, high) in zip(fields, _CRON_FIELD_RANGES):
            _cron_field_values(field, low, high)
    except ValueError:
        return False
    return True


def _fire_cron(entry, live):
    """Run one fired cron: post a placeholder into its thread, then _run_and_update.

    Resolves the cron's agent from the live handler set (so we get that agent's
    Slack client) and the registry (for the agent definition). A cron for an agent
    that is not currently live (no handler) is a no-op (logged), so a removed or
    unstartable agent's stale cron never crashes the tick. Mirrors _handle's flow:
    post a "thinking" placeholder, then synthesize the run via _run_and_update with
    the cron's agent + prompt + target thread.
    """
    from src import app as _appfacade

    name = entry.get("agent")
    handler_entry = live.get(name)
    if handler_entry is None:
        logger.warning("cron %s: agent %r is not live; skipping", entry.get("id"), name)
        return
    agent = next((a for a in agents.REGISTRY if a["name"] == name), None)
    if agent is None:
        logger.warning(
            "cron %s: agent %r not in registry; skipping", entry.get("id"), name
        )
        return
    client = handler_entry["handler"].app.client
    channel = entry.get("channel")
    thread_ts = entry.get("thread_ts")
    prompt = entry.get("prompt") or ""
    try:
        placeholder = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"{agent['display_name']} (scheduled) is thinking...",
        )
        placeholder_ts = placeholder["ts"]
    except Exception:  # noqa: BLE001 - a Slack hiccup must not crash the scheduler
        logger.exception("cron %s: failed to post placeholder", entry.get("id"))
        return
    _appfacade._run_and_update(
        client, channel, placeholder_ts, agent, prompt, thread_ts
    )


def _scheduler_tick(live, now):
    """One scheduler pass: fire every ENABLED cron whose expr matches `now`.

    Re-reads crons.json from disk (so SIGHUP/edits are picked up live), matches
    each enabled entry's 5-field schedule against the injected `now` datetime, and
    fires the matches via _fire_cron. Returns the number of crons fired. `now` is
    injected so tests are hermetic. One bad entry never aborts the pass.
    """
    from src import app as _appfacade

    fired = 0
    for entry in store.list_crons():
        if not entry.get("enabled"):
            continue
        try:
            if cron_matches(entry.get("schedule", ""), now):
                _appfacade._fire_cron(entry, live)
                fired += 1
        except Exception:  # noqa: BLE001 - one bad cron must not abort the tick
            logger.exception("cron %s: fire failed", entry.get("id"))
    return fired


# Seconds between scheduler ticks. 60s so each minute is evaluated once; matching
# is minute-granular (cron's smallest unit), so a 60s tick fires each cron at most
# once per matching minute. ponytail: a fixed interval, no env knob.
_SCHEDULER_TICK_SECONDS = 60


def _scheduler_loop(live, now=None, sleep=None, _once=False):
    """Daemon-thread body: every minute, run _scheduler_tick against the current time.

    `now` is an injectable zero-arg clock returning a datetime (defaults to
    datetime.now) and `sleep` an injectable sleeper (defaults to time.sleep), both
    so tests are hermetic (no real wall-clock, no real sleep). `_once` runs a single
    tick and returns (the test seam); production runs forever. To avoid firing a
    cron twice within the same minute (two ticks can land in one minute), we skip a
    tick whose minute equals the last fired minute.
    """
    from src import app as _appfacade

    if now is None:
        now = datetime.now
    if sleep is None:
        sleep = time.sleep
    last_minute = None
    while True:
        current = now()
        minute_key = current.replace(second=0, microsecond=0)
        if minute_key != last_minute:
            last_minute = minute_key
            try:
                _appfacade._scheduler_tick(live, now=current)
            except Exception:  # noqa: BLE001 - a tick error must not kill the thread
                logger.exception("scheduler tick failed")
        if _once:
            break
        sleep(_SCHEDULER_TICK_SECONDS)
