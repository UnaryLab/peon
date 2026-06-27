"""Generate a Slack app manifest for an agent from its registry entry.

agents.json is the single source of truth; this module derives each agent's
Slack app manifest from it on demand (see `python -m src manifest`), so there are
no static per-agent manifest files to keep in sync.

The constants below (bot scopes, bot events, socket settings) are fixed and
shared by every agent; only the two name fields vary per agent, both set to the
agent's display_name. The key ordering (display_information, features,
oauth_config, settings) is fixed so json.dumps output is deterministic.

Imports nothing from slack_bolt, so it stays importable/testable without Slack
installed (and `python -m src manifest <name>` needs no tokens or network).
"""

import json
from pathlib import Path

# Bot OAuth scopes. app_mentions
# + chat:write to read mentions and reply; the *:history scopes so the bot can
# read threaded replies it should continue. files:read + files:write back the
# attachment feature: files:read to download inbound files via url_private,
# files:write to upload files the agent produces back into the thread. Adding
# these scopes requires the operator to reinstall/refresh each Slack app from the
# regenerated manifest to grant them.
_BOT_SCOPES = [
    "app_mentions:read",
    "chat:write",
    "channels:history",
    "groups:history",
    "im:history",
    "files:read",
    "files:write",
]

# Event subscriptions. app_mention plus message.* for in-thread
# follow-ups.
_BOT_EVENTS = [
    "app_mention",
    "message.channels",
    "message.groups",
    "message.im",
]


def build_manifest(agent):
    """Return the Slack app manifest (a dict) for one registry `agent`.

    display_information.name and features.bot_user.display_name are both the
    agent's display_name; every other field is a fixed constant shared by all the
    agents. Key ordering is fixed so a json.dumps of this is deterministic.
    """
    display_name = agent["display_name"]
    return {
        "display_information": {
            "name": display_name,
        },
        "features": {
            "bot_user": {
                "display_name": display_name,
                "always_online": True,
            }
        },
        "oauth_config": {
            "scopes": {
                "bot": list(_BOT_SCOPES),
            }
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": list(_BOT_EVENTS),
            },
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def write_manifests(agents_list, dest_dir):
    """Write manifest-<name>.json for each agent into dest_dir; return the Paths.

    Backs `python -m src manifest --write`. The files are derived from
    agents.json, so they can be regenerated any time and need not be tracked.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for agent in agents_list:
        path = dest / f"manifest-{agent['name']}.json"
        path.write_text(json.dumps(build_manifest(agent), indent=2) + "\n")
        paths.append(path)
    return paths
