#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docs/*.template.md  →  docs/*.md

存在的意义: 阈值这类数字如果在数据里写一遍、在文档里再手写一遍, 迟早会对不上。
所有 {{占位符}} 都从 data/mock.json 的 meta 读, 改阈值只需要改生成器再重跑,
文档会跟着变 —— "看板与文档引用同一份配置"这句话才算数。

用法: python3 scripts/build_docs.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = json.loads((ROOT / "data" / "mock.json").read_text(encoding="utf-8"))["meta"]
TH, ACC = META["thresholds"], META["hll_accuracy"]


def pct(v, digits=0):
    return f"{v * 100:.{digits}f}%"


VALUES = {
    "success_rate_min": pct(TH["success_rate_min"]),
    "avg_gen_seconds_max": f"{TH['avg_gen_seconds_max']}",
    "reject_rate_max": pct(TH["reject_rate_max"]),
    "online_rate_min": pct(TH["online_rate_min"]),
    "scan_rate_min": pct(TH["scan_rate_min"]),
    "open_hour": str(META["open_hour"]),
    "close_hour": str(META["close_hour"]),
    "expected_minutes": str(META["expected_minutes_per_day"]),
    "hist_buckets": str(META["hist_buckets"]),
    "hll_registers": str(ACC["registers"]),
    "hll_median_err": f"{ACC['median_abs_err_pct']}%",
    "hll_p95_err": f"{ACC['p95_abs_err_pct']}%",
    "hll_max_err": f"{ACC['max_abs_err_pct']}%",
    "hll_slices": str(ACC["slices_tested"]),
    "date_from": META["date_range"][0],
    "date_to": META["date_range"][1],
}


def main():
    built = 0
    for tpl in sorted((ROOT / "docs").glob("*.template.md")):
        text = tpl.read_text(encoding="utf-8")
        missing = [k for k in re.findall(r"\{\{(\w+)\}\}", text) if k not in VALUES]
        if missing:
            print(f"错误: {tpl.name} 引用了未定义的占位符 {sorted(set(missing))}")
            return 1
        used = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)))
        for k, v in VALUES.items():
            text = text.replace("{{" + k + "}}", v)
        out = tpl.with_name(tpl.name.replace(".template.md", ".md"))
        out.write_text(text, encoding="utf-8")
        print(f"{tpl.name}  →  {out.name}   注入 {len(used)} 个占位符: {', '.join(used)}")
        built += 1
    if not built:
        print("没有找到 *.template.md")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
