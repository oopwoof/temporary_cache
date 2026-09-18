#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""噪声底分解 —— 两包判完后跑。

设计：噪声包里**基线四方与第一包逐字相同**，只有 ours 从第 1 轮换成第 2 轮。于是

    基线四方两包之差 = 判分噪声（同一份输入、同一把尺子，重判得到的差）
    ours 两包之差     = 判分噪声 + 模型噪声
    两者相减          ≈ 模型噪声（同一版本 skill 重跑一遍的抖动）

没有这个分解，「v4 比 v3 好」分不清是进步还是抖动——效应量必须和噪声底同表报。

用法:
    python3 scripts/noise_floor.py \\
        --pack-a runs/1/g0_pack      --keys-a runs/1/_answer_key/g0_keys.json \\
        --pack-b runs/1/g0_pack_r2   --keys-b runs/1/_answer_key/g0_keys_r2.json \\
        --out runs/1/噪声底.md
"""
import argparse, json, pathlib, statistics, sys

def load(pack, keys):
    keys = json.load(open(keys, encoding="utf-8"))
    pack = pathlib.Path(pack)
    out = {}                      # (party, case) -> [三轮分]
    for k, m in keys.items():
        p, case = k.split("/")
        rj = pack / p / case / "ranking.json"
        if not rj.exists():
            continue
        d = json.load(open(rj, encoding="utf-8"))
        for lab, s in (d.get("scores") or {}).items():
            party = m.get(lab)
            if party:
                out.setdefault((party, case[-2:]), []).append(s)
    return out

def mean(xs):
    return sum(xs) / len(xs) if xs else None

def main():
    ap = argparse.ArgumentParser()
    for x in ("pack-a", "keys-a", "pack-b", "keys-b"):
        ap.add_argument(f"--{x}", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    A, B = load(a.pack_a, a.keys_a), load(a.pack_b, a.keys_b)
    if not A or not B:
        sys.exit("有一包还没判回来")
    cases = sorted({c for _, c in A} & {c for _, c in B})
    base_parties = ["base", "base+skill", "WB", "gpt"]

    judge_d, model_d = [], []
    rows = []
    for c in cases:
        for p in base_parties:
            x, y = mean(A.get((p, c), [])), mean(B.get((p, c), []))
            if x is not None and y is not None:
                judge_d.append(abs(x - y))
                rows.append((c, p, x, y, y - x, "判分噪声"))
        x, y = mean(A.get(("ours", c), [])), mean(B.get(("ours", c), []))
        if x is not None and y is not None:
            model_d.append(abs(x - y))
            rows.append((c, "ours", x, y, y - x, "判分+模型"))

    jn, mn = mean(judge_d), mean(model_d)
    lines = ["# 噪声底分解", "",
             "基线四方在两包里是**逐字相同的输入**，只有 ours 从第 1 轮换成第 2 轮。", "",
             "| 量 | 平均绝对差 | 含义 |", "|---|---:|---|",
             f"| 基线四方（{len(judge_d)} 个格） | **{jn:.3f}** | 判分噪声：同一份答复重判的抖动 |",
             f"| ours（{len(model_d)} 个格） | **{mn:.3f}** | 判分噪声 + 模型噪声 |",
             f"| 相减 | **{max(mn - jn, 0):.3f}** | ≈ 模型噪声：同一版本重跑一遍的抖动 |", "",
             f"准出的分差门槛是 **0.4**。判分噪声 {jn:.3f} "
             + ("**已经吃掉门槛的一半以上**，这种情况下 0.4 的分差不足以证明版本变好。"
                if jn >= 0.2 else "低于门槛的一半，分差结论相对可信。"), "",
             "## 逐格明细", "",
             "| 题 | 方 | 第一包均分 | 噪声包均分 | 差 | 量的是什么 |", "|---|---|---:|---:|---:|---|"]
    for c, p, x, y, d, kind in rows:
        lines.append(f"| case{c} | {p} | {x:.2f} | {y:.2f} | {d:+.2f} | {kind} |")
    txt = "\n".join(lines) + "\n"
    print(txt)
    if a.out:
        pathlib.Path(a.out).write_text(txt, encoding="utf-8")

if __name__ == "__main__":
    main()
