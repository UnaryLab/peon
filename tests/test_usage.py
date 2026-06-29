"""Usage meta + footer rendering and context-window percentage."""

import json
from unittest import mock

from src import agents
from src.manifest import build_manifest, write_manifests
from src.runners import claude_runner, codex_runner

from tests.helpers import (
    PROMPT,
    THREAD_ID,
    BRUNEL,
    DIJKSTRA,
    _fake_proc,
    _codex_proc_writing,
    _appmod,
    _HAVE_APP,
)


# ---------------------------------------------------------------------------
# Usage meta + footer: both runners surface {context_pct, tokens, cost_usd,
# duration_s} from the CLI's own output (NO extra CLI call, NO argv change), and
# app._format_usage renders it as a one-line footer. slack_bolt IS installed in
# this env, so we import src.app and assert _format_usage directly; if it ever is
# not, the import is guarded and only the footer-rendering assertion is skipped.
# ---------------------------------------------------------------------------


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
