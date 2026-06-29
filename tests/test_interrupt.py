"""Run interrupt (!stop): phrase matcher, registry, SIGINT settle."""

import json
from unittest import mock

from src.runners import claude_runner, codex_runner

from tests.helpers import (
    SID,
    PROMPT,
    THREAD_ID,
    BRUNEL,
    DIJKSTRA,
    _FakeSay,
    _HANDLE_AGENT,
    _fake_popen_factory,
)


# ---------------------------------------------------------------------------
# Run interrupt (the Slack Ctrl-C analog): the interrupt-phrase matcher + the
# in-memory registry (src/slack/interrupt.py), the Interrupt token's SIGINT
# signalling (src/runners/common.py), the !stop control-phrase dispatch, and the
# runner GRACEFUL SETTLE that turns a SIGINT-induced nonzero exit into a returned
# partial reply (so the session id is persisted and the thread stays resumable).
# ---------------------------------------------------------------------------


def test_is_interrupt_phrase():
    from src.slack import interrupt

    for t in [
        "!stop",
        "stop",
        "STOP",
        " Stop ",
        "ctrl-c",
        "^C",
        "/interrupt",
        "interrupt",
    ]:
        assert interrupt.is_interrupt_phrase(t), t
    for t in ["!model x", "please stop the loop in my code", "stopwatch", "", None]:
        assert not interrupt.is_interrupt_phrase(t), t


def test_interrupt_token_request_sets_flag_and_signals():
    import signal as _sig

    from src.runners import common

    class _Proc:
        def __init__(self, alive=True):
            self.alive = alive
            self.signals = []

        def poll(self):
            return None if self.alive else 0

        def send_signal(self, sig):
            self.signals.append(sig)

    tok = common.Interrupt()
    assert tok.requested is False
    live = _Proc(alive=True)
    tok.proc = live
    tok.request()
    assert tok.requested is True
    assert live.signals == [_sig.SIGINT]  # a running proc gets SIGINT (Ctrl-C)

    # An already-dead proc is never signalled (poll() is not None).
    tok2 = common.Interrupt()
    dead = _Proc(alive=False)
    tok2.proc = dead
    tok2.request()
    assert tok2.requested is True
    assert dead.signals == []


def test_interrupt_registry_register_request_unregister():
    from src.slack import interrupt

    # Nothing registered for this thread -> request reports no running run.
    assert interrupt.request("aristotle", "t-nope") is False

    tok = interrupt.register("aristotle", "t-1")
    assert interrupt.request("aristotle", "t-1") is True  # found + signalled
    assert tok.requested is True
    assert interrupt.request("aristotle", "t-2") is False  # other threads untouched

    interrupt.unregister("aristotle", "t-1", tok)
    assert interrupt.request("aristotle", "t-1") is False  # dropped

    # Stale unregister (a newer run replaced ours) must NOT drop the new token.
    tok_a = interrupt.register("aristotle", "t-3")
    tok_b = interrupt.register("aristotle", "t-3")  # replaces tok_a
    interrupt.unregister("aristotle", "t-3", tok_a)
    assert interrupt.request("aristotle", "t-3") is True
    assert tok_b.requested is True
    interrupt.unregister("aristotle", "t-3", tok_b)


def test_control_phrase_interrupt_dispatch():
    from src.slack import control, interrupt

    # No run registered -> handled (returns True, agent NOT run) with a notice.
    say = _FakeSay()
    assert control._handle_control_phrase(_HANDLE_AGENT, "!stop", "t-x", say) is True
    assert "othing is running" in say.posts[-1]["text"]

    # A registered run -> signalled, ack posted, handled. Bare "ctrl-c" works too.
    tok = interrupt.register(_HANDLE_AGENT["name"], "t-y")
    say2 = _FakeSay()
    assert control._handle_control_phrase(_HANDLE_AGENT, "ctrl-c", "t-y", say2) is True
    assert tok.requested is True
    interrupt.unregister(_HANDLE_AGENT["name"], "t-y", tok)


def test_run_claude_streaming_settles_gracefully_on_interrupt(monkeypatch):
    # A user interrupt (cancel.requested) turns the SIGINT-induced nonzero exit into
    # a graceful settle: the partial accumulated delta text is RETURNED, not raised,
    # so the caller-known session id is persisted and the thread stays resumable.
    # No `result` event arrives (the run was cut off mid-stream).
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    from src.runners import common

    delta = json.dumps(
        {
            "type": "stream_event",
            "session_id": SID,
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "half a thou"},
            },
        }
    )
    cancel = common.Interrupt()
    cancel.request()  # simulate the !stop having fired (proc None here -> no signal)
    with mock.patch(
        "src.runners.claude_runner.subprocess.Popen",
        side_effect=_fake_popen_factory([delta], returncode=1, stderr="killed"),
    ):
        reply, _meta = claude_runner.run_claude(
            BRUNEL, PROMPT, SID, True, cancel=cancel
        )
    assert reply == "half a thou"  # partial text returned, not a ClaudeRunError


def test_run_codex_streaming_settles_gracefully_on_interrupt(monkeypatch):
    # A user interrupt turns codex's SIGINT-induced nonzero exit into a graceful
    # settle that still salvages the thread_id from the partial stream (so the
    # thread resumes). The -o reply is empty (interrupted before it was written),
    # so the worker is what later marks the message interrupted.
    monkeypatch.setenv("STREAM_OUTPUT", "1")
    from src.runners import common

    lines = [json.dumps({"type": "thread.started", "thread_id": THREAD_ID})]
    cancel = common.Interrupt()
    cancel.request()
    with mock.patch(
        "src.runners.codex_runner.subprocess.Popen",
        side_effect=_fake_popen_factory(
            lines, returncode=2, stderr="killed", writes_to_o=""
        ),
    ):
        reply, sid, _meta = codex_runner.run_codex(
            DIJKSTRA, PROMPT, None, True, cancel=cancel
        )
    assert sid == THREAD_ID  # resumable: thread_id salvaged from the partial stream
    assert reply == ""  # no -o reply yet; the worker appends the interrupted mark
