"""Usage-footer helpers: the SHOW_USAGE toggle and the one-line footer renderer.

Leaf module (no cross-module deps): reads os.environ live and formats a runner's
meta dict.
"""

from __future__ import annotations

import os


def _usage_enabled():
    """Whether to append the usage footer. Read at CALL time from os.environ so a
    SIGHUP .env reload takes effect. Default ON: the footer shows unless SHOW_USAGE
    is explicitly an off-value ("0"/"false"/"no"/"off", case-insensitive). An
    unset/absent SHOW_USAGE (or any other value, e.g. "1"/"true"/"yes"/"on") leaves
    the footer enabled.
    """
    return os.environ.get("SHOW_USAGE", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _format_usage(meta):
    """Render the one-line usage footer from a runner meta dict, or "" if empty.

    meta is {"context_pct", "tokens", "cost_usd", "duration_s"}; each field is
    omitted when its value is None, so codex (no cost/context) shows fewer fields
    and an all-None meta yields "" (no footer appended). Field order:
    context_pct, tokens, cost_usd, duration_s. Separator is " · " (middot).

    Formats:
      - context_pct: whole-number percent, e.g. "4%"
      - tokens:      < 1000 -> the integer + " tok" (e.g. "950 tok");
                     >= 1000 -> one-decimal k (e.g. 12345 -> "12.3k tok")
      - cost_usd:    "$%.2f" (e.g. "$0.04")
      - duration_s:  whole seconds + "s" (e.g. "18s")
    """
    parts = []

    pct = meta.get("context_pct")
    if pct is not None:
        parts.append(f"{round(pct)}%")

    tokens = meta.get("tokens")
    if tokens is not None:
        if tokens < 1000:
            parts.append(f"{tokens} tok")
        else:
            parts.append(f"{tokens / 1000:.1f}k tok")

    cost = meta.get("cost_usd")
    if cost is not None:
        parts.append(f"${cost:.2f}")

    duration = meta.get("duration_s")
    if duration is not None:
        parts.append(f"{round(duration)}s")

    if not parts:
        return ""
    return "· " + " · ".join(parts)
