#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G0 一致率 —— 判分回来后跑，回答「本轮读数能不能挂到历史刻度上」。

做法：本包里的 base / base+skill / WB / gpt 就是当年专家评过的**同一批答复**。
把判分方给出的排序里 ours 剔掉，只看这四方的相对顺序，与专家排序逐题比。

  · 四方顺序完全一致          → 强一致，ours 的名次可以按同一把尺子解读
  · 只是相邻两方换位          → 弱一致，分差类结论要标条件性
  · 出现跨两位以上的错位      → 尺子不同，本轮读数只能在轮内比较，不得挂到历史刻度

用法:
    python3 scripts/g0_consistency.py --pack runs/1/g0_pack \
        --keys runs/1/_answer_key/g0_keys.json \
        --expert runs/1/_answer_key/expert_ranking.json
"""
import argparse, json, pathlib, itertools, sys

def kendall(a, b):
    """同序对比例。1.0 = 完全一致，0.0 = 完全颠倒。"""
    idx_a = {x: i for i, x in enumerate(a)}
    idx_b = {x: i for i, x in enumerate(b)}
    common = [x for x in a if x in idx_b]
    pairs = list(itertools.combinations(common, 2))
    if not pairs:
        return None
    ok = sum(1 for x, y in pairs
             if (idx_a[x] < idx_a[y]) == (idx_b[x] < idx_b[y]))
    return ok / len(pairs)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--keys", required=True)
    ap.add_argument("--expert", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    keys = json.load(open(a.keys, encoding="utf-8"))
    exp = json.load(open(a.expert, encoding="utf-8"))
    pack = pathlib.Path(a.pack)
    rows, missing = [], []
    for k, mapping in sorted(keys.items()):
        p, case = k.split("/")
        rj = pack / p / case / "ranking.json"
        if not rj.exists():
            missing.append(k); continue
        got = json.load(open(rj, encoding="utf-8")).get("ranking") or []
        parties = [mapping.get(lab) for lab in got if mapping.get(lab)]
        base4 = [x for x in parties if x != "ours"]           # 剔掉候选，只留历史四方
        e = exp.get(case, {}).get("expert_rank_parties")
        if not e or len(base4) < 2:
            missing.append(k); continue
        tau = kendall(base4, e)
        ours_at = parties.index("ours") + 1 if "ours" in parties else None
        rows.append({"pass": p, "case": case, "judge_base4": base4, "expert": e,
                     "同序对比例": round(tau, 3), "完全一致": base4 == e,
                     "ours名次": ours_at})
    if not rows:
        sys.exit("没读到任何 ranking.json——判分还没回来？")
    exact = sum(1 for r in rows if r["完全一致"])
    avg = sum(r["同序对比例"] for r in rows) / len(rows)
    lines = ["# G0 一致率（历史四方在新尺子下的还原度）", "",
             f"- 有效题次：{len(rows)}（缺 {len(missing)}）",
             f"- 四方顺序**完全一致**：{exact}/{len(rows)} = {exact/len(rows):.1%}",
             f"- 平均同序对比例：{avg:.3f}", "",
             "| 轮 | 题 | 判分方给出的历史四方顺序 | 专家顺序 | 同序对 | 完全一致 | ours 名次 |",
             "|---|---|---|---|---:|:--:|:--:|"]
    for r in rows:
        lines.append(f"| {r['pass']} | {r['case']} | {' > '.join(r['judge_base4'])} | "
                     f"{' > '.join(r['expert'])} | {r['同序对比例']} | "
                     f"{'✓' if r['完全一致'] else '✗'} | {r['ours名次'] or '—'} |")
    verdict = ("强一致：ours 的名次可按同一把尺子解读" if avg >= 0.9 else
               "弱一致：分差类结论必须标条件性" if avg >= 0.75 else
               "**尺子不同：本轮读数只能在轮内比较，不得挂到历史刻度**")
    lines += ["", f"## 结论：{verdict}", "",
              "注：ours 不参与一致率计算（它没有历史读数）。本指标只回答"
              "「判分方是否还原了专家对同一批答复的相对判断」。"]
    txt = "\n".join(lines) + "\n"
    print(txt)
    if a.out:
        pathlib.Path(a.out).write_text(txt, encoding="utf-8")

if __name__ == "__main__":
    main()
