"""Codex-only runner internals around the headless `codex` CLI.

Mirrors the claude runner's responsibilities, but for the Codex CLI, whose
session lifecycle differs: Codex MINTS its own session id (a `thread_id`) on a
fresh run. You cannot pre-set it (unlike claude's `--session-id`). So the flow is:
  - fresh run  : `codex exec ...`, then capture the thread_id from stdout and
                 persist it; the reply is written to the `-o` last-message file.
  - resume run : `codex exec resume <thread_id> ...` to continue that context.

Responsibilities (and ONLY these):
  - build_command(...) : pure, (agent, prompt, session_id, is_new) -> argv
  - run_codex(...)     : run the subprocess, read the reply from the `-o` file,
                         capture the minted thread_id (fresh run) from stdout,
                         and build the usage `meta` from the same JSONL stream
  - answer(...)        : the unified seam shared with the claude runner

`meta` is a plain dict with exactly the keys
  {"context_pct": int|None, "tokens": int|None, "cost_usd": float|None, "duration_s": float|None}
so it matches the claude runner's shape. For codex: context_pct is always None (the
context window is unknown, so we do not guess), cost_usd is None (codex reports
no cost), tokens come from token-usage events in the --json stream (no extra CLI
call, no argv change), and duration_s is measured here as wall-clock around the
subprocess. Any missing piece -> None, never a crash. app.py renders it as a
small one-line footer when SHOW_USAGE is on.

Nothing here imports slack_bolt, so the registry + runners stay importable and
testable without Slack installed. Keep Slack out of this module on purpose. The
public surface is re-exported by the `codex_runner` facade module.

Verified against codex-cli 0.141.0. The exact invocations below were confirmed
in a shell before being encoded here; do not re-derive them.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

from src import agents
from src.runners.common import safe_on_update

# Default timeout for a single codex run, in MINUTES. A run can take
# 10s..minutes. Read as minutes and converted to seconds (*60) at the call site
# for subprocess.run; default 2880 minutes (2 days).
DEFAULT_TIMEOUT_MIN = int(os.environ.get("CODEX_TIMEOUT_MIN", "2880"))


def _stream_enabled():
    """Whether to consume codex's JSONL stdout incrementally (post partial text as
    it arrives) vs. only reading the -o file at the end. Read LIVE from os.environ
    so a SIGHUP .env reload takes effect. DEFAULT ON: unless STREAM_OUTPUT is
    explicitly falsy ("0"/"false"/"no"/"off", case-insensitive). Mirrors the
    claude runner's switch and the same single env var, so STREAM_OUTPUT toggles
    both backends together. The argv is UNCHANGED either way (codex already emits
    JSONL via --json); only HOW stdout is read differs.
    """
    return os.environ.get("STREAM_OUTPUT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


# Model and reasoning effort come SOLELY from the agent's agents.json entry
# (resolved in build_command via agents.resolve). There is NO global env-var layer.
# Both fall back to "" (omit -m / omit the override) only if an entry is missing
# the field, in which case agents.resolve logs a warning; the shipped Codex entry
# sets both. We deliberately do NOT hardcode a default model name we cannot verify.
# Codex has no dedicated effort flag, so effort is a config override
# `-c model_reasoning_effort=...`. Values are passed through unvalidated; the codex
# CLI/model validates them.


class CodexRunError(Exception):
    """Raised when a codex run fails (nonzero exit, timeout, empty reply)."""


def _cwd_from_overrides(overrides):
    """The subprocess cwd for this run: the per-thread workdir, or None for the
    inherited process cwd. Returns overrides["_workdir"] when present (the worker
    always injects it), creating the dir on demand so codex can write into it.
    Mirrors claude_runner's helper.
    """
    if overrides and overrides.get("_workdir"):
        workdir = overrides["_workdir"]
        os.makedirs(workdir, exist_ok=True)
        return workdir
    return None


# ---------------------------------------------------------------------------
# Command building (pure, trivially testable)
# ---------------------------------------------------------------------------


def build_command(
    agent, prompt, session_id, is_new_session, last_message_file, overrides=None
):
    """Build the argv list for one headless codex run.

    Contract (empirically verified against codex-cli 0.141.0):

      FRESH run (is_new_session=True, no thread_id yet):
        codex exec --json --skip-git-repo-check -s danger-full-access
              -o <last_message_file> [-m <model>] "<prompt>"

      RESUME run (is_new_session=False, session_id is the stored thread_id):
        codex exec resume <session_id> --json --skip-git-repo-check
              -c sandbox_mode=danger-full-access
              -o <last_message_file> [-m <model>] "<prompt>"

    Every run is fully unsandboxed via danger-full-access (the fresh `-s` flag / the
    resume `-c sandbox_mode` value). The runner sets the subprocess cwd to the
    thread's workdir (overrides["_workdir"], injected by the worker) so files the
    run produces can be uploaded back; the run itself is unconfined.

    Notes:
      - `--skip-git-repo-check` is REQUIRED (this project is not a git repo).
      - The `resume` subcommand does NOT accept `-s/--sandbox` or `-a`; sandbox
        is set via `-c sandbox_mode=danger-full-access`. Do not TOML-quote this
        enum: codex treats `"danger-full-access"` as the literal variant name.
      - `-o/--output-last-message <file>`: the agent's final reply text is
        written to this file (read it for the reply).
      - `-m <model>` is added ONLY when model is non-empty (from agents.json
        "model"; omitted so Codex uses its configured default when the field is
        absent/empty).
      - reasoning effort: when eff (from agents.json "effort") is non-empty, add
        `-c model_reasoning_effort="<eff>"` (TOML-quoted value) on BOTH fresh and
        resume runs, before the prompt; omitted when empty. Not validated here;
        the codex CLI/model validates it.
      - Model/effort are resolved here via agents.resolve SOLELY from the agent's
        agents.json entry (its "model"/"effort" field, else a single code-level
        fallback). There is no global env-var layer: agents.json is the source of
        truth, so a deployment configures model/effort per agent there.
      - `overrides` (default None) is the per-thread override dict set from Slack
        control phrases: a non-empty "model"/"effort" in it REPLACES the
        agents.json resolution for that field, right after the resolve. A model
        override is a non-empty string, so it adds `-m <override>` even though
        the codex default omits -m. overrides=None (or {}) leaves the argv
        byte-identical to the no-override path.
      - The prompt is always last.
      - codex_profile persona (OPTIONAL, codex-only): when the agent's agents.json
        entry has a non-empty "codex_profile" (a profile NAME), `--profile <name>`
        is appended so codex layers `~/.codex/<name>.config.toml` (whose
        `developer_instructions` is the persona) on top of the base config. This is
        the codex analog of claude's `claude_agent`: both name an operator-installed
        persona. The flag is applied ONLY on the FRESH `codex exec` run, NOT on
        `codex exec resume`: the resume subcommand does not accept `--profile` (only
        the fresh form does, verified against codex-cli 0.142.0), and the resumed
        thread already carries the persona from turn one. Model/effort still come
        from agents.json via the -m/-c flags below, which override profile config.
      - Codex has no namespaced-subagent concept; codex entries omit claude_agent
        (Dijkstra is a plain general Codex run). `agent` is accepted for a
        uniform signature with claude_runner.
    """
    argv = ["codex", "exec"]

    # Every run is fully unsandboxed: danger-full-access (fresh -s flag, resume -c
    # sandbox_mode). cwd is the thread's workdir (overrides["_workdir"], set by the
    # worker) so files the run produces can be uploaded back.
    sandbox = "danger-full-access"

    if is_new_session:
        argv += ["--json", "--skip-git-repo-check", "-s", sandbox]
        # codex_profile persona (codex-only, optional): name an operator-installed
        # ~/.codex/<name>.config.toml profile. Applied on the FRESH path only:
        # `codex exec resume` does NOT accept --profile (verified against codex-cli
        # 0.142.0; only the fresh `codex exec` form does), and the resumed thread
        # already carries the persona from turn one. A missing profile makes codex
        # itself error, surfaced by run_codex's existing error handling.
        profile = agent.get("codex_profile")
        if profile:
            argv += ["--profile", profile]
    else:
        argv += [
            "resume",
            session_id,
            "--json",
            "--skip-git-repo-check",
            "-c",
            f"sandbox_mode={sandbox}",
        ]

    argv += ["-o", last_message_file]

    # Model: from agents.json "model"; omit -m when absent/empty. A per-thread
    # override REPLACES it (and, being non-empty, forces -m even where the codex
    # default would omit it). overrides=None leaves this byte-identical.
    model = agents.resolve(agent, "model")
    if overrides and overrides.get("model"):
        model = overrides["model"]
    if model:
        argv += ["-m", model]

    # Reasoning effort: from agents.json "effort"; omitted when absent/empty. A
    # per-thread override REPLACES it (when non-empty). Codex has no dedicated
    # flag; it is a `-c` config override rendered as a TOML string.
    effort = agents.resolve(agent, "effort")
    if overrides and overrides.get("effort"):
        effort = overrides["effort"]
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']

    argv += [prompt]
    return argv


# ---------------------------------------------------------------------------
# Running codex
# ---------------------------------------------------------------------------


def _thread_id_from_stdout(stdout):
    """Parse the minted thread_id from codex's JSONL stdout.

    The FIRST event on a fresh run is {"type":"thread.started","thread_id":...}.
    We scan the lines for the first object carrying a thread_id rather than
    assuming line 0 is well-formed, so a stray prefix line does not break us.
    """
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = event.get("thread_id")
        if thread_id:
            return thread_id
    return None


def _usage_dict_from_event(event):
    """Return the usage/token sub-dict carried by a codex JSONL event, or None.

    DEFENSIVE by design: we do NOT have a live codex to pin the exact schema, so
    we accept several shapes. An event qualifies if it either IS a usage event
    (its "type" contains "usage" or "token") or it nests a usage-like dict under
    one of the common keys. We return the first dict that looks like a token
    record (carries an *_tokens field). Returns None for non-usage events.
    """
    if not isinstance(event, dict):
        return None

    etype = str(event.get("type") or "")
    candidates = []
    # Common nesting keys seen across CLIs.
    for key in ("usage", "token_usage", "token_count", "tokens"):
        val = event.get(key)
        if isinstance(val, dict):
            candidates.append(val)
    # If the event type itself signals usage, the event MAY carry the counts flat.
    if "usage" in etype.lower() or "token" in etype.lower():
        candidates.append(event)

    for cand in candidates:
        if any(isinstance(v, int) and k.endswith("tokens") for k, v in cand.items()):
            return cand
    return None


def _tokens_from_stdout(stdout):
    """Sum input+output token counts from codex's JSONL stdout, or None.

    Scans EVERY line and, for each event that carries a usage/token structure
    (per _usage_dict_from_event), reads the LAST one seen (codex emits a running
    total, so the final usage event is the cumulative count for the turn). Sums
    the input-side + output-side token fields present. Returns None if no usage
    event is found, so a stream without token data degrades to "no tokens" rather
    than crashing. The exact event shape could not be verified against a live
    codex; this matches a realistic {"type":"turn.completed","usage":{...}} event
    and tolerates its absence.
    """
    last_usage = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = _usage_dict_from_event(event)
        if usage is not None:
            last_usage = usage

    if last_usage is None:
        return None

    token_fields = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_output_tokens",
    )
    present = [
        last_usage.get(f) for f in token_fields if isinstance(last_usage.get(f), int)
    ]
    return sum(present) if present else None


def _agent_message_text_from_event(event):
    """Return the agent's message text carried by a codex JSONL event, or None.

    DEFENSIVE by design (no live codex to pin the exact schema; codex hit its
    usage limit during verification). The `codex exec --json` stream wraps the
    agent's reply in item events. We accept the shapes seen in codex-cli's typed
    "thread/turn/item" event vocabulary (verified by string-dumping the 0.142.0
    binary): an item event (type "item.completed"/"item.updated"/"item.started")
    carrying an item whose item_type/type is "agent_message" with its text under
    "text" (or "content"/"message"). We also accept a flat agent-message event
    (type containing "agent_message") carrying the text directly. Returns the text
    string for such events, else None (so reasoning/tool/usage events are ignored).
    The -o last-message file remains the AUTHORITATIVE final reply; this only feeds
    incremental updates, so an unrecognized shape degrades to "no live updates",
    never a wrong or lost reply.
    """
    if not isinstance(event, dict):
        return None

    def _text_of(d):
        if not isinstance(d, dict):
            return None
        for key in ("text", "content", "message"):
            val = d.get(key)
            if isinstance(val, str) and val:
                return val
        return None

    etype = str(event.get("type") or "")

    item = event.get("item")
    if isinstance(item, dict):
        itype = str(item.get("item_type") or item.get("type") or "")
        if "agent_message" in itype:
            return _text_of(item)

    if "agent_message" in etype:
        return _text_of(event)

    return None


def _is_completed_item(event):
    """True if a codex JSONL event signals a COMPLETED (terminal) item.

    Codex's typed vocabulary marks the final form of an item with a "completed"
    event type (e.g. "item.completed"). We only ever consult this for events that
    already yielded agent-message text (see the streaming loop), so a plain
    type-contains-"completed" check is enough. Paired with that text, a completed
    item force-flushes the updater so the finished message shows in FULL even if
    the 1/sec throttle dropped the prior update right before a long quiet (tool)
    gap. Defensive (no live codex to pin the schema), mirroring
    _agent_message_text_from_event.
    """
    if not isinstance(event, dict):
        return False
    return "completed" in str(event.get("type") or "").lower()


def run_codex(
    agent,
    prompt,
    session_id,
    is_new_session,
    timeout=None,
    overrides=None,
    on_update=None,
    cancel=None,
):
    """Run one headless codex invocation; return (reply_text, session_id_to_store, meta).

    session_id_to_store:
      - fresh run  : the freshly-minted thread_id parsed from stdout (so the
                     caller can persist it for resumes).
      - resume run : the same `session_id` passed in.

    `meta` is {"context_pct", "tokens", "cost_usd", "duration_s"} to match
    claude_runner. For codex: context_pct and cost_usd are always None (window
    unknown, no cost field), tokens come from token-usage events in the --json
    stream, and duration_s is the wall-clock time measured around the subprocess
    here (no extra CLI call, no argv change). Any missing piece -> None.

    STREAMING (default, _stream_enabled()): codex ALREADY emits JSONL on stdout
    (the argv is identical), so we read it line-by-line and call
    on_update(partial_text) as agent-message text arrives, for incremental Slack
    updates. The `-o` last-message file is STILL read at the end as the
    AUTHORITATIVE final reply (the incremental text only drives live updates), so
    the final result is byte-identical to the non-streaming path. NON-STREAMING
    (STREAM_OUTPUT=0): the legacy path that reads all of stdout at once via
    subprocess.run and only the -o file for the reply; on_update is ignored.

    `on_update` (default None) is an optional cumulative-text callback used only on
    the streaming path. Any exception it raises is swallowed (a Slack hiccup must
    not abort the run).

    Raises CodexRunError on: timeout, nonzero exit, an empty reply, or (on a
    fresh run) a missing thread_id. The caller (app.py) catches this and posts a
    short error message into the Slack thread; it must never crash the process.

    The `-o` last-message file is created here (a temp file) and cleaned up
    afterward; the reply text is read from it.
    """
    if timeout is None:
        # DEFAULT_TIMEOUT_MIN is in minutes; subprocess.run wants seconds.
        timeout = DEFAULT_TIMEOUT_MIN * 60

    fd, last_message_file = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
    os.close(fd)
    try:
        argv = build_command(
            agent,
            prompt,
            session_id,
            is_new_session,
            last_message_file,
            overrides=overrides,
        )

        cwd = _cwd_from_overrides(overrides)
        started = time.monotonic()
        if _stream_enabled():
            stdout = _run_codex_streaming(
                argv, timeout, on_update, cwd=cwd, cancel=cancel
            )
        else:
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexRunError(f"codex run timed out after {timeout}s") from exc
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                raise CodexRunError(
                    f"codex exited with code {proc.returncode}: {stderr[:500]}"
                )
            stdout = proc.stdout
        duration_s = time.monotonic() - started

        try:
            with open(last_message_file, "r", encoding="utf-8") as f:
                reply = f.read().strip()
        except OSError as exc:
            raise CodexRunError(f"could not read codex reply file: {exc}") from exc

        if not reply and not (cancel is not None and cancel.requested):
            raise CodexRunError("codex produced an empty reply")

        meta = {
            "context_pct": None,  # codex context window is unknown; do not guess
            "tokens": _tokens_from_stdout(stdout),
            "cost_usd": None,  # codex reports no cost
            "duration_s": duration_s,
        }

        if is_new_session:
            thread_id = _thread_id_from_stdout(stdout)
            if not thread_id:
                raise CodexRunError("codex did not report a thread_id on the fresh run")
            return reply, thread_id, meta

        return reply, session_id, meta
    finally:
        try:
            os.remove(last_message_file)
        except OSError:
            pass


def _run_codex_streaming(argv, timeout, on_update, cwd=None, cancel=None):
    """Run codex via Popen, consuming JSONL stdout line-by-line; return the full stdout.

    Accumulates the agent's message text from item events (see
    _agent_message_text_from_event) and calls on_update(cumulative_text) as it
    grows, for incremental Slack updates. Returns the COMPLETE stdout string (all
    lines rejoined) so the caller's existing _thread_id_from_stdout /
    _tokens_from_stdout parsing is byte-for-byte the same as the non-stream path.
    The reply itself is read from the -o file by the caller; this only feeds live
    updates. Raises CodexRunError on timeout or nonzero exit. on_update may be None;
    any exception it raises is swallowed so a Slack hiccup never aborts the run.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    if cancel is not None:
        cancel.proc = proc  # let a !stop SIGINT this run; see common.Interrupt

    lines = []
    latest_text = None
    try:
        assert proc.stdout is not None  # PIPE is set above
        for line in proc.stdout:
            lines.append(line)
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            text = _agent_message_text_from_event(event)
            # Codex emits the agent message as a growing/whole item; take the
            # latest non-empty text seen so a later, more-complete item wins.
            if not text:
                continue
            completed = _is_completed_item(event)
            # Skip an unchanged NON-terminal re-emission; a completed item always
            # flushes (force=True) so the finished message shows in FULL even if the
            # throttle dropped the prior update before a long quiet (tool) gap. The
            # updater's own last-text dedup drops a truly redundant re-post.
            if text == latest_text and not completed:
                continue
            latest_text = text
            safe_on_update(on_update, text, force=completed)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise CodexRunError(f"codex run timed out after {timeout}s") from exc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if proc.returncode != 0:
        # User interrupt (!stop): return the partial stdout we gathered (so the
        # caller can still salvage the thread_id and keep the thread resumable)
        # instead of raising on the SIGINT-induced nonzero exit.
        if cancel is not None and cancel.requested:
            return "".join(lines)
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        raise CodexRunError(
            f"codex exited with code {proc.returncode}: {stderr.strip()[:500]}"
        )

    return "".join(lines)


# ---------------------------------------------------------------------------
# Unified seam (shared shape with the claude runner's answer)
# ---------------------------------------------------------------------------


def answer(
    agent,
    prompt,
    prior_session_id,
    timeout=None,
    overrides=None,
    on_update=None,
    cancel=None,
):
    """Unified runner entrypoint: (reply_text, session_id_to_store, meta).

    prior_session_id is the stored thread_id for this (agent, thread) or None on
    the first message. None -> fresh codex run (mints + returns a thread_id);
    otherwise -> resume that thread_id (and return it unchanged). The caller
    persists whatever session id this returns under its (agent, thread) key and
    can render the usage footer from `meta` (same shape as claude_runner).

    `overrides` (default None) is the per-thread model/effort override dict;
    threaded into build_command, where a non-empty field REPLACES the agents.json
    resolution. None leaves behavior/argv unchanged.

    `on_update` (default None) is an optional incremental-text callback threaded
    into run_codex; used only on the streaming path (the run-time default). None
    or the non-streaming path means a single final reply, behavior unchanged.
    """
    is_new_session = prior_session_id is None
    return run_codex(
        agent,
        prompt,
        prior_session_id,
        is_new_session,
        timeout=timeout,
        overrides=overrides,
        on_update=on_update,
        cancel=cancel,
    )
