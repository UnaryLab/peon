"""Streaming output (both backends) + throttled stream-updater."""

import json
from unittest import mock

from src.runners import claude_runner, codex_runner

from tests.helpers import (
    SID,
    PROMPT,
    MODEL,
    THREAD_ID,
    BRUNEL,
    CICERO,
    DIJKSTRA,
    _clear_model_effort_env,
    _fake_proc,
    _codex_proc_writing,
    _fake_popen_factory,
    _claude_stream_lines,
    _FakeClient,
    _appmod,
    _HAVE_APP,
)


# ---------------------------------------------------------------------------
# Streaming output (both backends). Default ON (STREAM_OUTPUT unset/truthy); the
# tests above pin STREAM_OUTPUT="0" via conftest, so these re-enable it. Claude
# switches argv to --output-format stream-json --include-partial-messages
# --verbose and reads JSONL deltas; codex keeps its argv (already --json) but
# consumes stdout incrementally while still reading the -o file for the final
# reply. All subprocess I/O is mocked (subprocess.Popen here, since streaming uses
# Popen, vs subprocess.run on the legacy path). No real CLI/Slack/network calls.
# ---------------------------------------------------------------------------


def test_build_command_stream_flags_claude(monkeypatch):
    # stream=True swaps the output-format flags to the streaming set (verified
    # against claude 2.1.187: stream-json in -p mode REQUIRES --verbose). The rest
    # of the argv (session/agent/model/prompt) is unchanged. Both new and resume.
    _clear_model_effort_env(monkeypatch)
    new_argv = claude_runner.build_command(BRUNEL, PROMPT, SID, True, stream=True)
    assert new_argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--session-id",
        SID,
        "--agent",
        "unarylab-research:project_manager",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        PROMPT,
    ]
    resume_argv = claude_runner.build_command(CICERO, PROMPT, SID, False, stream=True)
    assert resume_argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--resume",
        SID,
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        PROMPT,
    ]
    # The DEFAULT (stream omitted) is still the legacy json argv, byte-identical.
    assert claude_runner.build_command(BRUNEL, PROMPT, SID, True)[:4] == [
        "claude",
        "-p",
        "--output-format",
        "json",
    ]


def test_run_claude_streaming_partial_and_final(monkeypatch):
    # STREAM_OUTPUT on: run_claude reads JSONL deltas, calls on_update with the
    # growing text, returns the result-event text as the final reply, and parses
    # meta (usage/cost/timing) FROM the stream's result event.
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    chunks = ["Hello", ", ", "world"]
    lines = _claude_stream_lines(
        chunks,
        usage={"input_tokens": 30000, "output_tokens": 2000},
        total_cost_usd=0.04,
        duration_ms=18000,
    )
    updates = []
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines),
    ):
        reply, meta = claude_runner.run_claude(
            BRUNEL, PROMPT, SID, True, on_update=updates.append
        )
    # At least one PARTIAL update, and the cumulative text grows toward the reply.
    assert len(updates) >= 1
    assert updates[-1] == "Hello, world"
    assert updates == ["Hello", "Hello, ", "Hello, world"]
    # Final reply is the result event's text.
    assert reply == "Hello, world"
    # Telemetry still parsed from the stream (BRUNEL -> [1m] pin -> 1M window).
    assert meta["tokens"] == 32000
    assert meta["context_pct"] == 3  # 30000 / 1_000_000 -> 3%
    assert meta["cost_usd"] == 0.04
    assert meta["duration_s"] == 18.0


def test_claude_answer_streaming_threads_on_update(monkeypatch):
    # The unified seam threads on_update through to the stream and surfaces the
    # minted session id (new run) alongside the streamed reply.
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    lines = _claude_stream_lines(["part1", "part2"])
    updates = []
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines),
    ) as m:
        reply, sid, _meta = claude_runner.answer(
            BRUNEL, PROMPT, None, on_update=updates.append
        )
    assert reply == "part1part2"
    assert updates[-1] == "part1part2"
    # A fresh uuid was minted and passed as --session-id on the streaming argv.
    argv = m.call_args[0][0]
    assert "--session-id" in argv and argv[argv.index("--session-id") + 1] == sid
    assert "stream-json" in argv  # the streaming path was used


def test_run_claude_streaming_salvages_text_when_no_result_event(monkeypatch):
    # If the stream carries deltas but NO terminal result event (a format hiccup),
    # the accumulated text is returned rather than lost; meta degrades to all-None.
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    lines = [
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "salvaged"},
                },
            }
        )
    ]
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines),
    ):
        reply, meta = claude_runner.run_claude(BRUNEL, PROMPT, SID, True)
    assert reply == "salvaged"
    assert all(meta[k] is None for k in meta)


def test_run_claude_streaming_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory([], returncode=1, stderr="kaboom"),
    ):
        try:
            claude_runner.run_claude(CICERO, PROMPT, SID, True)
            assert False, "expected ClaudeRunError"
        except claude_runner.ClaudeRunError as exc:
            assert "code 1" in str(exc)


def test_run_claude_streaming_ignores_thinking_deltas(monkeypatch):
    # thinking_delta (reasoning) chunks must NOT leak into the user-facing text;
    # only text_delta chunks accumulate, so the reply matches the result event.
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    lines = [
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "hmm secret"},
                },
            }
        ),
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "answer"},
                },
            }
        ),
        json.dumps({"type": "result", "is_error": False, "result": "answer"}),
    ]
    updates = []
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines),
    ):
        reply, _meta = claude_runner.run_claude(
            CICERO, PROMPT, SID, True, on_update=updates.append
        )
    assert reply == "answer"
    assert "secret" not in "".join(updates)
    assert updates == ["answer"]


def test_run_claude_stream_disabled_keeps_legacy_argv_and_single_path(monkeypatch):
    # STREAM_OUTPUT=0 forces the legacy single-blob path: build_command emits the
    # ORIGINAL --output-format json argv and run_claude uses subprocess.run (NOT
    # Popen), with one parse and no on_update calls. Byte-identical to pre-streaming.
    _clear_model_effort_env(monkeypatch)
    monkeypatch.setenv("STREAM_OUTPUT", "0")
    good = json.dumps({"result": "legacy", "is_error": False, "subtype": "success"})
    updates = []
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, good)
    ) as m_run:
        with mock.patch(
            "src.runners.claude_runner.subprocess.Popen",
            side_effect=AssertionError("Popen must not be used on the legacy path"),
        ):
            reply, _meta = claude_runner.run_claude(
                BRUNEL, PROMPT, SID, True, on_update=updates.append
            )
    assert reply == "legacy"
    assert updates == []  # on_update never fired on the legacy path
    argv = m_run.call_args[0][0]
    assert argv == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--session-id",
        SID,
        "--agent",
        "unarylab-research:project_manager",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        PROMPT,
    ]


def test_run_codex_streaming_partial_and_final(monkeypatch):
    # STREAM_OUTPUT on: run_codex reads codex JSONL incrementally, calls on_update
    # with the agent-message text as it grows, but the FINAL reply still comes from
    # the -o file (authoritative). thread_id + tokens are parsed from the stream.
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    lines = [
        json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
        json.dumps(
            {
                "type": "item.updated",
                "item": {"item_type": "agent_message", "text": "partial"},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"item_type": "agent_message", "text": "partial reply"},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 12000, "output_tokens": 2000},
            }
        ),
    ]
    updates = []
    with mock.patch(
        "src.runners.codex_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines, writes_to_o="final from -o file"),
    ):
        reply, sid, meta = codex_runner.run_codex(
            DIJKSTRA, PROMPT, None, True, on_update=updates.append
        )
    # Incremental updates fired with the growing agent-message text.
    assert updates == ["partial", "partial reply"]
    # FINAL reply is the -o file content (authoritative), NOT the streamed text.
    assert reply == "final from -o file"
    assert sid == THREAD_ID  # minted thread_id parsed from the stream
    assert meta["tokens"] == 14000  # 12000 + 2000, parsed from the stream
    assert meta["context_pct"] is None
    assert meta["cost_usd"] is None
    assert isinstance(meta["duration_s"], float)


def test_codex_answer_streaming_threads_on_update(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    lines = [
        json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"item_type": "agent_message", "text": "hi there"},
            }
        ),
    ]
    updates = []
    with mock.patch(
        "src.runners.codex_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines, writes_to_o="hi there"),
    ):
        reply, sid, _meta = codex_runner.answer(
            DIJKSTRA, PROMPT, None, on_update=updates.append
        )
    assert reply == "hi there"
    assert sid == THREAD_ID
    assert updates == ["hi there"]


def test_run_codex_streaming_flushes_full_message_on_completed(monkeypatch):
    # Parity with the claude block_stop flush: a partial agent_message item, then a
    # completed item with the full text, both arriving inside the 1s throttle gate.
    # With the real throttled updater the partial would freeze the display; the
    # completed-item force-flush posts the FULL message instead of a fragment.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    full = "On it, running the full analysis now."
    lines = [
        json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
        json.dumps(
            {
                "type": "item.updated",
                "item": {"item_type": "agent_message", "text": "On it, running"},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"item_type": "agent_message", "text": full},
            }
        ),
    ]
    clock = {"t": 0.0}

    def _tick():
        t = clock["t"]
        clock["t"] += 0.1  # both items < 1s after the first post -> throttled
        return t

    client = _FakeClient()
    updater = _appmod._make_stream_updater(client, "C1", "TS1", now=_tick)
    with mock.patch(
        "src.runners.codex_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines, writes_to_o="final from -o file"),
    ):
        reply, sid, _meta = codex_runner.run_codex(
            DIJKSTRA, PROMPT, None, True, on_update=updater
        )
    texts = [u["text"] for u in client.updates]
    # The completed item's FULL text landed live (force-flush), not just the partial.
    assert texts[-1] == full
    assert reply == "final from -o file"  # -o file stays authoritative


def test_run_codex_streaming_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    with mock.patch(
        "src.runners.codex_runner.subprocess.Popen",
        side_effect=_fake_popen_factory([], returncode=2, stderr="boom"),
    ):
        try:
            codex_runner.run_codex(DIJKSTRA, PROMPT, None, True)
            assert False, "expected CodexRunError"
        except codex_runner.CodexRunError as exc:
            assert "code 2" in str(exc)


def test_codex_stream_disabled_uses_subprocess_run(monkeypatch):
    # STREAM_OUTPUT=0: the legacy codex path reads stdout via subprocess.run (NOT
    # Popen) and the reply from -o. Byte-identical to pre-streaming behavior.
    monkeypatch.setenv("STREAM_OUTPUT", "0")
    stdout = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    updates = []
    with mock.patch(
        "src.runners.codex_runner.subprocess.run",
        side_effect=_codex_proc_writing("legacy reply", stdout=stdout),
    ):
        with mock.patch(
            "src.runners.codex_runner.subprocess.Popen",
            side_effect=AssertionError("Popen must not be used on the legacy path"),
        ):
            reply, sid, _meta = codex_runner.run_codex(
                DIJKSTRA, PROMPT, None, True, on_update=updates.append
            )
    assert reply == "legacy reply"
    assert sid == THREAD_ID
    assert updates == []  # on_update never fired on the legacy path


def test_agent_message_text_from_event_shapes():
    # The defensive codex extractor recognizes the typed item-event shapes and
    # ignores non-message events; the -o file remains authoritative regardless.
    f = codex_runner._agent_message_text_from_event
    assert (
        f(
            {
                "type": "item.completed",
                "item": {"item_type": "agent_message", "text": "x"},
            }
        )
        == "x"
    )
    assert (
        f({"type": "item.updated", "item": {"type": "agent_message", "content": "y"}})
        == "y"
    )
    assert f({"type": "agent_message", "text": "flat"}) == "flat"
    # Non-message events -> None (reasoning, usage, thread lifecycle, junk).
    assert (
        f({"type": "item.completed", "item": {"item_type": "reasoning", "text": "r"}})
        is None
    )
    assert f({"type": "turn.completed", "usage": {"input_tokens": 1}}) is None
    assert f({"type": "thread.started", "thread_id": THREAD_ID}) is None
    assert f("not a dict") is None


# ---------------------------------------------------------------------------
# app.py streaming updater: a throttled (~1/sec) chat_update callback with an
# INJECTED clock (never real wall-clock, never sleep). Tested directly with a fake
# Slack client + a controllable now(). src.app imported via the _HAVE_APP guard.
# ---------------------------------------------------------------------------


def test_stream_updater_throttles_to_one_per_second():
    if not _HAVE_APP:
        return  # _make_stream_updater lives in app.py; needs slack_bolt
    assert _appmod is not None
    client = _FakeClient()
    clock = {"t": 100.0}
    updater = _appmod._make_stream_updater(client, "C1", "TS1", now=lambda: clock["t"])

    updater("a")  # first chunk ALWAYS posts
    clock["t"] = 100.5
    updater("ab")  # 0.5s later -> throttled, dropped
    clock["t"] = 101.2
    updater("abc")  # 1.2s after the last POST -> allowed
    clock["t"] = 101.5
    updater("abcd")  # 0.3s later -> throttled, dropped

    texts = [u["text"] for u in client.updates]
    assert texts == ["a", "abc"]  # only the un-throttled posts landed
    assert all(u["channel"] == "C1" and u["ts"] == "TS1" for u in client.updates)


def test_stream_updater_skips_empty_text():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    client = _FakeClient()
    updater = _appmod._make_stream_updater(client, "C1", "TS1", now=lambda: 0.0)
    updater("")  # empty -> Slack rejects empty messages, so skip
    assert client.updates == []


def test_stream_updater_swallows_chat_update_errors():
    if not _HAVE_APP:
        return
    assert _appmod is not None

    class _BoomClient:
        def chat_update(self, **kwargs):
            raise RuntimeError("slack down")

    updater = _appmod._make_stream_updater(_BoomClient(), "C1", "TS1", now=lambda: 0.0)
    updater("x")  # must NOT raise (a Slack hiccup cannot abort the stream)


def test_stream_throttle_never_drops_the_final_update():
    # Simulate the worker's contract: the throttled updater may drop mid-stream
    # chunks, but the worker ALWAYS does a final unconditional chat_update. We
    # prove that final-update call lands even when the last throttled chunk was
    # dropped, with the full text + footer appended.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    client = _FakeClient()
    clock = {"t": 0.0}
    updater = _appmod._make_stream_updater(client, "C1", "TS1", now=lambda: clock["t"])

    updater("partial-1")  # posts (first)
    clock["t"] = 0.2
    updater("partial-1-2")  # dropped (0.2s < 1s); this would be lost if it were final

    # The worker's FINAL step: unconditional chat_update with full text + footer.
    meta = {"context_pct": 4, "tokens": 42000, "cost_usd": 0.04, "duration_s": 18.0}
    footer = _appmod._format_usage(meta)
    final_text = "full final reply" + "\n" + footer
    client.chat_update(channel="C1", ts="TS1", text=final_text)

    # The final update is present and carries the COMPLETE text + footer, even
    # though the immediately-preceding throttled chunk was dropped.
    assert client.updates[-1]["text"] == final_text
    assert "full final reply" in client.updates[-1]["text"]
    assert footer in client.updates[-1]["text"]


def test_stream_updater_force_bypasses_throttle():
    # A force=True flush (the runner fires it when a content block ENDS) posts the
    # full text past the 1/sec throttle, so a completed text block is never frozen
    # as a mid-sentence fragment. A repeat force with identical text is deduped.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    client = _FakeClient()
    clock = {"t": 0.0}
    updater = _appmod._make_stream_updater(client, "C1", "TS1", now=lambda: clock["t"])

    updater("I'll route this")  # first chunk -> posts
    clock["t"] = 0.2
    updater("I'll route this survey")  # 0.2s -> throttled, dropped
    clock["t"] = 0.4
    updater("I'll route this survey now.", force=True)  # force -> posts despite <1s
    clock["t"] = 0.5
    updater("I'll route this survey now.", force=True)  # same text -> deduped

    texts = [u["text"] for u in client.updates]
    assert texts == ["I'll route this", "I'll route this survey now."]


def test_run_claude_streaming_flushes_full_block_on_stop(monkeypatch):
    # Regression: a quick preamble text block, a content_block_stop, then NO more
    # text (the agent went into a long tool/subagent call). With the real throttled
    # updater the display would freeze at the first chunk; the block_stop
    # force-flush posts the FULL preamble instead of a mid-sentence fragment.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    chunks = [
        "I'll route this survey ",
        "through the research manager, ",
        "which will dispatch it to the various subagents.",
    ]
    full = "".join(chunks)
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": SID})]
    for chunk in chunks:
        lines.append(
            json.dumps(
                {
                    "type": "stream_event",
                    "session_id": SID,
                    "event": {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                }
            )
        )
    # Text block ends; no further deltas (a long tool call follows).
    lines.append(
        json.dumps(
            {
                "type": "stream_event",
                "session_id": SID,
                "event": {"type": "content_block_stop", "index": 1},
            }
        )
    )
    lines.append(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": SID,
                "result": full,
            }
        )
    )
    # Real throttled updater + an injected clock that keeps every delta inside the
    # 1s gate, so ONLY the force-flush on block_stop can post the full preamble.
    clock = {"t": 0.0}

    def _tick():
        t = clock["t"]
        clock["t"] += 0.1  # each delta < 1s after the first post -> throttled
        return t

    client = _FakeClient()
    updater = _appmod._make_stream_updater(client, "C1", "TS1", now=_tick)
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(lines),
    ):
        reply, _meta = claude_runner.run_claude(
            BRUNEL, PROMPT, SID, True, on_update=updater
        )
    assert reply == full
    texts = [u["text"] for u in client.updates]
    # The full preamble landed (the force-flush), not just the first chunk.
    assert texts[-1] == full
