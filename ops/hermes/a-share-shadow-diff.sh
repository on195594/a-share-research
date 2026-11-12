#!/usr/bin/env bash
# Consolidation stage 3 (2026-11-23..27): one old/new monitor comparison per trading day; silent when identical.
# The comparison tooling and its private outputs live outside the repository; remove this job with the stage 4 cleanup.
set -uo pipefail
dir="$HOME/.local/share/a-share-research-evaluations/consolidation/stage3"
day=$(TZ=Asia/Shanghai date +%Y%m%d)
if out=$("$dir/daily.sh" "$day" 2>&1); then
  exit 0
fi
printf '合一第 3 段影子对照 %s 未通过或未完成：\n%s\n' "$day" "$(printf '%s' "$out" | tail -n 5)"
