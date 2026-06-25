"""Always-on multi-agent Slack bots (Bolt + Socket Mode), ONE app per agent.

Topology: each agent in agents.REGISTRY is its OWN Slack app with its OWN bot +
app-level tokens. There is NO shared app and NO keyword routing. A user just
@-mentions that agent's bot directly in its own app; the whole de-mentioned
message text is the prompt. Each app's listener is bound to a FIXED agent.

ONE process still serves them all: this module iterates the registry, builds one
App + one SocketModeHandler per agent that has both of its tokens, and connects
each (every handler runs its own Socket Mode connection thread). The process
then blocks to stay alive. Partial startup is graceful: agents missing a token
are skipped with a warning, the rest still come up.

Per-agent tokens are sourced from env vars suffixed by the agent's uppercased
name (see agents.token_env_names): Brunel -> SLACK_BOT_TOKEN_BRUNEL / SLACK_APP_TOKEN_BRUNEL.

This is the ONLY module that imports slack_bolt, so agents.py and
claude_runner.py stay importable/testable without Slack installed. Importing
this module has NO side effects and needs NO tokens or network: all App
construction happens inside main().

Run always-on (loads .env if present):
    conda run -n peon python -m src
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time

from slack_bolt import App

from src import agents, store

from . import consent, handlers, scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("peon")

# Wall-clock seam for consent TTL/expiry. Module-level so tests inject a fake; the
# worker and the action handler read it so expiry is deterministic in tests.
_now = time.time

# Set by the SIGHUP handler, consumed by the main reload loop. Module-level so the
# (minimal) signal handler can flip it without touching any heavy state.
_reload_requested = threading.Event()


def _request_reload(signum, frame):  # noqa: ARG001 - signal handler signature
    """SIGHUP handler: do the MINIMUM. Just set the event; the main thread does the work."""
    _reload_requested.set()


def _has_existing_thread_session(agent, event):
    """Whether this unmentioned threaded message belongs to this agent already."""
    thread_ts = event.get("thread_ts")
    return bool(thread_ts and store.get_session(agent["name"], thread_ts))


def build_app_for(agent, bot_token):
    """Build one Slack App bound to a FIXED agent and register its listeners.

    Called only from main(); importing this module constructs no App and needs
    no tokens. Each App's app_mention handler is bound to this agent via closure,
    so there is no per-message routing.
    """
    app = App(token=bot_token)

    # This bot's own Slack user id, resolved once and cached per app. Used so a
    # mention-bearing in-thread reply (delivered as BOTH app_mention and
    # message.*) is owned by app_mention; the dedup guard is the backstop.
    cache = {}

    def bot_user_id():
        if "id" not in cache:
            try:
                cache["id"] = app.client.auth_test()["user_id"]
            except Exception:  # noqa: BLE001 - never let a lookup crash a handler
                logger.warning("could not resolve bot user id for %s", agent["name"])
                cache["id"] = None
        return cache["id"]

    @app.event("app_mention")
    def on_app_mention(event, client, say):
        handlers._handle(agent, event, client, say)

    @app.event("message")
    def on_message(event, client, say):
        # Only continue existing threaded conversations here; plain top-level
        # chatter is owned by app_mention. No thread_ts => nothing to continue.
        if not event.get("thread_ts"):
            return
        # A mention-bearing in-thread reply ALSO arrives as app_mention; let
        # app_mention own those so we never double-handle (dedup is the backstop).
        bot_id = bot_user_id()
        if bot_id and f"<@{bot_id}>" in (event.get("text") or ""):
            return
        if not _has_existing_thread_session(agent, event):
            return
        handlers._handle(agent, event, client, say)

    # Write-mode consent buttons. Socket Mode delivers these interactivity
    # payloads, so NO public request URL is needed; manifest interactivity must be
    # enabled (see manifest.py) for them to arrive after a reinstall. Both buttons
    # share one handler that dispatches on the action_id. ack() first (Slack's
    # interactivity deadline), then resolve the click via _handle_consent.
    @app.action(consent._WRITE_APPROVE_ACTION)
    def on_write_approve(ack, body, say):
        ack()
        consent._on_consent_action(agent, body, say)

    @app.action(consent._WRITE_DENY_ACTION)
    def on_write_deny(ack, body, say):
        ack()
        consent._on_consent_action(agent, body, say)

    return app


def _snapshot(agent):
    """A comparable fingerprint of everything that affects an agent's live connection:
    its full definition dict plus the two resolved token VALUES it connects with.
    A change in any of these means the handler must be rebuilt. Tokens are read from
    os.environ via token_env_names so a rotated token in .env triggers a restart.
    """
    bot_var, app_var = agents.token_env_names(agent["name"])
    return (
        json.dumps(agent, sort_keys=True),
        os.environ.get(bot_var),
        os.environ.get(app_var),
    )


def _start_handler(agent):
    """Build + connect ONE agent's App + SocketModeHandler and return (handler, snapshot).

    Mirrors the per-agent build in main() so startup and reload share one path.
    """
    from src import app as _appfacade

    bot_var, app_var = agents.token_env_names(agent["name"])
    app = _appfacade.build_app_for(agent, os.environ[bot_var])
    handler = _appfacade.SocketModeHandler(app, os.environ[app_var])
    handler.connect()
    return handler, _snapshot(agent)


def _stop_handler(name, entry):
    """Cleanly tear down one live handler. close() disconnects the websocket and shuts
    down the SDK's internal threads (it owns them; nothing for us to join). Guarded so a
    teardown error never aborts the reconcile.
    """
    try:
        entry["handler"].close()
    except Exception:  # noqa: BLE001 - a failed teardown must not abort reconcile
        logger.exception("error closing handler for %s", name)


def reconcile(live):
    """Re-read config + .env and reconcile the LIVE handlers to match, touching only the delta.

    Steps: (1) reload agents.json into agents.REGISTRY (validated; raises on bad config),
    (2) load_env(override=True) to refresh .env, (3) recompute startable_agents, (4) diff
    vs `live` and act ONLY on the delta:
      - startable now, not live    -> build + connect, record it.
      - live now absent/unstartable-> close its handler, drop it.
      - live but snapshot CHANGED  -> restart just that handler.
      - unchanged                  -> DO NOTHING (live conversation untouched).
    CRASH-SAFE: the whole thing is wrapped; if reload or any step throws, we log a clear
    warning and leave `live` exactly as it was (no handler dropped, process stays up).
    `live` is mutated in place. Returns True if reconcile ran, False if it was skipped.
    """
    from src.env import load_env

    try:
        # Validate + apply the fresh config FIRST. agents.reload() raises on a
        # missing/invalid agents.json and leaves REGISTRY untouched on failure.
        agents.reload()
        load_env(override=True)
        startable = {a["name"]: a for a in agents.startable_agents(os.environ)}
    except Exception:  # noqa: BLE001 - a bad reload must never drop live agents
        logger.warning(
            "reload failed; keeping the current live agents unchanged", exc_info=True
        )
        return False

    # From here on, individual handler ops are guarded so one failure doesn't strand the rest.
    # 1) Remove: live but no longer startable.
    for name in list(live):
        if name not in startable:
            logger.info("reload: stopping %s (no longer startable)", name)
            _stop_handler(name, live[name])
            del live[name]

    # 2) Add / restart.
    for name, agent in startable.items():
        snap = _snapshot(agent)
        if name not in live:
            try:
                handler, snap = _start_handler(agent)
                live[name] = {"handler": handler, "snapshot": snap}
                logger.info("reload: started %s", agent["display_name"])
            except Exception:  # noqa: BLE001
                logger.exception("reload: failed to start %s", name)
        elif live[name]["snapshot"] != snap:
            logger.info(
                "reload: restarting %s (definition or token changed)",
                agent["display_name"],
            )
            _stop_handler(name, live[name])
            try:
                handler, snap = _start_handler(agent)
                live[name] = {"handler": handler, "snapshot": snap}
            except Exception:  # noqa: BLE001
                logger.exception("reload: failed to restart %s; dropping it", name)
                del live[name]
        # else: unchanged -> do not touch the live connection.
    return True


def _reload_loop(live, *, _once=False):
    """Block the main thread, waking on each SIGHUP to reconcile the live handlers.

    This REPLACES the old `threading.Event().wait()` idle: it keeps the process alive
    while doing the heavy reload work in the main thread (never in the signal handler).
    `_once` is a test seam: it runs a single iteration so the loop is exercisable without
    spinning forever. Production calls it with the default (infinite) behavior.
    """
    from src import app as _appfacade

    while True:
        _reload_requested.wait()
        _reload_requested.clear()
        logger.info("SIGHUP received; reconciling agents")
        _appfacade.reconcile(live)
        if _once:
            break


def main():
    # Load .env (if present) so the per-agent token vars + CLAUDE_*/CODEX_* are
    # available. AUTHORITATIVE: override=True makes .env beat shell-exported vars.
    # Done here, not at import, so importing this module stays side-effect-free.
    # When launched via `python -m src`, __main__ has already loaded .env first
    # (so SESSIONS_PATH etc. are in effect before claude_runner imports); this
    # reload is idempotent and keeps `app.main()` self-sufficient if called
    # directly. dotenv is optional (load_env is a no-op without it).
    from src.env import load_env

    if not load_env():
        logger.info("python-dotenv not installed; relying on the ambient environment")

    startable = agents.startable_agents(os.environ)
    if not startable:
        wanted = []
        for agent in agents.REGISTRY:
            bot_var, app_var = agents.token_env_names(agent["name"])
            wanted.append(f"{bot_var} + {app_var}")
        logger.error(
            "No agent has both of its Slack tokens set, so there is nothing to "
            "run. Set a bot + app token pair for at least one agent: %s",
            "; ".join(wanted),
        )
        sys.exit(1)

    # Warn about (and skip) every registry agent that is NOT startable.
    startable_names = {a["name"] for a in startable}
    for agent in agents.REGISTRY:
        if agent["name"] not in startable_names:
            bot_var, app_var = agents.token_env_names(agent["name"])
            logger.warning(
                "skipping %s: missing %s/%s",
                agent["display_name"],
                bot_var,
                app_var,
            )

    # Build one App + SocketModeHandler per startable agent and connect each.
    # connect() is non-blocking and runs the Socket Mode connection in its own
    # thread, so N handlers share this one process. `live` keys each handler by
    # agent name with the snapshot it was built from, so SIGHUP reconcile can diff
    # against it (startup and reload share the _start_handler path).
    live = {}
    for agent in startable:
        handler, snap = _start_handler(agent)
        live[agent["name"]] = {"handler": handler, "snapshot": snap}
        logger.info("started Socket Mode connection for %s", agent["display_name"])

    logger.info(
        "peon running with %d agent(s): %s",
        len(startable),
        ", ".join(a["display_name"] for a in startable),
    )

    # Arm SIGHUP AFTER the handlers are up so a HUP during startup can't race the
    # build. `kill -HUP <pid>` (and `systemctl reload`, which sends SIGHUP) wakes
    # the loop below to re-read agents.json + .env and reconcile live connections.
    signal.signal(signal.SIGHUP, _request_reload)
    logger.info(
        "SIGHUP -> hot-reload armed (kill -HUP %d); reconciles agents.json + .env",
        os.getpid(),
    )

    # Start the Slack-native cron scheduler on its own daemon thread. It ticks once
    # a minute and fires any enabled crons.json entry whose 5-field schedule matches
    # the current minute, posting into the cron's target thread via the same run
    # seam as a live mention. It re-reads crons.json each tick, so a SIGHUP that
    # changed crons is picked up with no extra wiring. Shares the live handler set
    # so it can reach each agent's Slack client.
    scheduler_thread = threading.Thread(
        target=scheduler._scheduler_loop,
        args=(live,),
        daemon=True,
        name="cron-scheduler",
    )
    scheduler_thread.start()
    logger.info("cron scheduler armed (60s tick; reads crons.json)")

    # Block the main thread in the reload loop so the connection threads keep
    # running; each SIGHUP wakes it to reconcile. (Replaces the old idle wait.)
    _reload_loop(live)


if __name__ == "__main__":
    main()
