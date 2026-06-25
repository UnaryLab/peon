"""File attachments: inbound download + outbound upload.

Slack messages can carry files[] (uploads, images, diagrams). Inbound: each
file's url_private is downloaded with the bot token (a Bearer header) and its
local path appended to the prompt so the CLI can read it. Outbound: if the
thread has a designated read-write workdir (the read-write feature, shipped
later, installs claude_runner.get_workdir), files the run created/modified there
are uploaded back into the thread. Diagrams (image/svg) need no special case --
they are ordinary files. All HTTP/Slack I/O goes through small seams so tests
mock it; nothing here performs real network I/O at import time.

Moved verbatim from the former src/app.py. The Kind-B seams (_attachments_dir,
_http_get_bytes) are resolved THROUGH the app facade inside _download_attachments
so a test's monkeypatch on the facade is seen by this module's call sites.
"""

from __future__ import annotations

import logging
import os
import tempfile
import urllib.request

from src import store
from src.runners import claude_runner

logger = logging.getLogger("peon")


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
    (claude_runner._safe_token), so the path-component sanitizing rule lives in ONE
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
    """The designated read-write workdir for this (agent, thread), or None.

    Uses the shared helper `claude_runner.get_workdir(agent_name, thread_ts)` (a
    PURE path lookup here: create defaults False, so a read-only thread does not
    spawn an empty dir; the outbound scan guards on os.path.isdir). Guarded so a
    helper that is absent or raises still yields None (never aborts the run).
    """
    helper = getattr(claude_runner, "get_workdir", None)
    if helper is None:
        return None
    try:
        return helper(agent["name"], thread_ts)
    except Exception:  # noqa: BLE001 - a misbehaving helper must not abort the run
        logger.warning("get_workdir failed for %s", agent["name"])
        return None


def _files_modified_since(workdir, since):
    """Absolute paths of regular files under `workdir` with mtime >= `since`.

    Walks the tree (so files in subdirs are included) and keeps only files touched
    at or after `since` (the run's start time), i.e. created or modified during the
    run. A missing/unreadable workdir yields []. Sorted for deterministic order.
    """
    if not workdir or not os.path.isdir(workdir):
        return []
    found = []
    for root, _dirs, names in os.walk(workdir):
        for name in names:
            path = os.path.join(root, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) >= since:
                    found.append(path)
            except OSError:
                continue
    return sorted(found)


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


def _maybe_upload_outputs(client, channel, thread_ts, agent, since):
    """Scan the thread's designated workdir for files the run produced and upload them.

    No workdir configured (the read-write feature absent or no workdir for this
    thread) -> a no-op returning 0 (files_upload_v2 is never called). Otherwise
    every file under the workdir with mtime >= `since` (created/modified during the
    run) is uploaded into the thread. Guarded so an upload error never crashes the
    worker.
    """
    workdir = _thread_workdir(agent, thread_ts)
    if not workdir:
        return 0
    produced = _files_modified_since(workdir, since)
    if not produced:
        return 0
    return _upload_workdir_files(client, channel, thread_ts, produced)
