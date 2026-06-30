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


def test_claude_answer_persists_session_before_subprocess_runs(tmp_path):
    # Fix 1: the freshly minted id is persisted (via on_session) the MOMENT it's
    # minted, BEFORE the subprocess runs, so a run killed mid-flight (peon
    # hard-killed by launchd) still leaves a resumable id. We assert the store has
    # the id even though the fake subprocess raises mid-run.
    store = str(tmp_path / "sessions.json")
    minted = []

    def _persist(sid):
        minted.append(sid)
        claude_runner.set_session("brunel", "T1", sid, path=store)

    def _boom(*a, **k):
        raise RuntimeError("peon was hard-killed mid-run")

    with mock.patch("src.runners.claude_runner.subprocess.run", side_effect=_boom):
        try:
            claude_runner.answer(BRUNEL, PROMPT, None, on_session=_persist)
            assert False, "expected the subprocess failure to propagate"
        except RuntimeError:
            pass

    # The run blew up, but the minted id is already stored -> the thread is resumable.
    assert minted, "on_session should have fired before the subprocess ran"
    assert claude_runner.get_session("brunel", "T1", path=store) == minted[0]


def test_claude_answer_clears_dead_session_and_retries_fresh(tmp_path):
    # Fix 2: a --resume against an id claude no longer has fails with "No
    # conversation found with session ID"; answer must clear the dead id and retry
    # ONCE as a fresh session that succeeds, so the thread is never wedged forever.
    store = str(tmp_path / "sessions.json")
    dead = "dead-session-id"
    claude_runner.set_session("brunel", "T1", dead, path=store)

    def _persist(sid):
        claude_runner.set_session("brunel", "T1", sid, path=store)

    good = json.dumps({"result": "recovered", "is_error": False, "subtype": "success"})
    fail = _fake_proc(1, "", f"No conversation found with session ID: {dead}")
    ok = _fake_proc(0, good)
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", side_effect=[fail, ok]
    ) as m:
        prior = claude_runner.get_session("brunel", "T1", path=store)
        reply, sid, _meta = claude_runner.answer(
            BRUNEL, PROMPT, prior, on_session=_persist
        )

    assert reply == "recovered"  # the fresh retry succeeded
    assert sid != dead  # a brand-new id was minted for the retry
    # The dead id was cleared and replaced by the fresh one in the store.
    assert claude_runner.get_session("brunel", "T1", path=store) == sid
    # First call RESUMED the dead id; the retry started a FRESH session.
    first_argv = m.call_args_list[0][0][0]
    second_argv = m.call_args_list[1][0][0]
    assert "--resume" in first_argv and dead in first_argv
    assert "--session-id" in second_argv and sid in second_argv


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
