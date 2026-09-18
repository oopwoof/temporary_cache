#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复算解包后的目录内容摘要，并与校验清单逐文件比对。

用法: python3 校验.py <解包后的 skill 目录>
"""
import hashlib, pathlib, re, sys

d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "portfolio-trading-decision")
if not d.is_dir():
    sys.exit(f"找不到目录 {d}——先解开 zip 再跑")
h, per = hashlib.sha256(), {}
for f in sorted(d.rglob("*")):
    if f.is_file():
        rel = f.relative_to(d).as_posix()
        b = f.read_bytes()
        h.update(rel.encode()); h.update(b)
        per[rel] = hashlib.sha256(b).hexdigest()
got = h.hexdigest()
want = "b5082caece480718bfb59266ede22514aac9cf41fa24bc9554dae03ec331079b"
print(f"目录内容摘要 期望 {want}")
print(f"             实测 {got}")
if got == want:
    sys.exit(print("✓ 内容一致，可以开跑") or 0)
print("✗ 内容不一致，逐文件比对：")
man = pathlib.Path("校验清单.txt")
if not man.exists():
    sys.exit("缺 校验清单.txt，无法定位")
exp = dict(reversed(l.split("  ", 1)) for l in
           man.read_text(encoding="utf-8").splitlines()
           if l and not l.startswith("#") and "  " in l)
for rel in sorted(set(exp) | set(per)):
    if exp.get(rel) != per.get(rel):
        print(f"   {rel}: "
              + ("缺失" if rel not in per else
                 "多出" if rel not in exp else "内容不同"))
sys.exit(1)
