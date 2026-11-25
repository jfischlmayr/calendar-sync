"""Health status file + tiny HTTP endpoint (/healthz, /status). No secrets exposed.
Binds 127.0.0.1 by default; /status is served only on loopback unless CALSYNC_HEALTH_STATUS=1."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .config import Settings
from .util import now, read_json, write_json


def update(s: Settings, pair: str | None, **fields) -> None:
    p = s.state / "health.json"
    h = read_json(p, {"pairs": {}})
    if pair is None:
        h.update(fields)
    else:
        h.setdefault("pairs", {}).setdefault(pair, {}).update(fields)
    h["updated"] = now()
    write_json(p, h)


def evaluate(s: Settings) -> tuple[bool, dict]:
    h = read_json(s.state / "health.json", {})
    active = [p for p in s.pairs if p.mode != "disabled"]
    if not active:
        return True, {"state": "inert", "detail": "no active pairs configured", **h}
    ok = True
    stale_after = max(3 * s.interval_s, 900)
    for p in active:
        ph = h.get("pairs", {}).get(p.name, {})
        if ph.get("blocked"):
            ok = False
        ls = ph.get("last_success", 0)
        # a last_success in the future (clock jump / corrupt file) must not read as "fresh"
        if not isinstance(ls, (int, float)) or ls > now() + 60 or now() - ls > stale_after:
            ok = False
    return ok, {"state": "ok" if ok else "degraded", **h}


_LOOPBACK_BINDS = ("127.0.0.1", "::1", "localhost")


class _H(BaseHTTPRequestHandler):
    settings: Settings

    def do_GET(self):
        ok, body = evaluate(self.settings)
        if self.path.startswith("/healthz"):
            code, payload = (200 if ok else 503), {"ok": ok, "state": body["state"]}
        elif self.path.startswith("/status") and (
                self.settings.health_bind in _LOOPBACK_BINDS or self.settings.health_status_public):
            # /status lists pair names and error strings: loopback only unless explicitly opted in
            code, payload = 200, body
        else:
            code, payload = 404, {"error": "not found"}
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def serve(s: Settings) -> HTTPServer:
    _H.settings = s
    # Default bind is loopback; a non-loopback bind is an explicit operator decision (CALSYNC_HEALTH_BIND).
    srv = HTTPServer((s.health_bind, s.health_port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
