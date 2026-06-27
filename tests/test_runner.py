"""Self-check for the registry + runner. NO live Slack or Claude calls.

Run with pytest (from the project root):
    conda run -n peon python -m pytest tests/ -q

Or, if pytest is unavailable, run the asserts directly:
    conda run -n peon python tests/test_runner.py

These modules deliberately do NOT import slack_bolt, so this self-check runs
even when slack-bolt is not installed (it must NOT import src.app).
"""

import io
import json
import os
import sys
from unittest import mock

# Ensure the PROJECT ROOT (parent of tests/) is importable so `from src import ...`
# resolves without an install step. Under pytest this is also handled by
# conftest.py; doing it here too keeps `python tests/test_runner.py` working.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src import agents  # noqa: E402 - must follow the sys.path.insert above
from src.manifest import build_manifest, write_manifests  # noqa: E402 - same
from src.runners import claude_runner, codex_runner, get_runner  # noqa: E402 - same


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_contains_expected_agents():
    by_name = {a["name"]: a for a in agents.REGISTRY}
    assert set(["brunel", "aristotle", "cicero", "dijkstra"]).issubset(by_name.keys())
    assert by_name["brunel"]["claude_agent"] == "unarylab-research:project_manager"
    assert by_name["aristotle"]["claude_agent"] == "unarylab-research:research_manager"
    assert by_name["cicero"]["claude_agent"] is None
    assert (
        "claude_agent" not in by_name["dijkstra"]
    )  # codex backend: claude-only field omitted
    assert by_name["brunel"]["display_name"] == "Brunel"
    assert by_name["aristotle"]["display_name"] == "Aristotle"
    assert by_name["cicero"]["display_name"] == "Cicero"
    assert by_name["dijkstra"]["display_name"] == "Dijkstra"


def test_registry_loads_from_agents_json():
    # REGISTRY is loaded from the declarative agents.json: exactly four agents in
    # order, named aristotle/brunel/cicero/dijkstra with backends claude/claude/
    # claude/codex.
    names = [a["name"] for a in agents.REGISTRY]
    assert names == ["aristotle", "brunel", "cicero", "dijkstra"]
    by_name = {a["name"]: a for a in agents.REGISTRY}
    assert by_name["aristotle"]["backend"] == "claude"
    assert by_name["brunel"]["backend"] == "claude"
    assert by_name["cicero"]["backend"] == "claude"
    assert by_name["dijkstra"]["backend"] == "codex"


def test_resolve_reads_only_agents_json_field_else_default(monkeypatch):
    # New resolver: agents.json field is the SINGLE source; otherwise the code-level
    # default. There is NO env-var layer, so an env var matching the key name must
    # NOT affect the result.
    monkeypatch.setenv("k", "env-should-be-ignored")
    # Field present => the field value is returned.
    assert agents.resolve({"name": "x", "k": "field"}, "k", "D") == "field"
    # No field => the code-level default (NOT the env var).
    assert agents.resolve({"name": "x"}, "k", "D") == "D"
    # Empty field falls through to the default, too.
    assert agents.resolve({"name": "x", "k": ""}, "k", "D") == "D"
    # Default arg defaults to "" (the omit case for effort / codex model).
    assert agents.resolve({"name": "x"}, "k") == ""


def test_registry_backends_resolve():
    # Dijkstra is codex-backed; the claude agents default to "claude" whether or not
    # an explicit "backend" field is present (resolve via .get default).
    by_name = {a["name"]: a for a in agents.REGISTRY}
    assert by_name["dijkstra"]["backend"] == "codex"
    for name in ("aristotle", "brunel", "cicero"):
        assert by_name[name].get("backend", "claude") == "claude"


# ---------------------------------------------------------------------------
# get_runner: backend dispatch
# ---------------------------------------------------------------------------


def test_get_runner_dispatches_by_backend():
    assert get_runner("claude") is claude_runner
    assert get_runner("codex") is codex_runner


def test_get_runner_unknown_backend_raises():
    try:
        get_runner("nope")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "nope" in str(exc)


def test_token_env_names_per_agent():
    # One Slack app per agent: each agent's bot + app tokens come from env vars
    # suffixed by its uppercased name. Pure helper, no Slack needed.
    assert agents.token_env_names("brunel") == (
        "SLACK_BOT_TOKEN_BRUNEL",
        "SLACK_APP_TOKEN_BRUNEL",
    )
    assert agents.token_env_names("aristotle") == (
        "SLACK_BOT_TOKEN_ARISTOTLE",
        "SLACK_APP_TOKEN_ARISTOTLE",
    )
    assert agents.token_env_names("cicero") == (
        "SLACK_BOT_TOKEN_CICERO",
        "SLACK_APP_TOKEN_CICERO",
    )
    assert agents.token_env_names("dijkstra") == (
        "SLACK_BOT_TOKEN_DIJKSTRA",
        "SLACK_APP_TOKEN_DIJKSTRA",
    )


def test_startable_agents_only_those_with_both_tokens():
    # Graceful partial startup: only agents with BOTH tokens present + non-empty
    # are startable. Env is injected (a plain dict), so no Slack/real tokens.
    brunel_only = {
        "SLACK_BOT_TOKEN_BRUNEL": "xoxb-brunel",
        "SLACK_APP_TOKEN_BRUNEL": "xapp-brunel",
    }
    startable = agents.startable_agents(brunel_only)
    assert [a["name"] for a in startable] == ["brunel"]

    # No tokens at all => nothing startable.
    assert agents.startable_agents({}) == []

    # One token missing or empty => that agent is NOT startable.
    half_aristotle = {
        "SLACK_BOT_TOKEN_ARISTOTLE": "xoxb-aristotle",
        "SLACK_APP_TOKEN_ARISTOTLE": "",
    }
    assert agents.startable_agents(half_aristotle) == []

    # Dijkstra (codex backend) is gated on both his tokens exactly like the others.
    dijkstra_full = {
        "SLACK_BOT_TOKEN_DIJKSTRA": "xoxb-dijkstra",
        "SLACK_APP_TOKEN_DIJKSTRA": "xapp-dijkstra",
    }
    assert [a["name"] for a in agents.startable_agents(dijkstra_full)] == ["dijkstra"]
    dijkstra_half = {
        "SLACK_BOT_TOKEN_DIJKSTRA": "xoxb-dijkstra",
        "SLACK_APP_TOKEN_DIJKSTRA": "",
    }
    assert agents.startable_agents(dijkstra_half) == []


# ---------------------------------------------------------------------------
# build_command: exact argv for each agent, new + resume
# ---------------------------------------------------------------------------

# Agent dicts used in the argv tests. Plain dicts (no model/effort field) exercise
# the default path (the code-level fallback); per-agent tests pass a dict WITH a
# "model"/"effort" field to prove the agents.json field is the sole source.
BRUNEL = {"name": "brunel", "claude_agent": "unarylab-research:project_manager"}
ARISTOTLE = {"name": "aristotle", "claude_agent": "unarylab-research:research_manager"}
CICERO = {"name": "cicero", "claude_agent": None}

SID = "11111111-2222-3333-4444-555555555555"
PROMPT = "what is 2+2?"

# The shipped claude agents pin this model in agents.json; the default-path tests
# use agent dicts with no "model" field, so build_command falls back to this same
# pin. Either way every claude argv carries --model <MODEL> just before the prompt.
MODEL = "claude-opus-4-8[1m]"

# agents.json is now the SINGLE source of truth for model/effort: agents.resolve
# reads ONLY the agent dict's field (with one code-level fallback), with NO env-var
# layer. So these vars no longer affect resolution; we still scrub them defensively
# so a stray export in the developer's shell can never matter, and the default-path
# argv (built from agent dicts WITHOUT a model/effort field) is hermetic.
_LEGACY_MODEL_EFFORT_ENV_VARS = [
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CODEX_MODEL",
    "CODEX_EFFORT",
]


def _clear_model_effort_env(monkeypatch):
    """Defensively delenv the legacy model/effort env vars.

    These are no longer read by agents.resolve (agents.json is the source of
    truth), so this is belt-and-suspenders; default-path tests pass agent dicts
    with no model/effort field, exercising the code-level fallback.
    """
    for var in _LEGACY_MODEL_EFFORT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


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


def _fake_proc(returncode=0, stdout="", stderr=""):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


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

DIJKSTRA = {"name": "dijkstra", "claude_agent": None, "backend": "codex"}
THREAD_ID = "0199abcd-ef01-7234-89ab-cdef01234567"
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


def _codex_proc_writing(reply, stdout="", returncode=0, stderr=""):
    """Return a fake subprocess.run that writes `reply` to the -o file (the path
    is argv[argv.index("-o") + 1]) and returns a proc with the given stdout, so
    run_codex's read-from-file + parse-stdout paths are both exercised hermetically.
    """

    def _run(argv, **kwargs):
        out_path = argv[argv.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(reply)
        proc = mock.Mock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    return _run


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


# ---------------------------------------------------------------------------
# Usage meta + footer: both runners surface {context_pct, tokens, cost_usd,
# duration_s} from the CLI's own output (NO extra CLI call, NO argv change), and
# app._format_usage renders it as a one-line footer. slack_bolt IS installed in
# this env, so we import src.app and assert _format_usage directly; if it ever is
# not, the import is guarded and only the footer-rendering assertion is skipped.
# ---------------------------------------------------------------------------

try:
    from src import app as _appmod  # noqa: E402 - optional (needs slack_bolt)

    _HAVE_APP = True
except Exception:  # noqa: BLE001 - slack_bolt absent: skip footer-render asserts
    _appmod = None
    _HAVE_APP = False


# An agent whose resolved model has NO [1m] suffix -> 200k context window, so the
# percent denominator differs from the [1m] agents. SID/PROMPT reused from above.
NON_1M = {"name": "brunel", "claude_agent": None, "model": "claude-opus-4-8"}


def test_claude_meta_and_footer_1m_window():
    # A realistic claude --output-format json blob carrying usage/cost/timing.
    # input-side context = 30000 + 5000 + 5000 = 40000; window for a [1m] model is
    # 1,000,000 -> 4%. tokens sum input+output+cache = 40000 + 2000 = 42000.
    usage = {
        "input_tokens": 30000,
        "output_tokens": 2000,
        "cache_creation_input_tokens": 5000,
        "cache_read_input_tokens": 5000,
    }
    blob = json.dumps(
        {
            "result": "ok",
            "is_error": False,
            "subtype": "success",
            "usage": usage,
            "total_cost_usd": 0.04,
            "duration_ms": 18000,
        }
    )
    # BRUNEL here has no "model" field, so resolve falls back to the [1m] pin.
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, blob)
    ):
        reply, sid, meta = claude_runner.answer(BRUNEL, PROMPT, None)
    assert reply == "ok"
    assert sid is not None
    assert meta["context_pct"] == 4  # 40000 / 1_000_000 -> 4%
    assert meta["tokens"] == 42000
    assert meta["cost_usd"] == 0.04
    assert meta["duration_s"] == 18.0

    if _HAVE_APP:
        assert _appmod is not None
        footer = _appmod._format_usage(meta)
        assert footer == "· 4% · 42.0k tok · $0.04 · 18s"
        # Footer leads with the context percent.
        assert footer.startswith("· 4% · ")


def test_claude_meta_context_pct_200k_window():
    # SAME usage, but a model id WITHOUT the [1m] suffix -> 200k window, so the
    # input-side 40000 tokens is 20%, proving the denominator switch.
    usage = {
        "input_tokens": 30000,
        "output_tokens": 2000,
        "cache_creation_input_tokens": 5000,
        "cache_read_input_tokens": 5000,
    }
    blob = json.dumps(
        {
            "result": "ok",
            "is_error": False,
            "subtype": "success",
            "usage": usage,
            "total_cost_usd": 0.04,
            "duration_ms": 18000,
        }
    )
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, blob)
    ):
        _reply, _sid, meta = claude_runner.answer(NON_1M, PROMPT, None)
    assert meta["context_pct"] == 20  # 40000 / 200_000 -> 20%


def test_claude_meta_usage_omits_cache_fields_degrades_gracefully():
    # A usage blob with ONLY input/output tokens and NO cache fields (a real
    # non-cached call). The cache fields are absent, so they must contribute 0,
    # not crash. tokens = 30000 + 2000 = 32000; input-side context = 30000 only.
    # BRUNEL -> [1m] pin -> 1,000,000 window -> 3%.
    usage = {"input_tokens": 30000, "output_tokens": 2000}
    blob = json.dumps(
        {
            "result": "ok",
            "is_error": False,
            "subtype": "success",
            "usage": usage,
        }
    )
    with mock.patch(
        "src.runners.claude_runner.subprocess.run", return_value=_fake_proc(0, blob)
    ):
        _reply, _sid, meta = claude_runner.answer(BRUNEL, PROMPT, None)
    assert meta["tokens"] == 32000  # missing cache fields contribute 0
    assert meta["context_pct"] == 3  # 30000 / 1_000_000 -> 3%


def test_codex_meta_tokens_no_cost_no_context_pct():
    # JSONL with BOTH the thread.started event AND a realistic token-usage event.
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 12000, "output_tokens": 2000},
                }
            ),
        ]
    )
    fake = _codex_proc_writing("hi from codex", stdout=stdout)
    with mock.patch("src.runners.codex_runner.subprocess.run", side_effect=fake):
        reply, sid, meta = codex_runner.answer(DIJKSTRA, PROMPT, None)
    assert reply == "hi from codex"
    assert sid == THREAD_ID
    assert meta["tokens"] == 14000  # 12000 + 2000
    assert meta["cost_usd"] is None  # codex reports no cost
    assert meta["context_pct"] is None  # codex window unknown
    assert isinstance(meta["duration_s"], float)

    if _HAVE_APP:
        assert _appmod is not None
        footer = _appmod._format_usage(meta)
        # No leading percent, no cost: only tokens + duration.
        assert "%" not in footer
        assert "$" not in footer
        assert footer.startswith("· 14.0k tok · ")


def test_format_usage_all_none_returns_empty():
    if not _HAVE_APP:
        return  # _format_usage lives in app.py; nothing to assert without slack_bolt
    assert _appmod is not None
    empty = {
        "context_pct": None,
        "tokens": None,
        "cost_usd": None,
        "duration_s": None,
    }
    assert _appmod._format_usage(empty) == ""


def test_format_usage_token_formatting():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    base = {"context_pct": None, "cost_usd": None, "duration_s": None}
    assert _appmod._format_usage({**base, "tokens": 950}) == "· 950 tok"
    assert _appmod._format_usage({**base, "tokens": 12345}) == "· 12.3k tok"


def test_usage_enabled_default_on_off_and_unset(monkeypatch):
    # The footer now defaults ON: with SHOW_USAGE truly ABSENT, _usage_enabled() is
    # True. Only an explicit off-value ("0"/"false"/"no"/"off", case-insensitive)
    # disables it; on-values and arbitrary values leave it enabled.
    if not _HAVE_APP:
        return  # _usage_enabled lives in app.py; nothing to assert without slack_bolt
    assert _appmod is not None
    # Unset/absent -> ON (the new default).
    monkeypatch.delenv("SHOW_USAGE", raising=False)
    assert _appmod._usage_enabled() is True
    # Empty string -> ON (treated like unset).
    monkeypatch.setenv("SHOW_USAGE", "")
    assert _appmod._usage_enabled() is True
    # Explicit off-values -> OFF (case-insensitive, whitespace-tolerant).
    for off in ("0", "false", "no", "off", "FALSE", "Off", "  no  "):
        monkeypatch.setenv("SHOW_USAGE", off)
        assert _appmod._usage_enabled() is False, off
    # Explicit on-values -> ON.
    for on in ("1", "true", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("SHOW_USAGE", on)
        assert _appmod._usage_enabled() is True, on
    # Any other value -> ON (only the off-set disables).
    monkeypatch.setenv("SHOW_USAGE", "maybe")
    assert _appmod._usage_enabled() is True


def test_manifest_write_creates_files(tmp_path):
    """`write_manifests` materializes manifest-<name>.json for every agent."""
    import json as _json
    import os as _os

    dest = tmp_path / "manifests"  # Path under pytest, str under the no-pytest shim
    paths = write_manifests(agents.REGISTRY, dest)
    assert len(paths) == len(agents.REGISTRY)
    for p in paths:
        assert _os.path.exists(str(p))
    aristotle = next(a for a in agents.REGISTRY if a["name"] == "aristotle")
    apath = next(p for p in paths if str(p).endswith("manifest-aristotle.json"))
    assert _json.load(open(str(apath))) == build_manifest(aristotle)


# ---------------------------------------------------------------------------
# Authoritative .env loading (src/env.py): .env overrides the shell, including
# SESSIONS_PATH which the claude runner resolves at import time / store access.
# ---------------------------------------------------------------------------


def test_load_env_dotenv_beats_shell(monkeypatch, tmp_path):
    """.env wins over a shell-exported var: load_env(..., override=True) makes a
    CLAUDE_TIMEOUT_MIN in the .env file replace one already exported in the
    shell. (Uses a still-live env var; model/effort no longer come from env.)"""
    from src.env import load_env

    # Simulate a shell-exported value that should LOSE to .env.
    monkeypatch.setenv("CLAUDE_TIMEOUT_MIN", "111")
    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_TIMEOUT_MIN=222\n", encoding="utf-8")

    assert load_env(env_file) is True  # dotenv installed -> attempted
    assert os.environ["CLAUDE_TIMEOUT_MIN"] == "222"  # .env beat the shell


def test_load_env_missing_file_is_noop(monkeypatch, tmp_path):
    """A missing .env is a silent no-op (no crash, no clobber of existing vars)."""
    from src.env import load_env

    monkeypatch.setenv("CLAUDE_TIMEOUT_MIN", "111")
    missing = tmp_path / "does-not-exist.env"
    assert not missing.exists()

    load_env(missing)  # must not raise
    assert os.environ["CLAUDE_TIMEOUT_MIN"] == "111"  # untouched


def test_sessions_path_from_env_redirects_store(monkeypatch, tmp_path):
    """A SESSIONS_PATH in os.environ redirects the runner's store, resolved LIVE.

    The claude runner reads SESSIONS_PATH lazily at store-access time, so a value
    placed into os.environ (as load_env would do from .env) redirects where
    sessions.json is read/written, even though the runner was imported earlier."""
    store = tmp_path / "custom-sessions.json"
    monkeypatch.setenv("SESSIONS_PATH", str(store))

    # The resolver honors the env var...
    assert claude_runner._sessions_path() == str(store)

    # ...and a real round-trip through the default-path API writes THERE.
    sid, is_new = claude_runner.get_or_create_session("aristotle", "1.23")
    assert is_new
    assert store.exists()  # store materialized at the env-pointed path
    again, is_new2 = claude_runner.get_or_create_session("aristotle", "1.23")
    assert again == sid and not is_new2


def test_dotenv_sessions_path_wins_over_shell_via_main_import_order(tmp_path):
    """End-to-end proof in the real `python -m src` import order (subprocess).

    A shell-exported SESSIONS_PATH is set in the child's environment; a temp .env
    sets a DIFFERENT SESSIONS_PATH. __main__ calls load_env(override=True) FIRST,
    before .app (hence claude_runner) is imported, so the store resolves to the
    .env path, not the shell one. We run a tiny driver as `python -m src`-style
    code: it imports src.env + the runner exactly as the package does."""
    import subprocess

    shell_store = tmp_path / "shell-sessions.json"
    env_file = tmp_path / ".env"
    dotenv_store = tmp_path / "dotenv-sessions.json"
    env_file.write_text(f"SESSIONS_PATH={dotenv_store}\n", encoding="utf-8")

    # Driver mirrors __main__'s order: load_env(.env, override=True) BEFORE the
    # runner is imported, then read the resolved store path + do a round-trip.
    driver = (
        "from src.env import load_env\n"
        f"load_env(r'{env_file}')\n"  # authoritative, override=True default
        "from src.runners import claude_runner\n"
        "sid, new = claude_runner.get_or_create_session('aristotle', 't')\n"
        "print(claude_runner._sessions_path())\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = _PROJECT_ROOT
    env["SESSIONS_PATH"] = str(shell_store)  # the shell value that must LOSE
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    resolved = proc.stdout.strip().splitlines()[-1]
    assert resolved == str(dotenv_store), proc.stdout + proc.stderr  # .env won
    assert dotenv_store.exists()  # round-trip wrote to the .env path
    assert not shell_store.exists()  # the shell path was NOT used


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
    from src import agents
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
    from src import agents
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
    from src import agents
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
    from src import agents
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
    from src import agents

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


class _FakeSay:
    """A capturing stand-in for Slack's `say`: records the posted text/thread_ts.

    A callable object (not a function with an attached attribute) so the `.posts`
    list is a real, type-visible member.
    """

    def __init__(self):
        self.posts = []

    def __call__(self, text=None, thread_ts=None):
        self.posts.append({"text": text, "thread_ts": thread_ts})
        return {"ts": "placeholder-ts"}


def _fake_say():
    return _FakeSay()


_CONTROL_AGENT = {
    "name": "aristotle",
    "display_name": "Aristotle",
    "backend": "claude",
    "claude_agent": "unarylab-research:research_manager",
    "model": "claude-opus-4-8[1m]",
    "effort": "xhigh",
}


def test_control_phrase_set_model(monkeypatch, tmp_path):
    from src import app as appmod

    store = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: store)
    say = _fake_say()

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
    say = _fake_say()

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
    say = _fake_say()

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
    say = _fake_say()

    handled = appmod._handle_control_phrase(_CONTROL_AGENT, "!reset", "T1", say)
    assert handled is True
    assert claude_runner.get_override("aristotle", "T1", path=store) is None
    assert len(say.posts) == 1


def test_control_phrase_unknown_help(monkeypatch, tmp_path):
    from src import app as appmod

    store = str(tmp_path / "overrides.json")
    monkeypatch.setattr(claude_runner, "_overrides_path", lambda: store)
    say = _fake_say()

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
    say = _fake_say()

    # A normal prompt is NOT a control phrase: nothing posted, agent should run.
    handled = appmod._handle_control_phrase(_CONTROL_AGENT, "what is 2+2?", "T1", say)
    assert handled is False
    assert say.posts == []


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
        def answer(agent, prompt, prior, overrides=None, on_update=None, cancel=None):
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
        def answer(agent, prompt, prior, overrides=None, on_update=None, cancel=None):
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


# ---------------------------------------------------------------------------
# CRON SCHEDULING (Slack-native). The store (crons.json, sibling of sessions.json)
# CRUD lives in claude_runner; the 5-field cron-match logic + the scheduler tick
# live in app.py. All time-based tests INJECT a datetime/clock (no sleep, no real
# wall-clock). Note: Claude Code has its own /schedule (cloud routines); this is
# the in-process Slack-native equivalent the user asked for.
# ---------------------------------------------------------------------------


def test_cron_store_add_list_remove(tmp_path):
    crons = str(tmp_path / "crons.json")
    assert claude_runner.list_crons(path=crons) == []
    e = claude_runner.add_cron(
        "0 9 * * *", "aristotle", "C1", "T1", "standup", cron_id="abc", path=crons
    )
    assert e["id"] == "abc"
    assert e["enabled"] is True
    got = claude_runner.list_crons(path=crons)
    assert len(got) == 1
    assert got[0]["schedule"] == "0 9 * * *"
    assert got[0]["agent"] == "aristotle"
    assert got[0]["prompt"] == "standup"
    # Removing a non-existent id is a no-op (False); the real id removes (True).
    assert claude_runner.remove_cron("nope", path=crons) is False
    assert claude_runner.remove_cron("abc", path=crons) is True
    assert claude_runner.list_crons(path=crons) == []


def test_cron_store_set_enabled_toggles(tmp_path):
    crons = str(tmp_path / "crons.json")
    claude_runner.add_cron(
        "* * * * *", "brunel", "C1", "T1", "ping", cron_id="x1", path=crons
    )
    assert claude_runner.set_cron_enabled("x1", False, path=crons) is True
    assert claude_runner.list_crons(path=crons)[0]["enabled"] is False
    assert claude_runner.set_cron_enabled("x1", True, path=crons) is True
    assert claude_runner.list_crons(path=crons)[0]["enabled"] is True
    # Unknown id -> False, no crash.
    assert claude_runner.set_cron_enabled("nope", True, path=crons) is False


def test_cron_store_id_autogenerated_and_unique(tmp_path):
    crons = str(tmp_path / "crons.json")
    a = claude_runner.add_cron("* * * * *", "g", "C", "T", "p", path=crons)
    b = claude_runner.add_cron("* * * * *", "g", "C", "T", "p", path=crons)
    assert a["id"] and b["id"] and a["id"] != b["id"]


def test_cron_store_path_from_sessions_env(monkeypatch, tmp_path):
    # crons.json is a sibling of the sessions path, so SESSIONS_PATH redirects it.
    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "sessions.json"))
    assert claude_runner._crons_path() == str(tmp_path / "crons.json")


def test_cron_store_corrupt_file_yields_empty(tmp_path):
    crons = tmp_path / "crons.json"
    crons.write_text("{not json", encoding="utf-8")
    assert claude_runner.list_crons(path=str(crons)) == []


def test_cron_matches_wildcard_every_minute():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from datetime import datetime

    assert _appmod.cron_matches("* * * * *", datetime(2026, 6, 24, 9, 0))
    assert _appmod.cron_matches("* * * * *", datetime(2026, 1, 1, 0, 0))


def test_cron_matches_specific_minute_hour():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from datetime import datetime

    expr = "30 9 * * *"  # 09:30 daily
    assert _appmod.cron_matches(expr, datetime(2026, 6, 24, 9, 30))
    assert not _appmod.cron_matches(expr, datetime(2026, 6, 24, 9, 31))
    assert not _appmod.cron_matches(expr, datetime(2026, 6, 24, 10, 30))
    assert not _appmod.cron_matches(expr, datetime(2026, 6, 24, 8, 30))


def test_cron_matches_lists_ranges_and_steps():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from datetime import datetime

    # minute list
    assert _appmod.cron_matches("0,15,30 * * * *", datetime(2026, 6, 24, 9, 15))
    assert not _appmod.cron_matches("0,15,30 * * * *", datetime(2026, 6, 24, 9, 16))
    # hour range
    assert _appmod.cron_matches("0 9-17 * * *", datetime(2026, 6, 24, 12, 0))
    assert not _appmod.cron_matches("0 9-17 * * *", datetime(2026, 6, 24, 18, 0))
    # step every 15 minutes
    assert _appmod.cron_matches("*/15 * * * *", datetime(2026, 6, 24, 9, 45))
    assert not _appmod.cron_matches("*/15 * * * *", datetime(2026, 6, 24, 9, 46))
    # range with step on hours
    assert _appmod.cron_matches("0 0-12/6 * * *", datetime(2026, 6, 24, 6, 0))
    assert not _appmod.cron_matches("0 0-12/6 * * *", datetime(2026, 6, 24, 7, 0))


def test_cron_matches_day_of_week():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from datetime import datetime

    # 2026-06-22 is a Monday (dow 1; cron 0=Sun..6=Sat). 2026-06-21 is Sunday (0).
    expr = "0 9 * * 1"  # Mondays at 09:00
    assert _appmod.cron_matches(expr, datetime(2026, 6, 22, 9, 0))  # Monday
    assert not _appmod.cron_matches(expr, datetime(2026, 6, 21, 9, 0))  # Sunday
    # Sunday matches both 0 and 7.
    assert _appmod.cron_matches("0 9 * * 0", datetime(2026, 6, 21, 9, 0))
    assert _appmod.cron_matches("0 9 * * 7", datetime(2026, 6, 21, 9, 0))


def test_cron_matches_month_and_dom():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from datetime import datetime

    expr = "0 0 1 1 *"  # midnight on Jan 1
    assert _appmod.cron_matches(expr, datetime(2026, 1, 1, 0, 0))
    assert not _appmod.cron_matches(expr, datetime(2026, 2, 1, 0, 0))
    assert not _appmod.cron_matches(expr, datetime(2026, 1, 2, 0, 0))


def test_cron_matches_invalid_expr_never_matches():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from datetime import datetime

    # Wrong field count / garbage -> safe False, never a crash.
    assert not _appmod.cron_matches("not a cron", datetime(2026, 6, 24, 9, 0))
    assert not _appmod.cron_matches("* * *", datetime(2026, 6, 24, 9, 0))
    assert not _appmod.cron_matches("99 * * * *", datetime(2026, 6, 24, 9, 0))


# --- control phrase: !cron add/list/remove/off/on ---


def test_control_phrase_cron_add_then_list(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    say = _fake_say()
    handled = _appmod._handle_control_phrase(
        _CONTROL_AGENT,
        '!cron add "0 9 * * *" run the standup',
        "T1",
        say,
        channel_id="C1",
    )
    assert handled is True
    stored = claude_runner.list_crons(path=crons)
    assert len(stored) == 1
    assert stored[0]["schedule"] == "0 9 * * *"
    assert stored[0]["prompt"] == "run the standup"
    assert stored[0]["agent"] == "aristotle"
    assert stored[0]["channel"] == "C1"
    assert stored[0]["thread_ts"] == "T1"
    # The confirmation echoes the new id + schedule.
    assert stored[0]["id"] in say.posts[0]["text"]

    # !cron list shows it.
    say2 = _fake_say()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!cron list", "T1", say2)
    assert "0 9 * * *" in say2.posts[0]["text"]
    assert stored[0]["id"] in say2.posts[0]["text"]


def test_control_phrase_cron_list_empty(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    say = _fake_say()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!cron list", "T1", say)
    assert "no" in say.posts[0]["text"].lower()  # "no crons" / "none"


def test_control_phrase_cron_add_bad_usage(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    say = _fake_say()
    # Missing the quoted schedule -> usage line, nothing stored.
    _appmod._handle_control_phrase(
        _CONTROL_AGENT, "!cron add do something", "T1", say, channel_id="C1"
    )
    assert "usage" in say.posts[0]["text"].lower()
    assert claude_runner.list_crons(path=crons) == []


def test_control_phrase_cron_remove(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    claude_runner.add_cron(
        "0 9 * * *", "aristotle", "C1", "T1", "p", cron_id="rm1", path=crons
    )
    say = _fake_say()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!cron remove rm1", "T1", say)
    assert claude_runner.list_crons(path=crons) == []
    assert "rm1" in say.posts[0]["text"]
    # Removing an unknown id reports not-found, no crash.
    say2 = _fake_say()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!cron remove nope", "T1", say2)
    assert "not" in say2.posts[0]["text"].lower()


def test_control_phrase_cron_off_then_on(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    claude_runner.add_cron(
        "0 9 * * *", "aristotle", "C1", "T1", "p", cron_id="t1", path=crons
    )
    say = _fake_say()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!cron off t1", "T1", say)
    assert claude_runner.list_crons(path=crons)[0]["enabled"] is False
    say2 = _fake_say()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!cron on t1", "T1", say2)
    assert claude_runner.list_crons(path=crons)[0]["enabled"] is True


def test_control_phrase_cron_unknown_subcommand(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    say = _fake_say()
    _appmod._handle_control_phrase(_CONTROL_AGENT, "!cron frobnicate", "T1", say)
    assert "usage" in say.posts[0]["text"].lower()


# --- the fire path: a matching cron synthesizes a run via _run_and_update ---


def test_scheduler_tick_fires_matching_cron_via_run_seam(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from datetime import datetime

    crons = str(tmp_path / "crons.json")
    monkeypatch.setattr(claude_runner, "_crons_path", lambda: crons)
    claude_runner.add_cron(
        "30 9 * * *", "aristotle", "C9", "T9", "do it", cron_id="f1", path=crons
    )
    # A disabled cron at the same minute must NOT fire.
    claude_runner.add_cron(
        "30 9 * * *", "aristotle", "C9", "T9", "skip", cron_id="off1", path=crons
    )
    claude_runner.set_cron_enabled("off1", False, path=crons)
    # A non-matching cron must NOT fire.
    claude_runner.add_cron(
        "0 0 * * *", "aristotle", "C9", "T9", "later", cron_id="nomatch", path=crons
    )

    fired = []
    monkeypatch.setattr(
        _appmod,
        "_fire_cron",
        lambda entry, live: fired.append(entry["id"]),
    )

    n = _appmod._scheduler_tick({}, now=datetime(2026, 6, 24, 9, 30))
    assert fired == ["f1"]
    assert n == 1
    # A non-matching minute fires nothing.
    fired.clear()
    n2 = _appmod._scheduler_tick({}, now=datetime(2026, 6, 24, 9, 31))
    assert fired == []
    assert n2 == 0


def test_fire_cron_runs_agent_in_target_thread(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # _fire_cron resolves the agent from the registry, posts a placeholder into the
    # cron's channel/thread, and synthesizes a run via _run_and_update with the
    # cron's agent + prompt + thread. We stub _run_and_update and the live client.
    entry = {
        "id": "f1",
        "schedule": "* * * * *",
        "agent": "aristotle",
        "channel": "C9",
        "thread_ts": "T9",
        "prompt": "do the thing",
        "enabled": True,
    }
    captured = {}

    class _Client:
        def chat_postMessage(self, channel=None, thread_ts=None, text=None):
            captured["placeholder"] = {
                "channel": channel,
                "thread_ts": thread_ts,
                "text": text,
            }
            return {"ts": "ph-ts"}

    class _App:
        client = _Client()

    class _Handler:
        app = _App()

    live = {"aristotle": {"handler": _Handler()}}

    def _fake_run_and_update(client, channel, placeholder_ts, agent, prompt, thread_ts):
        captured["run"] = {
            "channel": channel,
            "placeholder_ts": placeholder_ts,
            "agent_name": agent["name"],
            "prompt": prompt,
            "thread_ts": thread_ts,
        }

    monkeypatch.setattr(_appmod, "_run_and_update", _fake_run_and_update)
    _appmod._fire_cron(entry, live)

    assert captured["placeholder"]["channel"] == "C9"
    assert captured["placeholder"]["thread_ts"] == "T9"
    run = captured["run"]
    assert run["channel"] == "C9"
    assert run["thread_ts"] == "T9"
    assert run["placeholder_ts"] == "ph-ts"
    assert run["agent_name"] == "aristotle"
    assert run["prompt"] == "do the thing"


def test_fire_cron_unknown_agent_is_noop(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    entry = {
        "id": "f1",
        "agent": "ghost",  # not in REGISTRY / not live
        "channel": "C9",
        "thread_ts": "T9",
        "prompt": "x",
        "enabled": True,
    }
    called = []
    monkeypatch.setattr(_appmod, "_run_and_update", lambda *a, **k: called.append(True))
    # No live handler for "ghost" -> _fire_cron must not raise and must not run.
    _appmod._fire_cron(entry, {})
    assert called == []


def test_scheduler_loop_ticks_once_with_injected_clock(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    from datetime import datetime

    # _scheduler_loop(live, _once=True) runs a single tick using the injected now()
    # and never sleeps in the test (the sleep seam is stubbed / the once path skips).
    ticks = []
    monkeypatch.setattr(_appmod, "_scheduler_tick", lambda live, now: ticks.append(now))
    fixed = datetime(2026, 6, 24, 9, 30)
    _appmod._scheduler_loop({}, now=lambda: fixed, _once=True)
    assert ticks == [fixed]


# ---------------------------------------------------------------------------
# Streaming output (both backends). Default ON (STREAM_OUTPUT unset/truthy); the
# tests above pin STREAM_OUTPUT="0" via conftest, so these re-enable it. Claude
# switches argv to --output-format stream-json --include-partial-messages
# --verbose and reads JSONL deltas; codex keeps its argv (already --json) but
# consumes stdout incrementally while still reading the -o file for the final
# reply. All subprocess I/O is mocked (subprocess.Popen here, since streaming uses
# Popen, vs subprocess.run on the legacy path). No real CLI/Slack/network calls.
# ---------------------------------------------------------------------------


class _FakePopen:
    """Hermetic stand-in for subprocess.Popen for the streaming runner paths.

    `stdout_lines` is the JSONL the CLI would emit (one event per element, no
    trailing newlines needed). The instance is iterable-by-line via .stdout, has a
    readable .stderr, and reports the given returncode after the stream is drained.
    No process, no threads, no network. If `out_file_writer` is given it is called
    with the argv on construction (so a codex fake can write the -o file), mirroring
    how the real codex writes its last-message file during the run.
    """

    def __init__(self, stdout_lines, returncode=0, stderr="", out_file_writer=None):
        self._lines = list(stdout_lines)
        self.returncode = returncode
        self._stderr_text = stderr
        self.stdout = iter(line + "\n" for line in self._lines)
        self.stderr = io.StringIO(stderr)
        self._waited = False
        self._out_file_writer = out_file_writer

    def wait(self, timeout=None):
        self._waited = True
        return self.returncode

    def poll(self):
        # Report finished once wait() has run (the runner calls wait() after
        # draining stdout); before that, None would mean "still running".
        return self.returncode if self._waited else self.returncode

    def kill(self):
        pass


def _fake_popen_factory(stdout_lines, returncode=0, stderr="", writes_to_o=None):
    """Build a subprocess.Popen replacement returning a _FakePopen.

    `writes_to_o`, when set, is the reply text written to the argv's -o file (used
    by the codex streaming tests, whose authoritative reply still comes from -o).
    """

    def _factory(argv, **kwargs):
        if writes_to_o is not None and "-o" in argv:
            out_path = argv[argv.index("-o") + 1]
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(writes_to_o)
        return _FakePopen(stdout_lines, returncode=returncode, stderr=stderr)

    return _factory


def _claude_stream_lines(text_chunks, *, session_id=SID, result=None, **result_extra):
    """JSONL events a streaming claude run emits: a system init, the text deltas,
    then the terminal `result` event (same shape as the non-stream JSON blob).
    `result` defaults to the concatenation of the chunks (the real CLI's final
    text == the streamed text). `result_extra` injects usage/cost/etc fields.
    """
    if result is None:
        result = "".join(text_chunks)
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": session_id})
    ]
    for chunk in text_chunks:
        lines.append(
            json.dumps(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                }
            )
        )
    result_event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": session_id,
        "result": result,
    }
    result_event.update(result_extra)
    lines.append(json.dumps(result_event))
    return lines


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


class _FakeClient:
    """Capturing stand-in for Slack's client: records every chat_update call."""

    def __init__(self):
        self.updates = []

    def chat_update(self, channel=None, ts=None, text=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text})
        return {"ok": True}


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


# ---------------------------------------------------------------------------
# app.py file attachments: inbound download (url_private -> local path appended to
# the prompt) and outbound upload (files produced in a designated workdir uploaded
# back into the thread). All HTTP + Slack I/O is mocked: the download seam
# (_http_get_bytes) is patched and a fake client captures files_upload_v2; NO real
# network/Slack call is ever made. src.app imported via the _HAVE_APP guard.
# ---------------------------------------------------------------------------


class _FakeFileClient:
    """Capturing stand-in for Slack's client for the attachment tests.

    Carries a .token (used by the inbound downloader for the Bearer header) and a
    files_upload_v2 that records every call. boom=True makes uploads raise so the
    swallow-error path is exercisable.
    """

    def __init__(self, token="xoxb-test", boom=False):
        self.token = token
        self.uploads = []
        self.boom = boom

    def files_upload_v2(self, channel=None, thread_ts=None, file=None, filename=None):
        if self.boom:
            raise RuntimeError("upload failed")
        self.uploads.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "file": file,
                "filename": filename,
            }
        )
        return {"ok": True}


_FILE_AGENT = {
    "name": "aristotle",
    "display_name": "Aristotle",
    "backend": "claude",
    "claude_agent": "unarylab-research:research_manager",
}


def test_download_attachments_appends_local_paths_to_prompt(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # Land downloads in tmp_path (no system-temp pollution); mock the HTTP seam.
    monkeypatch.setattr(_appmod, "_attachments_dir", lambda thread_ts: str(tmp_path))
    seen = {}

    def fake_get(url, token):
        seen["url"] = url
        seen["token"] = token
        return b"PNGDATA"

    monkeypatch.setattr(_appmod, "_http_get_bytes", fake_get)
    client = _FakeFileClient(token="xoxb-bot")
    files = [{"name": "diagram.png", "url_private": "https://files.slack.com/a.png"}]

    paths = _appmod._download_attachments(client, files, "T1")
    assert len(paths) == 1
    assert paths[0] == os.path.join(str(tmp_path), "diagram.png")
    # The bot token was used for the Bearer header, and the bytes were written.
    assert seen["token"] == "xoxb-bot"
    assert seen["url"] == "https://files.slack.com/a.png"
    with open(paths[0], "rb") as f:
        assert f.read() == b"PNGDATA"
    # The local path is appended to the prompt.
    prompt = _appmod._append_attachments("look at this", paths)
    assert prompt == "look at this\n\n[Attached files: " + paths[0] + "]"


def test_download_attachments_no_files_is_noop(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # Should never even touch the HTTP seam when there are no files.
    monkeypatch.setattr(
        _appmod,
        "_http_get_bytes",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")),
    )
    client = _FakeFileClient()
    assert _appmod._download_attachments(client, None, "T1") == []
    assert _appmod._download_attachments(client, [], "T1") == []
    # Prompt is byte-identical when there are no attachments.
    assert _appmod._append_attachments("hi", []) == "hi"


def test_download_attachments_skips_failed_download(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setattr(_appmod, "_attachments_dir", lambda thread_ts: str(tmp_path))

    def fake_get(url, token):
        if "bad" in url:
            raise OSError("network down")
        return b"GOOD"

    monkeypatch.setattr(_appmod, "_http_get_bytes", fake_get)
    client = _FakeFileClient()
    files = [
        {"name": "bad.png", "url_private": "https://files.slack.com/bad.png"},
        {"name": "good.png", "url_private": "https://files.slack.com/good.png"},
        {"name": "no-url.png"},  # no url_private at all -> skipped silently
    ]
    paths = _appmod._download_attachments(client, files, "T1")
    # Only the good download survives; the failed one and the URL-less one are dropped.
    assert paths == [os.path.join(str(tmp_path), "good.png")]


def test_attachments_dir_is_per_thread_and_created(tmp_path, monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # Redirect the system temp base so we don't pollute the real /tmp.
    monkeypatch.setattr(_appmod.tempfile, "gettempdir", lambda: str(tmp_path))
    d1 = _appmod._attachments_dir("T1")
    d2 = _appmod._attachments_dir("T2")
    assert os.path.isdir(d1) and os.path.isdir(d2)
    assert d1 != d2  # different threads -> different dirs


def test_maybe_upload_outputs_no_workdir_skips_upload(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # No get_workdir helper installed -> the outbound path is a no-op and
    # files_upload_v2 is NEVER called.
    monkeypatch.delattr(claude_runner, "get_workdir", raising=False)
    client = _FakeFileClient()
    count = _appmod._maybe_upload_outputs(client, "C1", "T1", _FILE_AGENT, since=0.0)
    assert count == 0
    assert client.uploads == []


def test_maybe_upload_outputs_uploads_produced_files(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    workdir = tmp_path / "wd"
    workdir.mkdir()
    produced = workdir / "result.txt"
    produced.write_text("generated output", encoding="utf-8")
    # Install a get_workdir helper that points at the workdir for this thread.
    monkeypatch.setattr(
        claude_runner, "get_workdir", lambda name, ts: str(workdir), raising=False
    )
    client = _FakeFileClient()
    # since=0 so the just-written file counts as produced during the run.
    count = _appmod._maybe_upload_outputs(client, "C1", "T1", _FILE_AGENT, since=0.0)
    assert count == 1
    assert len(client.uploads) == 1
    up = client.uploads[0]
    assert up["channel"] == "C1"
    assert up["thread_ts"] == "T1"
    assert up["file"] == str(produced)
    assert up["filename"] == "result.txt"


def test_files_modified_since_filters_by_mtime(tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    # Force mtimes around an explicit cutoff (no sleep, no real wall-clock read).
    os.utime(str(old), (100.0, 100.0))
    os.utime(str(new), (200.0, 200.0))
    found = _appmod._files_modified_since(str(tmp_path), since=150.0)
    assert found == [str(new)]  # only the file touched at/after the cutoff
    # A missing workdir is empty, never an error.
    assert _appmod._files_modified_since(str(tmp_path / "nope"), since=0.0) == []


def test_files_modified_since_skips_caches_and_dotfiles(tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # A real output file, a tool-cache dir with a file, and a dotfile -- all fresh.
    out = tmp_path / "result.txt"
    out.write_text("real", encoding="utf-8")
    cache = tmp_path / ".ruff_cache"
    cache.mkdir()
    (cache / "CACHEDIR.TAG").write_text("Signature", encoding="utf-8")
    dotfile = tmp_path / ".gitignore"
    dotfile.write_text("*.pyc", encoding="utf-8")
    for p in (out, cache / "CACHEDIR.TAG", dotfile):
        os.utime(str(p), (200.0, 200.0))
    found = _appmod._files_modified_since(str(tmp_path), since=0.0)
    assert found == [str(out)]  # cache dir + dotfiles excluded, only the real output


def test_upload_workdir_files_swallows_errors():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    client = _FakeFileClient(boom=True)  # every upload raises
    # Must not raise; returns 0 since nothing landed.
    count = _appmod._upload_workdir_files(client, "C1", "T1", ["/abs/a.png"])
    assert count == 0


# ---------------------------------------------------------------------------
# Placeholder quotes (src/slack/quotes.py): random_quote() loads a flat JSON array
# of strings from quotes.json (project-root-anchored, mtime-cached) and returns a
# random member, or "" when the file is missing/empty/invalid. Hermetic: each test
# redirects _QUOTES_PATH to a tmp file and resets the mtime cache so it controls
# the data (never depends on the real repo quotes.json contents or count).
# ---------------------------------------------------------------------------


def _set_quotes_file(monkeypatch, tmp_path, contents):
    """Point quotes._QUOTES_PATH at a tmp file with `contents` (or no file if None),
    reset the mtime cache, and return the quotes module. Keeps the loader hermetic.
    """
    from src.slack import quotes as quotes_mod

    qpath = str(tmp_path / "quotes.json")
    if contents is not None:
        with open(qpath, "w", encoding="utf-8") as f:
            f.write(contents)
    monkeypatch.setattr(quotes_mod, "_QUOTES_PATH", qpath)
    monkeypatch.setattr(quotes_mod, "_cache", None)  # reset mtime cache
    return quotes_mod


def test_random_quote_returns_member_of_list(monkeypatch, tmp_path):
    # With a populated quotes file, random_quote() returns one of its entries.
    entries = ["Work work.", "Ready to work.", "Yes, milord?"]
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, json.dumps(entries))
    for _ in range(20):  # several draws: every result must be a known member
        assert quotes_mod.random_quote() in entries


def test_random_quote_empty_when_missing_or_invalid(monkeypatch, tmp_path):
    # Missing file, empty array, empty file, non-list, and invalid JSON all yield ""
    # (the loader is graceful, so the caller falls back to the default placeholder).
    # Missing file (contents=None means no file is written).
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, None)
    assert quotes_mod.random_quote() == ""
    # Empty JSON array.
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, "[]")
    assert quotes_mod.random_quote() == ""
    # Completely empty file (invalid JSON).
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, "")
    assert quotes_mod.random_quote() == ""
    # Valid JSON but not a list.
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, '{"a": 1}')
    assert quotes_mod.random_quote() == ""
    # Malformed JSON.
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, "not json{")
    assert quotes_mod.random_quote() == ""


# ---------------------------------------------------------------------------
# handlers._handle placeholder text: the posted placeholder IS a random "peon"
# worker quote (random_quote()) when quotes.json has entries; an empty quote
# (no/invalid quotes file) falls back to the default "{display_name} is
# thinking...". Holds on both the @-mention and in-thread message paths. The slow
# worker is stubbed out (threading.Thread -> no-op) so no real CLI/Slack call happens.
# ---------------------------------------------------------------------------


class _NoopThread:
    """A drop-in for threading.Thread whose start() does nothing.

    _handle spawns a background worker after posting the placeholder; stubbing the
    thread keeps the test to just the placeholder logic (hermetic, no CLI run).
    """

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def _mention_event(text="<@U0BOT> hi", ts="111.001"):
    # A minimal user app_mention event with a UNIQUE ts so seen_before (the in-proc
    # dedup) never collides with another test's event id.
    return {"channel": "C1", "ts": ts, "text": text, "user": "U1"}


_HANDLE_AGENT = {
    "name": "aristotle",
    "display_name": "Aristotle",
    "backend": "claude",
    "claude_agent": "unarylab-research:research_manager",
}


def test_format_thread_history_includes_prior_visible_messages_only():
    from src.slack import handlers

    history = handlers._format_thread_history(
        [
            {"ts": "100.000", "user": "U1", "text": "<@UA> do the work"},
            {
                "ts": "100.100",
                "bot_profile": {"name": "Agent A"},
                "text": "result Y",
            },
            {"ts": "100.200", "user": "U2", "text": "<@UB> what did A say?"},
            {"ts": "100.300", "subtype": "message_deleted", "text": "gone"},
        ],
        current_ts="100.200",
    )

    assert "Visible Slack thread so far:" in history
    assert "- U1: <@UA> do the work" in history
    assert "- Agent A: result Y" in history
    assert "what did A say" not in history
    assert "gone" not in history


def test_handle_includes_prior_thread_history_in_prompt(monkeypatch):
    from src.slack import handlers

    captured = {}

    class _CaptureThread:
        def __init__(self, target=None, args=None, daemon=None):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            pass

    client = mock.Mock()
    client.conversations_replies.return_value = {
        "messages": [
            {"ts": "910.000", "user": "U1", "text": "<@UA> compute X"},
            {
                "ts": "910.100",
                "bot_profile": {"name": "Agent A"},
                "text": "X is 42",
            },
            {"ts": "910.200", "user": "U2", "text": "<@UB> what did A say?"},
        ]
    }
    monkeypatch.setattr(handlers.threading, "Thread", _CaptureThread)
    monkeypatch.setattr(handlers.quotes, "random_quote", lambda: "")

    event = _mention_event(text="<@U0BOT> what did A say?", ts="910.200")
    event["thread_ts"] = "910.000"
    say = _fake_say()
    handlers._handle(_HANDLE_AGENT, event, client, say)

    prompt = captured["args"][4]
    assert "Visible Slack thread so far:" in prompt
    assert "Agent A: X is 42" in prompt
    assert "Current request:\nwhat did A say?" in prompt
    assert prompt.count("what did A say?") == 1
    client.conversations_replies.assert_called_once_with(
        channel="C1",
        ts="910.000",
        limit=handlers._THREAD_HISTORY_LIMIT,
    )


def test_handle_placeholder_is_quote(monkeypatch):
    from src.slack import handlers

    monkeypatch.setattr(handlers.threading, "Thread", _NoopThread)
    monkeypatch.setattr(handlers.quotes, "random_quote", lambda: "Work work.")
    say = _fake_say()
    client = mock.Mock()
    handlers._handle(_HANDLE_AGENT, _mention_event(ts="900.001"), client, say)
    # The placeholder post (the only say call here) uses the quote verbatim.
    assert len(say.posts) == 1
    assert say.posts[0]["text"] == "Work work."


def test_handle_placeholder_falls_back_to_default_on_empty_quote(monkeypatch):
    from src.slack import handlers

    monkeypatch.setattr(handlers.threading, "Thread", _NoopThread)
    default = f"{_HANDLE_AGENT['display_name']} is thinking..."

    # Empty quote ("" = no/invalid quotes file): placeholder is the default text.
    monkeypatch.setattr(handlers.quotes, "random_quote", lambda: "")
    say = _fake_say()
    handlers._handle(_HANDLE_AGENT, _mention_event(ts="902.001"), mock.Mock(), say)
    assert len(say.posts) == 1
    assert say.posts[0]["text"] == default


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
    say = _fake_say()
    assert control._handle_control_phrase(_HANDLE_AGENT, "!stop", "t-x", say) is True
    assert "othing is running" in say.posts[-1]["text"]

    # A registered run -> signalled, ack posted, handled. Bare "ctrl-c" works too.
    tok = interrupt.register(_HANDLE_AGENT["name"], "t-y")
    say2 = _fake_say()
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


# ---------------------------------------------------------------------------
# Fallback runner: lets `python tests/test_runner.py` work without pytest installed.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    import os
    import traceback

    from pathlib import Path as _TmpPath  # real Path: supports .exists()/.write_text()

    # Mirror conftest's autouse fixture: pin the LEGACY non-stream path and the
    # footer-off default so the runner unit tests (which mock subprocess.run) and
    # the footer-free reply assertions are deterministic and shell-immune. Tests set
    # STREAM_OUTPUT/SHOW_USAGE via their own monkeypatch and undo back to this
    # baseline. (The no-pytest path does not load conftest.)
    os.environ["STREAM_OUTPUT"] = "0"
    os.environ["SHOW_USAGE"] = "0"

    class _MonkeyPatch:
        """Minimal stand-in for the pytest monkeypatch fixture.

        Supports setattr/delattr/setenv/delenv plus undo, enough for these tests.
        setattr/delattr accept the pytest `raising` kwarg and the _UNSET sentinel
        records a previously-absent attribute so undo deletes it (used to add/remove
        an optional helper like claude_runner.get_workdir).
        """

        _UNSET = object()

        def setattr(self, target, name, value, raising=True):
            if not hasattr(target, name):
                if raising:
                    raise AttributeError(name)
                self._undo.append(("attr", target, name, self._UNSET))
            else:
                self._undo.append(("attr", target, name, getattr(target, name)))
            setattr(target, name, value)

        def delattr(self, target, name, raising=True):
            if not hasattr(target, name):
                if raising:
                    raise AttributeError(name)
                return
            self._undo.append(("attr", target, name, getattr(target, name)))
            delattr(target, name)

        def __init__(self):
            self._undo = []

        def chdir(self, path):
            self._undo.append(("cwd", None, None, os.getcwd()))
            os.chdir(path)

        def setenv(self, name, value):
            self._undo.append(("env", None, name, os.environ.get(name, self._UNSET)))
            os.environ[name] = value

        def delenv(self, name, raising=True):
            if name not in os.environ:
                if raising:
                    raise KeyError(name)
                return
            self._undo.append(("env", None, name, os.environ[name]))
            del os.environ[name]

        def undo(self):
            for kind, target, name, old in reversed(self._undo):
                if kind == "attr":
                    if old is self._UNSET:
                        # The attribute did not exist before: remove what we added.
                        if hasattr(target, name):
                            delattr(target, name)
                    else:
                        setattr(target, name, old)
                elif kind == "cwd":
                    os.chdir(old)
                elif old is self._UNSET:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
            self._undo = []

    tests = [
        test_registry_contains_expected_agents,
        test_registry_loads_from_agents_json,
        test_resolve_reads_only_agents_json_field_else_default,
        test_registry_backends_resolve,
        test_get_runner_dispatches_by_backend,
        test_get_runner_unknown_backend_raises,
        test_token_env_names_per_agent,
        test_startable_agents_only_those_with_both_tokens,
        test_build_command_brunel_new_and_resume,
        test_build_command_aristotle_new_and_resume,
        test_build_command_cicero_has_no_agent_flag,
        test_build_command_invariants_for_all_agents,
        test_build_command_no_effort_when_unset,
        test_build_command_effort_from_agents_json_field_new_and_resume,
        test_build_command_effort_field_only,
        test_build_command_effort_ignores_legacy_env_var,
        test_build_command_model_default_when_unset,
        test_build_command_model_from_agents_json_field,
        test_build_command_model_ignores_legacy_env_var,
        test_build_command_overrides_model_and_effort,
        test_build_command_override_effort_only_leaves_model_default,
        test_build_command_overrides_none_or_empty_is_byte_identical,
        test_session_create_then_resume_same_id,
        test_sessions_are_independent_across_agent_and_thread,
        test_override_set_model_then_read_back,
        test_override_set_effort_merges_preserving_model,
        test_override_clear_removes_entry,
        test_overrides_independent_across_agent_and_thread,
        test_overrides_path_from_env_redirects_store,
        test_get_workdir_under_base_namespaced_and_created,
        test_get_workdir_independent_across_agent_and_thread,
        test_get_workdir_returns_absolute_path,
        test_get_workdir_default_base_is_under_home_projects,
        test_safe_token_collapses_dot_tokens_and_keeps_normal,
        test_seen_before_first_then_repeat,
        test_seen_before_distinct_ids_both_first_seen,
        test_run_claude_parses_result,
        test_run_claude_allows_empty_string_result,
        test_run_claude_raises_on_missing_result,
        test_run_claude_raises_on_is_error,
        test_run_claude_raises_on_nonzero_exit,
        test_run_claude_raises_on_malformed_json,
        test_run_claude_raises_on_timeout,
        test_codex_build_command_fresh,
        test_codex_build_command_resume,
        test_codex_build_command_model_gating,
        test_codex_build_command_model_ignores_legacy_env_var,
        test_codex_build_command_no_effort_when_unset,
        test_codex_build_command_effort_field_fresh_and_resume,
        test_codex_build_command_effort_ignores_legacy_env_var,
        test_codex_build_command_model_and_effort_field_fresh_and_resume,
        test_codex_build_command_overrides_model_and_effort,
        test_codex_build_command_overrides_none_or_empty_is_byte_identical,
        test_codex_build_command_profile_on_fresh,
        test_codex_build_command_profile_not_on_resume,
        test_codex_build_command_no_profile_when_unset,
        test_codex_build_command_dijkstra_uses_project_manager_profile,
        test_build_manifest_name_fields_scopes_events_and_socket,
        test_build_manifest_json_round_trip_offline,
        test_manifest_cli_prints_named_agent,
        test_manifest_write_creates_files,
        test_run_codex_fresh_captures_thread_id,
        test_run_codex_resume_returns_prior_id,
        test_run_codex_raises_on_nonzero_exit,
        test_run_codex_raises_on_empty_reply,
        test_run_codex_raises_on_missing_thread_id,
        test_run_codex_raises_on_timeout,
        test_codex_answer_fresh_then_resume,
        test_claude_answer_mints_uuid_when_no_prior,
        test_unified_seam_codex_stores_captured_thread_id,
        test_unified_seam_independent_across_agent_and_thread_both_backends,
        test_claude_meta_and_footer_1m_window,
        test_claude_meta_context_pct_200k_window,
        test_claude_meta_usage_omits_cache_fields_degrades_gracefully,
        test_codex_meta_tokens_no_cost_no_context_pct,
        test_format_usage_all_none_returns_empty,
        test_format_usage_token_formatting,
        test_usage_enabled_default_on_off_and_unset,
        test_load_env_dotenv_beats_shell,
        test_load_env_missing_file_is_noop,
        test_sessions_path_from_env_redirects_store,
        test_dotenv_sessions_path_wins_over_shell_via_main_import_order,
        test_reload_adds_new_startable_agent_and_leaves_others_untouched,
        test_reload_removes_now_unstartable_agent,
        test_reload_restarts_changed_agent_only,
        test_reload_invalid_json_keeps_live_set,
        test_agents_reload_mutates_registry_in_place,
        test_sighup_event_triggers_reconcile_loop_mechanism,
        test_unmentioned_thread_reply_requires_this_agents_session,
        test_control_phrase_set_model,
        test_control_phrase_set_effort_valid,
        test_control_phrase_set_effort_invalid_rejected,
        test_control_phrase_reset_clears_override,
        test_control_phrase_unknown_help,
        test_control_phrase_non_command_returns_false,
        test_run_and_update_always_injects_workdir,
        test_run_and_update_injects_identity_every_turn,
        test_cron_store_add_list_remove,
        test_cron_store_set_enabled_toggles,
        test_cron_store_id_autogenerated_and_unique,
        test_cron_store_path_from_sessions_env,
        test_cron_store_corrupt_file_yields_empty,
        test_cron_matches_wildcard_every_minute,
        test_cron_matches_specific_minute_hour,
        test_cron_matches_lists_ranges_and_steps,
        test_cron_matches_day_of_week,
        test_cron_matches_month_and_dom,
        test_cron_matches_invalid_expr_never_matches,
        test_control_phrase_cron_add_then_list,
        test_control_phrase_cron_list_empty,
        test_control_phrase_cron_add_bad_usage,
        test_control_phrase_cron_remove,
        test_control_phrase_cron_off_then_on,
        test_control_phrase_cron_unknown_subcommand,
        test_scheduler_tick_fires_matching_cron_via_run_seam,
        test_fire_cron_runs_agent_in_target_thread,
        test_fire_cron_unknown_agent_is_noop,
        test_scheduler_loop_ticks_once_with_injected_clock,
        test_build_command_stream_flags_claude,
        test_run_claude_streaming_partial_and_final,
        test_claude_answer_streaming_threads_on_update,
        test_run_claude_streaming_salvages_text_when_no_result_event,
        test_run_claude_streaming_raises_on_nonzero_exit,
        test_run_claude_streaming_ignores_thinking_deltas,
        test_run_claude_stream_disabled_keeps_legacy_argv_and_single_path,
        test_run_codex_streaming_partial_and_final,
        test_codex_answer_streaming_threads_on_update,
        test_run_codex_streaming_raises_on_nonzero_exit,
        test_codex_stream_disabled_uses_subprocess_run,
        test_agent_message_text_from_event_shapes,
        test_stream_updater_throttles_to_one_per_second,
        test_stream_updater_skips_empty_text,
        test_stream_updater_swallows_chat_update_errors,
        test_stream_throttle_never_drops_the_final_update,
        test_download_attachments_appends_local_paths_to_prompt,
        test_download_attachments_no_files_is_noop,
        test_download_attachments_skips_failed_download,
        test_attachments_dir_is_per_thread_and_created,
        test_maybe_upload_outputs_no_workdir_skips_upload,
        test_maybe_upload_outputs_uploads_produced_files,
        test_files_modified_since_filters_by_mtime,
        test_files_modified_since_skips_caches_and_dotfiles,
        test_upload_workdir_files_swallows_errors,
        test_random_quote_returns_member_of_list,
        test_random_quote_empty_when_missing_or_invalid,
        test_format_thread_history_includes_prior_visible_messages_only,
        test_handle_includes_prior_thread_history_in_prompt,
        test_handle_placeholder_is_quote,
        test_handle_placeholder_falls_back_to_default_on_empty_quote,
        test_is_interrupt_phrase,
        test_interrupt_token_request_sets_flag_and_signals,
        test_interrupt_registry_register_request_unregister,
        test_control_phrase_interrupt_dispatch,
        test_run_claude_streaming_settles_gracefully_on_interrupt,
        test_run_codex_streaming_settles_gracefully_on_interrupt,
    ]

    passed = 0
    failed = 0
    for t in tests:
        params = t.__code__.co_varnames[: t.__code__.co_argcount]
        # Build fixtures by NAME so a test can take any combination (e.g. both
        # tmp_path and monkeypatch), not just one. Mirrors pytest's by-name
        # fixture injection closely enough for these tests.
        tmpdir = mp = None
        try:
            kwargs = {}
            if "tmp_path" in params:
                tmpdir = tempfile.TemporaryDirectory()
                kwargs["tmp_path"] = _TmpPath(tmpdir.name)
            if "monkeypatch" in params:
                mp = _MonkeyPatch()
                kwargs["monkeypatch"] = mp
            t(**kwargs)
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
        finally:
            if mp is not None:
                mp.undo()
            if tmpdir is not None:
                tmpdir.cleanup()

    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
