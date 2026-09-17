#!/usr/bin/env bash
# 一条命令跑完全部校验。在项目根目录执行: bash scripts/verify.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "### 1/3  数据可复现性"
cp data/mock.json /tmp/_mock_before.json
python3 data/generate_mock.py > /dev/null
diff -q /tmp/_mock_before.json data/mock.json && echo "  OK   重新生成的结果与现有文件一致"
rm -f /tmp/_mock_before.json
echo
echo "### 2/3  指标独立重算"
python3 scripts/verify_metrics.py --report
echo
echo "### 3/3  渲染端对账"
node scripts/verify_render.mjs
