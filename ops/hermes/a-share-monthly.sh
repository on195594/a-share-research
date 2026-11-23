#!/usr/bin/env bash
# a-share-research monthly batch: submit (1st 20:00) / status (hourly, silent until terminal); see a-share-monthly.py.
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
exec python3 "$(dirname "$0")/a-share-monthly.py" "$@"
