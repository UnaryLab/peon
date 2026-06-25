"""Entrypoint for `python -m src`.

Two modes, dispatched by a plain sys.argv check (leaner than argparse):

  python -m src
      Run the always-on process (one Slack App + handler per startable agent).
      app.main() is imported lazily INSIDE this branch so the manifest mode below
      stays Slack-free (no slack_bolt import, no tokens, no network).

  python -m src manifest <name>
      Print THAT agent's Slack app manifest as indented JSON to stdout.

  python -m src manifest
      Print a JSON ARRAY of EVERY agent's manifest. Choosing "all manifests" (vs.
      an error) makes the no-name form a useful one-shot dump of the whole fleet,
      which is handy when wiring up several Slack apps at once.

  python -m src manifest [<name>] --write
      Write the manifest(s) to ./manifests/manifest-<name>.json instead of
      printing (every agent, or just <name>). Files are regenerated from
      agents.json, so they need not be tracked.

Importing this module does NOT start anything: the work happens inside the
branches below, only when invoked as `python -m src`.
"""

import json
import sys
from pathlib import Path

# Load .env FIRST and authoritatively, before any module that reads env at
# import time (notably runners.claude_runner, imported later via .app inside
# main(), which resolves the session-store path from os.environ). override=True
# makes .env beat the shell. dotenv is optional so the offline `manifest`
# subcommand still works without it (load_env returns False and is a no-op).
from .env import load_env

load_env()

# These imports are intentionally AFTER load_env() so .env is authoritative
# before any module reads the environment; hence the E402 suppressions.
from . import agents  # noqa: E402 - must follow the authoritative load_env() above
from .manifest import build_manifest  # noqa: E402 - same: load .env first


def _find_agent(name):
    """Return the registry entry named `name`, or None if there is no such agent."""
    return next((a for a in agents.REGISTRY if a["name"] == name), None)


def _print_manifest(argv):
    """Handle `python -m src manifest [<name>] [--write]` (Slack-free: agents + manifest only).

    Without --write, prints manifest JSON to stdout (one object for a named
    agent, else a JSON array of all). With --write, materializes
    manifest-<name>.json files into ./manifests/ instead of printing.
    """
    write = "--write" in argv
    names = [a for a in argv if a != "--write"]

    # Resolve targets: a single named agent, or every agent.
    if names:
        agent = _find_agent(names[0])
        if agent is None:
            valid = ", ".join(a["name"] for a in agents.REGISTRY)
            print(
                f"error: unknown agent {names[0]!r}; valid names: {valid}",
                file=sys.stderr,
            )
            sys.exit(1)
        targets = [agent]
    else:
        targets = list(agents.REGISTRY)

    if write:
        from .manifest import write_manifests

        dest = Path(__file__).resolve().parent.parent / "manifests"
        for path in write_manifests(targets, dest):
            print(f"wrote {path}")
        return

    # Print mode (unchanged): single object for a named agent, else a JSON array.
    if names:
        print(json.dumps(build_manifest(targets[0]), indent=2))
    else:
        print(json.dumps([build_manifest(a) for a in targets], indent=2))


def main():
    args = sys.argv[1:]
    if args and args[0] == "manifest":
        _print_manifest(args[1:])
        return
    # No args (or anything else) => run the always-on bots. Import app lazily so
    # the manifest path never needs slack_bolt / tokens / network.
    from .app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
