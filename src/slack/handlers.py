"""Mention/message handling: the event seam, the streaming updater, the
background worker, and the per-message dispatch.

A Slack event can arrive as BOTH an app_mention and a message.*, so _handle
dedups on a stable id (claude_runner.seen_before), acks immediately, posts a
"thinking" placeholder, runs the slow CLI in a background thread, then
chat_updates the placeholder with the reply. Moved verbatim from the former
src/app.py.

Kind-A seam: runner dispatch goes through the shared `runners` package object
(`runners.get_runner`) so a test's monkeypatch on `app.runners.get_runner` (the
same package object) is seen here. Kind-B seam: the wall-clock `_now` is resolved
THROUGH the app facade so a test that patches `app._now` is seen by the
write-mode TTL check.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from src import runners, store
from src.runners import claude_runner, codex_runner

from . import control, files, interrupt, quotes, usage

logger = logging.getLogger("peon")

# Matches a Slack user mention like <@U123ABC> so we can strip the bot tag.
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

# Italic notice appended to (or replacing) a reply when a run is interrupted via
# !stop. One constant so the worker's two emit sites never drift.
_INTERRUPTED_NOTICE = "_(interrupted)_"


def _event_id(event):
    """A stable per-message id for idempotency. Prefer Slack's client_msg_id
    (present on user messages); fall back to (channel, ts), which is unique per
    delivered message even when client_msg_id is absent.
    """
    return event.get("client_msg_id") or f"{event.get('channel')}:{event.get('ts')}"


def _clean_prompt(text):
    """Strip every bot mention from the message; the remainder is the prompt."""
    return MENTION_RE.sub("", text or "").strip()


_THREAD_HISTORY_LIMIT = 50


def _ts_before(left, right):
    """Whether Slack timestamp `left` is strictly before `right`."""
    try:
        return float(left) < float(right)
    except (TypeError, ValueError):
        return str(left or "") < str(right or "")


def _message_author(message):
    """Best-effort display label for one Slack message."""
    bot_profile = message.get("bot_profile")
    if isinstance(bot_profile, dict):
        name = bot_profile.get("name") or bot_profile.get("app_name")
        if name:
            return name
    return message.get("user") or message.get("username") or message.get("bot_id") or "unknown"


def _format_thread_history(messages, current_ts):
    """Render visible prior Slack messages into a compact transcript block."""
    lines = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("subtype") == "message_deleted":
            continue
        ts = message.get("ts")
        if current_ts and not _ts_before(ts, current_ts):
            continue
        text = (message.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"- {_message_author(message)}: {text}")
    if not lines:
        return ""
    return "Visible Slack thread so far:\n" + "\n".join(lines)


def _fetch_thread_history(client, channel, thread_ts, current_ts):
    """Fetch a bounded visible Slack-thread transcript before the current message."""
    try:
        response = client.conversations_replies(
            channel=channel,
            ts=thread_ts,
            limit=_THREAD_HISTORY_LIMIT,
        )
    except Exception:  # noqa: BLE001 - history is helpful context, not required
        logger.warning("failed to fetch Slack thread history for %s", thread_ts)
        return ""
    messages = response.get("messages") if hasattr(response, "get") else None
    if not isinstance(messages, list):
        return ""
    return _format_thread_history(messages, current_ts)


def _append_thread_history(prompt, history):
    """Attach prior visible Slack-thread context to the current request."""
    if not history:
        return prompt
    return f"{history}\n\nCurrent request:\n{prompt}"


# Minimum seconds between throttled incremental chat_update calls while streaming.
# ponytail: a fixed 1s min-interval gate (the ceiling is ~1 update/sec); the final
# update on stream close always fires regardless of this gate, so the last token is
# never dropped. Slack's chat.update is rate-limited (~Tier 3); 1/sec stays well
# under it. Not configurable on purpose; no env knob.
_STREAM_UPDATE_MIN_INTERVAL_S = 1.0


def _make_stream_updater(client, channel, placeholder_ts, now=None):
    """Return an on_update(text) callback that throttles chat_update to ~1/sec.

    The returned callback edits the placeholder with the cumulative streamed text,
    but only if at least _STREAM_UPDATE_MIN_INTERVAL_S has elapsed since the last
    posted update (the FIRST chunk always posts). This bounds Slack API calls to
    ~1/sec; the worker still does an unconditional FINAL chat_update after the run
    (with the full text + footer), so the throttle never drops the final reply.
    Empty text is skipped (Slack rejects empty messages). A failed chat_update is
    swallowed so a transient Slack error never aborts the stream.

    `now` is an injectable monotonic clock (a zero-arg callable) for hermetic
    tests; defaults to time.monotonic. State (last-post time) is closed over.
    """
    if now is None:
        now = time.monotonic
    last_post: float | None = None

    def _update(text):
        nonlocal last_post
        if not text:
            return
        current = now()
        if (
            last_post is not None
            and (current - last_post) < _STREAM_UPDATE_MIN_INTERVAL_S
        ):
            return
        last_post = current
        try:
            client.chat_update(channel=channel, ts=placeholder_ts, text=text)
        except Exception:  # noqa: BLE001 - a transient Slack error must not abort the stream
            logger.warning("incremental chat_update failed; will retry on next chunk")

    return _update


def _run_and_update(client, channel, placeholder_ts, agent, prompt, thread_ts):
    """Background worker: resolve session, run the agent's backend, edit the placeholder.

    Backend-agnostic via the unified runner seam: load the prior session id for
    this (agent, thread) key, dispatch to the agent's backend runner, then
    persist whatever session id the runner reports (a claude uuid or a codex
    thread_id). The (agent, thread) key stays identical for both backends, so
    contexts stay independent. The per-thread model/effort override (if any) is
    looked up here under the SAME key and passed into runner.answer, so a thread
    that set !model/!effort uses it.

    STREAMING (the run-time default): an on_update callback is passed into
    runner.answer that incrementally chat_updates the placeholder with the partial
    reply, throttled to ~1/sec (see _make_stream_updater). After the run returns we
    ALWAYS do a final chat_update with the full text (plus the usage footer when
    SHOW_USAGE is on), so the last update is never dropped and the footer is shown.
    STREAM_OUTPUT=0 just means on_update is never invoked (the runner ignores it),
    leaving a single final update. When SHOW_USAGE is on, a small usage footer
    (built from the runner's meta) is appended under the reply. One bad run must
    never crash the process: every failure path is caught and turned into a short
    error message in the thread.

    OUTBOUND FILES: after the final update, if the thread has a designated
    read-write workdir (the read-write feature installs claude_runner.get_workdir),
    files the run created/modified there are uploaded back into the thread (see
    _maybe_upload_outputs). With no workdir configured this is a no-op.
    """
    from src import app as _appfacade

    # Register a cancel token so a "!stop" in this thread can SIGINT the run; the
    # finally below always drops it. See src.slack.interrupt / common.Interrupt.
    token = interrupt.register(agent["name"], thread_ts)
    try:
        runner = runners.get_runner(agent.get("backend", "claude"))
        prior = store.get_session(agent["name"], thread_ts)
        # Identity: agents carry no injected name, so without this they read the
        # repo's CLAUDE.md (the inherited cwd) and identify as the framework / the
        # first agent listed there (Aristotle). Prepend a one-line identity to
        # EVERY message: a fresh thread learns its name, and a thread that already
        # learned the wrong one (created before this fix) is corrected on its next
        # turn. Gating on `prior is None` left those old threads stuck on Aristotle.
        # ponytail: prompt-prepend, not --append-system-prompt, so ONE seam covers
        # both backends without touching the load-bearing per-runner argv.
        prompt = (
            f"For this conversation your name is {agent['display_name']}. "
            f"If the user asks who you are, you are {agent['display_name']}.\n\n"
            f"{prompt}"
        )
        overrides = store.get_override(agent["name"], thread_ts)
        # WRITE-MODE (consent + TTL): inject the isolated, created workdir into
        # overrides ONLY while consent is currently ACTIVE (write flag on AND not
        # expired, per is_write_active against the module clock). An expired or
        # absent grant injects nothing, so the run reverts to the read-only argv
        # automatically (byte-identical to the read-only path).
        if overrides and store.is_write_active(
            agent["name"], thread_ts, now=_appfacade._now
        ):
            overrides = {
                **overrides,
                "_workdir": store.get_workdir(agent["name"], thread_ts, create=True),
            }
        updater = _make_stream_updater(client, channel, placeholder_ts)
        # Mark the run start so we can upload only files the run created/modified in
        # the thread's workdir (see _maybe_upload_outputs). Wall-clock mtime compare.
        run_started = time.time()
        text, session_id, meta = runner.answer(
            agent, prompt, prior, overrides=overrides, on_update=updater, cancel=token
        )
        store.set_session(agent["name"], thread_ts, session_id)
        # User interrupt: the run settled early. Mark the (partial) reply so the
        # thread reads like a terminal Ctrl-C. token.proc guards the rare non-stream
        # case where the flag was set but nothing was actually killable.
        if token.requested and token.proc is not None:
            text = f"{text}\n{_INTERRUPTED_NOTICE}" if text else _INTERRUPTED_NOTICE
        if usage._usage_enabled():
            footer = usage._format_usage(meta)
            if footer:
                text = text + "\n" + footer
        # Unconditional FINAL update with the complete text + footer. Always fires,
        # regardless of the streaming throttle, so the last chunk is never lost.
        client.chat_update(channel=channel, ts=placeholder_ts, text=text)
        # Outbound files: if this thread has a designated read-write workdir, upload
        # any files the run produced there back into the thread. A no-op (no Slack
        # upload call) when no workdir is configured (the common case today).
        files._maybe_upload_outputs(client, channel, thread_ts, agent, run_started)
    except (claude_runner.ClaudeRunError, codex_runner.CodexRunError) as exc:
        if token.requested:
            # Interrupted during a phase we cannot settle gracefully (e.g. a codex
            # fresh run killed before its thread_id was emitted): show a clean
            # interrupted notice, not the raw error.
            client.chat_update(
                channel=channel, ts=placeholder_ts, text=_INTERRUPTED_NOTICE
            )
        else:
            logger.warning(
                "%s run failed for %s: %s",
                agent.get("backend", "claude"),
                agent["name"],
                exc,
            )
            client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text=f":warning: {agent['display_name']} hit an error: {exc}",
            )
    except Exception:  # noqa: BLE001 - last-resort guard, keep process alive
        logger.exception("unexpected failure handling %s", agent["name"])
        try:
            client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text=f":warning: {agent['display_name']} hit an unexpected error.",
            )
        except Exception:
            logger.exception("failed to post error message")
    finally:
        interrupt.unregister(agent["name"], thread_ts, token)


def _handle(agent, event, client, say):
    """Handle one mention for a FIXED agent (the app this handler belongs to).

    No keyword routing: the prompt is just the de-mentioned message text.
    """
    if event.get("subtype") or event.get("bot_id"):
        return  # ignore edits/deletes and the bot's own messages

    # Idempotency guard: an in-thread mention can be delivered as BOTH an
    # app_mention and a message.* event. Dedup on a stable id so we answer once.
    # (Cheap and robust; kept even though one app per bot reduces double-firing.)
    if claude_runner.seen_before(_event_id(event)):
        return

    channel = event["channel"]
    # Top-level mention -> use its own ts as the thread root, so the whole
    # conversation in that thread shares one context key.
    thread_ts = event.get("thread_ts") or event["ts"]

    prompt = _clean_prompt(event.get("text", ""))
    if not prompt:
        say(
            text=f"{agent['display_name']} here. What would you like to ask?",
            thread_ts=thread_ts,
        )
        return

    # A "!"-prefixed message is a per-thread control phrase (set model/effort,
    # toggle write-mode, or reset): handle it inline and return WITHOUT running the
    # agent. The requester's user + channel ids gate the write-mode allowlist.
    if control._handle_control_phrase(
        agent,
        prompt,
        thread_ts,
        say,
        user_id=event.get("user"),
        channel_id=channel,
    ):
        return

    history = _fetch_thread_history(client, channel, thread_ts, event.get("ts"))

    # Inbound attachments: download any files[] on this message with the bot token
    # and append their local paths to the prompt so the CLI agent can read them.
    # A failed download is skipped (see _download_attachments); no files -> the
    # prompt is unchanged.
    attachment_paths = files._download_attachments(
        client, event.get("files"), thread_ts
    )
    prompt = files._append_attachments(prompt, attachment_paths)
    prompt = _append_thread_history(prompt, history)

    # Post a placeholder immediately (the Slack ack already happened), then do
    # the slow claude run off-thread so we never block. Show a random short
    # "peon" worker quote as the placeholder; an empty quote (no/invalid
    # quotes.json) falls back to the default "is thinking..." text.
    quote = quotes.random_quote()
    placeholder_text = quote or f"{agent['display_name']} is thinking..."
    placeholder = say(text=placeholder_text, thread_ts=thread_ts)
    placeholder_ts = placeholder["ts"]

    worker = threading.Thread(
        target=_run_and_update,
        args=(client, channel, placeholder_ts, agent, prompt, thread_ts),
        daemon=True,
    )
    worker.start()
