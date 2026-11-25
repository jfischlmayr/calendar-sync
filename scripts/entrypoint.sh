#!/bin/sh
# Entry wrapper: validates environment, never prints secret values, then execs calsync.
set -eu
umask 077
log() { printf '%s entrypoint: %s\n' "$(date -u +%FT%TZ)" "$*"; }

[ -d /data ] && [ -w /data ] || { log "FATAL: /data is not a writable persistent volume"; exit 78; }
# A non-persistent /data would silently lose state/tokens/mirrors on every redeploy.
if [ "${CALSYNC_REQUIRE_PERSISTENT:-1}" = "1" ] && ! grep -q ' /data ' /proc/self/mountinfo; then
  log "FATAL: /data is not a mount (no persistent volume). Set CALSYNC_REQUIRE_PERSISTENT=0 only for throwaway tests."
  exit 78
fi

active=0
if [ -f "${CALSYNC_CONFIG:-/config/calsync.toml}" ] && grep -Eq '^\s*mode\s*=\s*"(mirror|seed|live)"' "${CALSYNC_CONFIG:-/config/calsync.toml}"; then active=1; fi
if [ "$active" = 1 ]; then
  for v in ICLOUD_USER ICLOUD_APP_PASSWORD GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET; do
    eval "val=\${$v:-}"; eval "f=\${${v}_FILE:-}"
    if [ -n "$f" ] && [ -r "$f" ]; then export "$v=$(cat "$f")"; val=set; fi
    [ -n "$val" ] || { log "FATAL: $v missing but a pair is active"; exit 78; }
  done
  [ -s /data/tokens/google.json ] || { log "FATAL: /data/tokens/google.json missing (see docs/GOOGLE-OAUTH.md)"; exit 78; }
else
  log "no active pair (all disabled/absent): running INERT - nothing will be read or written remotely"
fi
log "writes_enabled=${CALSYNC_ALLOW_WRITES:-0} (default 0 = every remote write is blocked)"
exec python -m calsync "$@"
