#!/usr/bin/env bash
# a-share-research monthly batch: submit (1st 20:00) / status (hourly, silent until terminal); see a-share-monthly.py.
set -euo pipefail
export A_SHARE_RESEARCH_HOST="${A_SHARE_RESEARCH_HOST:-stock.keyi.win}"  # service rejects other Host headers
exec python3 "$(dirname "$0")/a-share-monthly.py" "${1:-status}" ${2:+"$2"}
