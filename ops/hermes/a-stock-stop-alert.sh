#!/usr/bin/env bash
# Intraday stop-line alert for all a-stock holdings (a-stock-agent-skills stop-alert); silent unless a new level is reached.
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
a-stock-cache stop-alert
