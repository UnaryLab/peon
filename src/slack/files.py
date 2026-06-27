"""File attachments: inbound download + outbound upload.

Slack messages can carry files[] (uploads, images, diagrams). Inbound: each
file's url_private is downloaded with the bot token (a Bearer header) and its
local path appended to the prompt so the CLI can read it. Outbound: the run
uploads files ONLY when it ends its reply with a trailing `<<files: a, b>>`
marker NAMING the files (resolved inside the thread's workdir); no marker (the
default) uploads nothing, so an ordinary reply never dumps the workdir back.
Diagrams (image/svg) need no special case -- they are ordinary files. All
HTTP/Slack I/O goes through small seams so tests mock it; nothing here performs
real network I/O at import time.

The Kind-B seams (_attachments_dir, _http_get_bytes) are resolved THROUGH the app
facade inside _download_attachments so a test's monkeypatch on the facade is seen
by this module's call sites.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import urllib.request

from src import store
from src.runners import claude_runner

logger = logging.getLogger("peon")

# Dirs the outbound resolver must never descend into (tool caches, VCS, venvs).
# Pruned in-place during os.walk so their contents are never even stat'd.
_SKIP_DIRS = {
    ".ruff_cache",
    ".git",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ipynb_checkpoints",
    ".venv",
}

# Outbound delivery is opt-in: a run requests it by ENDING its reply with a
# `<<files: a, b>>` marker naming the files. Default (no marker) uploads nothing.
# _RE matches a complete marker (group 1 = the comma-separated names); _STRIP_RE
# removes from the first marker-open to end so a partial/unterminated marker
# still mid-stream (e.g. "<<files: pl") is also scrubbed from the shown reply.
_FILES_MARKER_RE = re.compile(r"<<\s*files\s*:\s*(.*?)>>", re.IGNORECASE | re.DOTALL)
_FILES_MARKER_STRIP_RE = re.compile(r"\s*<<\s*files\s*:.*", re.IGNORECASE | re.DOTALL)


def _strip_file_marker(text):
    """Remove any `<<files: ...>>` marker (complete or trailing/partial) from text."""
    if not text:
        return text
    return _FILES_MARKER_STRIP_RE.sub("", text)


def _parse_file_marker(text):
    """Split text into (clean_text, names): names from the LAST complete marker.

    No marker -> (text, []). The marker and everything after it is stripped from
    clean_text (the marker is emitted last, so nothing real follows it).
    """
    if not text:
        return text, []
    matches = _FILES_MARKER_RE.findall(text)
    names = []
    if matches:
        names = [n.strip() for n in matches[-1].split(",") if n.strip()]
    clean = _FILES_MARKER_STRIP_RE.sub("", text).rstrip()
    return clean, names


def _resolve_named_files(workdir, names):
    """Abs paths of the named files that exist INSIDE workdir (sorted, unique).

    Security boundary: a resolved path that escapes workdir (via `..`, an
    absolute name, or a symlink) is rejected. Each name is tried as a relative
    path first, then by basename via an os.walk that prunes _SKIP_DIRS/dot-dirs.
    A name that resolves nowhere under workdir is silently omitted.
    """
    if not workdir or not names or not os.path.isdir(workdir):
        return []
    base = os.path.realpath(workdir)
    found = []
    for name in names:
        cand = os.path.realpath(os.path.join(workdir, name))
        if cand.startswith(base + os.sep) and os.path.isfile(cand):
            found.append(cand)
            continue
        target = os.path.basename(name)
        for root, dirs, fnames in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            if target in fnames:
                hit = os.path.realpath(os.path.join(root, target))
                if hit.startswith(base + os.sep) and os.path.isfile(hit):
                    found.append(hit)
                break
    return sorted(set(found))


def _http_get_bytes(url, token):
    """Download `url` with the bot token and return the raw bytes.

    Uses stdlib urllib (no new dependency, ponytail): Slack's url_private requires
    an `Authorization: Bearer <bot token>` header. Factored out as the single HTTP
    seam so tests patch it (or urllib.request.urlopen) and never hit the network.
    """
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - Slack https url, header-auth
        return resp.read()


def _attachments_dir(thread_ts):
    """A per-thread temp directory for downloaded attachments (created if absent).

    Scoped by thread_ts so files from different threads never collide. Lives under
    the system temp dir; left in place (the OS reaps temp), not cleaned per-run, so
    a later message in the same thread can still reference an earlier file path.

    The thread_ts is sanitized via the SAME canonical helper the workdir path uses
    (store._safe_token), so the path-component sanitizing rule lives in ONE
    place. Byte-identical for any real Slack thread_ts (digits + dot).
    """
    safe = store._safe_token(thread_ts)
    path = os.path.join(tempfile.gettempdir(), "peon-files", safe)
    os.makedirs(path, exist_ok=True)
    return path


def _download_attachments(client, files, thread_ts):
    """Download each Slack file (by url_private) into the per-thread dir.

    `files` is the event's files[] list (Slack file dicts). For each file with a
    private URL we GET it with the bot token (client.token) and write it under the
    per-thread attachments dir, returning the list of local absolute paths in order.
    A per-file failure (missing URL, network/IO error) is logged and skipped so one
    bad file never blocks the message; an empty/None files list yields []. No real
    network call in tests: _http_get_bytes is the mocked seam.
    """
    from src import app as _appfacade

    if not files:
        return []
    token = getattr(client, "token", None)
    dest_dir = _appfacade._attachments_dir(thread_ts)
    paths = []
    for idx, f in enumerate(files):
        if not isinstance(f, dict):
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        # Prefer Slack's own name; fall back to id/index so we always have one.
        name = f.get("name") or f.get("id") or f"file-{idx}"
        name = os.path.basename(str(name)) or f"file-{idx}"
        local = os.path.join(dest_dir, name)
        try:
            data = _appfacade._http_get_bytes(url, token)
            with open(local, "wb") as out:
                out.write(data)
        except Exception:  # noqa: BLE001 - one bad file must not drop the message
            logger.warning("failed to download attachment %s", url)
            continue
        paths.append(local)
    return paths


def _append_attachments(prompt, paths):
    """Append the downloaded local paths to the prompt text, or return it unchanged.

    Adds one line "[Attached files: /abs/a.png, /abs/b.pdf]" so the CLI agent can
    open the files. No paths -> the prompt is returned byte-identical.
    """
    if not paths:
        return prompt
    return prompt + "\n\n[Attached files: " + ", ".join(paths) + "]"


def _thread_workdir(agent, thread_ts):
    """The per-thread workdir for this (agent, thread), or None.

    Uses the shared helper `claude_runner.get_workdir(agent_name, thread_ts)` (a
    PURE path lookup here: create defaults False, so this never spawns an empty
    dir; the outbound scan guards on os.path.isdir). Guarded so a helper that is
    absent or raises still yields None (never aborts the run).
    """
    helper = getattr(claude_runner, "get_workdir", None)
    if helper is None:
        return None
    try:
        return helper(agent["name"], thread_ts)
    except Exception:  # noqa: BLE001 - a misbehaving helper must not abort the run
        logger.warning("get_workdir failed for %s", agent["name"])
        return None


def _upload_workdir_files(client, channel, thread_ts, paths):
    """Upload each produced file back into the thread via files_upload_v2.

    One call per file (filename = basename). A per-file upload failure is logged
    and skipped so one bad upload never aborts the rest. Returns the count uploaded.
    """
    uploaded = 0
    for path in paths:
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=path,
                filename=os.path.basename(path),
            )
            uploaded += 1
        except Exception:  # noqa: BLE001 - one bad upload must not abort the rest
            logger.warning("failed to upload produced file %s", path)
    return uploaded


def _maybe_upload_named(client, channel, thread_ts, agent, names):
    """Upload the run-named files (from its `<<files: ...>>` marker) into the thread.

    No names (the default, no marker) -> a no-op returning 0 (files_upload_v2 is
    never called). Otherwise each name is resolved inside the thread's workdir
    (see _resolve_named_files; paths escaping the workdir are rejected) and the
    resolved files are uploaded. Guarded so an upload error never crashes the
    worker.
    """
    if not names:
        return 0
    workdir = _thread_workdir(agent, thread_ts)
    if not workdir:
        return 0
    produced = _resolve_named_files(workdir, names)
    if not produced:
        return 0
    return _upload_workdir_files(client, channel, thread_ts, produced)
