"""Agent registry: the single source of truth for which personas exist.

The registry is loaded at import from the declarative `agents.json` at the
project root. That file is the ONE place that defines the agents; nothing else
in the codebase hardcodes the persona list. Editing agents.json (plus creating
the matching Slack app + its two token env vars) is the whole "add an agent"
change: app.py iterates this registry to build one App + handler per agent, and
the runner is fully agent-agnostic.

Topology: ONE Slack app per agent. Each agent (Aristotle, Brunel, Cicero,
Dijkstra, ...) is its OWN Slack app, with its OWN bot + app-level tokens. There
is no shared app and no keyword routing: a user just @-mentions that agent's bot
directly in its own app and the whole de-mentioned message is the prompt.

Each agents.json entry is a JSON object with:
  - name:         lowercase internal id (e.g. "brunel"). Used for the session-key
                  prefix and to DERIVE this agent's token env-var names
                  (SLACK_BOT_TOKEN_BRUNEL / SLACK_APP_TOKEN_BRUNEL via token_env_names).
                  Never shown to Slack users. REQUIRED.
  - display_name: capitalized human-facing name shown in Slack replies (e.g.
                  "Brunel"). Use this for everything a Slack user sees. REQUIRED.
  - backend:      which CLI backs this agent: "claude" (default) or "codex".
                  The JSON is explicit, but code still reads it defensively with
                  agent.get("backend", "claude"). app.py dispatches on this via
                  runners.get_runner(...). REQUIRED in agents.json.
  - claude_agent: the namespaced claude CLI --agent value, or null for a
                  general/default run (no --agent flag). This is the ONLY thing
                  that distinguishes one persona's brain from another. Codex-
                  backed agents have no subagent concept, so the field is omitted there.
  - codex_profile: OPTIONAL, codex-only. The NAME of an operator-installed
                  `~/.codex/<name>.config.toml` profile whose `developer_instructions`
                  become the persona. It is the codex analog of claude_agent: both
                  name an operator-installed persona. codex_runner appends
                  `--profile <name>` so codex layers that profile on the fresh run.
                  Absent = a plain run. Model/effort still come from agents.json via
                  the CLI flags (which override profile config).
  - model:        the per-agent model. agents.json is the SINGLE source of truth
                  for it; every shipped entry sets it explicitly.
  - effort:       the per-agent reasoning effort. agents.json is the SINGLE
                  source of truth for it; every shipped entry sets it explicitly.

Per-agent model and reasoning effort come SOLELY from the "model" and "effort"
fields in agents.json. There is NO global env-var layer for them. `resolve`
below reads the agent's field and, only if it is missing/empty, returns a single
code-level fallback default (logging a warning, since every shipped entry sets
both fields, so the fallback should not fire). Defaults per field:
  - claude model:  agents.json "model"  else "claude-opus-4-8[1m]"
  - claude effort: agents.json "effort" else "" (omit the flag)
  - codex model:   agents.json "model"  else "" (omit -m)
  - codex effort:  agents.json "effort" else "" (omit the override)
Values are passed through unvalidated (the CLI validates).

This module imports nothing from slack_bolt or claude_runner so it stays
trivially importable (and testable) without Slack installed.
"""

import json
import logging
import os

_log = logging.getLogger(__name__)

# Required keys every agents.json entry must carry. claude_agent/model/effort are
# optional here: claude_agent defaults to "no --agent", and model/effort fall back
# to resolve()'s single code-level default (with a logged warning) if absent.
_REQUIRED_KEYS = ("name", "backend", "display_name")

# agents.json lives at the PROJECT ROOT (the parent of this src/ package dir),
# resolved from __file__ so it is found regardless of the current working
# directory.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENTS_JSON_PATH = os.path.join(_PROJECT_ROOT, "agents.json")


def _load_registry(path):
    """Load + validate the agent registry from a JSON file at `path`.

    Fails LOUDLY (raises) if the file is missing, is malformed JSON, is not a
    list, or any entry is not an object or lacks a required key. A clear message
    beats a silent partial-startup with a half-formed registry.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"agents.json not found at {path}: it is the source of truth for the "
            f"agent registry and must exist."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"agents.json at {path} is not valid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise RuntimeError(
            f"agents.json at {path} must be a JSON array of agent objects, got "
            f"{type(data).__name__}."
        )

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"agents.json entry #{i} must be a JSON object, got "
                f"{type(entry).__name__}."
            )
        missing = [k for k in _REQUIRED_KEYS if k not in entry]
        if missing:
            raise RuntimeError(
                f"agents.json entry #{i} ({entry.get('name', '<no name>')!r}) is "
                f"missing required key(s): {', '.join(missing)}."
            )
    return data


REGISTRY = _load_registry(_AGENTS_JSON_PATH)


def reload(path=None):
    """Re-read agents.json and mutate REGISTRY IN PLACE so existing references stay valid.

    Validates the fresh file via _load_registry FIRST (which raises RuntimeError on
    a missing/invalid/malformed registry). Only on success does it replace REGISTRY's
    contents in place (REGISTRY[:] = new), so any code holding a reference to the
    REGISTRY list object sees the new agents without rebinding. On any validation
    error the exception propagates and REGISTRY is left UNTOUCHED.

    `path` defaults to the module-level _AGENTS_JSON_PATH, read at CALL time (not a
    default-arg binding) so tests can monkeypatch _AGENTS_JSON_PATH and have reload()
    pick it up.
    """
    if path is None:
        path = _AGENTS_JSON_PATH
    fresh = _load_registry(
        path
    )  # raises on bad config; REGISTRY untouched if it raises
    REGISTRY[:] = fresh
    return REGISTRY


def resolve(agent, key, default=""):
    """Resolve a per-agent knob SOLELY from the agent's agents.json entry.

    Returns the agent dict's non-empty `key` value if set, else the single
    code-level `default`. agents.json is the source of truth for model/effort;
    there is no global env-var layer. Every shipped entry sets both "model" and
    "effort", so the fallback should not fire in practice; when it does (a
    missing/empty field), we LOG a warning so a half-configured entry is visible
    rather than silently defaulted. Used by both runners so resolution lives in
    ONE place. Values are unvalidated (the CLI validates).
    """
    value = agent.get(key)
    if value:
        return value
    _log.warning(
        "agents.json entry %r is missing %r; falling back to default %r. "
        "Set %r explicitly in agents.json (it is the source of truth).",
        agent.get("name", "<no name>"),
        key,
        default,
        key,
    )
    return default


def token_env_names(name):
    """Return (bot_token_env_var, app_token_env_var) for an agent's lowercase
    name. One Slack app per agent: each agent's tokens are sourced from env vars
    suffixed by its uppercased name, e.g. "brunel" -> ("SLACK_BOT_TOKEN_BRUNEL",
    "SLACK_APP_TOKEN_BRUNEL"). Pure function, no Slack needed.
    """
    suffix = name.upper()
    return f"SLACK_BOT_TOKEN_{suffix}", f"SLACK_APP_TOKEN_{suffix}"


def startable_agents(env):
    """Return the registry entries whose BOTH token env vars are present and
    non-empty in `env` (a dict-like mapping, e.g. os.environ).

    Used for graceful partial startup: an agent is only started if it actually
    has credentials, so the process comes up with whatever subset is configured.
    Pure function (env is injected) so it is testable without Slack or real
    tokens.
    """
    startable = []
    for agent in REGISTRY:
        bot_var, app_var = token_env_names(agent["name"])
        if env.get(bot_var) and env.get(app_var):
            startable.append(agent)
    return startable
