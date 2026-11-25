#!/usr/bin/env bash
# End-to-end restore drill: restore an encrypted calsync backup into a fresh temp dir,
# verify it (hashes + event counts), print the counts and clean up.
#
# Usage: restore-drill.sh <archive.tar.gz.age> <identity-file>
# Run it where the PRIVATE age identity lives (never on the calendar-sync host).
# Exit code 0 only if restore and verification both succeeded.
set -euo pipefail
umask 077

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <archive.tar.gz.age> <identity-file>" >&2
  exit 2
fi
archive=$1
identity=$2
[ -f "$archive" ] || { echo "drill: archive not found: $archive" >&2; exit 2; }
[ -f "$identity" ] || { echo "drill: identity file not found" >&2; exit 2; }

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="${CALSYNC_SRC:-$here/../src}${PYTHONPATH:+:$PYTHONPATH}"
py=${PYTHON:-python3}
run() { "$py" -P -m calsync.backup "$@"; }

# Prefer RAM-backed storage: the restored tree contains OAuth tokens.
base=/dev/shm
[ -d "$base" ] && [ -w "$base" ] || base=${TMPDIR:-/tmp}
work=$(mktemp -d "$base/calsync-drill.XXXXXX")
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

target="$work/data"
echo "== restore (verified in staging, then written to a fresh dir)"
run restore "$archive" --identity "$identity" --target "$target"

echo "== verify archive (hashes + event counts vs manifest)"
run verify "$archive" --identity "$identity"

echo "== restored tree"
if [ -d "$target/tokens" ]; then
  mode=$(stat -c %a "$target/tokens")
  [ "$mode" = "700" ] || { echo "drill: tokens dir mode is $mode, expected 700" >&2; exit 1; }
  echo "tokens dir mode: $mode"
fi
if [ -d "$target/mirror" ]; then
  find "$target/mirror" -mindepth 2 -maxdepth 2 -type d | sort | while read -r side; do
    n=$(find "$side" -type f -name '*.ics' | wc -l)
    echo "  ${side#"$target"/mirror/}: $n .ics files"
  done
fi
echo "DRILL OK"
