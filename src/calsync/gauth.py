"""Google OAuth access-token source backed by a vdirsyncer-format token file.

The token file (JSON: access_token, expires_at, expires_in, refresh_token, scope,
token_type, optionally refresh_token_expires_in) is shared with vdirsyncer, so refreshes
re-read it first, merge into whatever is on disk (unknown keys and the refresh_token are
kept) and are written atomically with mode 0600.

Secrets (access_token, refresh_token, client_secret) never appear in exceptions, status()
or log output.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .store import AuthError, RateLimited, StoreError

log = logging.getLogger("calsync.gauth")

DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
EXPIRY_SKEW_S = 120
_MAX_RESPONSE = 1 << 20
_LOOPBACK = {"127.0.0.1", "localhost"}
_CODE_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")
INVALID_GRANT_HINT = ("refresh token expired/revoked (OAuth app in Testing status expires "
                      "refresh tokens after 7 days); re-authorise the Google account")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The refresh request carries client_secret + refresh_token: never follow redirects."""

    def redirect_request(self, *a, **kw):  # noqa: D401
        return None


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _redact(text: str, secret_values: list[str]) -> str:
    for s in secret_values:
        if s:
            text = text.replace(s, "[redacted]")
    text = re.sub(r"(?i)(access_token|refresh_token|client_secret|authorization)\W+[\w.\-/+=]+",
                  r"\1=[redacted]", text)
    text = re.sub(r"ya29\.[\w.\-]+", "[redacted]", text)
    return re.sub(r"\s+", " ", text)[:200]


class GoogleTokenSource:
    def __init__(self, token_file, client_id: str, client_secret: str, *,
                 token_url: str | None = None, clock: Callable[[], float] = time.time,
                 opener=None, timeout: float = 30):
        self.token_file = Path(token_file)
        self._client_id = client_id
        self._client_secret = client_secret
        self.token_url = token_url or os.environ.get("GOOGLE_TOKEN_URL") or DEFAULT_TOKEN_URL
        parts = urllib.parse.urlsplit(self.token_url)
        if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK)):
            raise ValueError("token_url must be https (http only for 127.0.0.1/localhost)")
        self._clock = clock
        self._opener = opener or urllib.request.build_opener(_NoRedirect)
        self._timeout = timeout
        self._lock = threading.Lock()          # serialises refreshes (held across HTTP)
        self._state = threading.Lock()         # guards the status fields only
        self._mem: dict | None = None          # last good token, used if persisting failed
        self._last_ok: float | None = None
        self._last_error: str | None = None

    # ---------------------------------------------------------------- public
    def headers(self) -> dict:
        with self._lock:
            tok = self._read_file(required=True)
            best = tok if self._fresh(tok) else None
            if best is None and self._mem is not None and self._fresh(self._mem):
                best = self._mem
            if best is None:
                best = self._refresh(tok)
            access = best["access_token"]
        if re.search(r"[\x00-\x1f\x7f]", access):
            raise StoreError("token file contains an invalid access_token", transient=False)
        return {"Authorization": "Bearer " + access}

    def status(self) -> dict:
        exists = self.token_file.exists()
        tok = None
        if exists:
            try:
                tok = self._read_file(required=False)
            except StoreError:
                tok = None
        exp = tok.get("expires_at") if tok else None
        exp = exp if _num(exp) else None
        hint = None
        if tok and exp is not None and _num(tok.get("expires_in")) and _num(tok.get("refresh_token_expires_in")):
            hint = (exp - tok["expires_in"]) + tok["refresh_token_expires_in"]
        with self._state:
            last_ok, last_err = self._last_ok, self._last_error
        return {
            "token_file_exists": exists,
            "access_expires_at": exp,
            "seconds_left": (exp - self._clock()) if exp is not None else None,
            "last_refresh_ok": last_ok,
            "last_error": last_err,
            "refresh_token_expires_hint": hint,
        }

    # ---------------------------------------------------------------- internals
    def _fresh(self, tok: dict | None) -> bool:
        return bool(tok) and isinstance(tok.get("access_token"), str) and bool(tok["access_token"]) \
            and _num(tok.get("expires_at")) and not (tok["expires_at"] - EXPIRY_SKEW_S < self._clock())

    def _read_file(self, *, required: bool) -> dict | None:
        try:
            raw = self.token_file.read_text()
        except FileNotFoundError:
            if required:
                raise AuthError("google token file missing") from None
            return None
        except OSError as e:
            raise StoreError(f"cannot read google token file ({type(e).__name__})", transient=True) from None
        try:
            tok = json.loads(raw)
        except ValueError:
            raise StoreError("google token file is not valid JSON", transient=True) from None
        if not isinstance(tok, dict):
            raise StoreError("google token file has unexpected structure", transient=False)
        return tok

    def _set_error(self, msg: str) -> None:
        with self._state:
            self._last_error = msg

    def _refresh(self, tok: dict) -> dict:
        rt = tok.get("refresh_token")
        if not isinstance(rt, str) or not rt:
            msg = "google token file has no refresh_token"
            self._set_error(msg)
            raise AuthError(msg)
        try:
            resp = self._post_refresh(rt)
        except StoreError as e:
            self._set_error(str(e))
            raise
        # The file may have changed while we talked to Google (vdirsyncer shares it).
        try:
            latest = self._read_file(required=False)
        except StoreError:
            latest = None
        if latest is None:
            latest = dict(tok)
        if self._fresh(latest) and latest["expires_at"] >= resp["expires_at"]:
            self._mem = latest
            with self._state:
                self._last_ok, self._last_error = self._clock(), None
            return latest
        merged = dict(latest)
        merged.update(resp)
        if not isinstance(merged.get("refresh_token"), str) or not merged["refresh_token"]:
            merged["refresh_token"] = rt
        self._mem = merged
        try:
            self._write_atomic(merged)
            err = None
        except OSError as e:
            err = f"refreshed token could not be persisted ({type(e).__name__})"
            log.warning("gauth: %s", err)
        with self._state:
            self._last_ok, self._last_error = self._clock(), err
        return merged

    def _post_refresh(self, refresh_token: str) -> dict:
        secrets_ = [self._client_secret, refresh_token]
        body = urllib.parse.urlencode({
            "client_id": self._client_id, "client_secret": self._client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode()
        req = urllib.request.Request(self.token_url, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self._timeout) as r:
                status, raw = r.status, r.read(_MAX_RESPONSE + 1)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read(_MAX_RESPONSE + 1)
            except Exception:
                raw = b""
            finally:
                e.close()
            self._raise_http(e.code, raw, e.headers)
        except StoreError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise StoreError("google token endpoint unreachable: "
                             + _redact(f"{type(e).__name__}: {getattr(e, 'reason', '') or ''}", secrets_),
                             transient=True) from None
        if status != 200 or len(raw) > _MAX_RESPONSE:
            raise StoreError(f"google token endpoint returned unexpected HTTP {status}", transient=True)
        try:
            j = json.loads(raw)
            access = j["access_token"]
            expires_in = j.get("expires_in", 3600)
            if not isinstance(access, str) or not access or not _num(expires_in) or expires_in <= 0:
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise StoreError("google token endpoint returned a malformed response", transient=True) from None
        out = {"access_token": access, "expires_in": int(expires_in),
               "expires_at": int(self._clock() + expires_in)}
        for k in ("scope", "token_type"):
            if isinstance(j.get(k), str):
                out[k] = j[k]
        if _num(j.get("refresh_token_expires_in")):
            out["refresh_token_expires_in"] = j["refresh_token_expires_in"]
        if isinstance(j.get("refresh_token"), str) and j["refresh_token"]:
            out["refresh_token"] = j["refresh_token"]       # rotation (rare); otherwise kept
        return out

    def _raise_http(self, status: int, raw: bytes, headers) -> None:
        code = None
        try:
            c = json.loads(raw).get("error")
            if isinstance(c, str) and _CODE_RE.match(c):
                code = c
        except (ValueError, AttributeError):
            pass
        if code == "invalid_grant":
            raise AuthError(f"google token refresh rejected (HTTP {status}, invalid_grant): "
                            + INVALID_GRANT_HINT, status=status)
        if status in (400, 401, 403):
            raise AuthError(f"google token refresh rejected (HTTP {status}"
                            + (f", {code}" if code else "") + ")", status=status)
        if status == 429:
            raise RateLimited("google token endpoint rate limited", status=429)
        if status >= 500 or status == 408:
            raise StoreError(f"google token endpoint error (HTTP {status})", transient=True, status=status)
        raise StoreError(f"google token endpoint unexpected HTTP {status}", transient=False, status=status)

    def _write_atomic(self, tok: dict) -> None:
        d = self.token_file.parent
        tmp = d / f".{self.token_file.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(tok, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.token_file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
