"""Worker-flavored "peon" placeholder quotes.

random_quote() returns a short, random acknowledgement line used as the
"thinking..." placeholder text for an @-mention (see src/slack/handlers.py).
The quotes live in a flat JSON array at quotes.json in the PROJECT ROOT (like
agents.json), so the operator can swap the whole list with a one-file edit and
no code change. The file is mtime-cached (reloaded only when it changes) and the
loader is graceful: a missing/empty/invalid file yields "" so the caller falls
back to the default placeholder. Never raises.
"""

from __future__ import annotations

import json
import os
import random

# Anchor to the PROJECT ROOT (NOT the package/subpackage dir), so quotes.json is
# read from the repo root regardless of the current working directory (so it
# works under launchd/systemd). This module lives in src/slack/, so the project
# root is THREE levels up from __file__ (src/slack/quotes.py -> src/slack ->
# src -> project root). Mirrors store.base's _PROJECT_ROOT anchoring.
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_QUOTES_PATH = os.path.join(_PROJECT_ROOT, "quotes.json")

# mtime-cache: (mtime, quotes_list). None until first load. Reloaded only when the
# file's mtime changes, so an operator edit is picked up without a restart.
_cache: tuple[float, list[str]] | None = None


def _load_quotes():
    """Load quotes.json (mtime-cached), returning a list of strings or [].

    Graceful by construction: a missing file, empty/invalid JSON, or a non-list /
    non-string-element payload yields [] rather than raising, so a bad or absent
    quotes file simply disables the feature (caller keeps the default placeholder).
    """
    global _cache
    try:
        mtime = os.path.getmtime(_QUOTES_PATH)
    except OSError:
        # Missing/unreadable file -> no quotes; clear any stale cache.
        _cache = None
        return []
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    try:
        with open(_QUOTES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        _cache = None
        return []
    if not isinstance(data, list):
        _cache = None
        return []
    quotes = [s for s in data if isinstance(s, str) and s.strip()]
    _cache = (mtime, quotes)
    return quotes


def random_quote():
    """Return a random quote, or "" if none are available.

    "" signals the caller to fall back to the default placeholder text, so the
    feature is purely additive and on whenever quotes.json has entries.
    """
    quotes = _load_quotes()
    if not quotes:
        return ""
    return random.choice(quotes)
