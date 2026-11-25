from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger("calsync")


class Blocked(Exception):
    """A safety gate/guard refused the operation. Not a transient error."""


def now() -> float:
    return time.time()


def read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text())
    except FileNotFoundError:
        return default


def write_json(p: Path, obj) -> None:
    """Atomic JSON write."""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def setup_logging(logdir: Path | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger("calsync")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.addHandler(h)
    if logdir:
        from logging.handlers import RotatingFileHandler
        logdir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(logdir / "calsync.log", maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)


SECRET_ENV = ("ICLOUD_APP_PASSWORD", "GOOGLE_CLIENT_SECRET", "ALERT_WEBHOOK_URL", "SMTP_PASSWORD", "ICLOUD_USER")


def redact(text: str) -> str:
    """Strip any secret env values from text before logging/alerting."""
    for k in SECRET_ENV:
        v = os.environ.get(k)
        if v and len(v) >= 4:
            text = text.replace(v, "***")
    return text
