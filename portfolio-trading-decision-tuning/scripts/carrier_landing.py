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

# v4 的五条载体。**按内容判，不按列名判**——第一版按列名写的正则报了假阴性：
# case02 实际逐条反解了（「风险贡献 95.40%（限 60%）→ 需卖出 21,600 股才达标」），
# 但因为它是列表而不是名为「反解值」的表格列，被判成没落地。教训同经验沉淀第 6 条。
SOLVE = re.compile(
    r"(限\s*[\d.]+\s*%|上限[^|\n]{0,10}[\d.]+\s*%)[^|\n]{0,60}?([\d][\d,]{2,})\s*(股|份)[^|\n]{0,20}?(达标|才|需|可)"
    r"|(达标|反解)[^|\n]{0,12}?([\d][\d,]{2,})\s*(股|份)")
CLAIM = re.compile(
    r"(?:交付|产出|生成|附上|已附)\s*(?:了)?\s*(\d+)\s*(?:份|件|个)\s*(?:文件|交付物|交付件|附件)?"
    r"|(?:全部\s*)?(\d+)\s*(?:份|件)\s*交付(?:物|件)")
CARRIERS = {
    "C-03 决策摘要表在开头": lambda b, a, n: bool(re.match(r"\s*(?:#+\s*)?\|?\s*(决策摘要|结论先行)", b)),
    "C-01 自检回执表": lambda b, a, n: bool(re.search(r"自检回执|交付自检|自检项|声明\s*\d+\s*件", a)),
    "C-02 硬约束反解出数量": lambda b, a, n: bool(SOLVE.search(a)),
    "C-04 规则表有口径列": lambda b, a, n: bool(re.search(r"数量口径|累计总卖出量|本阶段追加量", a)),
    "C-05 写出相邻一手验证": lambda b, a, n: bool(re.search(r"少(卖|一手|100\s*股)[^。\n]{0,40}(超限|不满足|回到)", a)),
}


# ── v5 的五条载体（D-01~D-05）。同样按内容判。────────────────────────
FILEEXT = re.compile(r"[\w\u4e00-\u9fff\-_.]+\.(?:csv|xlsx|docx|md|json|py|html|txt)\b")
CELL = re.compile(r"\b[A-Z]{1,2}\d{1,3}\b")
FORMULA = re.compile(r"(?<![=\w])=[A-Z(]|公式原文")
# D-04 按首句实际内容判：首句若只报交付状态、不带决策要素，即为未落地。
DELIVERY = re.compile(r"已?(?:全部)?(?:完成|交付|生成|送达|输出)|交付\s*\d+\s*(?:件|个|份)|文件已")
# 收紧：只认真正的决策要素。「可改卖出数量」这类描述性用词不算（实测误判过一次）。
DECISION = re.compile(r"结论|最紧|超限|减仓|清仓|不必|待补|"
                      r"[\d][\d,]*\s*(?:股|份|元|万)|[\d.]+\s*%")

def first_sentence(body):
    """取正文第一句：跳过 markdown 标题记号与空行，截到句号或换行。"""
    for line in body.splitlines():
        s = re.sub(r"^[#>*\-\s|]+", "", line).strip()
        if not s or s in ("表格", "plaintext", "---"):
            continue
        return re.split(r"[。；\n]", s)[0][:120]
    return ""

def opening_carries_decision(body):
    """v5 D-04 的准确语义：禁的是「裸交付状态句」开头。

    首句报了交付状态就必须同句带决策要素；首句是标题或表头时不算违反——
    决策摘要表本身是否存在由 C-03 单独判，两条不要重复计分。
    """
    s = first_sentence(body)
    if not s:
        return False
    if DELIVERY.search(s):
        return bool(DECISION.search(s))
    return True

CARRIERS_V5 = {
    "D-01 回执里写了实际文件名": lambda b, a, n: len(set(FILEEXT.findall(a))) >= 2,
    "D-01 主工件产出为文件或标注未另出": lambda b, a, n: bool(
        re.search(r"正文内含[^。\n]{0,12}未另出文件|未另出文件", a)) or bool(
        n and any(x.endswith((".md", ".docx")) for x in n)),
    "D-02 工作簿可复算抽查有证据": lambda b, a, n: bool(
        re.search(r"抽查[^。\n]{0,30}(单元格|公式)", a)) and bool(CELL.search(a)) and bool(FORMULA.search(a)),
    "D-03 占比写出分母": lambda b, a, n: bool(re.search(r"分母[^。\n]{0,30}?[\d][\d,]{2,}|分母\s*[=＝:：]", a)),
    "D-04 开头句自带决策": lambda b, a, n: opening_carries_decision(b),
    "D-05 状态表有相邻一手列": lambda b, a, n: bool(re.search(r"相邻一手|少一手", a)),
}


# ── v6 的四条（E-01~E-04）。按「载体分三类」重做：只加字段，不要求新增动作。──
TOTAL_CLAIM = re.compile(r"(?:交付|产出|生成|送达|输出)\s*(?:了)?\s*\d+\s*(?:件|个|份)|\d+\s*(?:件|个|份)\s*(?:文件|交付物|交付件)")
CARRIERS_V6 = {
    "E-01 工作簿含「核验」表": lambda b, a, n: bool(re.search(r"核验(?:结果)?表|核验\s*[|｜]|表名[^。\n]{0,20}核验", a)),
    "E-02 只枚举文件名、不报总数": lambda b, a, n: (not TOTAL_CLAIM.search(b)) and len(set(FILEEXT.findall(a))) >= 1,
    "E-02 主工件指认了位置": lambda b, a, n: bool(
        re.search(r"(?:见|在|位于)[^。\n]{0,24}(?:\.(?:xlsx|docx|md|csv)|工作表|第[一二三四五六七八九十\d]+节)", a))
        or bool(re.search(r"仅正文内含|未进交付件", a)),
    "E-04 决策摘要表存在（不论位置）": lambda b, a, n: bool(re.search(r"决策摘要|结论先行", a)),
}

# ── v7 的三条（G-01 冲突归属、G-02 币种含现金）。按内容判，不按句式判。────
# G-01 的病：硬约束与软偏好不可兼得时，把二选一交回客户/风控当结论。
# 只在请示语与「超限/违反/放宽/无解」**同句**时才算未落地——
# 「偏好偏离须客户书面确认」本身是合规写法，一并判就是第 6 条那种假阴性。
PUNT = (r"(?:须|由|交(?:由)?|请)(?:客户|风控|投资者|投委会)[^。；！\n]{0,14}"
        r"(?:确认|拍板|决定|选择|授权|定夺)|知情接受|客户自行(?:选择|决定)|二选一")
BREACH = r"超限|超过(?:上限|阈值)|违反|放宽|不合规|无解|硬(?:约束|线)"
PUNT_CLAUSE = re.compile(r"[^。；！\n]*(?:" + PUNT + r")[^。；！\n]*")

def no_punt_closing(full):
    """G-01：硬约束的解不得交回客户/风控。请示语与超限/放宽同句即判未落地。"""
    for s in PUNT_CLAUSE.findall(full):
        if re.search(BREACH, s):
            return False
    return True

# G-02 只适用于题面设了币种上限的题（按纪律包：case03、case06）。
# 其余题返回 None = 不适用，不进分母——否则 1/8 会被误读成失败。
FX_CTX = re.compile(r"币种|外币|港币|美元|汇率|HKD|USD")
FX_CASH = re.compile(r"币种[^。\n]{0,30}(?:含|计入|包含|加上)[^。\n]{0,14}现金"
                     r"|现金[^。\n]{0,16}(?:计入|纳入)[^。\n]{0,12}币种(?:敞口)?")

CARRIERS_V7 = {
    "G-01 无请示型收口（硬约束不交客户拍板）": lambda b, a, n: no_punt_closing(a),
    "G-01 回执写出与最紧约束冲突的软偏好": lambda b, a, n: bool(
        re.search(r"冲突[^。\n|]{0,10}软?偏好|与之冲突", a)),
    "G-02 币种敞口计入该币种现金": lambda b, a, n: (
        bool(FX_CASH.search(a)) if FX_CTX.search(a) else None),
}

def claim_vs_actual(body, names):
    """正文声称的件数 vs 实际产出。runs/1 与 runs/2 的同一处失分点。"""
    m = CLAIM.search(body)
    if not m:
        return None
    return int(m.group(1) or m.group(2)), len(names)

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
        for k, f in {**CARRIERS, **CARRIERS_V5, **CARRIERS_V6, **CARRIERS_V7}.items():
            res.setdefault(k, {})[c] = f(b, full, names)
        claims[c] = (claim_vs_actual(b, names), len(names))
    if not res:
        sys.exit("没读到任何答复")
    done = [c for c in cases if c in next(iter(res.values()))]
    lines = ["# 载体落地核验（内部主判据，判分噪声 = 0）", "",
             f"题数 {len(done)}｜轮次 {a.round}", "",
             "| 载体 | 逐题 | 落地 |", "|---|---|---:|"]
    for k, v in res.items():
        # None = 该题不适用（如题面没设币种上限），记「·」并从分母里剔除
        cells = "".join("·" if v[c] is None else ("✓" if v[c] else "✗") for c in done)
        appl = [c for c in done if v[c] is not None]
        n = sum(1 for c in appl if v[c])
        lines.append(f"| {k} | `{cells}` | **{n}/{len(appl)}** |")
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
