"""Configuration loading. Secrets come ONLY from environment variables (Coolify
secrets) or files referenced by *_FILE variables; they are never written to disk
config and never logged."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Modes, in increasing order of write capability.
#   disabled : pair ignored
#   mirror   : read-only fetch of both remotes into local mirrors (no remote writes)
#   seed     : like live, but side A is read_only and any change originating on B
#              aborts (partial_sync=error). Only B is ever written.
#   live     : bidirectional, conflict_resolution=null
MODES = ("disabled", "mirror", "seed", "seed_a", "live")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class ConfigError(Exception):
    pass


@dataclass
class Pair:
    name: str
    icloud_collection: str      # collection id (path segment) on iCloud
    google_collection: str      # Google calendar id
    mode: str = "disabled"
    icloud_url: str = ""        # per-pair override of calendar home / collection URL
    note: str = ""
    birthday_view_collection: str = ""   # optional iCloud collection id of the birthday view calendar (read-only ledger)

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ConfigError(f"invalid pair name {self.name!r}")
        if self.mode not in MODES:
            raise ConfigError(f"pair {self.name}: mode must be one of {MODES}")
        if "/" in self.name or self.name in ("global",):
            raise ConfigError("reserved pair name")


@dataclass
class Settings:
    data_dir: Path = Path("/data")
    run_dir: Path = Path("/run/calsync")
    interval_s: int = 300
    min_interval_s: int = 120            # rate limit between runs of one pair
    max_backoff_s: int = 3600
    max_delete_abs: int = 3              # max UIDs that may vanish per side per run
    max_delete_pct: float = 10.0
    backup_max_age_h: int = 26           # live writes need a verified backup this fresh
    alert_after_failures: int = 2
    alert_repeat_s: int = 3600
    icloud_url: str = "https://caldav.icloud.com/"
    health_port: int = 8080
    health_bind: str = "127.0.0.1"         # loopback by default: nothing is published unless the operator opts in
    health_status_public: bool = False     # allow /status on a non-loopback bind (CALSYNC_HEALTH_STATUS=1)
    self_addresses: tuple[str, ...] = ()   # my own addresses on both providers (guest/organiser detection)
    propagate_deletes: bool = False        # engine: report deletions instead of executing them
    max_creates_per_run: int = 25
    max_updates_per_run: int = 25
    audit_interval_s: int = 3600           # read-only engine audit cadence for mirror-mode pairs
    max_burst_ops: int = 100               # initialised pair: create/update ops per cycle above which a fresh approval is required
    write_enabled: bool = False          # CALSYNC_ALLOW_WRITES=1 (gate 1 of 4)
    pairs: list[Pair] = field(default_factory=list)

    @property
    def state(self) -> Path: return self.data_dir / "state"
    @property
    def mirror(self) -> Path: return self.data_dir / "mirror"
    @property
    def status(self) -> Path: return self.data_dir / "status"
    @property
    def tokens(self) -> Path: return self.data_dir / "tokens"
    @property
    def manifests(self) -> Path: return self.data_dir / "manifests"
    @property
    def logs(self) -> Path: return self.data_dir / "logs"
    @property
    def plans(self) -> Path: return self.data_dir / "plans"
    @property
    def decisions(self) -> Path: return self.data_dir / "decisions"
    @property
    def reports(self) -> Path: return self.data_dir / "reports"
    @property
    def backups(self) -> Path: return self.data_dir / "backups"

    def ensure_dirs(self) -> None:
        for p in (self.state, self.mirror, self.status, self.manifests, self.logs, self.backups, self.plans, self.decisions):
            p.mkdir(parents=True, exist_ok=True)
        self.tokens.mkdir(parents=True, exist_ok=True)
        os.chmod(self.tokens, 0o700)
        self.reports.mkdir(parents=True, exist_ok=True)
        for d in (self.plans, self.decisions, self.reports):
            os.chmod(d, 0o700)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)


def env_secret(name: str) -> str | None:
    """Read secret from $NAME or file at $NAME_FILE. Never log the return value."""
    f = os.environ.get(f"{name}_FILE")
    if f:
        return Path(f).read_text().strip()
    return os.environ.get(name) or None


def load(path: str | os.PathLike | None = None, env=os.environ) -> Settings:
    path = Path(path or env.get("CALSYNC_CONFIG", "/config/calsync.toml"))
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text())
    g = raw.get("global", {})
    s = Settings(
        data_dir=Path(env.get("CALSYNC_DATA", g.get("data_dir", "/data"))),
        run_dir=Path(env.get("CALSYNC_RUN", "/run/calsync")),
        interval_s=int(env.get("CALSYNC_INTERVAL", g.get("interval_s", 300))),
        min_interval_s=int(g.get("min_interval_s", 120)),
        max_backoff_s=int(g.get("max_backoff_s", 3600)),
        max_delete_abs=int(g.get("max_delete_abs", 3)),
        max_delete_pct=float(g.get("max_delete_pct", 10.0)),
        backup_max_age_h=int(g.get("backup_max_age_h", 26)),
        alert_after_failures=int(g.get("alert_after_failures", 2)),
        alert_repeat_s=int(g.get("alert_repeat_s", 3600)),
        icloud_url=g.get("icloud_url", "https://caldav.icloud.com/"),
        health_port=int(env.get("CALSYNC_HEALTH_PORT", g.get("health_port", 8080))),
        health_bind=str(env.get("CALSYNC_HEALTH_BIND", g.get("health_bind", "127.0.0.1"))),
        health_status_public=env.get("CALSYNC_HEALTH_STATUS", "0") == "1",
        self_addresses=tuple(sorted({a.strip().lower() for a in g.get("self_addresses", []) if a.strip()})),
        propagate_deletes=bool(g.get("propagate_deletes", False)),
        max_creates_per_run=int(env.get("CALSYNC_MAX_CREATES", g.get("max_creates_per_run", 25))),
        max_updates_per_run=int(env.get("CALSYNC_MAX_UPDATES", g.get("max_updates_per_run", 25))),
        audit_interval_s=int(g.get("audit_interval_s", 3600)),
        max_burst_ops=int(g.get("max_burst_ops", 100)),
        write_enabled=env.get("CALSYNC_ALLOW_WRITES", "0") == "1",
    )
    if s.interval_s < 60:
        raise ConfigError("interval_s below 60 would risk provider rate limits")
    seen = set()
    for p in raw.get("pair", []):
        pair = Pair(**{k: v for k, v in p.items() if k in Pair.__dataclass_fields__})
        pair.validate()
        if pair.name in seen:
            raise ConfigError(f"duplicate pair {pair.name}")
        seen.add(pair.name)
        s.pairs.append(pair)
    return s
