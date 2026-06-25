"""peon: always-on multi-agent Slack bots, one Slack app per agent.

Importing this package has NO side effects and needs NO tokens or network.
Module layout:
  - agents.py        the registry (single source of truth) + token helpers
  - claude_runner.py Slack-agnostic runner (build_command, sessions, run_claude)
  - app.py           the Bolt/Socket Mode startup (the ONLY module importing slack_bolt)

agents and claude_runner stay importable without slack-bolt installed; only
app.py pulls in slack_bolt (and dotenv), and only inside main().

Intra-package imports are relative (e.g. `from . import agents`), so the package
directory name ("src") is not hardcoded in the code and a future rename is trivial.
"""
