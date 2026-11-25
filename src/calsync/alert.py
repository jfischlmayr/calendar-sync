"""Failure alerts: generic JSON webhook (ntfy/Gotify/Slack-compatible) and/or SMTP.
De-duplicated: one alert per (key) per alert_repeat_s, plus a recovery notice."""
from __future__ import annotations

import json
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage

from .config import Settings, env_secret
from .util import log, now, read_json, redact, write_json


def _send(subject: str, body: str) -> list[str]:
    sent = []
    url = env_secret("ALERT_WEBHOOK_URL")
    if url and urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        log.error("webhook alert skipped: ALERT_WEBHOOK_URL must be http(s)")   # blocks file:/ftp: etc.
        url = None
    if url:
        data = json.dumps({"title": subject, "message": body, "text": f"{subject}\n{body}"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Title": subject})
        try:
            urllib.request.urlopen(req, timeout=15).read()  # nosec B310 - scheme checked above
            sent.append("webhook")
        except Exception as e:  # never raise from alerting
            log.error("webhook alert failed: %s", redact(str(e)))
    host = os.environ.get("SMTP_HOST")
    if host and os.environ.get("ALERT_EMAIL_TO"):
        try:
            m = EmailMessage()
            m["Subject"], m["To"] = subject, os.environ["ALERT_EMAIL_TO"]
            m["From"] = os.environ.get("ALERT_EMAIL_FROM", os.environ.get("SMTP_USER", "calsync@localhost"))
            m.set_content(body)
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=20) as sm:
                sm.starttls()
                if os.environ.get("SMTP_USER"):
                    sm.login(os.environ["SMTP_USER"], env_secret("SMTP_PASSWORD") or "")
                sm.send_message(m)
            sent.append("smtp")
        except Exception as e:
            log.error("smtp alert failed: %s", redact(str(e)))
    if not sent:
        log.error("ALERT (no channel configured/working): %s | %s", subject, redact(body))
    return sent


def alert(s: Settings, key: str, subject: str, body: str, *, recovery: bool = False) -> bool:
    st_path = s.state / "alerts.json"
    st = read_json(st_path, {})
    last = st.get(key, 0)
    if recovery:
        if last:
            if _send(f"[calsync] RECOVERED {redact(subject)}", redact(body)):
                st.pop(key, None)
                write_json(st_path, st)
        return True
    if now() - last < s.alert_repeat_s:
        return False
    if _send(f"[calsync] {redact(subject)}", redact(body)):
        # only stamp dedupe time when at least one channel delivered; otherwise retry next cycle
        st[key] = now()
        write_json(st_path, st)
    return True
