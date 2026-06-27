"""FACADE for the Slack layer (the src/slack/ package).

The Slack-facing body lives across src/slack/{app, handlers, control, scheduler,
files, usage}.py. This thin facade re-exports the public surface so external
callers (src/__main__.py's `from .app import main`) and the test suite's
`app.<name>` references keep resolving unchanged.

Two patch-target categories drive what this facade must expose:

  - Kind A (shared singletons): `tempfile` and the `runners` PACKAGE are re-exposed
    here so a `monkeypatch.setattr(app.tempfile, ...)` / `app.runners.get_runner`
    mutates the SAME object the submodules import and call through. The submodules
    must import these by the same path (`import tempfile`, `from src import
    runners`) for the patch to be seen.

  - Kind B (facade-owned names): functions/attributes the tests patch by name on
    THIS module (e.g. `build_app_for`, `SocketModeHandler`, `_run_and_update`,
    `_scheduler_tick`, `_attachments_dir`, `_http_get_bytes`). Cross-module callers
    of these names resolve them THROUGH this facade (a lazy `from src import app`
    inside the function body) so a patch here is seen at their call sites.

Importing this facade imports the submodules; the submodules NEVER import this
facade at top level (only lazily, in-body) to avoid an import cycle.
"""

from __future__ import annotations

# Kind-A shared singletons: kept so tests can patch them here and have the patch
# seen by the submodules that import the SAME object.
import tempfile  # noqa: F401 - Kind A: tests patch app.tempfile.gettempdir

from slack_bolt.adapter.socket_mode import (  # noqa: F401 - Kind B: tests patch app.SocketModeHandler
    SocketModeHandler,
)

from . import runners  # noqa: F401 - Kind A: tests patch app.runners.get_runner

# --- Build/reconcile/main core (src/slack/app.py) ---------------------------
from .slack.app import (  # noqa: F401
    _has_existing_thread_session,
    _reload_loop,
    _reload_requested,
    _request_reload,
    _snapshot,
    _start_handler,
    _stop_handler,
    build_app_for,
    main,
    reconcile,
)

# --- Per-thread control phrases (src/slack/control.py) ----------------------
from .slack.control import (  # noqa: F401
    CONTROL_RE,
    VALID_EFFORTS,
    _ack_control_help,
    _ack_effective,
    _effective_config,
    _handle_control_phrase,
)

# --- File attachments: inbound download + outbound upload (src/slack/files.py)
from .slack.files import (  # noqa: F401
    _append_attachments,
    _attachments_dir,
    _download_attachments,
    _http_get_bytes,
    _maybe_upload_named,
    _parse_file_marker,
    _resolve_named_files,
    _strip_file_marker,
    _thread_workdir,
    _upload_workdir_files,
)

# --- Mention/message handling (src/slack/handlers.py) -----------------------
from .slack.handlers import (  # noqa: F401
    MENTION_RE,
    _STREAM_UPDATE_MIN_INTERVAL_S,
    _THREAD_HISTORY_LIMIT,
    _append_thread_history,
    _clean_prompt,
    _event_id,
    _fetch_thread_history,
    _format_thread_history,
    _handle,
    _message_author,
    _make_stream_updater,
    _run_and_update,
    _ts_before,
)

# --- Cron scheduling (src/slack/scheduler.py) -------------------------------
from .slack.scheduler import (  # noqa: F401
    _CRON_ADD_RE,
    _CRON_FIELD_RANGES,
    _SCHEDULER_TICK_SECONDS,
    _cron_expr_valid,
    _cron_field_values,
    _fire_cron,
    _handle_cron_command,
    _scheduler_loop,
    _scheduler_tick,
    cron_matches,
)

# --- Usage footer (src/slack/usage.py) --------------------------------------
from .slack.usage import (  # noqa: F401
    _format_usage,
    _usage_enabled,
)

# --- Placeholder quotes (src/slack/quotes.py) -------------------------------
from .slack.quotes import (  # noqa: F401
    random_quote,
)
