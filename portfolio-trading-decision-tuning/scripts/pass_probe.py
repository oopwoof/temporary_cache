#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三轮独立性的**机械**判定：往判分包里埋两处无害扰动，回包后机械核。

起因：三轮被合成一轮这种失效已经两次（runs/3 批 A、runs/4）。
两次我都是靠「评语逐字相同」这种间接证据才发现的，很被动；
runs/4 那包整整一节在讲这件事、还加了两条回执要求，回包里一个字都没答。

## 埋什么（两处，都不影响判分）

1. **`pass_token`**：每个 `pass<N>/case<NN>/ranking.json` 预置一段随机 hex，要求原样保留。
   它证明**这份文件来自那一轮的目录**。
2. **`longest_label`**：要求填「本题里**字数最多**的那份答复的标签」。
   标签每轮重新洗，所以正确答案逐轮不同（本包实测 8/8 题可判别）。
   它证明**填表的人真的打开了那一轮的答复**，而且我这边能独立复算、编不出来。

## 这两处**不能**证明什么（写在前面，免得高估）

它们证明不了「三轮是独立判的」。一个人完全可以判一次、
再按每轮的洗牌把分数映射过去、顺手把 token 和 longest_label 填对。
**真正的判据仍然是 `--verify` 里那两条内容级检查**：
按候选归一后分数是否逐格相同、评语原句是否逐字相同。
埋扰动的价值是**定位**——告诉我塌在哪一层（整文件复制 / 内容级映射），
以及给编排方一个具体的、可对错的东西去照做。

用法:
    python3 scripts/pass_probe.py inject --pack runs/5/g0_pack --key runs/5/_answer_key/probe_key.json
    python3 scripts/pass_probe.py verify --pack runs/5/g0_pack --key runs/5/_answer_key/probe_key.json \
        --answer-key runs/5/_answer_key/g0_keys.json
"""
import argparse, json, pathlib, secrets, sys


def cases_of(pack):
    for p in sorted(pack.glob("pass*")):
        if not p.is_dir():
            continue
        for c in sorted(p.glob("case*")):
            if c.is_dir():
                yield p.name, c.name, c


def longest_label(case_dir):
    """本题字数最多的那份答复的标签。按**字符数**，不按字节。"""
    best, n = None, -1
    for f in sorted((case_dir / "答复").glob("*.md")):
        k = len(f.read_text(encoding="utf-8"))
        if k > n:
            best, n = f.stem, k
    return best


def inject(a):
    pack = pathlib.Path(a.pack)
    tpl = json.loads((pack / "templates" / "ranking.json").read_text(encoding="utf-8"))
    key = {}
    for ps, cs, cd in cases_of(pack):
        labels = sorted(f.stem for f in (cd / "答复").glob("*.md"))
        stub = dict(tpl)
        stub["case"] = cs.replace("case", "")
        stub["ranking"] = labels
        stub["scores"] = {l: 3 for l in labels}
        stub["pass_token"] = secrets.token_hex(8)
        stub["longest_label"] = ""          # 判分方填
        (cd / "ranking.json").write_text(
            json.dumps(stub, ensure_ascii=False, indent=1), encoding="utf-8")
        key[f"{ps}/{cs}"] = {"pass_token": stub["pass_token"],
                             "longest_label": longest_label(cd)}
    pathlib.Path(a.key).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(a.key).write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    disc = sum(1 for c in {k.split("/")[1] for k in key}
               if len({key[k]["longest_label"] for k in key if k.endswith(c)}) > 1)
    print(f"已埋 {len(key)} 个题次｜答案留在 {a.key}（**不要随包外发**）")
    print(f"longest_label 逐轮不同的题: {disc}/{len({k.split('/')[1] for k in key})}"
          f"（相同的题上这个字段不产生信息，属正常）")


def verify(a):
    pack = pathlib.Path(a.pack)
    key = json.loads(pathlib.Path(a.key).read_text(encoding="utf-8")) if a.key else {}
    bad_tok, bad_lab, missing = [], [], []
    for ps, cs, cd in cases_of(pack):
        f = cd / "ranking.json"
        if not f.exists():
            missing.append(f"{ps}/{cs}"); continue
        r = json.loads(f.read_text(encoding="utf-8"))
        k = key.get(f"{ps}/{cs}")
        if k:
            if r.get("pass_token") != k["pass_token"]:
                bad_tok.append(f"{ps}/{cs}: 期望 {k['pass_token']} 实得 {r.get('pass_token')!r}")
            if r.get("longest_label") != k["longest_label"]:
                bad_lab.append(f"{ps}/{cs}: 正确 {k['longest_label']} 实填 {r.get('longest_label')!r}")
    print("## 扰动核验（证明文件来自本轮目录 / 填表人打开过本轮答复）\n")
    print(f"- 缺 ranking.json：{len(missing)}" + (f" {missing[:5]}" if missing else ""))
    if key:
        print(f"- `pass_token` 对不上：{len(bad_tok)}")
        for x in bad_tok[:5]: print(f"    · {x}")
        print(f"- `longest_label` 填错：{len(bad_lab)}")
        for x in bad_lab[:5]: print(f"    · {x}")
    else:
        print("- （没给 --key，跳过扰动核验）")

    # ── 真正的判据：内容级 ────────────────────────────────────────
    ak = json.loads(pathlib.Path(a.answer_key).read_text(encoding="utf-8")) if a.answer_key else None
    passes = sorted({ps for ps, _, _ in cases_of(pack)})
    cases = sorted({cs for _, cs, _ in cases_of(pack)})
    same_slot = same_cand = same_quote = 0
    for cs in cases:
        rows_s, rows_c, rows_q = [], [], []
        for ps in passes:
            r = json.loads((pack / ps / cs / "ranking.json").read_text(encoding="utf-8"))
            labs = sorted(r["scores"])
            rows_s.append(tuple(r["scores"][l] for l in labs))
            rows_q.append(tuple(x.get("quote", "") for x in r.get("why_first", []) + r.get("why_last", [])))
            if ak:
                m = ak[f"{ps}/{cs}"]
                rows_c.append(tuple(sorted((m[l], r["scores"][l]) for l in labs)))
        same_slot += len(set(rows_s)) == 1
        same_quote += len(set(rows_q)) == 1
        if ak: same_cand += len(set(rows_c)) == 1
    n = len(cases)
    print("\n## 内容级判据（这才是真正的独立性判据）\n")
    print(f"- 按**槽位字母**三轮全同：{same_slot}/{n}   （高 = 疑似照字母抄）")
    if ak:
        print(f"- 按**候选映射**三轮全同：{same_cand}/{n}   ← **≥90% 即判三轮不独立**")
    print(f"- **评语原句**三轮逐字全同：{same_quote}/{n}   ← 比分数灵敏，通常先触发")
    fail = (ak and same_cand >= 0.9 * n) or same_quote >= 0.5 * n or missing or bad_tok or bad_lab
    print("\n## 结论\n")
    if fail:
        print("✗ **不合格**：见上（扰动对不上 = 塌在文件层；内容级全同 = 塌在映射层）")
    elif key:
        print("✓ 通过：扰动全对，且三轮在内容级有分歧")
    else:
        print("✓ 内容级通过：三轮有分歧。**扰动未核**（没给 --key），"
              "所以只证明了内容不同，没证明文件来自各自轮次的目录")
    return 1 if fail else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inject"); i.add_argument("--pack", required=True); i.add_argument("--key", required=True)
    v = sub.add_parser("verify"); v.add_argument("--pack", required=True)
    v.add_argument("--key"); v.add_argument("--answer-key")
    a = ap.parse_args()
    sys.exit(inject(a) or 0 if a.cmd == "inject" else verify(a))


if __name__ == "__main__":
    main()
