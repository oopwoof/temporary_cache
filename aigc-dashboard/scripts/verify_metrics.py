#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
指标交叉校验 —— 完全绕开前端, 独立重算一遍看板上的每个数字。

存在的意义: index.html 里的 computeMetrics() 是唯一的聚合入口, 但"唯一"不等于"正确"。
这个脚本用另一套实现算同样的口径, 两边对不上就说明至少有一边错了。

用法:
    python3 scripts/verify_metrics.py            # 打印各场景期望值(JSON)
    python3 scripts/verify_metrics.py --report   # 人看的报告, 含 HLL 精度实测
"""

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "data"))
from hll import hll_decode, hll_estimate, hll_merge, hll_new   # noqa: E402

D = json.loads((ROOT / "data" / "mock.json").read_text(encoding="utf-8"))
META, EDGES = D["meta"], D["meta"]["hist_edges"]
TH = META["thresholds"]

SUM_KEYS = ["participants", "participants_unique", "gen_users", "gen_requests",
            "gen_success", "gen_failed", "gen_seconds_total", "gen_success_users",
            "qr_scans", "shares", "review_total", "review_pass", "review_reject",
            "review_pending", "online_minutes", "expected_minutes"]


def percentile_from_hist(hist, q=0.95):
    """与前端 percentileFromHist() 同算法。注意这是**估算**, 不是精确分位数。"""
    total = sum(hist)
    if not total:
        return 0.0
    target, cum = q * total, 0
    for i, c in enumerate(hist):
        if cum + c >= target and c > 0:
            lo, hi = EDGES[i], EDGES[i + 1]
            return round(lo + (hi - lo) * (target - cum) / c, 1)
        cum += c
    return float(EDGES[-1])


def compute(records):
    a = {k: sum(r[k] for r in records) for k in SUM_KEYS}
    hist = [0] * (len(EDGES) - 1)
    regs = hll_new()
    for r in records:
        for i, c in enumerate(r["gen_seconds_hist"]):
            hist[i] += c
        hll_merge(regs, hll_decode(r["participants_hll"]))
    safe = lambda x, y: (x / y) if y else 0.0
    # 与前端同样的夹逼: 上界是人次, 下界是单格去重的最大值; 单条记录直接取精确值
    if len(records) == 1:
        uniq = float(records[0]["participants_unique"])
    elif records:
        floor = max(r["participants_unique"] for r in records)
        uniq = min(max(hll_estimate(regs), floor), float(a["participants"]))
    else:
        uniq = 0.0
    return dict(a,
                days=len({r["date"] for r in records}),
                uniqueVisitors=uniq,
                visitsPerUser=safe(a["participants"], uniq),
                successRate=safe(a["gen_success"], a["gen_requests"]),
                avgGen=safe(a["gen_seconds_total"], a["gen_success"]),
                p95=percentile_from_hist(hist),
                scanRate=safe(a["qr_scans"], a["participants"]),
                shareRate=safe(a["shares"], a["participants"]),
                passRate=safe(a["review_pass"], a["review_total"]),
                rejectRate=safe(a["review_reject"], a["review_total"]),
                onlineRate=safe(a["online_minutes"], a["expected_minutes"]))


def sel(start, end, devices=None):
    return [r for r in D["daily"]
            if start <= r["date"] <= end and (not devices or r["device_id"] in devices)]


# 页面上的格式化规则, 必须和 index.html 里的 fmt* 一致, 否则比的是格式不是数
def f_compact(n):
    return f"{n / 10000:.1f}万" if n >= 10000 else f"{round(n):,}"


def f_pct(v, d=1):
    return f"{v * 100:.{d}f}%"


SCENARIOS = [
    ("默认 · 近30天 · 全部点位", "2026-08-17", "2026-09-15", None),
    ("近7天 · 仅成都·太古里", "2026-09-09", "2026-09-15", {"CD-001"}),
    ("近30天 · 深圳2点位", "2026-08-17", "2026-09-15", {"SZ-001", "SZ-002"}),
    ("单日 · 2026-09-05 · 全部点位", "2026-09-05", "2026-09-05", None),
    ("全部60天 · 全部点位", "2026-07-18", "2026-09-15", None),
]


def expectations():
    out = {}
    for label, s, e, devs in SCENARIOS:
        m = compute(sel(s, e, devs))
        out[label] = {
            "参与人数_去重": f_compact(m["uniqueVisitors"]),
            "参与副标题": f"{f_compact(m['participants'])} 人次 · 人均 {m['visitsPerUser']:.2f} 次",
            "生成成功率": f_pct(m["successRate"]),
            "扫码转化率": f_pct(m["scanRate"]),
            "分享转化率": f_pct(m["shareRate"]),
            "平均生成时长": f"{m['avgGen']:.1f}",
            "P95": f"{m['p95']:.1f}s",
            "审核通过率": f_pct(m["passRate"]),
            "审核拒绝率": f_pct(m["rejectRate"]),
            "设备在线率": f_pct(m["onlineRate"]),
            "在线损失小时": f"{round((m['expected_minutes'] - m['online_minutes']) / 60):,}",
            "漏斗": [round(m["participants"]), round(m["gen_users"]),
                     round(m["gen_success_users"]), round(m["qr_scans"]), round(m["shares"])],
        }
    return out


def report():
    print("=" * 72)
    print("指标交叉校验 —— 独立实现重算（不经过前端）")
    print("=" * 72)
    for label, s, e, devs in SCENARIOS:
        m = compute(sel(s, e, devs))
        print(f"\n[{label}]  {s} .. {e}  {len(sel(s, e, devs))} 条记录")
        print(f"  参与人数(去重估算) {m['uniqueVisitors']:>10,.0f}   "
              f"人次 {m['participants']:>9,}   人均 {m['visitsPerUser']:.2f} 次")
        print(f"  生成成功率 {f_pct(m['successRate']):>7}   平均时长 {m['avgGen']:>5.1f}s   "
              f"P95(估算) {m['p95']:>5.1f}s")
        print(f"  扫码转化 {f_pct(m['scanRate']):>7}   分享转化 {f_pct(m['shareRate']):>7}   "
              f"审核通过 {f_pct(m['passRate']):>7}   在线率 {f_pct(m['onlineRate']):>7}")
        funnel = [m["participants"], m["gen_users"], m["gen_success_users"], m["qr_scans"], m["shares"]]
        assert all(funnel[i] < funnel[i - 1] for i in range(1, 5)), "漏斗必须单调递减"
        print(f"  漏斗 参与{funnel[0]:,} → 发起{funnel[1]:,} → 成功{funnel[2]:,} "
              f"→ 扫码{funnel[3]:,} → 分享{funnel[4]:,}  (单调递减 ✓)")

    print("\n" + "=" * 72)
    print("HLL 精度 —— 生成时对账的实测值（写在 meta.hll_accuracy）")
    print("=" * 72)
    acc = META["hll_accuracy"]
    print(f"  {acc['slices_tested']} 个随机切片：中位 {acc['median_abs_err_pct']}% · "
          f"P95 {acc['p95_abs_err_pct']}% · 最大 {acc['max_abs_err_pct']}%")
    print(f"  寄存器 {acc['registers']} 个，理论标准误 {acc['theoretical_stderr_pct']}%")
    print("  注：去重人数是估算值，不是精确计数；P95 同理（直方图桶内线性插值）。")

    print("\n" + "=" * 72)
    print("不变量检查")
    print("=" * 72)
    ok = True
    for r in D["daily"]:
        if r["participants_unique"] > r["participants"]:
            print(f"  FAIL {r['date']} {r['device_id']}: 单日去重 > 人次"); ok = False
        if sum(r["hourly_participants"]) != r["participants"]:
            print(f"  FAIL {r['date']} {r['device_id']}: 分小时之和 != 人次"); ok = False
        if r["gen_success"] + r["gen_failed"] != r["gen_requests"]:
            print(f"  FAIL {r['date']} {r['device_id']}: 成功+失败 != 请求"); ok = False
        if r["review_pass"] + r["review_reject"] + r["review_pending"] != r["review_total"]:
            print(f"  FAIL {r['date']} {r['device_id']}: 审核三项之和 != 送审总量"); ok = False
        if len(r["gen_seconds_hist"]) != len(EDGES) - 1:
            print(f"  FAIL {r['date']} {r['device_id']}: 直方图档数不对"); ok = False
    for label, s, e2, devs in SCENARIOS:
        m = compute(sel(s, e2, devs))
        if m["uniqueVisitors"] > m["participants"] + 1e-9:
            print(f"  FAIL {label}: 去重人数 > 人次"); ok = False
        if m["participants"] and m["visitsPerUser"] < 1.0 - 1e-9:
            print(f"  FAIL {label}: 人均参与次数 < 1"); ok = False
    for e in D["incidents"]:
        if e["end_date"] < e["start_date"]:
            print(f"  FAIL {e['id']}: 结束早于开始"); ok = False
        if e["ongoing"] and e["end_date"] != META["date_range"][1]:
            print(f"  FAIL {e['id']}: 未恢复事件的结束日应为数据最后一天"); ok = False
    print("  " + ("全部 480 条记录与 44 条事件通过不变量检查" if ok else "存在不变量违例"))
    return ok


if __name__ == "__main__":
    if "--report" in sys.argv:
        sys.exit(0 if report() else 1)
    print(json.dumps(expectations(), ensure_ascii=False, indent=1))
