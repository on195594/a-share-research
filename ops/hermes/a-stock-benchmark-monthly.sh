#!/usr/bin/env bash
# Monthly ledger comparison through the unified tools service.
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
exec python3 "$(dirname "$0")/a-share-benchmark.py"
