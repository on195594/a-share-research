#!/usr/bin/env bash
# a-share-research disclosure scan / weekly digest (a-share-advisor.py); silent when nothing to report.
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
export A_SHARE_RESEARCH_HOST="${A_SHARE_RESEARCH_HOST:-stock.keyi.win}"  # service rejects other Host headers
exec python3 "$(dirname "$0")/a-share-advisor.py" "${1:-scan}"
