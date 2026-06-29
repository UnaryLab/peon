"""build_command exact argv (claude) + session persistence."""

import json

from src.runners import claude_runner

from tests.helpers import (
    SID,
    PROMPT,
    MODEL,
    BRUNEL,
    ARISTOTLE,
    CICERO,
    _clear_model_effort_env,
)


# ---------------------------------------------------------------------------
# build_command: exact argv for each agent, new + resume
# ---------------------------------------------------------------------------


def test_build_command_brunel_new_and_resume(monkeypatch):
    # Clear all model/effort env so this proves the no-effort default argv
    # regardless of any CLAUDE_EFFORT etc. exported in the developer's shell.
    _clear_model_effort_env(monkeypatch)
    assert claude_runner.build_command(BRUNEL, PROMPT, SID, True) == [
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
    assert claude_runner.build_command(BRUNEL, PROMPT, SID, False) == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--resume",
        SID,
        "--agent",
        "unarylab-research:project_manager",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        PROMPT,
    ]


def test_build_command_aristotle_new_and_resume(monkeypatch):
    # With no env overrides, Aristotle's argv is the bare default (model pinned,
    # no --effort) on both new and resume runs.
    _clear_model_effort_env(monkeypatch)
    assert claude_runner.build_command(ARISTOTLE, PROMPT, SID, True) == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--session-id",
        SID,
        "--agent",
        "unarylab-research:research_manager",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        PROMPT,
    ]
    assert claude_runner.build_command(ARISTOTLE, PROMPT, SID, False) == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--resume",
        SID,
        "--agent",
        "unarylab-research:research_manager",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        PROMPT,
    ]


def test_build_command_cicero_has_no_agent_flag(monkeypatch):
    _clear_model_effort_env(monkeypatch)
    new_argv = claude_runner.build_command(CICERO, PROMPT, SID, True)
    resume_argv = claude_runner.build_command(CICERO, PROMPT, SID, False)
    assert new_argv == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--session-id",
        SID,
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        PROMPT,
    ]
    assert resume_argv == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--resume",
        SID,
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        PROMPT,
    ]
    assert "--agent" not in new_argv
    assert "--agent" not in resume_argv


def test_build_command_invariants_for_all_agents():
    for agent in (BRUNEL, ARISTOTLE, CICERO):
        for is_new in (True, False):
            argv = claude_runner.build_command(agent, PROMPT, SID, is_new)
            # --output-format json always present
            assert "--output-format" in argv
            assert argv[argv.index("--output-format") + 1] == "json"
            # exactly one of --session-id / --resume
            assert ("--session-id" in argv) != ("--resume" in argv)
            # prompt is always last
            assert argv[-1] == PROMPT


# ---------------------------------------------------------------------------
# claude reasoning effort: optional, off by default. Its sole source is the
# agents.json "effort" field; absent/empty => omit the flag. There is no env-var
# layer, so the legacy CLAUDE_EFFORT is ignored. Tests scrub the legacy env
# defensively, then set the effort per-agent via a dict field.
# ---------------------------------------------------------------------------


def test_build_command_no_effort_when_unset(monkeypatch):
    # No agents.json "effort" field => no --effort flag.
    _clear_model_effort_env(monkeypatch)
    for is_new in (True, False):
        argv = claude_runner.build_command(BRUNEL, PROMPT, SID, is_new)
        assert "--effort" not in argv


def test_build_command_effort_from_agents_json_field_new_and_resume(monkeypatch):
    # An agents.json "effort" field on the agent => --effort <value> after --model
    # and before the prompt, on both new and resume. (No env-var layer exists.)
    _clear_model_effort_env(monkeypatch)
    brunel_with_effort = {**BRUNEL, "effort": "medium"}
    new_argv = claude_runner.build_command(brunel_with_effort, PROMPT, SID, True)
    assert new_argv == [
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
        "--effort",
        "medium",
        PROMPT,
    ]
    resume_argv = claude_runner.build_command(brunel_with_effort, PROMPT, SID, False)
    assert resume_argv == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--resume",
        SID,
        "--agent",
        "unarylab-research:project_manager",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        MODEL,
        "--effort",
        "medium",
        PROMPT,
    ]
    for argv in (new_argv, resume_argv):
        assert argv[argv.index("--effort") + 1] == "medium"
        assert argv.index("--model") < argv.index("--effort")
        assert argv.index("--effort") < argv.index(PROMPT)


def test_build_command_effort_field_only(monkeypatch):
    # An agents.json "effort" field on one agent => that agent gets --effort high;
    # other agents (no field) carry no --effort.
    _clear_model_effort_env(monkeypatch)
    aristotle_with_effort = {**ARISTOTLE, "effort": "high"}
    aristotle_argv = claude_runner.build_command(
        aristotle_with_effort, PROMPT, SID, True
    )
    assert aristotle_argv[aristotle_argv.index("--effort") + 1] == "high"
    assert "--effort" not in claude_runner.build_command(BRUNEL, PROMPT, SID, True)
    assert "--effort" not in claude_runner.build_command(CICERO, PROMPT, SID, True)


def test_build_command_effort_ignores_legacy_env_var(monkeypatch):
    # The legacy CLAUDE_EFFORT env var is no longer a source: with it set but no
    # agents.json "effort" field, build_command emits NO --effort. The agent's
    # field is the only thing that drives effort.
    _clear_model_effort_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_EFFORT", "low")  # must be ignored now
    assert "--effort" not in claude_runner.build_command(CICERO, PROMPT, SID, True)
    aristotle_with_effort = {**ARISTOTLE, "effort": "high"}
    aristotle_argv = claude_runner.build_command(
        aristotle_with_effort, PROMPT, SID, True
    )
    assert aristotle_argv[aristotle_argv.index("--effort") + 1] == "high"


# ---------------------------------------------------------------------------
# claude model: sole source is the agents.json "model" field, else the pinned
# code-level fallback. No env-var layer, so the legacy CLAUDE_MODEL is ignored.
# --model is ALWAYS present (the fallback is non-empty).
# ---------------------------------------------------------------------------


def test_build_command_model_default_when_unset(monkeypatch):
    # No agents.json "model" field => the pinned code-level fallback is used.
    _clear_model_effort_env(monkeypatch)
    argv = claude_runner.build_command(BRUNEL, PROMPT, SID, True)
    assert argv[argv.index("--model") + 1] == MODEL  # claude-opus-4-8[1m]


def test_build_command_model_from_agents_json_field(monkeypatch):
    # An agents.json "model" field is used verbatim as the --model value.
    _clear_model_effort_env(monkeypatch)
    brunel_with_model = {**BRUNEL, "model": "claude-sonnet-4-6"}
    argv = claude_runner.build_command(brunel_with_model, PROMPT, SID, True)
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"


def test_build_command_model_ignores_legacy_env_var(monkeypatch):
    # The legacy CLAUDE_MODEL env var is no longer a source: with it set but no
    # agents.json "model" field, build_command falls back to the pinned default,
    # NOT the env value.
    _clear_model_effort_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")  # must be ignored now
    argv = claude_runner.build_command(BRUNEL, PROMPT, SID, True)
    assert argv[argv.index("--model") + 1] == MODEL  # the pinned fallback, not env


# ---------------------------------------------------------------------------
# build_command: per-thread overrides REPLACE the resolved model/effort.
# overrides=None / {} MUST be byte-identical to the no-override call.
# ---------------------------------------------------------------------------


def test_build_command_overrides_model_and_effort(monkeypatch):
    _clear_model_effort_env(monkeypatch)
    overrides = {"model": "claude-sonnet-4-6", "effort": "high"}
    argv = claude_runner.build_command(BRUNEL, PROMPT, SID, True, overrides=overrides)
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert argv[argv.index("--effort") + 1] == "high"


def test_build_command_override_effort_only_leaves_model_default(monkeypatch):
    _clear_model_effort_env(monkeypatch)
    argv = claude_runner.build_command(
        BRUNEL, PROMPT, SID, True, overrides={"effort": "low"}
    )
    # Model stays the agents.json/default value; only effort is overridden.
    assert argv[argv.index("--model") + 1] == MODEL
    assert argv[argv.index("--effort") + 1] == "low"


def test_build_command_overrides_none_or_empty_is_byte_identical(monkeypatch):
    _clear_model_effort_env(monkeypatch)
    base = claude_runner.build_command(BRUNEL, PROMPT, SID, True)
    assert (
        claude_runner.build_command(BRUNEL, PROMPT, SID, True, overrides=None) == base
    )
    assert claude_runner.build_command(BRUNEL, PROMPT, SID, True, overrides={}) == base


# ---------------------------------------------------------------------------
# get_or_create_session: persistence + independent contexts
# ---------------------------------------------------------------------------


def test_session_create_then_resume_same_id(tmp_path):
    store = str(tmp_path / "sessions.json")
    sid1, is_new1 = claude_runner.get_or_create_session("brunel", "T1", path=store)
    assert is_new1 is True
    sid2, is_new2 = claude_runner.get_or_create_session("brunel", "T1", path=store)
    assert is_new2 is False
    assert sid1 == sid2
    # persisted to disk under the composite key
    with open(store, encoding="utf-8") as f:
        data = json.load(f)
    assert data["brunel:T1"] == sid1


def test_sessions_are_independent_across_agent_and_thread(tmp_path):
    store = str(tmp_path / "sessions.json")
    brunel_t1, _ = claude_runner.get_or_create_session("brunel", "T1", path=store)
    # same thread, different agent => different session (independent contexts)
    aristotle_t1, _ = claude_runner.get_or_create_session("aristotle", "T1", path=store)
    # same agent, different thread => different session
    brunel_t2, _ = claude_runner.get_or_create_session("brunel", "T2", path=store)
    assert brunel_t1 != aristotle_t1
    assert brunel_t1 != brunel_t2
    assert aristotle_t1 != brunel_t2
