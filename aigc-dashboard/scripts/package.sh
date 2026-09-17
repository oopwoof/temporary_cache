#!/usr/bin/env bash
# 打包成提交用的 zip。在项目根目录执行: bash scripts/package.sh
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="aigc-dashboard.zip"
rm -f "$OUT"
zip -qr "$OUT" \
  index.html README.md \
  vendor/echarts.min.js \
  data/generate_mock.py data/hll.py data/mock.json data/mock.js \
  docs/ scripts/ \
  -x '*.DS_Store' '*/node_modules/*' '*/__pycache__/*' '*.pyc'
SIZE_KB=$(du -k "$OUT" | cut -f1)
echo "$OUT  $((SIZE_KB / 1024)).$(((SIZE_KB % 1024) * 10 / 1024)) MB  (上限 30MB)"
unzip -l "$OUT" | tail -n +4 | head -n -2
[ "$SIZE_KB" -lt 30720 ] || { echo "超过 30MB 上限"; exit 1; }
