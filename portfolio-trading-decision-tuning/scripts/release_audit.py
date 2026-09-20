#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把历史上**每一次**判分（整批 + 逐轮）都按需求方判据核一遍，回答一个问题：
有没有任何一次读数过准出？

判据结构直接读 gates/thresholds.yaml，并与 autotune 模板 score_release.py 的
eval_criteria() 对齐——**这一点必须写清楚，因为列名容易误读**：

    floor 三条 = 分差≥0.4 且 对 base 胜率>60% 且 超过题数≥6/8
                 ↑ 三条**全部**必须满足；松弛路径替代不了 floor 里的任何一条
    主要对照条 = 对 WB 胜率>60%  或  松弛（分差>0.5 且 对 WB 胜率≥50%）
                 ↑ 松弛只替代「对 WB 胜率」这一条
    准出       = floor 三条 且 主要对照条 且 硬条件（对 base+skill 零负场）

`score_release.py` 逐轮表里的列名「路径一 / 路径二」指的就是 floor 三条 / 主要对照条，
不是「两条通往准出的并列路径」。我曾按后者读过一次，写成「松弛路径过了两次，
唯一阻塞点是硬条件」——**那是错的**，已在本脚本的输出里改正。

用法: python3 scripts/release_audit.py [--out 文件]
"""
import argparse, json, pathlib, statistics as st, sys
import yaml

PACKS = [("runs/1 第一包 (v3.0 r1)", "runs/1/g0_pack",    "runs/1/_answer_key/g0_keys.json"),
         ("runs/1 噪声包 (v3.0 r2)", "runs/1/g0_pack_r2", "runs/1/_answer_key/g0_keys_r2.json"),
         ("runs/2 (v4)",             "runs/2/g0_pack",    "runs/2/_answer_key/g0_keys.json"),
         ("runs/3 批A (v5)",         "runs/3/_judged_A",  "runs/3/_answer_key/g0_keys.json"),
         ("runs/3 批B (v5)",         "runs/3/g0_pack_B",  "runs/3/_answer_key/g0_keys.json")]
PASSES = ("pass1", "pass2", "pass3")


def table(pack, keys, passes):
    """{case: {候选: 均分}} 与名次表。槽位按答案键还原成候选。"""
    sc, rk = {}, {}
    for c in range(1, 9):
        cs = f"case{c:02d}"
        s, r_ = {}, {}
        for p in passes:
            kk = keys.get(f"{p}/{cs}")
            f = pathlib.Path(pack) / p / cs / "ranking.json"
            if kk is None or not f.exists():
                return None, None
            j = json.load(open(f, encoding="utf-8"))
            for slot, cand in kk.items():
                s.setdefault(cand, []).append(j["scores"][slot])
                r_.setdefault(cand, []).append(j["ranking"].index(slot))
        sc[cs] = {k: st.mean(v) for k, v in s.items()}
        rk[cs] = {k: st.mean(v) for k, v in r_.items()}
    return sc, rk


def evaluate(sc, rk, g):
    """平局按判分方给的名次定胜负（thresholds 的 winrate_tie_mode: rank）。"""
    def wl(a, b):
        W = L = 0
        for cs in sc:
            x, y = sc[cs][a], sc[cs][b]
            if x > y:   W += 1
            elif x < y: L += 1
            else:
                W += rk[cs][a] < rk[cs][b]
                L += rk[cs][a] > rk[cs][b]
        return W, L
    n = len(sc)
    gap = st.mean(sc[c]["ours"] for c in sc) - st.mean(sc[c]["base"] for c in sc)
    Wb, _ = wl("ours", "base"); Ww, _ = wl("ours", "WB"); _, Lbs = wl("ours", "base+skill")
    ok1 = gap >= g["min_score_gap_vs_baseline"]
    ok2 = Wb / n > g["min_winrate_vs_baseline"]
    ok3 = Wb >= g["min_cases_beating_baseline"]
    ok4 = Ww / n > g["min_winrate_vs_primary"]
    ok5 = gap > g["relax_score_gap_vs_baseline"] and Ww / n >= g["relax_winrate_vs_primary"]
    floor, prim = ok1 and ok2 and ok3, ok4 or ok5
    return dict(gap=gap, wrb=Wb / n, beat=f"{Wb}/{n}", wrw=Ww / n, loss=Lbs,
                ok1=ok1, ok2=ok2, ok3=ok3, ok4=ok4, ok5=ok5,
                floor=floor, prim=prim, hard=Lbs == 0,
                passed=floor and prim and Lbs == 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    g = yaml.safe_load(open("gates/thresholds.yaml", encoding="utf-8"))["G4_release"]
    m = lambda b: "✓" if b else "✗"
    L = ["# 准出审计：历史上每一次判分都核一遍", "",
         "判据结构（读 `gates/thresholds.yaml`，与模板 `score_release.eval_criteria()` 对齐）：", "",
         f"- **floor 三条**：分差 ≥{g['min_score_gap_vs_baseline']} 且 对 base 胜率 >"
         f"{g['min_winrate_vs_baseline']:.0%} 且 超过题数 ≥{g['min_cases_beating_baseline']}/8"
         "　←　三条**全部**必须满足",
         f"- **主要对照条**：对 WB 胜率 >{g['min_winrate_vs_primary']:.0%}　**或**　"
         f"松弛（分差 >{g['relax_score_gap_vs_baseline']} 且 对 WB 胜率 ≥"
         f"{g['relax_winrate_vs_primary']:.0%}）　←　松弛**只**替代「对 WB 胜率」这一条",
         "- **准出** = floor 三条 且 主要对照条 且 **硬条件**（对 base+skill 零负场）", "",
         "> ⚠ 常见误读：`score_release.py` 逐轮表的列名「路径一 / 路径二」指的是"
         "floor 三条 / 主要对照条，**不是两条并列通往准出的路径**。"
         "松弛路径替代不了 floor 里的任何一条。", "",
         "| 判分实例 | 分差 | ① | 对base胜率 | ② | 超过题数 | ③ | floor | 对WB | 松弛 | 主要对照 | 硬条件负场 | **准出** |",
         "|---|---:|:--:|---:|:--:|:--:|:--:|:--:|---:|:--:|:--:|:--:|:--:|"]
    n = ok = nf = nh = 0
    for label, pack, kf in PACKS:
        keys = json.load(open(kf, encoding="utf-8"))
        for tag, ps in [("整批", PASSES)] + [(p, (p,)) for p in PASSES]:
            sc, rk = table(pack, keys, ps)
            if sc is None:
                continue
            r = evaluate(sc, rk, g)
            single = tag != "整批"
            n += single; ok += single and r["passed"]
            nf += single and r["floor"]; nh += single and r["hard"]
            L.append(f"| {label} · {tag} | {r['gap']:+.3f} | {m(r['ok1'])} | {r['wrb']:.1%} | "
                     f"{m(r['ok2'])} | {r['beat']} | {m(r['ok3'])} | {m(r['floor'])} | "
                     f"{r['wrw']:.1%} | {m(r['ok5'])} | {m(r['prim'])} | {r['loss']} | "
                     f"{'✅' if r['passed'] else '❌'} |")
    L += ["", "## 结论", "",
          f"- 单次判分实例 **{n}** 个（5 批 × 逐轮），过准出的 **{ok}** 个",
          f"- 连 floor 三条都同时满足的：**{nf}** 个",
          f"- 硬条件（对 base+skill 零负场）满足的：**{nh}** 个", "",
          "**两条判据一次都没满足过**：`超过题数 ≥6/8`（历史最好 5/8）与 `硬条件零负场`（历史最好 1 场）。",
          "其中 `超过题数` 是**每一个**读数都不满足的那一条——它在 floor 里，谁也替代不了。"]
    txt = "\n".join(L) + "\n"
    print(txt)
    if a.out:
        pathlib.Path(a.out).write_text(txt, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
