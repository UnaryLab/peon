"""SIGHUP hot-reload reconcile + !model/!effort/!reset control phrases."""

import json
from unittest import mock

from src import agents
from src.runners import claude_runner

from tests.helpers import (
    _FakeSay,
    _CONTROL_AGENT,
    _HAVE_APP,
)


# ---------------------------------------------------------------------------
# SIGHUP hot-reload: reconcile, delta semantics, crash-safety, event/loop wiring.
#
# These import src.app LAZILY (inside each test) so the rest of the suite keeps
# its "no slack_bolt needed" property, and MOCK SocketModeHandler/build_app_for so
# no real Slack connection is ever made. Each test that mutates agents.REGISTRY or
# agents._AGENTS_JSON_PATH restores REGISTRY's contents in a finally block so the
# other tests (which assert the 4 real agents) stay green regardless of order.
# ---------------------------------------------------------------------------


class _FakeHandler:
    """Stand-in for SocketModeHandler: connect()/close() just record, no network."""

    def __init__(self, app, app_token):
        self.app = app
        self.app_token = app_token
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True


def _arm_fake_slack(monkeypatch, appmod):
    """Swap SocketModeHandler -> _FakeHandler and build_app_for -> a sentinel."""
    monkeypatch.setattr(appmod, "SocketModeHandler", _FakeHandler)
    monkeypatch.setattr(appmod, "build_app_for", lambda agent, bot_token: object())


def _set_tokens(monkeypatch, name):
    """Make `name` startable by setting its bot + app token env vars."""
    monkeypatch.setenv(f"SLACK_BOT_TOKEN_{name.upper()}", f"xoxb-{name}")
    monkeypatch.setenv(f"SLACK_APP_TOKEN_{name.upper()}", f"xapp-{name}")


def _write_registry(path, entries):
    """Write a tiny agents.json with the given entries at `path`."""
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_reload_adds_new_startable_agent_and_leaves_others_untouched(
    monkeypatch, tmp_path
):
    from src import app as appmod

    saved = list(agents.REGISTRY)
    try:
        _arm_fake_slack(monkeypatch, appmod)
        a1 = {
            "name": "alpha",
            "display_name": "Alpha",
            "backend": "claude",
            "claude_agent": None,
        }
        a2 = {
            "name": "beta",
            "display_name": "Beta",
            "backend": "claude",
            "claude_agent": None,
        }
        # Reloaded registry contains BOTH; tokens for both present -> beta becomes startable.
        reg = tmp_path / "agents.json"
        _write_registry(reg, [a1, a2])
        monkeypatch.setattr(agents, "_AGENTS_JSON_PATH", str(reg))
        _set_tokens(monkeypatch, "alpha")
        _set_tokens(monkeypatch, "beta")

        # Live set starts with only alpha (its snapshot must match what reconcile computes).
        h1, snap1 = appmod._start_handler(a1)
        assert isinstance(h1, _FakeHandler)
        live = {"alpha": {"handler": h1, "snapshot": snap1}}

        assert appmod.reconcile(live) is True
        assert set(live) == {"alpha", "beta"}
        # New agent connected.
        assert live["beta"]["handler"].connected is True
        # First agent's handler is the SAME instance, untouched (not closed).
        assert live["alpha"]["handler"] is h1
        assert h1.closed is False
    finally:
        agents.REGISTRY[:] = saved


def test_reload_removes_now_unstartable_agent(monkeypatch, tmp_path):
    from src import app as appmod

    saved = list(agents.REGISTRY)
    try:
        _arm_fake_slack(monkeypatch, appmod)
        a1 = {
            "name": "alpha",
            "display_name": "Alpha",
            "backend": "claude",
            "claude_agent": None,
        }
        a2 = {
            "name": "beta",
            "display_name": "Beta",
            "backend": "claude",
            "claude_agent": None,
        }
        reg = tmp_path / "agents.json"
        _write_registry(reg, [a1, a2])
        monkeypatch.setattr(agents, "_AGENTS_JSON_PATH", str(reg))
        _set_tokens(monkeypatch, "alpha")
        _set_tokens(monkeypatch, "beta")

        h1, snap1 = appmod._start_handler(a1)
        h2, snap2 = appmod._start_handler(a2)
        assert isinstance(h1, _FakeHandler)
        assert isinstance(h2, _FakeHandler)
        live = {
            "alpha": {"handler": h1, "snapshot": snap1},
            "beta": {"handler": h2, "snapshot": snap2},
        }

        # Pull beta's tokens so it is no longer startable.
        monkeypatch.delenv("SLACK_BOT_TOKEN_BETA", raising=False)
        monkeypatch.delenv("SLACK_APP_TOKEN_BETA", raising=False)

        assert appmod.reconcile(live) is True
        assert set(live) == {"alpha"}
        assert h2.closed is True  # removed handler was closed
        assert live["alpha"]["handler"] is h1  # other untouched
        assert h1.closed is False
    finally:
        agents.REGISTRY[:] = saved


def test_reload_restarts_changed_agent_only(monkeypatch, tmp_path):
    from src import app as appmod

    saved = list(agents.REGISTRY)
    try:
        _arm_fake_slack(monkeypatch, appmod)
        a1 = {
            "name": "alpha",
            "display_name": "Alpha",
            "backend": "claude",
            "claude_agent": None,
        }
        a2_old = {
            "name": "beta",
            "display_name": "Beta",
            "backend": "claude",
            "claude_agent": None,
        }
        reg = tmp_path / "agents.json"
        monkeypatch.setattr(agents, "_AGENTS_JSON_PATH", str(reg))
        _set_tokens(monkeypatch, "alpha")
        _set_tokens(monkeypatch, "beta")

        h1, snap1 = appmod._start_handler(a1)
        h2, snap2 = appmod._start_handler(a2_old)
        assert isinstance(h1, _FakeHandler)
        assert isinstance(h2, _FakeHandler)
        live = {
            "alpha": {"handler": h1, "snapshot": snap1},
            "beta": {"handler": h2, "snapshot": snap2},
        }

        # Reloaded registry: beta's definition CHANGED (model added); alpha identical.
        a2_new = dict(a2_old, model="claude-opus-4-8")
        _write_registry(reg, [a1, a2_new])

        assert appmod.reconcile(live) is True
        # beta restarted: old handler closed, a NEW handler instance connected.
        assert h2.closed is True
        assert live["beta"]["handler"] is not h2
        assert live["beta"]["handler"].connected is True
        # alpha unchanged: same instance, not closed.
        assert live["alpha"]["handler"] is h1
        assert h1.closed is False
    finally:
        agents.REGISTRY[:] = saved


def test_reload_invalid_json_keeps_live_set(monkeypatch, tmp_path):
    from src import app as appmod

    saved = list(agents.REGISTRY)
    try:
        _arm_fake_slack(monkeypatch, appmod)
        _set_tokens(monkeypatch, "alpha")
        a1 = {
            "name": "alpha",
            "display_name": "Alpha",
            "backend": "claude",
            "claude_agent": None,
        }
        h1, snap1 = appmod._start_handler(a1)
        assert isinstance(h1, _FakeHandler)
        live = {"alpha": {"handler": h1, "snapshot": snap1}}

        # Point the reload at a malformed agents.json.
        reg = tmp_path / "agents.json"
        reg.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(agents, "_AGENTS_JSON_PATH", str(reg))

        registry_before = list(agents.REGISTRY)
        # Must not raise; must report it skipped.
        assert appmod.reconcile(live) is False
        # Live set byte-identical: same dict contents, same instance, not closed.
        assert set(live) == {"alpha"}
        assert live["alpha"]["handler"] is h1
        assert h1.closed is False
        # REGISTRY was NOT mutated by the failed reload.
        assert agents.REGISTRY == registry_before
    finally:
        agents.REGISTRY[:] = saved


def test_agents_reload_mutates_registry_in_place(monkeypatch, tmp_path):

    saved = list(agents.REGISTRY)
    try:
        obj = agents.REGISTRY  # capture the list object identity
        new_agent = {
            "name": "gamma",
            "display_name": "Gamma",
            "backend": "claude",
            "claude_agent": None,
        }
        reg = tmp_path / "agents.json"
        _write_registry(reg, [new_agent])

        returned = agents.reload(path=str(reg))
        assert agents.REGISTRY is obj  # SAME list object (mutated in place)
        assert returned is obj
        assert [a["name"] for a in agents.REGISTRY] == ["gamma"]
    finally:
        agents.REGISTRY[:] = saved


def test_sighup_event_triggers_reconcile_loop_mechanism(monkeypatch):
    import signal as _signal

    from src import app as appmod

    # The signal handler does the minimum: it just sets the module-level event.
    appmod._reload_requested.clear()
    appmod._request_reload(_signal.SIGHUP, None)
    assert appmod._reload_requested.is_set()

    # Prove the loop body consumes the event and calls reconcile exactly once.
    calls = []
    monkeypatch.setattr(appmod, "reconcile", lambda live: calls.append(live))
    sentinel = {"sentinel": True}
    appmod._reload_loop(sentinel, _once=True)
    assert calls == [sentinel]  # reconcile called once, with the live dict
    assert appmod._reload_requested.is_set() is False  # event was cleared


def test_unmentioned_thread_reply_requires_this_agents_session(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    from src import store
    from src.slack import app as slack_app

    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "sessions.json"))
    agent = {
        "name": "brunel",
        "display_name": "Brunel",
        "backend": "claude",
        "claude_agent": None,
    }
    calls = []

    class _FakeBoltApp:
        def __init__(self, token):
            self.client = mock.Mock()
            self.client.auth_test.return_value = {"user_id": "UBRUNEL"}
            self.events = {}
            self.actions = {}

        def event(self, name):
            def _decorator(fn):
                self.events[name] = fn
                return fn

            return _decorator

        def action(self, name):
            def _decorator(fn):
                self.actions[name] = fn
                return fn

            return _decorator

    monkeypatch.setattr(slack_app, "App", _FakeBoltApp)
    monkeypatch.setattr(
        slack_app.handlers,
        "_handle",
        lambda agent, event, client, say: calls.append((agent["name"], event)),
    )

    bolt_app = slack_app.build_app_for(agent, "xoxb-brunel")
    event = {
        "channel": "C1",
        "thread_ts": "T1",
        "ts": "T2",
        "text": "who are you?",
    }

    # Another agent has joined this thread, but Brunel has not. The unmentioned
    # message must not wake Brunel.
    store.set_session("aristotle", "T1", "sid-aristotle")
    bolt_app.events["message"](event, mock.Mock(), mock.Mock())
    assert calls == []

    # Once Brunel has a session in the thread, unmentioned replies continue it.
    store.set_session("brunel", "T1", "sid-brunel")
    bolt_app.events["message"](event, mock.Mock(), mock.Mock())
    assert [name for name, _event in calls] == ["brunel"]


# ---------------------------------------------------------------------------
# app.py control phrases: !model / !effort / !reset. These mutate the per-thread
# override store and ack into the thread WITHOUT running the agent. Tested via
# _handle_control_phrase directly (a fake `say`, the override store redirected to
# a tmp file) so no real Slack client is needed. src.app is imported LAZILY.
# ---------------------------------------------------------------------------


def test_control_phrase_set_model(monkeypatch, tmp_path):
    from src import app as appmod

    store = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: store)
    say = _FakeSay()

    handled = appmod._handle_control_phrase(
        _CONTROL_AGENT, "!model claude-sonnet-4-6", "T1", say
    )
    assert handled is True  # it WAS a control phrase, agent must not run
    assert claude_runner.get_override("aristotle", "T1", path=store) == {
        "model": "claude-sonnet-4-6"
    }
    assert len(say.posts) == 1
    assert say.posts[0]["thread_ts"] == "T1"
    # The ack shows the EFFECTIVE config (override model, agents.json effort).
    assert "claude-sonnet-4-6" in say.posts[0]["text"]
    assert "xhigh" in say.posts[0]["text"]


def test_control_phrase_set_effort_valid(monkeypatch, tmp_path):
    from src import app as appmod

    store = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: store)
    say = _FakeSay()

    handled = appmod._handle_control_phrase(_CONTROL_AGENT, "!effort high", "T1", say)
    assert handled is True
    assert claude_runner.get_override("aristotle", "T1", path=store) == {
        "effort": "high"
    }
    assert len(say.posts) == 1
    assert "high" in say.posts[0]["text"]


def test_control_phrase_set_effort_invalid_rejected(monkeypatch, tmp_path):
    from src import app as appmod

    store = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: store)
    say = _FakeSay()

    handled = appmod._handle_control_phrase(_CONTROL_AGENT, "!effort turbo", "T1", say)
    assert handled is True  # still a control phrase (handled), just rejected
    # Store UNCHANGED (the invalid value was not written).
    assert claude_runner.get_override("aristotle", "T1", path=store) is None
    assert len(say.posts) == 1
    # The ack lists the valid values.
    for level in ("low", "medium", "high", "xhigh", "max"):
        assert level in say.posts[0]["text"]


def test_control_phrase_reset_clears_override(monkeypatch, tmp_path):
    from src import app as appmod

    store = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: store)
    claude_runner.set_override("aristotle", "T1", "effort", "high", path=store)
    say = _FakeSay()

    handled = appmod._handle_control_phrase(_CONTROL_AGENT, "!reset", "T1", say)
    assert handled is True
    assert claude_runner.get_override("aristotle", "T1", path=store) is None
    assert len(say.posts) == 1


def test_control_phrase_unknown_help(monkeypatch, tmp_path):
    from src import app as appmod

    store = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: store)
    say = _FakeSay()

    handled = appmod._handle_control_phrase(_CONTROL_AGENT, "!foo bar", "T1", say)
    assert handled is True  # matched `!`, handled as help (agent must not run)
    assert claude_runner.get_override("aristotle", "T1", path=store) is None
    assert len(say.posts) == 1
    # The help line lists the three commands.
    text = say.posts[0]["text"]
    assert "!model" in text and "!effort" in text and "!reset" in text


def test_control_phrase_non_command_returns_false(monkeypatch, tmp_path):
    from src import app as appmod

    store = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: store)
    say = _FakeSay()

    # A normal prompt is NOT a control phrase: nothing posted, agent should run.
    handled = appmod._handle_control_phrase(_CONTROL_AGENT, "what is 2+2?", "T1", say)
    assert handled is False
    assert say.posts == []
