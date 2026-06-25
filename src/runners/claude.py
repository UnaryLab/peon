"""Claude-only runner internals around the headless `claude` CLI.

Responsibilities (and ONLY these):
  - build_command(...)        : pure function, (agent, prompt, session_id, is_new) -> argv
  - run_claude(...)           : run the subprocess, parse JSON, return (text, meta) or raise
  - answer(...)               : the unified seam, returns (text, session_id, meta)

`meta` is a plain dict with exactly the keys
  {"context_pct": int|None, "tokens": int|None, "cost_usd": float|None, "duration_s": float|None}
derived from the same `--output-format json` blob run_claude already parses (no
extra CLI call, no argv change). Any missing field -> that value is None, never a
crash. app.py renders it as a small one-line footer when SHOW_USAGE is on.

Nothing here imports slack_bolt, so the registry + runner stay importable and
testable without Slack installed. Keep Slack out of this module on purpose. The
public surface is re-exported by the `claude_runner` facade module.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid

from src import agents

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default timeout for a single claude run, in MINUTES. A run can take
# 10s..minutes. This is the only env-driven knob here; model/effort are NOT (see
# the note below). Read as minutes and converted to seconds (*60) at the call site
# for subprocess.run; default 2880 minutes (2 days).
DEFAULT_TIMEOUT_MIN = int(os.environ.get("CLAUDE_TIMEOUT_MIN", "2880"))

# Model and reasoning effort come SOLELY from the agent's agents.json entry
# (resolved in build_command via agents.resolve). There is NO global env-var layer.
# Model carries one code-level fallback default, claude-opus-4-8[1m], used only if
# an entry is missing its "model" (agents.resolve logs a warning then); every
# shipped entry sets it, so the fallback should not fire and the default-path argv
# stays --model claude-opus-4-8[1m]. Effort falls back to "" (omit the flag).
# Values are passed through unvalidated; the claude CLI validates them.
_CLAUDE_MODEL_FALLBACK = "claude-opus-4-8[1m]"

# Context-window sizes (tokens) used as the denominator for the context-usage
# percentage in meta. A model id carrying the "[1m]" suffix (e.g.
# "claude-opus-4-8[1m]") has a 1,000,000-token window; otherwise 200,000. The
# model is resolved the SAME way build_command resolves it, so the percent
# reflects the model actually used.
_CONTEXT_WINDOW_1M = 1_000_000
_CONTEXT_WINDOW_DEFAULT = 200_000


def _stream_enabled():
    """Whether to use the incremental streaming output path. Read LIVE from
    os.environ (so a SIGHUP .env reload takes effect). DEFAULT ON: streaming is
    used unless STREAM_OUTPUT is explicitly falsy ("0"/"false"/"no"/"off",
    case-insensitive). STREAM_OUTPUT=0 forces the legacy single-JSON path with
    its original exact argv and one final update.
    """
    return os.environ.get("STREAM_OUTPUT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


class ClaudeRunError(Exception):
    """Raised when a claude run fails (nonzero exit, timeout, is_error, bad JSON)."""


# ---------------------------------------------------------------------------
# Command building (pure, trivially testable)
# ---------------------------------------------------------------------------


def build_command(
    agent, prompt, session_id, is_new_session, overrides=None, stream=False
):
    """Build the argv list for one headless claude run.

    Contract (empirically verified against claude CLI 2.1.187):
      base (stream=False): ["claude", "-p", "--output-format", "json"]
      base (stream=True):  ["claude", "-p", "--output-format", "stream-json",
                            "--include-partial-messages", "--verbose"]
      new session:        + ["--session-id", session_id]
      resume session:     + ["--resume", session_id]
      named agent:        + ["--agent", agent["claude_agent"]]  (None => omit)
      write mode (ON):    + ["--permission-mode", "acceptEdits",
                             "--add-dir", overrides["_workdir"]]
                            (only when overrides has write=True AND _workdir;
                            default OFF => omitted, argv byte-identical)
      model:              + ["--model", model]   (from agents.json "model", else
                            the claude-opus-4-8[1m] fallback; always present
                            since the default is non-empty)
      reasoning effort:   + ["--effort", eff]   (from agents.json "effort";
                            omitted when absent/empty; not validated here, the
                            CLI validates)
      prompt:             + [prompt]   (always last)

    `stream` (default False) selects the output format. The DEFAULT (stream=False)
    is the legacy single-JSON-blob path and its argv is byte-identical to the
    pre-streaming code (the run-time default is streaming ON; STREAM_OUTPUT=0
    forces stream=False). stream=True emits the streaming flags, verified against
    claude CLI 2.1.187: in `-p` mode `--output-format stream-json` REQUIRES
    `--verbose` (the CLI errors otherwise), and `--include-partial-messages`
    enables the incremental `content_block_delta` text deltas. The terminal
    `result` event in the stream has the SAME shape as the non-stream JSON blob
    (result/is_error/session_id/usage/total_cost_usd/duration_ms), so meta parsing
    is identical across both paths.

    Model/effort are resolved here via agents.resolve SOLELY from the agent's
    agents.json entry (its "model"/"effort" field, else a single code-level
    fallback). There is no global env-var layer: agents.json is the source of
    truth, so a deployment configures model/effort per agent there.

    `overrides` (default None) is the per-thread override dict set from Slack
    control phrases: a non-empty "model"/"effort" in it REPLACES the agents.json
    resolution for that field, applied right after the resolve. overrides=None
    (or {}) leaves the argv byte-identical to the no-override path. A non-empty
    overrides["write"] together with overrides["_workdir"] additionally enables
    the read-write tool surface (see the write-mode contract line above); both are
    required, so a write flag without a workdir stays read-only / byte-identical.

    --agent is added on BOTH new and resume runs. Resume was verified to work,
    and repeating --agent on resume is harmless/consistent (it just re-asserts
    the same persona), so we always include it when the agent has one. This
    avoids any chance of a resumed thread losing its persona.
    """
    if stream:
        argv = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
    else:
        argv = ["claude", "-p", "--output-format", "json"]

    if is_new_session:
        argv += ["--session-id", session_id]
    else:
        argv += ["--resume", session_id]

    if agent.get("claude_agent") is not None:
        argv += ["--agent", agent["claude_agent"]]

    # WRITE-MODE (read-write tool surface), default OFF. Only when the per-thread
    # override has write=True AND a confined workdir is present do we enable
    # non-interactive tool use: --permission-mode acceptEdits (the least-privilege
    # mode that lets -p run tools without prompting; NOT bypassPermissions) plus
    # --add-dir <workdir> so edits are confined to the thread's isolated dir (the
    # runner also sets cwd to it). This also unblocks claude's native sub-agent
    # (Task tool); we add NO tool-restriction flag, so Task stays available. With
    # write off (the default), or no workdir, NONE of these are added and the argv
    # is byte-identical to the read-only path.
    if overrides and overrides.get("write") and overrides.get("_workdir"):
        argv += ["--permission-mode", "acceptEdits", "--add-dir", overrides["_workdir"]]

    # Model: from agents.json "model"; fallback to the pinned default if absent.
    # A per-thread override REPLACES it. Always present because the default is
    # non-empty. (overrides=None leaves this byte-identical to the old path.)
    model = agents.resolve(agent, "model", _CLAUDE_MODEL_FALLBACK)
    if overrides and overrides.get("model"):
        model = overrides["model"]
    if model:
        argv += ["--model", model]

    # Reasoning effort: from agents.json "effort"; omitted when absent/empty. A
    # per-thread override REPLACES it (when the override is non-empty).
    effort = agents.resolve(agent, "effort")
    if overrides and overrides.get("effort"):
        effort = overrides["effort"]
    if effort:
        argv += ["--effort", effort]

    argv += [prompt]
    return argv


def _cwd_from_overrides(overrides):
    """The subprocess cwd for this run, or None to keep the default (process cwd).

    Returns the confined workdir ONLY when write-mode is on (overrides has
    write=True AND a non-empty _workdir); otherwise None, so the read-only path
    runs in the inherited cwd exactly as before (behavior byte-identical). The dir
    is created on demand so the CLI can write into it.
    """
    if overrides and overrides.get("write") and overrides.get("_workdir"):
        workdir = overrides["_workdir"]
        os.makedirs(workdir, exist_ok=True)
        return workdir
    return None


# ---------------------------------------------------------------------------
# Running claude
# ---------------------------------------------------------------------------


def _context_window_for(agent, overrides=None):
    """The context-window size (tokens) for this agent's resolved claude model.

    Resolve the model the SAME way build_command does (including a per-thread
    model override, so the usage-footer percentage tracks the model actually
    used), then pick 1M if the id carries the "[1m]" suffix (substring match),
    else the 200k default. This is the denominator for the context-usage
    percentage; it does NOT affect argv.
    """
    model = agents.resolve(agent, "model", _CLAUDE_MODEL_FALLBACK)
    if overrides and overrides.get("model"):
        model = overrides["model"]
    return _CONTEXT_WINDOW_1M if "[1m]" in (model or "") else _CONTEXT_WINDOW_DEFAULT


def _meta_from_payload(payload, agent, overrides=None):
    """Build the usage meta dict from a parsed claude --output-format json blob.

    Returns {"context_pct": int|None, "tokens": int|None, "cost_usd": float|None,
    "duration_s": float|None}. Defensive: any missing/unparseable field -> None
    for that piece, never a crash. No extra CLI call: the data is already in the
    JSON blob run_claude parses.

      - tokens: sum of the token-count fields present in payload["usage"]
        (input_tokens, output_tokens, cache_creation_input_tokens,
        cache_read_input_tokens). None if usage is absent or has no such field.
      - context_pct: (input-side context tokens this turn) / (model window) * 100,
        rounded to a whole number. The numerator sums the INPUT-side fields only
        (input_tokens + cache_creation_input_tokens + cache_read_input_tokens),
        since those approximate the prompt context occupied this turn (the output
        tokens are the new reply, not prior context). None if no input-side field
        is present.
      - cost_usd: payload["total_cost_usd"] as a float, else None.
      - duration_s: payload["duration_ms"] / 1000.0 as a float, else None.
    """
    meta: dict[str, float | int | None] = {
        "context_pct": None,
        "tokens": None,
        "cost_usd": None,
        "duration_s": None,
    }

    usage = payload.get("usage")
    if isinstance(usage, dict):
        token_fields = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        present = [usage.get(f) for f in token_fields if isinstance(usage.get(f), int)]
        if present:
            # None-safe sum: a missing field is absent here (the isinstance
            # filter drops non-ints), so an absent field contributes 0.
            meta["tokens"] = sum(v for v in present if v is not None)

        # Numerator for context_pct: the INPUT-side fields only (prompt context).
        input_fields = (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        input_present = [
            usage.get(f) for f in input_fields if isinstance(usage.get(f), int)
        ]
        if input_present:
            window = _context_window_for(agent, overrides)
            if window:
                numerator = sum(v for v in input_present if v is not None)
                meta["context_pct"] = round(numerator / window * 100)

    cost = payload.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        meta["cost_usd"] = float(cost)

    duration_ms = payload.get("duration_ms")
    if isinstance(duration_ms, (int, float)):
        meta["duration_s"] = duration_ms / 1000.0

    return meta


def _text_delta_from_stream_event(event):
    """Return the assistant TEXT delta carried by a claude stream_event, or None.

    The streaming format wraps Anthropic SSE events as
    {"type":"stream_event","event":{...}}. An incremental answer chunk is a
    content_block_delta whose delta is a text_delta:
      event["event"]["type"] == "content_block_delta"
      event["event"]["delta"]["type"] == "text_delta"  -> its "text" is the chunk
    We deliberately return ONLY text_delta chunks, not thinking_delta (reasoning),
    so the accumulated incremental text matches the final reply. None for anything
    else (system/assistant/init/thinking events, etc.).
    """
    if not isinstance(event, dict) or event.get("type") != "stream_event":
        return None
    inner = event.get("event")
    if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
        return None
    delta = inner.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) else None


def _run_claude_streaming(agent, argv, timeout, overrides, on_update, cancel=None):
    """Run claude with --output-format stream-json, consuming stdout line-by-line.

    Reads JSONL events as they arrive: accumulates text_delta chunks (calling
    on_update(accumulated) on each new chunk for incremental Slack updates) and
    captures the terminal `result` event, whose shape matches the non-stream JSON
    blob (so _meta_from_payload is reused unchanged). Returns (reply_text, meta).

    The AUTHORITATIVE reply is the `result` event's "result" field (complete and
    exact); the accumulated deltas drive only the incremental updates. If the
    stream carries no result event (a parse/format failure) but DID accumulate
    text, we fall back to the accumulated text so the reply is never lost; only a
    truly empty stream raises. Raises ClaudeRunError on timeout, nonzero exit,
    is_error, or an entirely empty stream. on_update may be None (no incremental
    updates); any exception it raises is swallowed so a Slack hiccup can never
    abort the run.

    ponytail: `timeout` bounds proc.wait() AFTER stdout closes, not the whole
    stream (a streaming read has no single subprocess.run timeout). A CLI that
    hangs mid-stream is rare and not worth a watchdog thread here; the legacy
    (STREAM_OUTPUT=0) path keeps the hard subprocess.run wall-clock bound.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=_cwd_from_overrides(overrides),
    )
    if cancel is not None:
        cancel.proc = proc  # let a !stop SIGINT this run; see common.Interrupt

    accumulated = []
    result_payload = None
    try:
        # Readline-loop over stdout. The CLI emits one JSON object per line.
        assert proc.stdout is not None  # PIPE is set above
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A stray non-JSON line (rare) is skipped, not fatal.
                continue
            if event.get("type") == "result":
                result_payload = event
                continue
            chunk = _text_delta_from_stream_event(event)
            if chunk:
                accumulated.append(chunk)
                if on_update is not None:
                    try:
                        on_update("".join(accumulated))
                    except Exception:  # noqa: BLE001 - a bad update must not abort the run
                        pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise ClaudeRunError(f"claude run timed out after {timeout}s") from exc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if proc.returncode != 0:
        # User interrupt (!stop): settle gracefully with the result payload if it
        # arrived, else the accumulated deltas (possibly ""), rather than raising.
        # The caller knows the session id up front, so the thread stays resumable.
        if cancel is not None and cancel.requested:
            if result_payload is not None and result_payload.get("result") is not None:
                return result_payload["result"], _meta_from_payload(
                    result_payload, agent, overrides
                )
            return "".join(accumulated), _meta_from_payload({}, agent, overrides)
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        raise ClaudeRunError(
            f"claude exited with code {proc.returncode}: {stderr.strip()[:500]}"
        )

    if result_payload is not None:
        if result_payload.get("is_error"):
            raise ClaudeRunError(
                f"claude reported an error: {result_payload.get('result', '<no detail>')}"
            )
        result = result_payload.get("result")
        if result is None:
            # Result event present but no text: fall back to deltas if we have them.
            if accumulated:
                return "".join(accumulated), _meta_from_payload(
                    result_payload, agent, overrides
                )
            raise ClaudeRunError("claude returned no result text")
        return result, _meta_from_payload(result_payload, agent, overrides)

    # No result event at all. Salvage the accumulated text rather than lose the
    # reply; only a totally empty stream is an error. Meta is empty (no payload).
    if accumulated:
        return "".join(accumulated), _meta_from_payload({}, agent, overrides)
    raise ClaudeRunError("claude produced empty output")


def run_claude(
    agent,
    prompt,
    session_id,
    is_new_session,
    timeout=None,
    overrides=None,
    on_update=None,
    cancel=None,
):
    """Run one headless claude invocation; return (reply_text, meta).

    `meta` is the usage dict built by _meta_from_payload from the claude result
    payload (the single JSON blob in the non-stream path, or the terminal `result`
    event in the stream path; both have the same shape, so no extra CLI call and
    no second parse):
    {"context_pct", "tokens", "cost_usd", "duration_s"}, any missing piece None.

    STREAMING (default, _stream_enabled()): runs with --output-format stream-json
    and reads stdout line-by-line, calling on_update(partial_text) as text deltas
    arrive (for incremental Slack updates) and returning the final result.
    NON-STREAMING (STREAM_OUTPUT=0): the legacy single-blob path with its ORIGINAL
    exact argv and one parse; on_update is ignored. The default-OFF argv is
    byte-identical to the pre-streaming code.

    `on_update` (default None) is an optional callback invoked with the cumulative
    reply text so far; only used on the streaming path. Any exception it raises is
    swallowed so a Slack hiccup never aborts the run.

    Raises ClaudeRunError on: timeout, nonzero exit, empty/malformed output, or a
    result with is_error true. The caller (app.py) catches this and posts a
    short error message into the Slack thread; it must never crash the process.
    """
    if timeout is None:
        # DEFAULT_TIMEOUT_MIN is in minutes; subprocess.run wants seconds.
        timeout = DEFAULT_TIMEOUT_MIN * 60

    stream = _stream_enabled()
    argv = build_command(
        agent, prompt, session_id, is_new_session, overrides=overrides, stream=stream
    )

    if stream:
        return _run_claude_streaming(
            agent, argv, timeout, overrides, on_update, cancel=cancel
        )

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_cwd_from_overrides(overrides),
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeRunError(f"claude run timed out after {timeout}s") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise ClaudeRunError(
            f"claude exited with code {proc.returncode}: {stderr[:500]}"
        )

    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise ClaudeRunError("claude produced empty output")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeRunError(f"could not parse claude JSON output: {exc}") from exc

    if payload.get("is_error"):
        raise ClaudeRunError(
            f"claude reported an error: {payload.get('result', '<no detail>')}"
        )

    # A missing result key (or null) is the error; a present-but-empty string is
    # a legitimate (if empty) reply and is passed through unchanged.
    result = payload.get("result")
    if result is None:
        raise ClaudeRunError("claude returned no result text")

    return result, _meta_from_payload(payload, agent, overrides)


# ---------------------------------------------------------------------------
# Unified seam (shared shape with codex.answer)
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

    prior_session_id is the stored session id for this (agent, thread) or None on
    the first message. Claude needs the id up front, so None -> mint a uuid and
    run a NEW session; otherwise -> --resume that id. Returns (reply, the id used,
    meta) so the caller persists whatever id was used under its (agent, thread)
    key and can render the usage footer. `meta` is the dict run_claude built
    (context_pct/tokens/cost_usd/duration_s; missing pieces None). This module
    does NOT touch the store here; persistence is the caller's job (symmetry with
    codex, where the id is only known after the run).

    `overrides` (default None) is the per-thread model/effort override dict; it
    is threaded into build_command, where a non-empty field REPLACES the
    agents.json resolution. None leaves behavior/argv unchanged.

    `on_update` (default None) is an optional incremental-text callback threaded
    into run_claude; used only on the streaming path (the run-time default). None
    or the non-streaming path means a single final reply, behavior unchanged.
    """
    if prior_session_id is None:
        session_id = str(uuid.uuid4())
        is_new_session = True
    else:
        session_id = prior_session_id
        is_new_session = False
    reply, meta = run_claude(
        agent,
        prompt,
        session_id,
        is_new_session,
        timeout=timeout,
        overrides=overrides,
        on_update=on_update,
        cancel=cancel,
    )
    return reply, session_id, meta
