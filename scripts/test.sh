#!/bin/sh
# Run the full suite with the real vdirsyncer + Radicale binaries from a local venv.
#   python3 -m venv .venv-test && .venv-test/bin/pip install -r requirements.lock radicale pytest
set -eu
cd "$(dirname "$0")/.."
V=${VENV:-.venv-test}
export VDIRSYNCER_BIN="$PWD/$V/bin/vdirsyncer" RADICALE_BIN="$PWD/$V/bin/radicale" PYTHONPATH="src:tests"
exec "$V/bin/python" -m pytest -q "$@" tests
