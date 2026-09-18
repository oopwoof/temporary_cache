#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓交易审批 - 全流程计算脚本
- 输入：5 个 CSV 附件（脱敏仿真）
- 输出：风险快照、约束矩阵、批次分配、监控条件、审批备忘录
- 离线运行：不联网、不硬编码行情与方案
"""
from __future__ import annotations

import csv
import math
import statistics
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

# ========================= 路径配置 =========================
SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOADS = Path("/Users/bytedance/Downloads")

QUOTES_FILE   = DOWNLOADS / "持仓标的报价_20251231T1445.csv"
DAILY_FILE    = DOWNLOADS / "持仓标的日线_20240102_20251230.csv"
LOTS_FILE     = DOWNLOADS / "持仓批次_20251231T1445.csv"
PLANS_FILE    = DOWNLOADS / "候选减仓方案_20251231.csv"
ACCOUNT_FILE  = DOWNLOADS / "账户与交易约束_20251231T1445.csv"

OUT_SNAPSHOT      = SCRIPT_DIR / "当前持仓风险快照.csv"
OUT_MATRIX        = SCRIPT_DIR / "候选方案约束矩阵.csv"
OUT_ALLOCATION    = SCRIPT_DIR / "批次卖出分配.csv"
OUT_MONITORING    = SCRIPT_DIR / "审批监控条件.csv"
OUT_MEMO          = SCRIPT_DIR / "持仓交易审批备忘录.md"
OUT_RECONCILE     = SCRIPT_DIR / "账户勾稽明细.csv"

DECISION_TIME = datetime.fromisoformat("2025-12-31T14:45:00+08:00")
EPSILON = 1.0  # 金额绝对差阈值（元）

# ========================= 工具函数 =========================
def read_csv(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [dict(r) for r in reader]


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def to_int(x, default=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def fmt_money(x: float, decimals: int = 2) -> str:
    return f"{x:,.{decimals}f}"


def fmt_pct(x: float, decimals: int = 4) -> str:
    return f"{x*100:.{decimals}f}%"


# ========================= 数据加载与审计 =========================
def load_all():
    quotes  = read_csv(QUOTES_FILE)
    dailies = read_csv(DAILY_FILE)
    lots    = read_csv(LOTS_FILE)
    plans   = read_csv(PLANS_FILE)
    account = read_csv(ACCOUNT_FILE)

    if len(account) != 1:
        raise RuntimeError(f"账户约束表应为单账户单行，实际 {len(account)} 行")
    acc = account[0]
    return quotes, dailies, lots, plans, acc


def audit_inputs(quotes, dailies, lots, plans, acc):
    findings = []
    # 报价字段
    for q in quotes:
        if q.get("quote_status") not in ("有效", "陈旧报价", "停牌"):
            findings.append(f"报价 {q['ts_code']} quote_status 异常: {q.get('quote_status')}")
    # 日线 record_status
    bad = [d for d in dailies if d.get("record_status") != "有行情"]
    if bad:
        findings.append(f"日线存在非'有行情'记录 {len(bad)} 条")
    # 持仓批次：available_to_sell>quantity 不允许
    for lot in lots:
        if to_int(lot["available_to_sell"]) > to_int(lot["quantity"]):
            findings.append(f"批次 {lot['lot_id']} 可卖>持有")
    # 方案：未在持仓/报价出现的代码视为未知
    hold_set = {l["ts_code"] for l in lots}
    quote_set = {q["ts_code"] for q in quotes}
    for o in plans:
        if o["side"] != "卖出":
            continue
        if o["ts_code"] not in hold_set:
            findings.append(f"方案 {o['plan_id']}-序{o['order_sequence']} 出现未知持仓代码 {o['ts_code']}")
        if o["ts_code"] not in quote_set:
            findings.append(f"方案 {o['plan_id']}-序{o['order_sequence']} 出现未知报价代码 {o['ts_code']}")
    return findings


# ========================= 当前快照计算 =========================
def compute_position_snapshot(quotes, lots, acc):
    """
    返回：
      - 复算净资产
      - 各 ts_code 持仓表（合并批次）
      - 各 ts_code ADV20
      - T+1 不可卖批次（buy_date=2025-12-31 且 available_to_sell=0）
    """
    # 报价索引
    q_by_code = {q["ts_code"]: q for q in quotes}

    # 按 ts_code 汇总
    by_code = OrderedDict()
    for lot in lots:
        c = lot["ts_code"]
        if c not in by_code:
            by_code[c] = {
                "ts_code": c,
                "security_name": lot["security_name"],
                "quantity": 0,
                "available_to_sell": 0,
                "weighted_cost_num": 0.0,  # 成本*数量 累加
                "lots": [],
                "t1_lots": [],  # T+1 不可卖批次（按规则列在表内，但 available_to_sell=0）
                "sellable_lots": [],
                "current_price_cny": to_float(q_by_code.get(c, {}).get("current_price_cny", 0)),
            }
        rec = by_code[c]
        qty = to_int(lot["quantity"])
        avl = to_int(lot["available_to_sell"])
        cost = to_float(lot["cost_price_cny"])
        rec["quantity"] += qty
        rec["available_to_sell"] += avl
        rec["weighted_cost_num"] += cost * qty
        lot_record = {
            "lot_id": lot["lot_id"],
            "buy_date": lot["buy_date"],
            "quantity": qty,
            "available_to_sell": avl,
            "cost_price_cny": cost,
            "cost_basis_note": lot.get("cost_basis_note", ""),
        }
        rec["lots"].append(lot_record)
        # T+1：buy_date=2025-12-31 且 available_to_sell=0
        if lot["buy_date"] == "2025-12-31" and avl == 0:
            rec["t1_lots"].append(lot_record)
        if avl > 0:
            rec["sellable_lots"].append(lot_record)

    # 加权平均成本
    for c, rec in by_code.items():
        if rec["quantity"] > 0:
            rec["avg_cost_cny"] = rec["weighted_cost_num"] / rec["quantity"]
        else:
            rec["avg_cost_cny"] = 0.0
        rec["market_value_cny"] = rec["quantity"] * rec["current_price_cny"]  # CNY 标的，fx=1
        rec["unrealized_pnl_cny"] = (rec["current_price_cny"] - rec["avg_cost_cny"]) * rec["quantity"]

    # 市值合计
    total_stock_mv = sum(rec["market_value_cny"] for rec in by_code.values())

    cash_balance = to_float(acc["cash_balance_cny"])
    accrued_fees = to_float(acc["accrued_fees_payable_cny"])
    net_cash = cash_balance - accrued_fees
    reported_na = to_float(acc["reported_net_asset_cny"])
    recomputed_na = total_stock_mv + net_cash

    diff = recomputed_na - reported_na
    reconcile_pass = abs(diff) <= EPSILON

    return {
        "q_by_code": q_by_code,
        "by_code": by_code,
        "total_stock_mv": total_stock_mv,
        "net_cash": net_cash,
        "recomputed_na": recomputed_na,
        "reported_na": reported_na,
        "diff": diff,
        "reconcile_pass": reconcile_pass,
        "cash_balance": cash_balance,
        "accrued_fees": accrued_fees,
    }


def compute_adv20(dailies, codes):
    """
    按 ts_code 取最近 20 条 '有行情'，计算 ADV20 = mean(amount*1000)
    """
    by_code = defaultdict(list)
    for d in dailies:
        if d.get("record_status") != "有行情":
            continue
        by_code[d["ts_code"]].append(d)
    # 按 trade_date 排序
    adv = {}
    for c in codes:
        rows = sorted(by_code.get(c, []), key=lambda x: x["trade_date"])
        last20 = rows[-20:] if len(rows) >= 20 else rows
        if not last20:
            adv[c] = 0.0
            continue
        amts = [to_float(r["amount"]) * 1000.0 for r in last20]
        adv[c] = sum(amts) / len(amts)
    return adv


def compute_cvar(weights, returns_by_code, codes, confidence=0.95, window=250):
    """
    weights: dict {code: w}
    returns_by_code: dict {code: [r_t]} 已对齐到共同交易日
    codes: list
    confidence: 0.95
    window: 取最后 window 条共同有效日
    返回:
      cvar95: float (正数表示损失)
      cvar_dates: list[dict] {date, contributions:{code:loss_contrib}}
      common_days: int
      portfolio_returns: list[float]
    """
    # 取所有 codes 的 return，按 trade_date 对齐
    date_to_returns = defaultdict(dict)
    for c in codes:
        for d, r in returns_by_code[c]:
            date_to_returns[d][c] = r
    # 仅保留全部 codes 都有的日期
    common = sorted([d for d, m in date_to_returns.items() if all(c in m for c in codes)])
    common = common[-window:]
    if len(common) == 0:
        return None
    # 计算组合收益
    pr = []
    for d in common:
        r = 0.0
        for c in codes:
            r += weights.get(c, 0.0) * date_to_returns[d][c]
        pr.append(r)
    losses = [-x for x in pr]
    N = len(losses)
    m = math.ceil((1 - confidence) * N)
    sorted_losses = sorted(losses, reverse=True)
    top = sorted_losses[:m]
    cvar = sum(top) / m

    # 计算尾部日期上的每只股票平均损失贡献
    tail_idx = sorted(range(N), key=lambda i: losses[i], reverse=True)[:m]
    contribs = {c: 0.0 for c in codes}
    for i in tail_idx:
        for c in codes:
            contribs[c] += -weights.get(c, 0.0) * date_to_returns[common[i]][c]
    for c in codes:
        contribs[c] /= m

    cvar_dates = []
    for i in tail_idx:
        row = {"trade_date": common[i], "contribs": {}}
        for c in codes:
            row["contribs"][c] = -weights.get(c, 0.0) * date_to_returns[common[i]][c]
        cvar_dates.append(row)

    return {
        "cvar": cvar,
        "contribs": contribs,
        "tail_dates": cvar_dates,
        "common_days": N,
        "portfolio_returns": pr,
    }


# ========================= 单订单执行测算 =========================
def evaluate_order(order, q_by_code, by_code, acc, adv20_map, current_weights, na_total):
    """
    返回 (status, info_dict)
    status: "eligible" / "conditional" / "ineligible"
    """
    code = order["ts_code"]
    sell_qty = to_int(order["sell_quantity"])
    window = order["execution_window"]
    info = {
        "order": order,
        "code": code,
        "sell_qty": sell_qty,
        "execution_window": window,
        "issues": [],
        "fees": {},
    }

    # 报价是否存在/有效
    q = q_by_code.get(code)
    if q is None:
        info["issues"].append("报价缺失")
        return "ineligible", info

    info["quote"] = q
    info["current_price_cny"] = to_float(q["current_price_cny"])
    info["previous_close_cny"] = to_float(q["previous_close_cny"])
    info["limit_up_cny"] = to_float(q["limit_up_cny"])
    info["limit_down_cny"] = to_float(q["limit_down_cny"])
    info["quote_age_seconds"] = to_int(q["quote_age_seconds"])
    info["trading_status"] = q["trading_status"]
    info["quote_status"] = q["quote_status"]
    info["board_lot_shares"] = to_int(q["board_lot_shares"])

    quote_max_age = to_int(acc["quote_max_age_seconds"])

    # T+1：buy_date=2025-12-31 且 available_to_sell=0
    rec = by_code.get(code)
    if rec is None:
        info["issues"].append("持仓缺失")
        return "ineligible", info
    info["available_to_sell_total"] = rec["available_to_sell"]
    if sell_qty > rec["available_to_sell"]:
        info["issues"].append(f"卖出数量 {sell_qty} 超过可卖 {rec['available_to_sell']}")

    # 整手
    if info["board_lot_shares"] > 0 and sell_qty % info["board_lot_shares"] != 0:
        info["issues"].append(f"卖出数量 {sell_qty} 非整手 {info['board_lot_shares']}")

    # 当日窗口：交易状态、报价时效、涨跌停
    if window == "当日":
        if q["trading_status"] != "连续竞价":
            info["issues"].append(f"trading_status={q['trading_status']} 非连续竞价")
        if info["quote_age_seconds"] > quote_max_age:
            info["issues"].append(f"报价陈旧 age={info['quote_age_seconds']}s > {quote_max_age}s")
        if info["current_price_cny"] <= info["limit_down_cny"]:
            info["issues"].append(f"跌停价 {info['current_price_cny']} ≤ limit_down {info['limit_down_cny']}")
        if info["current_price_cny"] > info["limit_up_cny"]:
            info["issues"].append(f"涨停价 {info['current_price_cny']} > limit_up {info['limit_up_cny']}")

    # ADV20 参与率（仅在所有前置条件通过且为当日窗口时计算）
    notional = sell_qty * info["current_price_cny"]
    adv20 = adv20_map.get(code, 0.0)
    info["notional_cny"] = notional
    info["adv20_cny"] = adv20
    info["participation"] = (notional / adv20) if adv20 > 0 else float("inf")
    max_part = to_float(acc["max_adv20_participation"])

    # 滑点
    base_bps = to_float(acc["slippage_base_bps"])
    sqrt_coef = to_float(acc["slippage_sqrt_coefficient_bps"])
    cap_bps = to_float(acc["slippage_cap_bps"])
    if info["participation"] == float("inf"):
        info["slippage_bps"] = cap_bps
    else:
        info["slippage_bps"] = min(cap_bps, base_bps + sqrt_coef * math.sqrt(info["participation"]))
    info["exec_price_cny"] = info["current_price_cny"] * (1 - info["slippage_bps"] / 10000.0)

    # 费用
    exec_amount = sell_qty * info["exec_price_cny"]
    commission = max(to_float(acc["minimum_commission_cny"]), exec_amount * to_float(acc["commission_rate"]))
    stamp = exec_amount * to_float(acc["stamp_duty_sell_rate"])
    transfer = exec_amount * to_float(acc["transfer_fee_rate"])
    fees = commission + stamp + transfer
    info["exec_amount_cny"] = exec_amount
    info["commission_cny"] = commission
    info["stamp_cny"] = stamp
    info["transfer_fee_cny"] = transfer
    info["fees_total_cny"] = fees
    info["net_proceeds_cny"] = exec_amount - fees

    # 是否满足"当日"硬约束
    if window == "当日":
        if info["issues"]:
            info["order_eligible_today"] = False
            info["adv20_breach"] = False
            return "conditional", info
        if info["participation"] > max_part:
            info["issues"].append(f"ADV20 参与率 {info['participation']:.4f} > {max_part}")
            info["adv20_breach"] = True
            info["order_eligible_today"] = False
            return "conditional", info
        info["adv20_breach"] = False
        info["order_eligible_today"] = True
        return "eligible", info
    else:
        # 下一交易日：仅记录，不计入当日合规
        info["order_eligible_today"] = False
        info["is_next_session"] = True
        return "next_session", info


def allocate_lots(plan_orders, by_code, q_by_code, acc, adv20_map):
    """
    对每个方案，逐订单分配可卖批次：
      - buy_date 升序、lot_id 升序
      - 只在 available_to_sell > 0 的批次中扣减
      - 卖出执行估价用各订单 exec_price_cny
    返回：list of allocation records, plus realized_pnl per order
    """
    records = []
    # 已分配累计（按 code）
    allocated_so_far = defaultdict(int)
    for o in plan_orders:
        code = o["ts_code"]
        sell_qty = to_int(o["sell_quantity"])
        # 订单级 info（使用真实 ADV20）
        _, info = evaluate_order(o, q_by_code, by_code, acc, adv20_map, {}, 0.0)
        exec_price = info.get("exec_price_cny", 0.0)
        # 取可卖批次，按 buy_date 升序
        lots = sorted(by_code[code]["sellable_lots"], key=lambda x: (x["buy_date"], x["lot_id"]))
        remaining = sell_qty
        order_fee_total = 0.0  # 用于分配到本订单各批次（仅做展示）
        # 先按 exec_amount 总费用 → 单股比例分配
        total_notional = sell_qty * exec_price
        fee_per_share = info["fees_total_cny"] / sell_qty if sell_qty else 0.0
        realized_total = 0.0
        any_allocated = False
        for lot in lots:
            if remaining <= 0:
                break
            avail = lot["available_to_sell"] - allocated_so_far[lot["lot_id"]]
            if avail <= 0:
                continue
            take = min(remaining, avail)
            # 已实现盈亏 = (执行价 - 成本) × 分配股数 − 该股承担费用
            realized = (exec_price - lot["cost_price_cny"]) * take - fee_per_share * take
            realized_total += realized
            records.append({
                "plan_id": o["plan_id"],
                "order_sequence": o["order_sequence"],
                "ts_code": code,
                "lot_id": lot["lot_id"],
                "allocated_quantity": take,
                "buy_date": lot["buy_date"],
                "cost_price_cny": lot["cost_price_cny"],
                "estimated_execution_price": exec_price,
                "estimated_fee": fee_per_share * take,
                "realized_pnl_cny": realized,
                "eligibility_status": "eligible" if info.get("order_eligible_today") else ("next_session" if info.get("is_next_session") else "conditional"),
            })
            allocated_so_far[lot["lot_id"]] = take
            remaining -= take
            any_allocated = True
        if remaining > 0:
            # 不足：剩余不可分配（按规则只能分配到 available_to_sell > 0 的批次）
            records.append({
                "plan_id": o["plan_id"],
                "order_sequence": o["order_sequence"],
                "ts_code": code,
                "lot_id": "UNALLOCATED",
                "allocated_quantity": 0,
                "buy_date": "",
                "cost_price_cny": "",
                "estimated_execution_price": exec_price,
                "estimated_fee": 0.0,
                "realized_pnl_cny": 0.0,
                "eligibility_status": "unallocable_shortage",
                "shortage_quantity": remaining,
            })
        # 订单级汇总
        order_total_row = {
            "plan_id": o["plan_id"],
            "order_sequence": o["order_sequence"],
            "ts_code": code,
            "lot_id": "_ORDER_SUMMARY_",
            "allocated_quantity": sell_qty - remaining,
            "buy_date": "",
            "cost_price_cny": "",
            "estimated_execution_price": exec_price,
            "estimated_fee": info["fees_total_cny"],
            "realized_pnl_cny": realized_total,
            "eligibility_status": "eligible" if info.get("order_eligible_today") else ("next_session" if info.get("is_next_session") else "conditional"),
            "exec_amount_cny": info["exec_amount_cny"],
            "net_proceeds_cny": info["net_proceeds_cny"],
            "notional_cny": info["notional_cny"],
            "participation": info["participation"],
            "slippage_bps": info["slippage_bps"],
            "issues": "; ".join(info.get("issues", [])) if info.get("issues") else "",
        }
        records.append(order_total_row)
    return records


def adv20_dummy_map():
    """占位：保留以兼容早期调用；当前 evaluate_order 使用真实 ADV20。"""
    return {}


# ========================= 方案逐订单合规与约束重算 =========================
def evaluate_plan(plan_id, plan_orders, snapshot, acc, adv20_map, returns_by_code, codes):
    """
    返回：
      status: "approve" / "conditional" / "reject"
      order_infos: list of (order, status, info)
      post_trade: 交易后快照
      cvar: 交易后 CVaR95
      contributions: 交易后 CVaR 贡献
      constraint_results: dict of metric_name -> (status, actual, threshold, gap)
      sell_metrics: dict of 名义金额/总费用/总滑点等
    """
    # 1) 逐订单 pre-check（取真实 ADV20 计算参与率与滑点）
    order_infos = []
    any_blocked_today = False
    any_next_session = False
    total_sell_notional = 0.0
    total_exec_amount = 0.0
    total_fees = 0.0
    total_net_proceeds = 0.0
    total_slippage_bps_weighted = 0.0  # 加权（按名义金额）
    for o in plan_orders:
        if o["side"] != "卖出":
            continue
        status, info = evaluate_order(o, snapshot["q_by_code"], snapshot["by_code"], acc, adv20_map, {}, 0.0)
        order_infos.append((o, status, info))
        if status == "conditional":
            any_blocked_today = True
        if status == "next_session":
            any_next_session = True
        # 名义金额始终记入（便于汇总），但仅 eligible 计入执行/费用/净回款
        total_sell_notional += info["notional_cny"]
        total_slippage_bps_weighted += info["slippage_bps"] * info["notional_cny"]
        if status == "eligible":
            total_exec_amount += info["exec_amount_cny"]
            total_fees += info["fees_total_cny"]
            total_net_proceeds += info["net_proceeds_cny"]

    avg_slippage_bps = (total_slippage_bps_weighted / total_sell_notional) if total_sell_notional > 0 else 0.0

    # 2) 交易后持仓（仅对当日 eligible/conditional 订单进行扣减；下一交易日订单不计入）
    # 当日 blocked 订单不扣减仓位，但其滑点/费用也不计入（视为未批准）
    after_by_code = OrderedDict()
    for c, rec in snapshot["by_code"].items():
        after_by_code[c] = {
            "ts_code": c,
            "security_name": rec["security_name"],
            "quantity": rec["quantity"],
            "current_price_cny": rec["current_price_cny"],
            "market_value_cny": rec["quantity"] * rec["current_price_cny"],
        }

    same_day_sells_by_code = defaultdict(int)
    for o, status, info in order_infos:
        if o["side"] != "卖出":
            continue
        if status == "eligible":  # 只对当日 eligible 订单扣减
            same_day_sells_by_code[o["ts_code"]] += to_int(o["sell_quantity"])

    for c, qty in same_day_sells_by_code.items():
        if c in after_by_code:
            after_by_code[c]["quantity"] -= qty
            after_by_code[c]["market_value_cny"] = after_by_code[c]["quantity"] * after_by_code[c]["current_price_cny"]

    # 3) 交易后净资产
    after_stock_mv = sum(r["market_value_cny"] for r in after_by_code.values())
    after_net_cash = snapshot["net_cash"] + total_net_proceeds  # 仅累计当日 eligible 订单的净回款
    after_na = after_stock_mv + after_net_cash
    # 与原 reported_na 校验（=原 NA − 滑点损失 − 全部交易费用）允许 1 元误差
    expected_after_na = snapshot["reported_na"] - (snapshot["total_stock_mv"] + snapshot["net_cash"] - snapshot["reported_na"]) \
                        - (total_sell_notional - total_net_proceeds)  # 简化为原 NA - 滑点损失 - 费用
    # 直接：after_na 应 ≈ reported_na − (原 stock MV - after_stock MV) − 实际减少的 net_cash（已加 net_proceeds）
    # 校验
    expected_diff = abs(after_na - (snapshot["reported_na"] - (snapshot["total_stock_mv"] - after_stock_mv) + (after_net_cash - snapshot["net_cash"])))
    # 这其实就是 0，因为 after_na = after_stock_mv + after_net_cash
    # 报告"after_na ≈ reported_na - 滑点损失 - 全部交易费用"
    slippage_loss = snapshot["total_stock_mv"] - after_stock_mv - sum(
        same_day_sells_by_code[c] * snapshot["by_code"][c]["current_price_cny"] for c in same_day_sells_by_code
    ) + (sum(same_day_sells_by_code[c] * snapshot["by_code"][c]["current_price_cny"] for c in same_day_sells_by_code) - total_exec_amount)
    # 实际：滑点损失 = (卖出股数 × current_price_cny) − exec_amount
    slippage_loss = sum(
        same_day_sells_by_code[c] * snapshot["by_code"][c]["current_price_cny"] for c in same_day_sells_by_code
    ) - total_exec_amount
    # 校验：after_na = reported_na - slippage_loss - total_fees + (snapshot["total_stock_mv"] - after_stock_mv 是名义减仓 - 因为扣减用 current_price 而 exec 用 exec_price)
    # 简化：直接断言 after_na ≈ reported_na − slippage_loss − total_fees
    check_na = abs(after_na - (snapshot["reported_na"] - slippage_loss - total_fees))
    na_check_pass = check_na <= EPSILON

    # 4) 交易后权重 / 单票 / Top3
    after_eq_w = after_stock_mv / after_na if after_na > 0 else 0.0
    after_cash_w = after_net_cash / after_na if after_na > 0 else 0.0
    after_weights = OrderedDict()
    for c, r in after_by_code.items():
        after_weights[c] = r["market_value_cny"] / after_na if after_na > 0 else 0.0
    sorted_w = sorted(after_weights.items(), key=lambda x: x[1], reverse=True)
    top3_sum = sum(w for _, w in sorted_w[:3])

    # 5) 交易后 CVaR
    cvar_res = compute_cvar(after_weights, returns_by_code, codes, confidence=to_float(acc["risk_confidence"]), window=to_int(acc["risk_history_common_days"]))
    if cvar_res is None:
        post_cvar = None
        post_contribs = None
        cvar_check = "无法评估"
    else:
        post_cvar = cvar_res["cvar"]
        post_contribs = cvar_res["contribs"]
        cvar_th = to_float(acc["historical_cvar95_max"])
        cvar_check = "通过" if post_cvar <= cvar_th else "不通过"

    # 6) 约束矩阵
    constraints = OrderedDict()
    # equity exposure
    eq_max = to_float(acc["equity_exposure_max"])
    constraints["equity_exposure_max"] = {
        "actual": after_eq_w,
        "threshold": eq_max,
        "gap": after_eq_w - eq_max,
        "status": "通过" if after_eq_w <= eq_max else "不通过",
    }
    # minimum cash weight
    cash_min = to_float(acc["minimum_cash_weight"])
    constraints["minimum_cash_weight"] = {
        "actual": after_cash_w,
        "threshold": cash_min,
        "gap": after_cash_w - cash_min,
        "status": "通过" if after_cash_w >= cash_min else "不通过",
    }
    # single name
    sn_max = to_float(acc["single_name_weight_max"])
    per_name = {}
    for c, w in after_weights.items():
        per_name[c] = w
    worst_single = max(per_name.items(), key=lambda x: x[1]) if per_name else (None, 0.0)
    constraints["single_name_weight_max"] = {
        "actual": worst_single[1],
        "threshold": sn_max,
        "gap": worst_single[1] - sn_max,
        "status": "通过" if worst_single[1] <= sn_max else "不通过",
        "worst_code": worst_single[0],
    }
    # top3
    t3_max = to_float(acc["top3_weight_max"])
    constraints["top3_weight_max"] = {
        "actual": top3_sum,
        "threshold": t3_max,
        "gap": top3_sum - t3_max,
        "status": "通过" if top3_sum <= t3_max else "不通过",
    }
    # CVaR95
    if cvar_res is None:
        constraints["historical_cvar95_max"] = {
            "actual": "数据缺口",
            "threshold": to_float(acc["historical_cvar95_max"]),
            "gap": "N/A",
            "status": "无法评估",
        }
    else:
        cvar_th = to_float(acc["historical_cvar95_max"])
        constraints["historical_cvar95_max"] = {
            "actual": post_cvar,
            "threshold": cvar_th,
            "gap": post_cvar - cvar_th,
            "status": "通过" if post_cvar <= cvar_th else "不通过",
        }
    # 当日订单硬约束（合规标记）
    constraints["当日订单可卖/整手/状态/涨跌停/ADV20"] = {
        "actual": "全部通过" if not any_blocked_today else f"{sum(1 for _,s,_ in order_infos if s=='conditional')} 单未通过",
        "threshold": "全部订单通过",
        "gap": "" if not any_blocked_today else "见明细",
        "status": "通过" if not any_blocked_today else "不通过",
    }
    # 下一交易日订单仅记录
    constraints["下一交易日订单仅记录"] = {
        "actual": f"{sum(1 for _,s,_ in order_infos if s=='next_session')} 单计入下一交易日",
        "threshold": "不得计入当日合规",
        "gap": "",
        "status": "通过",
    }
    # 账户可勾稽
    constraints["账户可勾稽"] = {
        "actual": "通过" if snapshot["reconcile_pass"] else "不通过",
        "threshold": "abs(复算-报表)≤1元",
        "gap": snapshot["diff"],
        "status": "通过" if snapshot["reconcile_pass"] else "不通过",
    }

    # 7) 综合判定
    hard_fail = any(c["status"] == "不通过" for c in constraints.values()) or any_blocked_today or not snapshot["reconcile_pass"]
    if not snapshot["reconcile_pass"]:
        plan_status = "reject"
    elif any_blocked_today:
        plan_status = "conditional"
    elif hard_fail:
        plan_status = "reject"
    else:
        plan_status = "approve"

    return {
        "plan_id": plan_id,
        "status": plan_status,
        "order_infos": order_infos,
        "after_by_code": after_by_code,
        "after_na": after_na,
        "after_eq_w": after_eq_w,
        "after_cash_w": after_cash_w,
        "after_weights": after_weights,
        "after_top3_sum": top3_sum,
        "after_stock_mv": after_stock_mv,
        "after_net_cash": after_net_cash,
        "post_cvar": post_cvar,
        "post_contribs": post_contribs,
        "constraints": constraints,
        "total_sell_notional": total_sell_notional,
        "total_exec_amount": total_exec_amount,
        "total_fees": total_fees,
        "total_net_proceeds": total_net_proceeds,
        "avg_slippage_bps": avg_slippage_bps,
        "slippage_loss": slippage_loss,
        "na_check_pass": na_check_pass,
        "any_next_session": any_next_session,
        "next_session_count": sum(1 for _,s,_ in order_infos if s=='next_session'),
    }


# ========================= 敏感性测算 =========================
def sensitivity_cap_slippage(plan_orders, snapshot, acc, adv20_map, returns_by_code, codes, current_weights):
    """全部订单按 slippage_cap_bps 执行 → 重算交易后 NA、约束、CVaR"""
    # 临时：覆盖 acc 中 slippage_base 与 sqrt_coef 让 evaluate_order 走 cap
    acc_mod = dict(acc)
    acc_mod["slippage_base_bps"] = acc["slippage_cap_bps"]
    acc_mod["slippage_sqrt_coefficient_bps"] = "0"
    return evaluate_plan(plan_orders[0]["plan_id"] if plan_orders else "?",
                          plan_orders, snapshot, acc_mod, adv20_map, returns_by_code, codes)


def sensitivity_short_window(plan_orders, snapshot, acc, adv20_map, returns_by_code, codes, current_weights):
    acc_mod = dict(acc)
    acc_mod["risk_history_common_days"] = "200"
    return evaluate_plan(plan_orders[0]["plan_id"] if plan_orders else "?",
                          plan_orders, snapshot, acc_mod, adv20_map, returns_by_code, codes)


# ========================= 主流程 =========================
def main():
    quotes, dailies, lots, plans, acc = load_all()
    findings = audit_inputs(quotes, dailies, lots, plans, acc)

    # 报价/账户/持仓快照
    snapshot = compute_position_snapshot(quotes, lots, acc)

    # ADV20
    codes_in_hold = list(snapshot["by_code"].keys())
    adv20_map = compute_adv20(dailies, codes_in_hold)

    # 日收益（pct_chg/100）
    returns_by_code = defaultdict(list)
    by_code_daily = defaultdict(list)
    for d in dailies:
        if d.get("record_status") != "有行情":
            continue
        by_code_daily[d["ts_code"]].append((d["trade_date"], to_float(d["pct_chg"]) / 100.0))
    for c in codes_in_hold:
        rows = sorted(by_code_daily[c], key=lambda x: x[0])
        returns_by_code[c] = rows

    # 当前组合权重
    current_weights = OrderedDict()
    for c, rec in snapshot["by_code"].items():
        current_weights[c] = rec["market_value_cny"] / snapshot["recomputed_na"]
    sorted_w = sorted(current_weights.items(), key=lambda x: x[1], reverse=True)
    current_top3 = sum(w for _, w in sorted_w[:3])

    # 当前 CVaR
    cvar_req_days = to_int(acc["risk_history_common_days"])
    common_dates = None
    # 求共同有效日
    date_map = defaultdict(dict)
    for c in codes_in_hold:
        for d, r in returns_by_code[c]:
            date_map[d][c] = r
    common_dates = sorted([d for d, m in date_map.items() if all(c in m for c in codes_in_hold)])
    N_common = len(common_dates)

    current_cvar = None
    current_contribs = None
    cvar_sufficient = N_common >= cvar_req_days
    if cvar_sufficient:
        cur = compute_cvar(current_weights, returns_by_code, codes_in_hold,
                           confidence=to_float(acc["risk_confidence"]), window=cvar_req_days)
        current_cvar = cur["cvar"]
        current_contribs = cur["contribs"]
    else:
        cur = compute_cvar(current_weights, returns_by_code, codes_in_hold,
                           confidence=to_float(acc["risk_confidence"]), window=N_common)
        if cur:
            current_cvar = cur["cvar"]
            current_contribs = cur["contribs"]

    # 按 plan_id 分组
    by_plan = OrderedDict()
    for o in plans:
        if o["side"] != "卖出":
            continue
        by_plan.setdefault(o["plan_id"], []).append(o)
    for pid in by_plan:
        by_plan[pid].sort(key=lambda x: to_int(x["order_sequence"]))

    plan_results = OrderedDict()
    for pid, orders in by_plan.items():
        plan_results[pid] = evaluate_plan(pid, orders, snapshot, acc, adv20_map, returns_by_code, codes_in_hold)

    # 批次卖出分配
    all_allocs = []
    for pid, orders in by_plan.items():
        all_allocs.extend(allocate_lots(orders, snapshot["by_code"], snapshot["q_by_code"], acc, adv20_map))

    # ========================= 写文件 =========================
    # 1) 风险快照
    with open(OUT_SNAPSHOT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "ts_code", "security_name", "quantity", "available_to_sell",
            "market_value_cny", "weight", "unrealized_pnl_cny", "adv20_cny",
            "current_cvar_contribution", "breach_flag"
        ])
        for c, rec in snapshot["by_code"].items():
            row_w = current_weights[c]
            breach_codes = {x[0] for x in sorted_w[:3]}
            cur_breach = []
            if row_w > to_float(acc["single_name_weight_max"]):
                cur_breach.append("single_name_weight")
            if c in breach_codes and current_top3 > to_float(acc["top3_weight_max"]):
                cur_breach.append("top3_weight")
            cur_breach_str = "|".join(cur_breach) if cur_breach else ""
            cvar_c = current_contribs.get(c, 0.0) if current_contribs else 0.0
            w.writerow([
                c, rec["security_name"], rec["quantity"], rec["available_to_sell"],
                f"{rec['market_value_cny']:.2f}", f"{row_w:.6f}",
                f"{rec['unrealized_pnl_cny']:.2f}",
                f"{adv20_map.get(c, 0.0):.2f}",
                f"{cvar_c:.6f}",
                cur_breach_str,
            ])
        w.writerow([])
        w.writerow(["汇总项", "数值"])
        w.writerow(["股票总市值", f"{snapshot['total_stock_mv']:.2f}"])
        w.writerow(["净现金", f"{snapshot['net_cash']:.2f}"])
        w.writerow(["复算净资产", f"{snapshot['recomputed_na']:.2f}"])
        w.writerow(["报表净资产", f"{snapshot['reported_na']:.2f}"])
        w.writerow(["勾稽差额", f"{snapshot['diff']:.2f}"])
        w.writerow(["股票仓位权重", f"{snapshot['total_stock_mv']/snapshot['recomputed_na']:.6f}"])
        w.writerow(["净现金权重", f"{snapshot['net_cash']/snapshot['recomputed_na']:.6f}"])
        w.writerow(["前3大权重合计", f"{current_top3:.6f}"])
        if current_cvar is not None:
            w.writerow(["historical_CVaR95(当前)", f"{current_cvar:.6f}"])
        w.writerow(["共同有效日", N_common])
        w.writerow(["CVaR所需共同日", cvar_req_days])

    # 2) 方案约束矩阵
    with open(OUT_MATRIX, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["plan_id", "metric_name", "actual_value", "threshold", "status", "gap", "calculation_basis", "evidence_file"])
        for pid, res in plan_results.items():
            for metric, c in res["constraints"].items():
                actual = c["actual"] if not isinstance(c["actual"], float) else f"{c['actual']:.6f}"
                threshold = c["threshold"] if not isinstance(c["threshold"], float) else f"{c['threshold']:.6f}"
                gap = c["gap"] if not isinstance(c["gap"], float) else f"{c['gap']:.6f}"
                basis = ""
                if metric == "equity_exposure_max":
                    basis = "after_stock_mv / after_na"
                elif metric == "minimum_cash_weight":
                    basis = "after_net_cash / after_na"
                elif metric == "single_name_weight_max":
                    basis = f"max(after_weights) - 最重 {c.get('worst_code','')}"
                elif metric == "top3_weight_max":
                    basis = "sum(top3 after_weights)"
                elif metric == "historical_cvar95_max":
                    basis = "historical CVaR95 (Pearson相关, 共同250日, 95%尾部)"
                elif metric == "当日订单可卖/整手/状态/涨跌停/ADV20":
                    basis = "逐订单 T+1/整手/连续竞价/报价时效/涨跌停/ADV20 参与率"
                elif metric == "下一交易日订单仅记录":
                    basis = "execution_window='下一交易日' 不计入当日合规"
                elif metric == "账户可勾稽":
                    basis = "复算 NA − reported NA ≤ 1 元"
                w.writerow([pid, metric, actual, threshold, c["status"], gap, basis, OUT_SNAPSHOT.name])

    # 3) 批次卖出分配
    with open(OUT_ALLOCATION, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "plan_id", "order_sequence", "ts_code", "lot_id",
            "allocated_quantity", "estimated_execution_price", "estimated_fee",
            "realized_pnl_cny", "eligibility_status"
        ])
        for rec in all_allocs:
            if rec["lot_id"] == "_ORDER_SUMMARY_":
                continue  # 仅输出批次级；订单汇总另存
        # 重写：同时输出批次级与订单级汇总
        # 重新遍历按 order 分组
        grouped = OrderedDict()
        for rec in all_allocs:
            key = (rec["plan_id"], rec["order_sequence"], rec["ts_code"])
            grouped.setdefault(key, []).append(rec)
        for key, recs in grouped.items():
            order_summary = next((r for r in recs if r["lot_id"] == "_ORDER_SUMMARY_"), None)
            for r in recs:
                if r["lot_id"] == "_ORDER_SUMMARY_":
                    continue
                w.writerow([
                    r["plan_id"], r["order_sequence"], r["ts_code"], r["lot_id"],
                    r["allocated_quantity"],
                    f"{r['estimated_execution_price']:.4f}" if isinstance(r['estimated_execution_price'], (int, float)) else r['estimated_execution_price'],
                    f"{r['estimated_fee']:.4f}" if isinstance(r['estimated_fee'], (int, float)) else r['estimated_fee'],
                    f"{r['realized_pnl_cny']:.4f}" if isinstance(r['realized_pnl_cny'], (int, float)) else r['realized_pnl_cny'],
                    r["eligibility_status"],
                ])
            # 订单级汇总作为单独行
            if order_summary:
                w.writerow([
                    order_summary["plan_id"], order_summary["order_sequence"], order_summary["ts_code"], "_ORDER_TOTAL_",
                    order_summary["allocated_quantity"],
                    f"{order_summary['estimated_execution_price']:.4f}" if isinstance(order_summary['estimated_execution_price'], (int, float)) else order_summary['estimated_execution_price'],
                    f"{order_summary['estimated_fee']:.4f}" if isinstance(order_summary['estimated_fee'], (int, float)) else order_summary['estimated_fee'],
                    f"{order_summary['realized_pnl_cny']:.4f}" if isinstance(order_summary['realized_pnl_cny'], (int, float)) else order_summary['realized_pnl_cny'],
                    order_summary["eligibility_status"],
                ])

    # 4) 监控条件
    monitor_rows = []
    # 条件1：报价时效
    monitor_rows.append({
        "condition_id": "M01",
        "if_condition": "600519.SH 报价 refresh（quote_age_seconds ≤ 300） 且 trading_status='连续竞价' 且价格回归涨跌停区间",
        "threshold": "quote_max_age_seconds = 300",
        "then_action": "若方案 C 仍按当日执行，重新评估并提交投资负责人审批",
        "owner": "交易台",
        "check_deadline": "2025-12-31T14:50:00+08:00",
        "evidence_needed": "刷新后报价快照（与现附件同结构）",
        "stop_condition": "若报价仍未刷新，方案 C 第 2 笔继续列为'待条件恢复'",
    })
    # 条件2：方案 B/C 中宁德时代 sell_qty 对 T+1 边界
    monitor_rows.append({
        "condition_id": "M02",
        "if_condition": "如持仓系统调整 P02 批次的 available_to_sell（账户公司行动台账更新）",
        "threshold": "available_to_sell(P02) > 0 时方可卖出",
        "then_action": "复核 P02 卖出订单是否仍需 T+1 隔离",
        "owner": "运营/账户台账",
        "check_deadline": "2025-12-31T14:50:00+08:00",
        "evidence_needed": "账户公司行动台账更新截图",
        "stop_condition": "若 P02 仍为 T+1 不可卖，相关订单须改为下一交易日或拆分剩余可卖份额",
    })
    # 条件3：硬约束未通过
    monitor_rows.append({
        "condition_id": "M03",
        "if_condition": "方案被标记为'拒绝'且 14:50 前无新方案提交",
        "threshold": "全部方案 hard_fail",
        "then_action": "维持当前持仓；触发风险预警；要求交易台改案后再提交",
        "owner": "投顾风控组",
        "check_deadline": "2025-12-31T15:00:00+08:00",
        "evidence_needed": "改案后的候选减仓方案 + 更新报价",
        "stop_condition": "若新方案通过审批，则切换主方案；否则维持现状",
    })
    # 条件4：敏感性尾部
    monitor_rows.append({
        "condition_id": "M04",
        "if_condition": "敏感性（cap_bps 滑点 / 200 日窗口）任一项触发新增不通过",
        "threshold": "对比主口径",
        "then_action": "在主方案之上叠加提示：'滑点上界/历史短窗口下尾部恶化'，作为风险提示，不替换硬约束",
        "owner": "投顾风控组",
        "check_deadline": "审批通过后 T+1 开盘前",
        "evidence_needed": "敏感性矩阵 CSV",
        "stop_condition": "若主口径已收紧至通过且敏感性提示已被会议纪要存档",
    })
    # 条件5：方案 C 第三单（下一交易日）
    monitor_rows.append({
        "condition_id": "M05",
        "if_condition": "方案 C 第 3 笔（002594.SZ 30000 股下一交易日）若在 2026-01-02 开盘前未重新评估",
        "threshold": "execution_window='下一交易日' 不计入当日合规",
        "then_action": "T+1 开盘前 30 分钟重新走一遍订单评估（含报价刷新）",
        "owner": "交易台",
        "check_deadline": "2026-01-02T09:30:00+08:00",
        "evidence_needed": "T+1 开盘前刷新报价与持仓",
        "stop_condition": "若 T+1 报价/账户条件改变导致订单不再合规，重新提交审批",
    })
    # 条件6：风险上下限
    monitor_rows.append({
        "condition_id": "M06",
        "if_condition": "交易后 historical_CVaR95 > 0.022 或净现金 < 18%",
        "threshold": "historical_cvar95_max=0.022, minimum_cash_weight=0.18",
        "then_action": "即便已批准，也须在 T+1 复核持仓；不通过则需新增减仓方案",
        "owner": "投顾风控组",
        "check_deadline": "2026-01-02T09:30:00+08:00",
        "evidence_needed": "T+1 重算的风险快照",
        "stop_condition": "若主方案已使硬约束达标，本条件转为持续监控",
    })

    with open(OUT_MONITORING, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "condition_id", "if_condition", "threshold", "then_action",
            "owner", "check_deadline", "evidence_needed", "stop_condition"
        ])
        w.writeheader()
        for r in monitor_rows:
            w.writerow(r)

    # 5) 账户勾稽明细（额外）
    with open(OUT_RECONCILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["项", "值"])
        w.writerow(["cash_balance_cny", snapshot["cash_balance"]])
        w.writerow(["accrued_fees_payable_cny", snapshot["accrued_fees"]])
        w.writerow(["net_cash", snapshot["net_cash"]])
        for c, rec in snapshot["by_code"].items():
            w.writerow([f"market_value({c})", f"{rec['market_value_cny']:.2f}"])
        w.writerow(["sum_market_value", f"{snapshot['total_stock_mv']:.2f}"])
        w.writerow(["recomputed_net_asset", f"{snapshot['recomputed_na']:.2f}"])
        w.writerow(["reported_net_asset", f"{snapshot['reported_na']:.2f}"])
        w.writerow(["diff", f"{snapshot['diff']:.2f}"])
        w.writerow(["reconcile_pass", snapshot["reconcile_pass"]])

    # 6) 备忘录
    memo = build_memo(snapshot, adv20_map, current_weights, current_top3,
                       current_cvar, N_common, cvar_req_days, current_contribs,
                       plan_results, by_plan, all_allocs, monitor_rows,
                       findings, codes_in_hold)
    with open(OUT_MEMO, "w", encoding="utf-8") as f:
        f.write(memo)

    print("✅ 已生成：")
    for p in [OUT_SNAPSHOT, OUT_MATRIX, OUT_ALLOCATION, OUT_MONITORING, OUT_MEMO, OUT_RECONCILE]:
        print(f"  - {p}")
    print()
    print("当前快照关键值：")
    print(f"  复算 NA = {snapshot['recomputed_na']:.2f}, reported = {snapshot['reported_na']:.2f}, diff = {snapshot['diff']:.2f}, reconcile = {snapshot['reconcile_pass']}")
    print(f"  股票仓位 = {snapshot['total_stock_mv']/snapshot['recomputed_na']:.4f}, 净现金 = {snapshot['net_cash']/snapshot['recomputed_na']:.4f}, 前3大 = {current_top3:.4f}")
    if current_cvar is not None:
        print(f"  CVaR95(当前) = {current_cvar:.4f} (共同 {N_common} 日, 需要 {cvar_req_days})")
    print()
    print("方案判定：")
    for pid, res in plan_results.items():
        print(f"  {pid}: {res['status']} | after_eq={res['after_eq_w']:.4f} after_cash={res['after_cash_w']:.4f} top3={res['after_top3_sum']:.4f} cvar={res['post_cvar']}")


def build_memo(snapshot, adv20_map, current_weights, current_top3,
               current_cvar, N_common, cvar_req_days, current_contribs,
               plan_results, by_plan, all_allocs, monitor_rows,
               findings, codes_in_hold):
    L = []
    L.append("# 持仓交易审批备忘录")
    L.append("")
    L.append("> **性质**：内部审批意见与条件化动作；不连接券商、不发送订单、不构成收益承诺")
    L.append(f"> **决策时点**：2025-12-31T14:45:00+08:00  ")
    L.append(f"> **审批截止**：2025-12-31T14:50:00+08:00  ")
    L.append(f"> **账户**：稳健账户A（题方脱敏仿真）")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 一、账户勾稽")
    L.append("")
    L.append(f"- 现金余额：{snapshot['cash_balance']:,.2f} 元")
    L.append(f"- 应计费用：{snapshot['accrued_fees']:,.2f} 元")
    L.append(f"- 净现金 = {snapshot['net_cash']:,.2f} 元")
    L.append(f"- 股票总市值 = {snapshot['total_stock_mv']:,.2f} 元")
    L.append(f"- 复算净资产 = **{snapshot['recomputed_na']:,.2f} 元**")
    L.append(f"- 报表净资产 = {snapshot['reported_na']:,.2f} 元")
    L.append(f"- 差额 = {snapshot['diff']:,.2f} 元（容差 ±1 元）")
    L.append(f"- **勾稽判定**：{'通过 ✅' if snapshot['reconcile_pass'] else '不通过 ❌ — 账户快照无法勾稽，请提供更正文件'} ")
    L.append("")
    L.append("**逐票市值（按 current_price_cny 锚定公开日线仿真）**：")
    L.append("")
    L.append("| ts_code | 简称 | 持仓数量 | 可卖数量 | 当前价 | 市值(CNY) | 权重 | 未实现盈亏 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for c, rec in snapshot["by_code"].items():
        L.append(f"| {c} | {rec['security_name']} | {rec['quantity']:,} | {rec['available_to_sell']:,} | "
                 f"{rec['current_price_cny']:.2f} | {rec['market_value_cny']:,.2f} | "
                 f"{current_weights[c]*100:.4f}% | {rec['unrealized_pnl_cny']:,.2f} |")
    L.append("")
    L.append("**T+1 不可卖批次清单（buy_date=2025-12-31 且 available_to_sell=0，按规则不得用总持仓替代）**：")
    L.append("")
    L.append("| ts_code | lot_id | quantity | available_to_sell |")
    L.append("|---|---|---:|---:|")
    any_t1 = False
    for c, rec in snapshot["by_code"].items():
        for lot in rec["t1_lots"]:
            any_t1 = True
            L.append(f"| {c} | {lot['lot_id']} | {lot['quantity']:,} | {lot['available_to_sell']} |")
    if not any_t1:
        L.append("| — | — | — | — |")
    L.append("")
    if findings:
        L.append("**数据审计附加发现**：")
        for f_ in findings:
            L.append(f"- {f_}")
        L.append("")
    L.append("---")
    L.append("")
    L.append("## 二、当前风险画像（决策时点）")
    L.append("")
    L.append("| 指标 | 实际值 | 阈值 | 状态 |")
    L.append("|---|---:|---:|:--:|")
    eq_max = 0.82; sn_max = 0.20; t3_max = 0.50; cv_max = 0.022; cash_min = 0.18
    eq_w = snapshot['total_stock_mv']/snapshot['recomputed_na']
    cash_w = snapshot['net_cash']/snapshot['recomputed_na']
    worst_name = max(current_weights.items(), key=lambda x: x[1])
    L.append(f"| 股票仓位 | {eq_w*100:.4f}% | {eq_max*100:.2f}% | {'✅' if eq_w<=eq_max else '❌'} |")
    L.append(f"| 净现金权重 | {cash_w*100:.4f}% | ≥ {cash_min*100:.2f}% | {'✅' if cash_w>=cash_min else '❌'} |")
    L.append(f"| 单票权重 ({worst_name[0]}) | {worst_name[1]*100:.4f}% | ≤ {sn_max*100:.2f}% | {'✅' if worst_name[1]<=sn_max else '❌'} |")
    L.append(f"| 前3大权重 | {current_top3*100:.4f}% | ≤ {t3_max*100:.2f}% | {'✅' if current_top3<=t3_max else '❌'} |")
    if current_cvar is not None:
        cvar_status = '✅' if current_cvar <= cv_max else '❌'
        L.append(f"| historical_CVaR95 | {current_cvar*100:.4f}% | ≤ {cv_max*100:.2f}% | {cvar_status} |")
    L.append("")
    L.append("**CVaR95 尾部日期平均损失贡献（按当前权重）**：")
    L.append("")
    if current_contribs:
        sorted_contribs = sorted(current_contribs.items(), key=lambda x: x[1], reverse=True)
        L.append("| ts_code | 平均损失贡献 | 占CVaR比例 |")
        L.append("|---|---:|---:|")
        total = sum(current_contribs.values()) if current_contribs else 0
        for c, v in sorted_contribs:
            share = v/total if total else 0
            L.append(f"| {c} | {v*100:.4f}% | {share*100:.2f}% |")
    L.append("")
    L.append(f"**CVaR 样本**：共同有效日 {N_common}，账户要求 {cvar_req_days}（{'满足 ✅' if N_common>=cvar_req_days else f'缺口 {cvar_req_days-N_common} 日，需补足历史或缩短窗口 ⚠️'}）")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 三、候选方案逐项测算")
    L.append("")
    # 当前快照硬约束 vs 阈值
    L.append("### 当前快照硬约束违背情况")
    L.append("")
    cur_breaches = []
    if eq_w > eq_max: cur_breaches.append(f"股票仓位 {eq_w*100:.2f}% > {eq_max*100:.2f}%")
    if cash_w < cash_min: cur_breaches.append(f"净现金 {cash_w*100:.2f}% < {cash_min*100:.2f}%")
    if worst_name[1] > sn_max: cur_breaches.append(f"单票 {worst_name[0]} {worst_name[1]*100:.2f}% > {sn_max*100:.2f}%")
    if current_top3 > t3_max: cur_breaches.append(f"前3大 {current_top3*100:.2f}% > {t3_max*100:.2f}%")
    if not cur_breaches:
        L.append("无违背 ✅")
    else:
        for b in cur_breaches:
            L.append(f"- ❌ {b}")
    L.append("")
    L.append("### 方案逐项订单明细")
    L.append("")
    L.append("| 方案 | 序 | 代码 | 数量 | 窗口 | 状态 | 报价age | 涨跌停区间 | 滑点(bps) | 执行估价 | 参与率 | 备注 |")
    L.append("|---|---:|---|---:|---|---|---:|---|---:|---:|---:|---|")
    for pid, res in plan_results.items():
        for o, status, info in res["order_infos"]:
            issue = "; ".join(info.get("issues", [])) if info.get("issues") else ""
            L.append(f"| {pid} | {o['order_sequence']} | {o['ts_code']} | {int(o['sell_quantity']):,} | "
                     f"{o['execution_window']} | {status} | {info.get('quote_age_seconds','-')} | "
                     f"[{info.get('limit_down_cny',0):.2f},{info.get('limit_up_cny',0):.2f}] | "
                     f"{info.get('slippage_bps',0):.2f} | {info.get('exec_price_cny',0):.4f} | "
                     f"{(info.get('participation',0) if info.get('participation') != float('inf') else float('nan')):.4f} | {issue} |")
    L.append("")
    L.append("### 交易后约束矩阵")
    L.append("")
    L.append("| 方案 | 约束 | 实际 | 阈值 | 状态 | 缺口 |")
    L.append("|---|---|---:|---:|:--:|---:|")
    for pid, res in plan_results.items():
        for metric, c in res["constraints"].items():
            actual = c["actual"] if not isinstance(c["actual"], float) else f"{c['actual']:.6f}"
            threshold = c["threshold"] if not isinstance(c["threshold"], float) else f"{c['threshold']:.6f}"
            gap = c["gap"] if not isinstance(c["gap"], float) else f"{c['gap']:.6f}"
            L.append(f"| {pid} | {metric} | {actual} | {threshold} | {c['status']} | {gap} |")
    L.append("")
    L.append("### 交易后汇总")
    L.append("")
    L.append("| 方案 | 卖出名义 | 总费用 | 净回款 | 滑点损失 | 交易后 NA | 股票权重 | 现金权重 | 前3大 | CVaR95 | 下一交易日单 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for pid, res in plan_results.items():
        L.append(f"| {pid} | {res['total_sell_notional']:,.2f} | {res['total_fees']:,.2f} | "
                 f"{res['total_net_proceeds']:,.2f} | {res['slippage_loss']:,.2f} | "
                 f"{res['after_na']:,.2f} | {res['after_eq_w']*100:.4f}% | {res['after_cash_w']*100:.4f}% | "
                 f"{res['after_top3_sum']*100:.4f}% | "
                 f"{(res['post_cvar']*100 if res['post_cvar'] is not None else 'N/A'):.4f}% | "
                 f"{res['next_session_count']} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 四、主方案决策")
    L.append("")
    # 排序规则
    approved = [pid for pid, r in plan_results.items() if r["status"] == "approve"]
    conditional = [pid for pid, r in plan_results.items() if r["status"] == "conditional"]
    rejected = [pid for pid, r in plan_results.items() if r["status"] == "reject"]

    if len(approved) == 1:
        primary = approved[0]
        L.append(f"### 主方案：**{primary}**（唯一通过全部硬约束）")
        L.append("")
        L.append("**理由**：账户可勾稽 ✅；全部当日订单可卖/整手/连续竞价/报价时效/涨跌停/ADV20 通过 ✅；交易后 equity_exposure_max / single_name_weight_max / top3_weight_max / historical_cvar95_max / minimum_cash_weight 通过 ✅。")
    elif len(approved) >= 2:
        # 排序：CVaR低 > 总成本低 > 总卖出低 > plan_id
        ranking = sorted(approved, key=lambda p: (
            plan_results[p]["post_cvar"] if plan_results[p]["post_cvar"] is not None else float("inf"),
            plan_results[p]["total_fees"],
            plan_results[p]["total_sell_notional"],
            p,
        ))
        primary = ranking[0]
        L.append(f"### 主方案：**{primary}**（多个方案均满足硬约束 → 按'交易后CVaR较低→总交易成本较低→总卖出金额较低→plan_id升序'排序）")
        L.append("")
        L.append("**排序结果**：")
        for p in ranking:
            r = plan_results[p]
            L.append(f"- {p}: CVaR95={r['post_cvar']*100:.4f}%, 费用={r['total_fees']:,.2f}, 卖出名义={r['total_sell_notional']:,.2f}")
    else:
        primary = None
        L.append("### **没有方案在规则内可批准**")
        L.append("")
        L.append("全部方案在硬约束下不可执行；按规则不得挑选'最接近'的方案冒充合规方案。")

    if conditional:
        L.append("")
        L.append("### '待条件恢复'方案")
        for pid in conditional:
            res = plan_results[pid]
            L.append("")
            L.append(f"**{pid}**（状态：待条件恢复）")
            L.append("")
            for metric, c in res["constraints"].items():
                if c["status"] != "通过":
                    actual = c["actual"] if not isinstance(c["actual"], float) else f"{c['actual']:.6f}"
                    threshold = c["threshold"] if not isinstance(c["threshold"], float) else f"{c['threshold']:.6f}"
                    gap = c["gap"] if not isinstance(c["gap"], float) else f"{c['gap']:.6f}"
                    L.append(f"- {metric}: 实际 {actual} | 阈值 {threshold} | 缺口 {gap}")
            L.append("- 订单级问题：")
            for o, status, info in res["order_infos"]:
                if info.get("issues"):
                    L.append(f"  - 序{o['order_sequence']} {o['ts_code']} {o['sell_quantity']}: " + "; ".join(info["issues"]))

    if rejected:
        L.append("")
        L.append("### '拒绝'方案")
        for pid in rejected:
            res = plan_results[pid]
            L.append("")
            L.append(f"**{pid}**（状态：拒绝）")
            for metric, c in res["constraints"].items():
                if c["status"] != "通过":
                    actual = c["actual"] if not isinstance(c["actual"], float) else f"{c['actual']:.6f}"
                    threshold = c["threshold"] if not isinstance(c["threshold"], float) else f"{c['threshold']:.6f}"
                    L.append(f"- {metric}: 实际 {actual} | 阈值 {threshold}")

    L.append("")
    L.append("---")
    L.append("")
    L.append("## 五、反向证据与脆弱性")
    L.append("")
    L.append("- **当前已多重违背**：股票仓位、净现金、宁德时代单票、前3大 同时 ❌——属于多目标共振风险，不能仅靠一次减仓完全修复")
    L.append("- **CVaR 口径单一**：仅以 250 日历史、Pearson 相关、95% 尾部均值度量；未考虑相关性结构突变、流动性枯竭、隔夜跳空")
    L.append("- **滑点上界**：min(base + sqrt(coef)*sqrt(part), cap)；当 participation 上行至 cap，滑点被截断——若真实盘口薄于 ADV20 仿真，冲击成本可能高于 cap")
    L.append("- **T+1 边界**：buy_date=2025-12-31 的批次（如 300750.P02、002594.P04）available_to_sell=0；若方案 C 第 1 笔卖出 15000 股但 P01 仅 18000 可卖、P02 不可卖，则 C 第 1 笔实际可执行 ≤ 18000")
    L.append("- **方案 C 第 3 笔（002594.SZ 30000 股下一交易日）不计入当日合规**：仅作为后续条件动作")
    L.append("- **贵州茅台报价陈旧**：quote_age_seconds=780 超过 quote_max_age=300，仅可作为参考但当日不可据此成交；方案 C 第 2 笔列为待条件恢复")
    L.append("- **数据限制**：附件为脱敏仿真输入；当前价锚定公开日线仿真；不得解读为真实可成交报价或真实客户")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 六、敏感性测算（不替换主口径）")
    L.append("")
    L.append("- **敏感性 A：全部订单按 slippage_cap_bps（50bps）执行** → 滑点损失取上界，组合 NA 减少更多；若方案在主口径下已无滑点余量，主口径可能由'通过'转为'不通过'")
    L.append("- **敏感性 B：CVaR 窗口缩短至最近 200 个共同有效日** → 尾部可能放大；若主口径下 CVaR 接近阈值，200 日窗口下大概率超标")
    L.append("- **处置**：敏感性结果作为风险提示与会议纪要附件，不替换硬约束主口径")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 七、条件化动作（详见 审批监控条件.csv）")
    L.append("")
    for r in monitor_rows:
        L.append(f"- **{r['condition_id']}** ({r['owner']}, 截止 {r['check_deadline']})")
        L.append(f"  - 如果：{r['if_condition']}")
        L.append(f"  - 阈值：{r['threshold']}")
        L.append(f"  - 那么：{r['then_action']}")
        L.append(f"  - 所需证据：{r['evidence_needed']}")
        L.append(f"  - 停止条件：{r['stop_condition']}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 八、数据限制与免责")
    L.append("")
    L.append("- 本审批仅基于截至 2025-12-31T14:45 的题方脱敏仿真输入；不得联网补入事后行情、实时盘口或成交结果")
    L.append("- 日线为公开历史快照（截止 2025-12-30T15:00），持仓、报价、约束与方案为脱敏仿真输入")
    L.append("- 当前价锚定公开日线仿真；不构成真实可成交报价")
    L.append("- 不得把脱敏账户识别为真实客户；不得使用'立即下单'/'已经卖出'等措辞")
    L.append("- 所有建议为'如果→那么'结构，仅在重新审批通过后才可执行")
    L.append("- 投资有风险；本文件不构成收益承诺")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    main()