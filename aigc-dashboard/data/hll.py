# -*- coding: utf-8 -*-
"""
HyperLogLog 基数估算 —— 让"参与人数"可以跨天、跨点位去重。

为什么需要它
------------
daily 是按 天 × 点位 聚合的。人数和分位数一样, **不能**把每格算好的标量相加:
同一个人今天来南京路、明天来环球港, 直接求和会把他算成 3 个人。
所以每格存的不是一个数, 而是一个可合并的草图 —— 任意日期 × 点位切片下,
先合并草图再估算, 才是该切片真正的去重人数。
(这与 gen_seconds_hist 存直方图而不是存 P95 是同一个思路: 可加的存标量,
 不可加的存结构。)

规格 —— 必须与 index.html 中的 JS 实现逐位一致
------------------------------------------------
  b = 10 → m = 1024 个寄存器, 理论标准误 1.04/√m ≈ 3.25%
  哈希 = 32 位 FNV-1a + murmur3 finalizer(fmix32)
  高 b 位取寄存器下标, 低 32-b 位数前导零 +1, 寄存器取最大值
  小基数走线性计数, 大基数走标准 HLL + alpha 修正
  寄存器按 1 字节存, base64 编码后进 JSON

关于 finalizer: 不能省。裸 FNV-1a 对 "u0000001" 这类连续键混合不足,
实测 n=300 时估算偏低 49%, 寄存器下标卡方 2473(理想 1023)。
加 fmix32 后最大误差 3.9%, 卡方 1093。

寄存器为什么按 1 字节而不是 4 bit 打包: 22 位尾部的理论最大 rank 是 23,
全量十几万次抽样中预计有几次超过 15, 打包会静默截断。
"""

import base64
import math

HLL_B = 10
HLL_M = 1 << HLL_B           # 1024
_TAIL_BITS = 32 - HLL_B      # 22
_ALPHA = 0.7213 / (1 + 1.079 / HLL_M)
_LINEAR_COUNTING_CUTOFF = 2.5 * HLL_M


def hash32(s: str) -> int:
    """32 位 FNV-1a, 再过一遍 murmur3 的 fmix32 做雪崩。"""
    h = 0x811C9DC5
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def hll_new():
    return [0] * HLL_M


def hll_add(regs, item: str) -> None:
    h = hash32(item)
    idx = h >> _TAIL_BITS                       # 高 10 位 = 寄存器下标
    tail = (h << HLL_B) & 0xFFFFFFFF            # 低 22 位左移到高位, 便于数前导零
    if tail == 0:
        rank = _TAIL_BITS + 1
    else:
        rank = 1
        while not tail & 0x80000000:
            tail = (tail << 1) & 0xFFFFFFFF
            rank += 1
    if rank > regs[idx]:
        regs[idx] = rank


def hll_merge(target, other) -> None:
    """并集 = 寄存器逐位取最大。这就是"可合并"的全部含义。"""
    for i in range(HLL_M):
        if other[i] > target[i]:
            target[i] = other[i]


def hll_estimate(regs) -> float:
    zeros = 0
    inv = 0.0
    for r in regs:
        if r == 0:
            zeros += 1
        inv += 2.0 ** -r
    est = _ALPHA * HLL_M * HLL_M / inv
    if est <= _LINEAR_COUNTING_CUTOFF and zeros > 0:
        # 小基数时 HLL 的估计量偏差大, 改用线性计数(按空寄存器占比反推)
        return HLL_M * math.log(HLL_M / zeros)
    return est


def hll_encode(regs) -> str:
    return base64.b64encode(bytes(regs)).decode("ascii")


def hll_decode(b64: str):
    return list(base64.b64decode(b64))
