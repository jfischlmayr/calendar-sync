"""Daily backup trigger. Delegates to calsync.backup (age-encrypted, off-host)."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys

from . import alert as alertmod
from .config import Settings
from .util import log, now, read_json, redact, write_json


def maybe_run(s: Settings) -> None:
    if not os.environ.get("CALSYNC_BACKUP_AGE_RECIPIENTS"):
        return
    hour = int(os.environ.get("CALSYNC_BACKUP_HOUR_UTC", "3"))
    st = read_json(s.state / "backup_ok.json", {})
    ts = st.get("ts", 0)
    if not isinstance(ts, (int, float)) or ts > now() + 300:
        ts = 0  # unreadable or future-dated marker (clock jump) must never suppress backups
    u = dt.datetime.now(dt.timezone.utc)
    if u.hour < hour and now() - ts < 20 * 3600 + 1:
        return
    if now() - ts < 20 * 3600:
        return
    ok, info = run_backup(s)
    if not ok:
        alertmod.alert(s, "backup_fail", "BACKUP FAILED", info)


def run_backup(s: Settings) -> tuple[bool, str]:
    """create -> upload exactly the archive just created. backup_ok.json is only written after
    both succeeded; ``offhost`` comes from the structured upload result, never from a substring."""
    env = dict(os.environ, CALSYNC_DATA=str(s.data_dir))

    def run(*cmd):
        p = subprocess.run([sys.executable, "-m", "calsync.backup", *cmd], capture_output=True, text=True,
                           env=env, timeout=900)
        if p.returncode != 0:
            msg = redact((p.stdout + p.stderr)[-600:])
            log.error("backup %s failed: %s", cmd[0], msg)
            return None, msg
        return p, ""

    p, msg = run("create")
    if p is None:
        return False, msg
    lines = p.stdout.splitlines()
    if not lines or not lines[0].strip():
        return False, "backup create printed no archive path"
    p, msg = run("upload", lines[0].strip())
    if p is None:
        return False, msg
    try:
        res = json.loads(p.stdout.splitlines()[0])
        status = res["status"]
    except (ValueError, KeyError, IndexError, TypeError):
        return False, "backup upload result not understood"
    if status not in ("ok", "offhost_not_configured"):
        return False, f"backup upload status {status}"
    write_json(s.state / "backup_ok.json", {"ts": now(), "offhost": status == "ok" and bool(res.get("offhost"))})
    return True, "ok"
