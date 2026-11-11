#!/usr/bin/env bash
# Intraday stop-line alert through the unified service; Hermes owns delivery.
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
exec python3 "$(dirname "$0")/a-share-stop-alert.py"
