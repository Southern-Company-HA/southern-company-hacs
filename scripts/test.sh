#!/usr/bin/env bash
# Run the test suite in Docker (HA's test harness needs Python 3.14).
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -q -f Dockerfile.test -t southern-company-test . >/dev/null
SRC="$(pwd -W 2>/dev/null || pwd)"
MSYS_NO_PATHCONV=1 docker run --rm -v "$SRC:/src" southern-company-test pytest -q "$@"
