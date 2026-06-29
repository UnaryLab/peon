"""Unified runner.answer() seam for both backends."""

import json
from unittest import mock

from src.runners import claude_runner, codex_runner

from tests.helpers import (
    PROMPT,
    THREAD_ID,
    BRUNEL,
    DIJKSTRA,
    _fake_proc,
    _codex_proc_writing,
)


# ---------------------------------------------------------------------------
# Unified answer() seam: both backends return (reply, session_id_to_store)
# ---------------------------------------------------------------------------


def test_codex_answer_fresh_then_resume():
    stdout = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    with mock.patch(
        "src.runners.codex_runner.subprocess.run",
        side_effect=_codex_proc_writing("fresh", stdout=stdout),
    ):
        reply, sid, _meta = codex_runner.answer(DIJKSTRA, PROMPT, None)
    assert reply == "fresh"
    assert sid == THREAD_ID  # minted id surfaced for the caller to persist

    with mock.patch(
        "src.runners.codex_runner.subprocess.run",
        side_effect=_codex_proc_writing("again", stdout=""),
    ):
        reply2, sid2, _meta2 = codex_runner.answer(DIJKSTRA, PROMPT, THREAD_ID)
    assert reply2 == "again"
    assert sid2 == THREAD_ID  # resume returns the prior id unchanged


def test_claude_answer_mints_uuid_when_no_prior():
    good = json.dumps({"result": "4", "is_error": False, "subtype": "success"})
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, good)
    ) as m:
        reply, sid, _meta = claude_runner.answer(BRUNEL, PROMPT, None)
    assert reply == "4"
    # a fresh uuid was minted and used as the NEW session id
    argv = m.call_args[0][0]
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == sid

    # with a prior id, claude resumes it and returns it unchanged
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, good)
    ) as m2:
        reply2, sid2, _meta2 = claude_runner.answer(BRUNEL, PROMPT, sid)
    assert sid2 == sid
    argv2 = m2.call_args[0][0]
    assert "--resume" in argv2
    assert argv2[argv2.index("--resume") + 1] == sid


def _seam_store(tmp_path):
    """Drive the app.py session seam (get_session -> runner.answer -> set_session)
    against a temp store, returning the stored id for assertions. Backend-agnostic.
    """
    return str(tmp_path / "sessions.json")


def test_unified_seam_codex_stores_captured_thread_id(tmp_path):
    store = _seam_store(tmp_path)
    # First message: no prior id -> fresh codex run mints THREAD_ID, app persists it.
    prior = claude_runner.get_session("dijkstra", "T1", path=store)
    assert prior is None
    stdout = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    with mock.patch(
        "src.runners.codex_runner.subprocess.run",
        side_effect=_codex_proc_writing("hi", stdout=stdout),
    ):
        _reply, sid, _meta = codex_runner.answer(DIJKSTRA, PROMPT, prior)
    claude_runner.set_session("dijkstra", "T1", sid, path=store)

    # Later message in the same thread returns the captured thread_id.
    assert claude_runner.get_session("dijkstra", "T1", path=store) == THREAD_ID


def test_unified_seam_independent_across_agent_and_thread_both_backends(tmp_path):
    # Independence holds for the SAME key scheme across both backends: a claude
    # agent and a codex agent in the same thread, and one agent across threads,
    # never collide. Keys are (agent_name, thread_ts) for every backend.
    store = _seam_store(tmp_path)
    # claude Brunel in T1 (mint + store a uuid via the seam)
    brunel_prior = claude_runner.get_session("brunel", "T1", path=store)
    good = json.dumps({"result": "ok", "is_error": False, "subtype": "success"})
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, good)
    ):
        _r, brunel_sid, _m = claude_runner.answer(BRUNEL, PROMPT, brunel_prior)
    claude_runner.set_session("brunel", "T1", brunel_sid, path=store)

    # codex Dijkstra in the SAME thread T1 (mints its own thread_id, independent key)
    dijkstra_prior = claude_runner.get_session("dijkstra", "T1", path=store)
    stdout = json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    with mock.patch(
        "src.runners.codex_runner.subprocess.run",
        side_effect=_codex_proc_writing("hi", stdout=stdout),
    ):
        _r2, dijkstra_sid, _m2 = codex_runner.answer(DIJKSTRA, PROMPT, dijkstra_prior)
    claude_runner.set_session("dijkstra", "T1", dijkstra_sid, path=store)

    # codex Dijkstra in a DIFFERENT thread T2 starts fresh (no prior id)
    assert claude_runner.get_session("dijkstra", "T2", path=store) is None

    # Stored ids are distinct: brunel:T1 (uuid) != dijkstra:T1 (thread_id), and dijkstra:T2
    # has no entry yet, so the three contexts are independent.
    with open(store, encoding="utf-8") as f:
        data = json.load(f)
    assert data["brunel:T1"] == brunel_sid
    assert data["dijkstra:T1"] == THREAD_ID
    assert data["brunel:T1"] != data["dijkstra:T1"]
    assert "dijkstra:T2" not in data
