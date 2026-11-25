"""Generate vdirsyncer configs. Credentials are referenced through
`.fetch = ["command", "printenv", VAR]` so they never touch disk."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Pair, Settings


def q(s: str) -> str:
    return json.dumps(s)  # JSON string syntax is valid vdirsyncer config quoting


def _icloud(name: str, p: Pair, s: Settings, read_only: bool) -> str:
    url = p.icloud_url or s.icloud_url
    ro = "read_only = true\n" if read_only else ""
    return (
        f"[storage {name}]\ntype = \"caldav\"\nurl = {q(url)}\n"
        f"username.fetch = [\"command\", \"printenv\", \"ICLOUD_USER\"]\n"
        f"password.fetch = [\"command\", \"printenv\", \"ICLOUD_APP_PASSWORD\"]\n"
        f"{ro}\n"
    )


def _google(name: str, s: Settings, read_only: bool) -> str:
    ro = "read_only = true\n" if read_only else ""
    return (
        f"[storage {name}]\ntype = \"google_calendar\"\n"
        f"token_file = {q(str(s.tokens / 'google.json'))}\n"
        f"client_id.fetch = [\"command\", \"printenv\", \"GOOGLE_CLIENT_ID\"]\n"
        f"client_secret.fetch = [\"command\", \"printenv\", \"GOOGLE_CLIENT_SECRET\"]\n"
        f"{ro}\n"
    )


def _fs(name: str, path: Path) -> str:
    return f"[storage {name}]\ntype = \"filesystem\"\npath = {q(str(path))}\nfileext = \".ics\"\n\n"


def _test_urls() -> tuple[str, str] | None:
    """TEST-ONLY hook (integration tests against a local Radicale): both sides are plain
    caldav URLs, no collection discovery. Never set in production."""
    a, b = os.environ.get("CALSYNC_TEST_A_URL"), os.environ.get("CALSYNC_TEST_B_URL")
    return (a, b) if a and b else None


def _tcal(name: str, url: str, read_only: bool) -> str:
    ro = "read_only = true\n" if read_only else ""
    return (f"[storage {name}]\ntype = \"caldav\"\nurl = {q(url)}\nusername = \"test\"\npassword = \"test\"\n{ro}\n")


def mirror_conf(p: Pair, s: Settings, side: str, mirror_root: Path | None = None,
                status_root: Path | None = None) -> tuple[str, str]:
    """Remote (read_only) -> local filesystem mirror. Never writes to the remote:
    the remote storage is read_only and partial_sync=error aborts otherwise.
    mirror_root/status_root (default: the live s.mirror/<pair> and s.status/mirror) let the runner
    build a fresh mirror in scratch staging without touching the live one."""
    assert side in ("a", "b")
    mdir = (mirror_root or (s.mirror / p.name)) / side
    status_path = status_root or (s.status / "mirror")
    mdir.mkdir(parents=True, exist_ok=True)
    pair = f"m_{p.name}_{side}"
    remote = f"remote_{side}"
    tu = _test_urls()
    if tu:
        return (
            f"[general]\nstatus_path = {q(str(status_path))}\n\n"
            f"[pair {pair}]\na = {q(remote)}\nb = \"local\"\ncollections = null\n"
            f"conflict_resolution = null\npartial_sync = \"error\"\n\n"
            f"{_tcal(remote, tu[0] if side == 'a' else tu[1], True)}{_fs('local', mdir)}"
        ), pair
    rem = _icloud(remote, p, s, True) if side == "a" else _google(remote, s, True)
    cid = p.icloud_collection if side == "a" else p.google_collection
    return (
        f"[general]\nstatus_path = {q(str(status_path))}\n\n"
        f"[pair {pair}]\na = {q(remote)}\nb = \"local\"\n"
        f"collections = [[\"{p.name}\", {q(cid)}, \"{side}\"]]\n"
        f"conflict_resolution = null\npartial_sync = \"error\"\n\n"
        f"{rem}{_fs('local', mdir.parent)}"
    ), pair


def export_conf(s: Settings, side: str, collection: str, out_dir: Path, icloud_url: str = "") -> tuple[str, str]:
    """READ-ONLY full export of one remote calendar into out_dir (one .ics per event).
    Used for pre-migration backups of every calendar, incl. ones that are never synced."""
    assert side in ("icloud", "google")
    out_dir.mkdir(parents=True, exist_ok=True)
    pair = "export"
    status = s.run_dir / "export-status"
    tu = _test_urls()
    if tu:
        remote = _tcal("remote", tu[0] if side == "icloud" else tu[1], True)
        coll, fs = "collections = null\n", _fs("local", out_dir)
    else:
        p = Pair(name="x_export", icloud_collection=collection, google_collection=collection, icloud_url=icloud_url)
        remote = (_icloud("remote", p, s, True) if side == "icloud" else _google("remote", s, True)).replace("[storage remote]", "[storage remote]", 1)
        coll, fs = f"collections = [[\"export\", {q(collection)}, \"c\"]]\n", _fs("local", out_dir.parent)
        out_dir = out_dir  # collection name "c" -> caller passes out_dir ending in /c
    return (
        f"[general]\nstatus_path = {q(str(status))}\n\n[pair {pair}]\na = \"remote\"\nb = \"local\"\n{coll}"
        f"conflict_resolution = null\npartial_sync = \"error\"\n\n{remote}{fs}"
    ), pair
