"""Override/workdir stores, seen_before, run_claude/run_codex parsing, codex build_command argv, codex_profile, build_manifest."""

import json
import os
import sys
from unittest import mock

from src import agents
from src.manifest import build_manifest
from src.runners import claude_runner, codex_runner

from tests.helpers import (
    _PROJECT_ROOT,
    SID,
    PROMPT,
    THREAD_ID,
    BRUNEL,
    CICERO,
    DIJKSTRA,
    _clear_model_effort_env,
    _fake_proc,
    _codex_proc_writing,
)


# ---------------------------------------------------------------------------
# Override store: per-thread, per-agent model/effort override (JSON file,
# sibling of sessions.json). Mirrors the session-store pattern.
# ---------------------------------------------------------------------------


def test_override_set_model_then_read_back(tmp_path):
    store = str(tmp_path / "overrides.json")
    assert claude_runner.get_override("brunel", "T1", path=store) is None
    claude_runner.set_override("brunel", "T1", "model", "claude-sonnet-4-6", path=store)
    assert claude_runner.get_override("brunel", "T1", path=store) == {
        "model": "claude-sonnet-4-6"
    }


def test_override_set_effort_merges_preserving_model(tmp_path):
    store = str(tmp_path / "overrides.json")
    claude_runner.set_override("brunel", "T1", "model", "claude-sonnet-4-6", path=store)
    claude_runner.set_override("brunel", "T1", "effort", "high", path=store)
    # The second set MERGES, leaving the model in place.
    assert claude_runner.get_override("brunel", "T1", path=store) == {
        "model": "claude-sonnet-4-6",
        "effort": "high",
    }


def test_override_clear_removes_entry(tmp_path):
    store = str(tmp_path / "overrides.json")
    claude_runner.set_override("brunel", "T1", "effort", "high", path=store)
    claude_runner.clear_override("brunel", "T1", path=store)
    assert claude_runner.get_override("brunel", "T1", path=store) is None
    # Clearing an absent key is a no-op (must not raise).
    claude_runner.clear_override("brunel", "T1", path=store)


def test_overrides_independent_across_agent_and_thread(tmp_path):
    store = str(tmp_path / "overrides.json")
    claude_runner.set_override("brunel", "T1", "effort", "high", path=store)
    # Same thread, different agent => independent.
    assert claude_runner.get_override("aristotle", "T1", path=store) is None
    # Same agent, different thread => independent.
    assert claude_runner.get_override("brunel", "T2", path=store) is None


def test_overrides_path_from_env_redirects_store(monkeypatch, tmp_path):
    # SESSIONS_PATH redirects the SESSION store; the override store lives as a
    # SIBLING (overrides.json) in the same directory, so it follows along.
    custom_sessions = str(tmp_path / "custom-sessions.json")
    monkeypatch.setenv("SESSIONS_PATH", custom_sessions)
    claude_runner.set_override("aristotle", "1.23", "model", "x-model")
    expected = str(tmp_path / "overrides.json")
    assert os.path.exists(expected)
    with open(expected, encoding="utf-8") as f:
        data = json.load(f)
    assert data["aristotle:1.23"] == {"model": "x-model"}


# ---------------------------------------------------------------------------
# get_workdir: per-(agent, thread) directory under WORKDIR_BASE (env, default
# ~/Projects/.peon-workdirs, absolute and OUTSIDE the repo). Namespaced by agent +
# thread, created on demand, always ABSOLUTE.
# ---------------------------------------------------------------------------


def test_get_workdir_under_base_namespaced_and_created(monkeypatch, tmp_path):
    base = str(tmp_path / "wd-base")
    monkeypatch.setenv("WORKDIR_BASE", base)
    # create=False: pure path, no directory made.
    path = claude_runner.get_workdir("aristotle", "T1")
    assert path.startswith(base + os.sep)
    assert "aristotle" in path
    assert not os.path.exists(path)  # not created when create defaults False
    # create=True: the directory is made on demand.
    created = claude_runner.get_workdir("aristotle", "T1", create=True)
    assert created == path
    assert os.path.isdir(created)


def test_get_workdir_independent_across_agent_and_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKDIR_BASE", str(tmp_path / "wd-base"))
    a_t1 = claude_runner.get_workdir("aristotle", "T1")
    a_t2 = claude_runner.get_workdir("aristotle", "T2")
    b_t1 = claude_runner.get_workdir("brunel", "T1")
    assert a_t1 != a_t2  # same agent, different thread => different dir
    assert a_t1 != b_t1  # same thread, different agent => different dir


def test_get_workdir_returns_absolute_path(monkeypatch, tmp_path):
    # get_workdir must ALWAYS return an absolute path even when WORKDIR_BASE is set
    # to a relative override: the subprocess cwd and claude --add-dir cannot take a
    # relative/ambiguous path. Use a deliberately RELATIVE WORKDIR_BASE and confirm
    # the result is absolute and resolves against cwd.
    monkeypatch.setenv("WORKDIR_BASE", "rel-base")
    path = claude_runner.get_workdir("aristotle", "T1")
    assert os.path.isabs(path)
    assert path == os.path.join(os.getcwd(), "rel-base", "aristotle", "T1")


def test_get_workdir_default_base_is_under_home_projects(monkeypatch, tmp_path):
    # With WORKDIR_BASE unset the default base is ~/Projects/.peon-workdirs, an
    # ABSOLUTE path OUTSIDE the project repo (so a run's default cwd is never the
    # framework source). No chdir needed since the default is absolute; create
    # defaults False so nothing is written regardless.
    monkeypatch.delenv("WORKDIR_BASE", raising=False)
    path = claude_runner.get_workdir("aristotle", "T1")
    # (a) absolute.
    assert os.path.isabs(path)
    # (b) under the expanded ~/Projects/.peon-workdirs base.
    home_base = os.path.expanduser("~/Projects/.peon-workdirs")
    assert os.path.commonpath([path, home_base]) == home_base
    # (c) OUTSIDE the project repo root.
    assert os.path.commonpath([path, _PROJECT_ROOT]) != _PROJECT_ROOT


def test_safe_token_collapses_dot_tokens_and_keeps_normal(monkeypatch, tmp_path):
    # Defense-in-depth: a pure-dots/empty token would let get_workdir escape
    # WORKDIR_BASE (e.g. '..' -> a parent dir). _safe_token must collapse those to
    # a safe placeholder, while leaving a real agent/thread token byte-identical.
    for bad in ("..", ".", ""):
        out = claude_runner._safe_token(bad)
        assert out == "_"
        assert set(out) > {"."} or out == "_"  # never pure-dots/empty
    # Normal tokens are unchanged vs the raw sanitization (no over-collapsing).
    assert claude_runner._safe_token("aristotle") == "aristotle"
    assert claude_runner._safe_token("1700000000.000100") == "1700000000.000100"
    # get_workdir('..') stays under WORKDIR_BASE (no parent escape).
    base = str(tmp_path / "wd-base")
    monkeypatch.setenv("WORKDIR_BASE", base)
    base_resolved = os.path.realpath(base)
    escaped = os.path.realpath(claude_runner.get_workdir("aristotle", ".."))
    assert os.path.commonpath([escaped, base_resolved]) == base_resolved
    assert ".." not in escaped.split(os.sep)


# ---------------------------------------------------------------------------
# seen_before: idempotency dedup helper (Slack-agnostic, opaque string ids)
# ---------------------------------------------------------------------------


def test_seen_before_first_then_repeat():
    # First time a given id is presented => not seen before (False).
    # Second time the SAME id is presented => already seen (True).
    mid = "client-msg-aaaa"
    assert claude_runner.seen_before(mid) is False
    assert claude_runner.seen_before(mid) is True


def test_seen_before_distinct_ids_both_first_seen():
    # Two DIFFERENT ids are each first-seen the first time (both False).
    assert claude_runner.seen_before("client-msg-bbbb") is False
    assert claude_runner.seen_before("client-msg-cccc") is False


# ---------------------------------------------------------------------------
# run_claude: parse the result field; surface errors gracefully
# ---------------------------------------------------------------------------


def test_run_claude_parses_result():
    good = json.dumps(
        {
            "result": "4",
            "session_id": SID,
            "is_error": False,
            "subtype": "success",
        }
    )
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, good)
    ) as m:
        text, meta = claude_runner.run_claude(BRUNEL, PROMPT, SID, True)
    assert text == "4"
    # meta is the four-key dict even when the blob carries no usage/cost fields.
    assert set(meta) == {"context_pct", "tokens", "cost_usd", "duration_s"}
    assert all(meta[k] is None for k in meta)
    # confirm we actually invoked the built command
    called_argv = m.call_args[0][0]
    assert called_argv[0] == "claude"
    assert "--agent" in called_argv


def test_run_claude_allows_empty_string_result():
    # A present-but-empty result ("") is a legitimate reply, not an error.
    payload = json.dumps({"result": "", "is_error": False, "subtype": "success"})
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, payload)
    ):
        text, _meta = claude_runner.run_claude(BRUNEL, PROMPT, SID, True)
    assert text == ""


def test_run_claude_raises_on_missing_result():
    # No result key at all (None) is the error case.
    payload = json.dumps({"is_error": False, "subtype": "success"})
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, payload)
    ):
        try:
            claude_runner.run_claude(BRUNEL, PROMPT, SID, True)
            assert False, "expected ClaudeRunError"
        except claude_runner.ClaudeRunError as exc:
            assert "result" in str(exc).lower()


def test_run_claude_raises_on_is_error():
    payload = json.dumps({"result": "boom", "is_error": True})
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, payload)
    ):
        try:
            claude_runner.run_claude(CICERO, PROMPT, SID, True)
            assert False, "expected ClaudeRunError"
        except claude_runner.ClaudeRunError as exc:
            assert "error" in str(exc).lower()


def test_run_claude_raises_on_nonzero_exit():
    with mock.patch(
        "src.runners.claude_runner.subprocess.run",
        return_value=_fake_proc(1, "", "kaboom"),
    ):
        try:
            claude_runner.run_claude(CICERO, PROMPT, SID, True)
            assert False, "expected ClaudeRunError"
        except claude_runner.ClaudeRunError as exc:
            assert "code 1" in str(exc)


def test_run_claude_raises_on_malformed_json():
    with mock.patch(
        "src.runners.claude_runner.subprocess.run",
        return_value=_fake_proc(0, "not json"),
    ):
        try:
            claude_runner.run_claude(CICERO, PROMPT, SID, True)
            assert False, "expected ClaudeRunError"
        except claude_runner.ClaudeRunError as exc:
            assert "json" in str(exc).lower()


def test_run_claude_raises_on_timeout():
    import subprocess as sp

    def _raise(*a, **k):
        raise sp.TimeoutExpired(cmd="claude", timeout=1)

    with mock.patch("src.runners.claude_runner.subprocess.run", side_effect=_raise):
        try:
            claude_runner.run_claude(CICERO, PROMPT, SID, True, timeout=1)
            assert False, "expected ClaudeRunError"
        except claude_runner.ClaudeRunError as exc:
            assert "timed out" in str(exc).lower()


# ---------------------------------------------------------------------------
# codex build_command: exact argv, fresh + resume, model gating
# ---------------------------------------------------------------------------

LASTMSG = "/tmp/codex-last-xyz.txt"


def test_codex_build_command_fresh(monkeypatch):
    # Clear all model/effort env so the default argv (no -m, no effort) is asserted
    # regardless of any CODEX_* set in the developer's shell.
    _clear_model_effort_env(monkeypatch)
    argv = codex_runner.build_command(DIJKSTRA, PROMPT, None, True, LASTMSG)
    assert argv == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-s",
        "danger-full-access",
        "-o",
        LASTMSG,
        PROMPT,
    ]


def test_codex_build_command_resume(monkeypatch):
    _clear_model_effort_env(monkeypatch)
    argv = codex_runner.build_command(DIJKSTRA, PROMPT, THREAD_ID, False, LASTMSG)
    assert argv == [
        "codex",
        "exec",
        "resume",
        THREAD_ID,
        "--json",
        "--skip-git-repo-check",
        "-c",
        "sandbox_mode=danger-full-access",
        "-o",
        LASTMSG,
        PROMPT,
    ]
    # resume must NOT carry -s/--sandbox (the resume subcommand rejects them).
    assert "-s" not in argv
    assert "--sandbox" not in argv


def test_codex_build_command_model_gating(monkeypatch):
    # No agents.json "model" field => no -m; with the field => -m <model> just
    # before the prompt, on both fresh and resume.
    _clear_model_effort_env(monkeypatch)
    argv = codex_runner.build_command(DIJKSTRA, PROMPT, None, True, LASTMSG)
    assert "-m" not in argv
    dijkstra_with_model = {**DIJKSTRA, "model": "gpt-5.4"}
    fresh = codex_runner.build_command(dijkstra_with_model, PROMPT, None, True, LASTMSG)
    assert fresh[-3:] == ["-m", "gpt-5.4", PROMPT]
    resume = codex_runner.build_command(
        dijkstra_with_model, PROMPT, THREAD_ID, False, LASTMSG
    )
    assert resume[-3:] == ["-m", "gpt-5.4", PROMPT]


def test_codex_build_command_model_ignores_legacy_env_var(monkeypatch):
    # The legacy CODEX_MODEL env var is no longer a source: with it set but no
    # agents.json "model" field, build_command emits NO -m (Codex uses its own
    # default). The agent's field is the only thing that adds -m.
    _clear_model_effort_env(monkeypatch)
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.4")  # must be ignored now
    argv = codex_runner.build_command(DIJKSTRA, PROMPT, None, True, LASTMSG)
    assert "-m" not in argv
    dijkstra_with_model = {**DIJKSTRA, "model": "o3"}
    fresh = codex_runner.build_command(dijkstra_with_model, PROMPT, None, True, LASTMSG)
    assert fresh[fresh.index("-m") + 1] == "o3"  # only the field drives -m


def test_codex_build_command_no_effort_when_unset(monkeypatch):
    # No agents.json "effort" field => no model_reasoning_effort -c override.
    _clear_model_effort_env(monkeypatch)
    fresh = codex_runner.build_command(DIJKSTRA, PROMPT, None, True, LASTMSG)
    resume = codex_runner.build_command(DIJKSTRA, PROMPT, THREAD_ID, False, LASTMSG)
    for argv in (fresh, resume):
        assert not any("model_reasoning_effort" in tok for tok in argv)


def test_codex_build_command_effort_field_fresh_and_resume(monkeypatch):
    # An agents.json "effort": "high" field => -c model_reasoning_effort="high" on
    # BOTH branches, before the prompt. The exact argv token is the TOML-quoted form.
    _clear_model_effort_env(monkeypatch)
    dijkstra_with_effort = {**DIJKSTRA, "effort": "high"}
    token = 'model_reasoning_effort="high"'

    fresh = codex_runner.build_command(
        dijkstra_with_effort, PROMPT, None, True, LASTMSG
    )
    assert token in fresh
    assert fresh[fresh.index(token) - 1] == "-c"
    assert fresh.index(token) < fresh.index(PROMPT)

    resume = codex_runner.build_command(
        dijkstra_with_effort, PROMPT, THREAD_ID, False, LASTMSG
    )
    assert token in resume
    assert resume[resume.index(token) - 1] == "-c"
    assert resume.index(token) < resume.index(PROMPT)


def test_codex_build_command_effort_ignores_legacy_env_var(monkeypatch):
    # The legacy CODEX_EFFORT env var is no longer a source: with it set but no
    # agents.json "effort" field, build_command emits NO model_reasoning_effort
    # override. Only the agent's field drives it.
    _clear_model_effort_env(monkeypatch)
    monkeypatch.setenv("CODEX_EFFORT", "low")  # must be ignored now
    fresh = codex_runner.build_command(DIJKSTRA, PROMPT, None, True, LASTMSG)
    assert not any("model_reasoning_effort" in tok for tok in fresh)
    dijkstra_with_effort = {**DIJKSTRA, "effort": "high"}
    fresh2 = codex_runner.build_command(
        dijkstra_with_effort, PROMPT, None, True, LASTMSG
    )
    assert 'model_reasoning_effort="high"' in fresh2


def test_codex_build_command_model_and_effort_field_fresh_and_resume(monkeypatch):
    # A per-agent agents.json model AND effort flow into argv for codex on BOTH
    # fresh and resume: -m <model> and -c model_reasoning_effort="<effort>".
    _clear_model_effort_env(monkeypatch)
    dijkstra_full = {**DIJKSTRA, "model": "o3", "effort": "high"}
    effort_token = 'model_reasoning_effort="high"'
    for is_new, sid in ((True, None), (False, THREAD_ID)):
        argv = codex_runner.build_command(dijkstra_full, PROMPT, sid, is_new, LASTMSG)
        assert argv[argv.index("-m") + 1] == "o3"
        assert effort_token in argv
        assert argv[argv.index(effort_token) - 1] == "-c"
        assert argv[-1] == PROMPT


def test_codex_build_command_overrides_model_and_effort(monkeypatch):
    # A per-thread override REPLACES the resolved codex model/effort: -m <override>
    # and -c model_reasoning_effort="<override>" appear, on both fresh and resume.
    _clear_model_effort_env(monkeypatch)
    overrides = {"model": "gpt-5.4", "effort": "low"}
    effort_token = 'model_reasoning_effort="low"'
    for is_new, sid in ((True, None), (False, THREAD_ID)):
        argv = codex_runner.build_command(
            DIJKSTRA, PROMPT, sid, is_new, LASTMSG, overrides=overrides
        )
        assert argv[argv.index("-m") + 1] == "gpt-5.4"
        assert effort_token in argv
        assert argv[argv.index(effort_token) - 1] == "-c"


def test_codex_build_command_overrides_none_or_empty_is_byte_identical(monkeypatch):
    _clear_model_effort_env(monkeypatch)
    base = codex_runner.build_command(DIJKSTRA, PROMPT, None, True, LASTMSG)
    assert (
        codex_runner.build_command(
            DIJKSTRA, PROMPT, None, True, LASTMSG, overrides=None
        )
        == base
    )
    assert (
        codex_runner.build_command(DIJKSTRA, PROMPT, None, True, LASTMSG, overrides={})
        == base
    )


# ---------------------------------------------------------------------------
# codex_profile persona: OPTIONAL, codex-only. The NAME of an operator-installed
# ~/.codex/<name>.config.toml profile (whose developer_instructions is the
# persona). The codex analog of claude_agent: codex_runner appends
# `--profile <name>` so codex layers that profile. Applied on the FRESH `codex
# exec` run only: `codex exec resume` does not accept --profile (verified against
# codex-cli 0.142.0), so resume must NOT carry it. Model/effort still come from
# agents.json (the CLI flags override profile config).
# ---------------------------------------------------------------------------


def test_codex_build_command_profile_on_fresh():
    # codex_profile set, fresh run: --profile <name> appears in the argv.
    agent = {**DIJKSTRA, "codex_profile": "dijkstra"}
    argv = codex_runner.build_command(agent, PROMPT, None, True, LASTMSG)
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == "dijkstra"


def test_codex_build_command_profile_not_on_resume():
    # codex_profile set, resume run: --profile is NOT applied on resume, because
    # `codex exec resume` does not accept the flag (verified, codex-cli 0.142.0).
    # The resumed thread already carries the persona from turn one.
    agent = {**DIJKSTRA, "codex_profile": "dijkstra"}
    argv = codex_runner.build_command(agent, PROMPT, THREAD_ID, False, LASTMSG)
    assert "--profile" not in argv


def test_codex_build_command_no_profile_when_unset():
    # A codex agent dict with NO codex_profile field: no --profile on either branch.
    # Uses a SYNTHETIC dict (not the real Dijkstra entry, which now ships a profile)
    # so this keeps validating the unset behavior independent of agents.json.
    no_profile = {"name": "noprof", "backend": "codex"}
    fresh = codex_runner.build_command(no_profile, PROMPT, None, True, LASTMSG)
    resume = codex_runner.build_command(no_profile, PROMPT, THREAD_ID, False, LASTMSG)
    assert "--profile" not in fresh
    assert "--profile" not in resume


def test_codex_build_command_dijkstra_uses_project_manager_profile():
    # The REAL Dijkstra entry from agents.json now ships codex_profile=project_manager.
    # Its fresh-run argv carries --profile project_manager; its resume argv does NOT
    # (codex exec resume rejects --profile). Model/effort stay from agents.json.
    dijkstra = next(a for a in agents.REGISTRY if a["name"] == "dijkstra")
    assert dijkstra.get("codex_profile") == "project_manager"

    fresh = codex_runner.build_command(dijkstra, PROMPT, None, True, LASTMSG)
    assert "--profile" in fresh
    assert fresh[fresh.index("--profile") + 1] == "project_manager"

    resume = codex_runner.build_command(dijkstra, PROMPT, THREAD_ID, False, LASTMSG)
    assert "--profile" not in resume


# ---------------------------------------------------------------------------
# build_manifest: derive a Slack app manifest from a registry entry
# ---------------------------------------------------------------------------


def test_build_manifest_name_fields_scopes_events_and_socket():
    # Both name fields are the display_name; the scopes/events/socket constants are
    # the fixed shared values copied from the old static manifests.
    agent = {"name": "aristotle", "display_name": "Aristotle"}
    m = build_manifest(agent)
    assert m["display_information"]["name"] == "Aristotle"
    assert m["features"]["bot_user"]["display_name"] == "Aristotle"
    assert m["features"]["bot_user"]["always_online"] is True
    assert m["oauth_config"]["scopes"]["bot"] == [
        "app_mentions:read",
        "chat:write",
        "channels:history",
        "groups:history",
        "im:history",
        "files:read",
        "files:write",
    ]
    assert m["settings"]["event_subscriptions"]["bot_events"] == [
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im",
    ]
    assert m["settings"]["socket_mode_enabled"] is True
    assert m["settings"]["token_rotation_enabled"] is False


def test_build_manifest_json_round_trip_offline():
    # The manifest is JSON-serializable and round-trips (offline; no Slack/network).
    # This mirrors what `python -m src manifest <name>` prints to stdout.
    agent = {"name": "cicero", "display_name": "Cicero"}
    parsed = json.loads(json.dumps(build_manifest(agent), indent=2))
    assert parsed["display_information"]["name"] == "Cicero"
    assert parsed["features"]["bot_user"]["display_name"] == "Cicero"


def test_manifest_cli_prints_named_agent():
    # `python -m src manifest aristotle` prints that agent's manifest as JSON. Runs
    # the package entrypoint in a subprocess with the project root on PYTHONPATH so
    # `from . import agents` resolves; it must NOT need Slack tokens or network
    # (the manifest path imports neither slack_bolt nor any token).
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = _PROJECT_ROOT
    proc = subprocess.run(
        [sys.executable, "-m", "src", "manifest", "aristotle"],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed["display_information"]["name"] == "Aristotle"


# ---------------------------------------------------------------------------
# run_codex: mock subprocess + the -o file; parse reply, capture thread_id
# ---------------------------------------------------------------------------


def test_run_codex_fresh_captures_thread_id():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
            json.dumps({"type": "item.completed"}),
        ]
    )
    fake = _codex_proc_writing("hello from codex", stdout=stdout)
    with mock.patch("src.runners.codex_runner.subprocess.run", side_effect=fake):
        reply, sid, meta = codex_runner.run_codex(DIJKSTRA, PROMPT, None, True)
    assert reply == "hello from codex"
    assert sid == THREAD_ID
    # meta carries the four keys; codex has no context_pct/cost; duration measured.
    assert set(meta) == {"context_pct", "tokens", "cost_usd", "duration_s"}
    assert meta["context_pct"] is None
    assert meta["cost_usd"] is None
    assert isinstance(meta["duration_s"], float)


def test_run_codex_resume_returns_prior_id():
    # Resume run: no fresh thread_id in stdout; the prior id is returned unchanged.
    fake = _codex_proc_writing("resumed reply", stdout="")
    with mock.patch("src.runners.codex_runner.subprocess.run", side_effect=fake):
        reply, sid, _meta = codex_runner.run_codex(DIJKSTRA, PROMPT, THREAD_ID, False)
    assert reply == "resumed reply"
    assert sid == THREAD_ID


def test_run_codex_raises_on_nonzero_exit():
    fake = _codex_proc_writing("", stdout="", returncode=2, stderr="boom")
    with mock.patch("src.runners.codex_runner.subprocess.run", side_effect=fake):
        try:
            codex_runner.run_codex(DIJKSTRA, PROMPT, None, True)
            assert False, "expected CodexRunError"
        except codex_runner.CodexRunError as exc:
            assert "code 2" in str(exc)


def test_run_codex_raises_on_empty_reply():
    fake = _codex_proc_writing(
        "", stdout=json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
    )
    with mock.patch("src.runners.codex_runner.subprocess.run", side_effect=fake):
        try:
            codex_runner.run_codex(DIJKSTRA, PROMPT, None, True)
            assert False, "expected CodexRunError"
        except codex_runner.CodexRunError as exc:
            assert "empty" in str(exc).lower()


def test_run_codex_raises_on_missing_thread_id():
    # Fresh run but stdout never reports a thread_id => error (we cannot persist).
    fake = _codex_proc_writing("a reply", stdout=json.dumps({"type": "item"}))
    with mock.patch("src.runners.codex_runner.subprocess.run", side_effect=fake):
        try:
            codex_runner.run_codex(DIJKSTRA, PROMPT, None, True)
            assert False, "expected CodexRunError"
        except codex_runner.CodexRunError as exc:
            assert "thread_id" in str(exc).lower()


def test_run_codex_raises_on_timeout():
    import subprocess as sp

    def _raise(*a, **k):
        raise sp.TimeoutExpired(cmd="codex", timeout=1)

    with mock.patch("src.runners.codex_runner.subprocess.run", side_effect=_raise):
        try:
            codex_runner.run_codex(DIJKSTRA, PROMPT, None, True, timeout=1)
            assert False, "expected CodexRunError"
        except codex_runner.CodexRunError as exc:
            assert "timed out" in str(exc).lower()
