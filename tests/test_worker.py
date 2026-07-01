"""Worker run: per-thread workdir injection + identity prepend."""

import os

from src.runners import claude_runner

from tests.helpers import (
    _FILE_AGENT,
    _appmod,
    _HANDLE_AGENT,
    _HAVE_APP,
    _FakeSay,
)


# ---------------------------------------------------------------------------
# Worker run: per-thread workdir injection + identity prepend.
# ---------------------------------------------------------------------------


def test_run_and_update_always_injects_workdir(monkeypatch, tmp_path):
    # The worker always injects the per-thread, created _workdir into overrides so
    # the run has a home dir and the outbound file-upload-back works.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))
    monkeypatch.setenv("STREAM_OUTPUT", "0")

    captured = {}

    class _Runner:
        @staticmethod
        def answer(
            agent,
            prompt,
            prior,
            overrides=None,
            on_update=None,
            cancel=None,
            on_session=None,
        ):
            captured["overrides"] = overrides
            return "ok", "sid-1", {}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)
    client = _Client()

    _appmod._run_and_update(client, "C1", "TS1", _FILE_AGENT, "hi", "T_full")
    over = captured["overrides"]
    wd = over.get("_workdir")
    assert wd and os.path.isdir(wd)
    assert wd == claude_runner.get_workdir("aristotle", "T_full")


def test_run_and_update_injects_identity_every_turn(monkeypatch, tmp_path):
    # The worker prepends the agent's display_name so "who are you" answers as
    # itself, not the first agent listed in the repo CLAUDE.md. It fires on EVERY
    # turn (not just new threads), so a thread created before the fix that already
    # learned the wrong identity is corrected on its next message.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("STREAM_OUTPUT", "0")

    captured = {}

    class _Runner:
        @staticmethod
        def answer(
            agent,
            prompt,
            prior,
            overrides=None,
            on_update=None,
            cancel=None,
            on_session=None,
        ):
            captured["prompt"] = prompt
            return "ok", "sid-1", {}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)
    client = _Client()

    # New thread: identity preamble carries the display_name and the user text.
    _appmod._run_and_update(client, "C1", "TS1", _FILE_AGENT, "who are you", "T_new")
    assert "Aristotle" in captured["prompt"]
    assert captured["prompt"].endswith("who are you")

    # Resumed thread (prior session stored): the preamble STILL fires, so a stale
    # thread is corrected rather than passed through verbatim.
    claude_runner.set_session("aristotle", "T_old", "sid-prev", path=sessions)
    _appmod._run_and_update(client, "C1", "TS1", _FILE_AGENT, "who are you", "T_old")
    assert "Aristotle" in captured["prompt"]
    assert captured["prompt"].endswith("who are you")


def test_run_and_update_marker_uploads_named_file_and_strips_marker(
    monkeypatch, tmp_path
):
    # A reply ENDING in a <<files: ...>> marker delivers ONLY the named file, and
    # the marker is stripped from the text posted back to Slack.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))
    monkeypatch.setenv("STREAM_OUTPUT", "0")

    # Pre-create the named file in the thread's workdir so the marker resolves it.
    workdir = claude_runner.get_workdir("aristotle", "T_files", create=True)
    with open(os.path.join(workdir, "plot.png"), "w", encoding="utf-8") as f:
        f.write("PNG")

    posted = {}
    uploads = []

    class _Runner:
        @staticmethod
        def answer(
            agent,
            prompt,
            prior,
            overrides=None,
            on_update=None,
            cancel=None,
            on_session=None,
        ):
            return "Here is your plot.\n<<files: plot.png>>", "sid-1", {}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            posted["text"] = text
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            uploads.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)
    client = _Client()

    _appmod._run_and_update(client, "C1", "TS1", _FILE_AGENT, "make a plot", "T_files")
    # Marker is gone from the shown reply; the prose remains.
    assert "<<files:" not in posted["text"]
    assert "Here is your plot." in posted["text"]
    # Exactly the named file was uploaded.
    assert len(uploads) == 1
    assert os.path.basename(uploads[0]["file"]) == "plot.png"


def test_run_and_update_errored_partial_keeps_streamed_text(monkeypatch, tmp_path):
    # Fix 3: a run that streamed PARTIAL text and then ERRORS finalizes the Slack
    # message as that partial reply + a resume note, NOT a frozen mid-sentence
    # fragment or a bare ":warning: hit an error" that throws the partial away.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))

    posted = {}

    class _Runner:
        @staticmethod
        def answer(
            agent,
            prompt,
            prior,
            overrides=None,
            on_update=None,
            cancel=None,
            on_session=None,
        ):
            # Stream a partial reply, then die (the mid-run kill / ClaudeRunError).
            on_update("Here is the first half of the surv")
            raise claude_runner.ClaudeRunError("claude exited with code 1: boom")

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            posted["text"] = text
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)
    client = _Client()

    _appmod._run_and_update(client, "C1", "TS1", _FILE_AGENT, "survey X", "T_err")

    # The streamed partial is kept and a resume note appended; no bare error text.
    assert "first half of the surv" in posted["text"]
    assert "send any message to continue" in posted["text"]
    assert "hit an error" not in posted["text"]


def test_run_and_update_no_marker_uploads_nothing(monkeypatch, tmp_path):
    # A plain reply (no marker) uploads nothing, even when the workdir has files.
    if not _HAVE_APP:
        return
    assert _appmod is not None
    sessions = str(tmp_path / "sessions.json")
    overrides = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_sessions_path", lambda: sessions)
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: overrides)
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd"))
    monkeypatch.setenv("STREAM_OUTPUT", "0")

    workdir = claude_runner.get_workdir("aristotle", "T_nofiles", create=True)
    with open(os.path.join(workdir, "scratch.txt"), "w", encoding="utf-8") as f:
        f.write("noise")

    uploads = []

    class _Runner:
        @staticmethod
        def answer(
            agent,
            prompt,
            prior,
            overrides=None,
            on_update=None,
            cancel=None,
            on_session=None,
        ):
            return "just a normal answer", "sid-1", {}

    class _Client:
        def chat_update(self, channel=None, ts=None, text=None):
            return {"ok": True}

        def files_upload_v2(self, **kwargs):
            uploads.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(_appmod.runners, "get_runner", lambda backend: _Runner)
    client = _Client()

    _appmod._run_and_update(client, "C1", "TS1", _FILE_AGENT, "hi", "T_nofiles")
    assert uploads == []


def test_handle_declines_when_thread_busy(monkeypatch):
    """A second message to a thread with a run in flight is refused, not run
    (a second run would --resume the same session id concurrently: a race)."""
    if not _HAVE_APP:
        return
    from src.slack import handlers, interrupt

    monkeypatch.setattr(handlers.claude_runner, "seen_before", lambda _id: False)

    def _no_thread(*a, **k):
        raise AssertionError("must not spawn a worker while the thread is busy")

    monkeypatch.setattr(handlers.threading, "Thread", _no_thread)

    held = interrupt.try_register(_HANDLE_AGENT["name"], "T_busy")
    assert held is not None
    try:
        say = _FakeSay()
        event = {"channel": "C1", "ts": "T_busy", "text": "hello"}
        handlers._handle(_HANDLE_AGENT, event, object(), say)
        assert "still working" in say.posts[-1]["text"]
        # The in-flight run's slot is untouched by the declined message.
        assert interrupt.try_register(_HANDLE_AGENT["name"], "T_busy") is None
    finally:
        interrupt.unregister(_HANDLE_AGENT["name"], "T_busy", held)


def test_handle_claims_slot_and_threads_token(monkeypatch):
    """A free thread: _handle claims the busy slot and threads THAT SAME token
    into the worker, so !stop and the finally-release act on the live run."""
    if not _HAVE_APP:
        return
    from src.slack import handlers, interrupt

    monkeypatch.setattr(handlers.claude_runner, "seen_before", lambda _id: False)

    class _Client:
        def conversations_replies(self, **kwargs):
            return {"messages": []}

    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            captured["args"] = args
            captured["kwargs"] = kwargs or {}
            captured["daemon"] = daemon

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(handlers.threading, "Thread", _FakeThread)

    say = _FakeSay()
    event = {"channel": "C1", "ts": "T_free", "text": "hello"}
    handlers._handle(_HANDLE_AGENT, event, _Client(), say)

    # Worker spawned as a daemon with a token threaded in.
    assert captured.get("started") is True
    assert captured["daemon"] is True
    token = captured["kwargs"]["token"]
    assert token is not None

    # The threaded token IS the one holding the slot: the thread reads busy, and
    # signaling the run finds and trips exactly this token.
    assert interrupt.try_register(_HANDLE_AGENT["name"], "T_free") is None
    assert interrupt.request(_HANDLE_AGENT["name"], "T_free") is True
    assert token.requested is True
    interrupt.unregister(_HANDLE_AGENT["name"], "T_free", token)
