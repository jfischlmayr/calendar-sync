#!/bin/sh
# Run ON YOUR WORKSTATION (needs a browser). Obtains the Google OAuth token via vdirsyncer's own flow
# (read-only enumeration into a throw-away local dir: no calendar is created or modified anywhere).
# Usage: GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... scripts/google-oauth-local.sh OUT_TOKEN_FILE
# Then copy OUT_TOKEN_FILE to the data volume as /data/tokens/google.json (mode 600), see docs/GOOGLE-OAUTH.md
set -eu
umask 077
OUT="${1:?usage: $0 OUT_TOKEN_FILE}"
: "${GOOGLE_CLIENT_ID:?}" "${GOOGLE_CLIENT_SECRET:?}"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
cat > "$T/config" <<CFG
[general]
status_path = "$T/status"
[pair g]
a = "google"
b = "scratch"
collections = ["from a"]
[storage google]
type = "google_calendar"
token_file = "$OUT"
client_id.fetch = ["command", "printenv", "GOOGLE_CLIENT_ID"]
client_secret.fetch = ["command", "printenv", "GOOGLE_CLIENT_SECRET"]
[storage scratch]
type = "filesystem"
path = "$T/scratch"
fileext = ".ics"
CFG
echo "A browser window opens; sign in with the Google account that owns the calendars." >&2
echo "(If vdirsyncer asks to create anything, it is only in $T/scratch, a temp dir.)" >&2
vdirsyncer -c "$T/config" discover g
chmod 600 "$OUT"
echo "token written to $OUT (not printed)." >&2
