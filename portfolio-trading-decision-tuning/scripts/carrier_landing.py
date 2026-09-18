#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""载体落地核验 —— 内部主判据，判分噪声为 0。

runs/1 实测：有载体的规则落地并赢下题次，纯措辞的规则两轮都没落地。
v4 的五条改动全部是载体，因此可以**完全机械地**验收——不依赖判分方、没有噪声。

用法:
    python3 scripts/carrier_landing.py --run <跑题回包目录> --round 1 --out runs/2/载体落地.md

目录约定（跑题回包）:
    <run>/runs/<round>/responses[_rerun]/ours_case<NN>.md
    <run>/runs/<round>/response_files/ours_case<NN>/
"""
import argparse, json, pathlib, re, sys

def xlsx_text(p):
    best = ""
    try:
        import openpyxl
    except ImportError:
        return best
    for ro in (False, True):
        try:
            wb = openpyxl.load_workbook(p, read_only=ro, data_only=True)
            t = "\n".join(str(ws.title) + "\n" + "\n".join(
                " ".join("" if v is None else str(v) for v in r)
                for r in ws.iter_rows(values_only=True)) for ws in wb.worksheets)
            if len(t) > len(best):
                best = t                      # 两种读法都跑、取多的那个
        except Exception:
            pass
    return best

def docx_text(p):
    try:
        import docx
    except ImportError:
        return ""
    try:
        d = docx.Document(p)
        return "\n".join([q.text for q in d.paragraphs]
                         + [" ".join(c.text for c in r.cells)
                            for t in d.tables for r in t.rows])
    except Exception:
        return ""

def collect(run, rnd, case):
    base = pathlib.Path(run) / "runs" / str(rnd)
    sub = "responses" if rnd == 1 else "responses_rerun"
    body = (base / sub / f"ours_case{case}.md").read_text(encoding="utf-8")
    files, names = "", []
    fd = base / "response_files" / f"ours_case{case}"
    if fd.exists():
        for f in sorted(fd.iterdir()):
            if not f.is_file() or f.name.startswith("_"):
                continue
            names.append(f.name)
            if f.suffix in (".md", ".csv", ".json", ".py", ".txt"):
                files += f.read_text(encoding="utf-8", errors="ignore")
            elif f.suffix == ".xlsx":
                files += xlsx_text(f)
            elif f.suffix == ".docx":
                files += docx_text(f)
    return body, body + files, names

# v4 的五条载体。每条给「在不在」与「填没填」两级——在不在容易，填没填才是 v4 的赌注。
CARRIERS = {
    "C-03 决策摘要表在开头": lambda b, a, n: bool(
        re.match(r"\s*(#+\s*)?\|?\s*(决策摘要|结论先行)", b)) or "决策摘要" in b[:400],
    "C-01 自检回执表存在": lambda b, a, n: bool(re.search(r"自检回执|交付自检|自检项", a)),
    "C-01 回执填了件数": lambda b, a, n: bool(re.search(r"声明\s*\d+\s*件|实际\s*\d+\s*件|\d+\s*件\s*/\s*\d+\s*件", a)),
    "C-02 反解值列存在": lambda b, a, n: bool(re.search(r"反解值|达标所需|反解达标", a)),
    "C-02 反解值填了数量": lambda b, a, n: bool(re.search(r"(反解值|达标所需[^|\n]{0,8})[^|\n]{0,20}?[\d,]{3,}\s*股", a)),
    "C-04 规则表有口径列": lambda b, a, n: bool(re.search(r"数量口径|累计总卖出量|本阶段追加量", a)),
    "C-05 写出相邻一手验证": lambda b, a, n: bool(re.search(r"少(卖|一手|100\s*股)[^。\n]{0,40}(超限|不满足|回到)", a)),
}
# 直击 runs/1 两处硬门禁的核验（能机械判的部分）
def claim_vs_actual(body, names):
    m = re.search(r"(?:全部\s*)?(\d+)\s*(?:份|件)\s*交付(?:物|件)", body)
    if not m:
        return None
    return int(m.group(1)), len(names)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--cases", default="01,02,03,04,05,06,07,08")
    ap.add_argument("--out")
    a = ap.parse_args()
    cases = [c.strip() for c in a.cases.split(",")]
    res, claims = {}, {}
    for c in cases:
        try:
            b, full, names = collect(a.run, a.round, c)
        except FileNotFoundError:
            print(f"⚠ case{c} 缺文件，跳过", file=sys.stderr)
            continue
        for k, f in CARRIERS.items():
            res.setdefault(k, {})[c] = f(b, full, names)
        claims[c] = (claim_vs_actual(b, names), len(names))
    if not res:
        sys.exit("没读到任何答复")
    done = [c for c in cases if c in next(iter(res.values()))]
    lines = ["# 载体落地核验（内部主判据，判分噪声 = 0）", "",
             f"题数 {len(done)}｜轮次 {a.round}", "",
             "| 载体 | 逐题 | 落地 |", "|---|---|---:|"]
    for k, v in res.items():
        cells = "".join("✓" if v[c] else "✗" for c in done)
        n = sum(1 for c in done if v[c])
        lines.append(f"| {k} | `{cells}` | **{n}/{len(done)}** |")
    lines += ["", "## 交付声明 vs 实际件数（runs/1 case01 的失分点）", "",
              "| 题 | 正文声称 | 实际产出 | 一致 |", "|---|---:|---:|:--:|"]
    for c in done:
        cv, actual = claims[c]
        if cv is None:
            lines.append(f"| case{c} | 未声称件数 | {actual} | — |")
        else:
            claimed, act = cv
            lines.append(f"| case{c} | {claimed} | {act} | {'✓' if claimed == act else '**✗**'} |")
    txt = "\n".join(lines) + "\n"
    print(txt)
    if a.out:
        pathlib.Path(a.out).write_text(txt, encoding="utf-8")

if __name__ == "__main__":
    main()
