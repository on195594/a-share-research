#!/usr/bin/env bash
# Monthly a-stock ledger vs total-return index comparison (a-stock-agent-skills benchmark-report).
set -euo pipefail
export PATH="${HOME}/.local/bin:${PATH}"
report="$(a-stock-cache benchmark-report)"
printf '📊 A股账本月度对比\n%s\n' "$report"
