#!/usr/bin/env bash
# Post-close a-stock holdings brief (a-stock-agent-skills daily-brief); empty on non-trading days.
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
a-stock-cache daily-brief
