"""Mention/message handling: the event seam, the streaming updater, the
background worker, and the per-message dispatch.

A Slack event can arrive as BOTH an app_mention and a message.*, so _handle
dedups on a stable id (claude_runner.seen_before), acks immediately, posts a
"thinking" placeholder, runs the slow CLI in a background thread, then
chat_updates the placeholder with the reply.

Kind-A seam: runner dispatch goes through the shared `runners` package object
(`runners.get_runner`) so a test's monkeypatch on `app.runners.get_runner` (the
same package object) is seen here.
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

# Appended to the partial streamed reply when a run ERRORS (or is interrupted) after
# producing some text: keep what was streamed instead of freezing a mid-sentence
# fragment or replacing it with a bare error, and tell the user the thread is still
# resumable (the session id was persisted at run start, so the next message
# continues). Distinct from the clean !stop _INTERRUPTED_NOTICE above.
_INTERRUPTED_RESUME_NOTICE = (
    "\n\n_(interrupted - the reply was cut off; send any message to continue)_"
)


def _event_id(event):
    """A stable per-message id for idempotency. Prefer Slack's client_msg_id
    (present on user messages); fall back to (channel, ts), which is unique per
    delivered message even when client_msg_id is absent.
    """
    return event.get("client_msg_id") or f"{event.get('channel')}:{event.get('ts')}"


def _clean_prompt(text):
    """Strip every bot mention from the message; the remainder is the prompt."""
    return MENTION_RE.sub("", text or "").strip()


def _format_final_response(text):
    """Normalize a terminal chat_update body to end in exactly one blank line.

    Applied to every final post (reply + usage footer, the interrupted notice,
    and error messages) so they end uniformly: trailing whitespace is stripped,
    then a single trailing blank line is appended, collapsing any newline
    buildup from footer joins or model output to one.
    """
    return (text or "").rstrip() + "\n\n"


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
    return (
        message.get("user")
        or message.get("username")
        or message.get("bot_id")
        or "unknown"
    )


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
    last_text: str | None = None

    def _update(text, force=False):
        nonlocal last_post, last_text
        # Scrub any (possibly partial) <<files: ...>> marker so it never flashes
        # mid-stream; the final update strips it for real via _parse_file_marker.
        text = files._strip_file_marker(text)
        # Skip empties (Slack rejects them) and no-op re-posts (a force-flush on a
        # tool_use block_stop carries the SAME text as the text block before it).
        if not text or text == last_text:
            return
        current = now()
        # `force` bypasses the 1/sec gate: the runner force-flushes when a content
        # block ENDS, so a completed text block (the agent's initial preamble,
        # right before a long tool/subagent call that emits no more text) shows in
        # FULL instead of the mid-sentence fragment the throttle last posted.
        if (
            not force
            and last_post is not None
            and (current - last_post) < _STREAM_UPDATE_MIN_INTERVAL_S
        ):
            return
        last_post = current
        last_text = text
        try:
            client.chat_update(channel=channel, ts=placeholder_ts, text=text)
        except Exception as exc:  # noqa: BLE001 - a transient Slack error must not abort the stream
            logger.warning(
                "incremental chat_update failed (%s); will retry on next chunk", exc
            )

    return _update


def _run_and_update(
    client, channel, placeholder_ts, agent, prompt, thread_ts, token=None
):
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

    OUTBOUND FILES: a run delivers files ONLY by ending its reply with a
    `<<files: a, b>>` marker; we strip that marker from the shown reply and upload
    just the named files (resolved inside the workdir; see _maybe_upload_named).
    No marker (the default) uploads nothing.
    """
    # Register a cancel token so a "!stop" in this thread can SIGINT the run; the
    # finally below always drops it. See src.slack.interrupt / common.Interrupt.
    # _handle pre-registers via try_register (the busy guard) and passes the token
    # in; callers without one (cron) register their own here.
    if token is None:
        token = interrupt.register(agent["name"], thread_ts)
    # Bound before the try so the error branch can always read the last streamed
    # text, even if a ClaudeRunError were ever raised before the updater is built.
    streamed = {"text": ""}
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
            "This run is a single non-interactive turn: the process exits as soon "
            "as your reply is complete, so any subagent or task you start in the "
            "BACKGROUND (e.g. run_in_background) is killed before it finishes and "
            "its work is lost. Do all long or multi-step work (surveys, research, "
            "builds) synchronously in the FOREGROUND within this turn; it is fine "
            "for the reply to take a while.\n\n"
            "If -- and only if -- the user explicitly asks you to produce, send, "
            "attach, or share a file, end your reply with a line "
            "`<<files: name1, name2>>` naming the files (paths in your working "
            "directory) to deliver. Otherwise never write that marker and no "
            "files are sent.\n\n"
            f"{prompt}"
        )
        overrides = store.get_override(agent["name"], thread_ts)
        # Always give the run its per-thread workdir as cwd. The run is fully
        # unsandboxed and can touch any path; the workdir is just its home so the
        # outbound file delivery (the run's named files, in this dir) works.
        overrides = {
            **(overrides or {}),
            "_workdir": store.get_workdir(agent["name"], thread_ts, create=True),
        }
        # Wrap the throttled updater to also retain the latest cumulative streamed
        # text, so an errored/interrupted run can finalize the message as the partial
        # reply (see the except branch) instead of a frozen fragment or a bare error.
        base_updater = _make_stream_updater(client, channel, placeholder_ts)

        def updater(partial, force=False):
            streamed["text"] = partial
            base_updater(partial, force=force)

        # Persist the session id the moment the runner mints it (claude: BEFORE the
        # subprocess starts) so an interrupted run leaves a resumable id. Codex ignores
        # this (its id is only known post-run); the post-run set_session below covers it.
        def on_session(session_id):
            store.set_session(agent["name"], thread_ts, session_id)

        text, session_id, meta = runner.answer(
            agent,
            prompt,
            prior,
            overrides=overrides,
            on_update=updater,
            cancel=token,
            on_session=on_session,
        )
        store.set_session(agent["name"], thread_ts, session_id)
        # Split off any `<<files: ...>>` delivery marker BEFORE the interrupt notice
        # / usage footer are appended, so it is gone from the posted reply and the
        # named files drive the outbound upload below.
        text, upload_names = files._parse_file_marker(text)
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
        client.chat_update(
            channel=channel, ts=placeholder_ts, text=_format_final_response(text)
        )
        # Outbound files: upload only the files the run named in its marker.
        files._maybe_upload_named(client, channel, thread_ts, agent, upload_names)
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
            # If the run streamed partial text before erroring, keep it (plus a
            # resume note) rather than throwing it away for a bare error. The
            # session id was persisted at run start, so the thread is resumable.
            partial = files._strip_file_marker(streamed["text"]).strip()
            if partial:
                client.chat_update(
                    channel=channel,
                    ts=placeholder_ts,
                    text=_format_final_response(partial + _INTERRUPTED_RESUME_NOTICE),
                )
            else:
                client.chat_update(
                    channel=channel,
                    ts=placeholder_ts,
                    text=_format_final_response(
                        f":warning: {agent['display_name']} hit an error: {exc}"
                    ),
                )
    except Exception:  # noqa: BLE001 - last-resort guard, keep process alive
        logger.exception("unexpected failure handling %s", agent["name"])
        try:
            client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text=_format_final_response(
                    f":warning: {agent['display_name']} hit an unexpected error."
                ),
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
    # manage crons, or reset): handle it inline and return WITHOUT running the
    # agent. channel_id is where a !cron schedules.
    if control._handle_control_phrase(
        agent,
        prompt,
        thread_ts,
        say,
        channel_id=channel,
    ):
        return

    # Busy guard: one run per (agent, thread) at a time. try_register atomically
    # claims the thread's slot; if a run is already in flight it returns None and
    # we decline rather than spawn a second run that would --resume the same
    # session id concurrently (a race). The token is threaded into the worker and
    # released in _run_and_update's finally. "!stop" still reaches the live run
    # (handled above, before this guard), so a stuck thread is never wedged.
    token = interrupt.try_register(agent["name"], thread_ts)
    if token is None:
        say(
            text=(
                f"{agent['display_name']} is still working on an earlier message "
                "in this thread. Wait for it to finish, or send `!stop` to cancel it."
            ),
            thread_ts=thread_ts,
        )
        return

    # We hold the thread's busy slot now (token). Until the worker starts and its
    # finally takes over cleanup, any failure here (history fetch, placeholder
    # post) must release the slot, or the thread would wedge as permanently busy.
    try:
        history = _fetch_thread_history(client, channel, thread_ts, event.get("ts"))

        # Inbound attachments: download any files[] on this message with the bot
        # token and append their local paths to the prompt so the CLI agent can
        # read them. A failed download is skipped (see _download_attachments); no
        # files -> the prompt is unchanged.
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
            kwargs={"token": token},
            daemon=True,
        )
        worker.start()
    except Exception:
        # Handoff failed before the worker could own the slot: release it so the
        # thread is not stuck busy, then let the error propagate as before.
        interrupt.unregister(agent["name"], thread_ts, token)
        raise
