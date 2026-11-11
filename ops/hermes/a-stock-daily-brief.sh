#!/usr/bin/env bash
# Post-close holdings and dividend brief through the unified tools service.
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
exec python3 "$(dirname "$0")/a-share-daily-brief.py"
