"""Registry, get_runner backend dispatch, token env names, startable agents."""

from src import agents
from src.runners import claude_runner, codex_runner, get_runner


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
