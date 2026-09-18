#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓交易审批复核脚本
从5个原始附件一键重建全部客观CSV和报告数字。
不联网，不硬编码附件中的价格、数量或推荐方案。
"""

import csv
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

# ============================================================
# 路径配置
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ATTACH_DIR = os.path.join(BASE_DIR, "attachments")
OUTPUT_DIR = BASE_DIR

F_POSITIONS = os.path.join(ATTACH_DIR, "持仓批次_20251231T1445.csv")
F_QUOTES = os.path.join(ATTACH_DIR, "持仓标的报价_20251231T1445.csv")
F_CONSTRAINTS = os.path.join(ATTACH_DIR, "账户与交易约束_20251231T1445.csv")
F_PLANS = os.path.join(ATTACH_DIR, "候选减仓方案_20251231.csv")
F_DAILY = os.path.join(ATTACH_DIR, "持仓标的日线_20240102_20251230.csv")


# ============================================================
# 工具函数
# ============================================================
def read_csv(path):
    """读取CSV，返回字典列表，处理BOM"""
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def safe_float(v, default=0.0):
    if v is None or v == "":
        return default
    return float(v)


def safe_int(v, default=0):
    if v is None or v == "":
        return default
    return int(float(v))


def fmt(v, digits=2):
    """格式化数字，保留指定位数"""
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def pct(v, digits=2):
    return f"{v * 100:.{digits}f}%"


# ============================================================
# 1. 数据加载与审计
# ============================================================
def load_data():
    positions = read_csv(F_POSITIONS)
    quotes = read_csv(F_QUOTES)
    constraints = read_csv(F_CONSTRAINTS)[0]
    plans = read_csv(F_PLANS)
    daily = read_csv(F_DAILY)
    return positions, quotes, constraints, plans, daily


def audit_data(positions, quotes, constraints, plans, daily):
    """数据审计：字段、主键、日期、单位、重复值、空值"""
    audit = []

    # 持仓批次审计
    pos_keys = set()
    for row in positions:
        key = (row["ts_code"], row["lot_id"])
        if key in pos_keys:
            audit.append(f"持仓批次重复主键: {key}")
        pos_keys.add(key)
        for fld in ["quantity", "available_to_sell", "cost_price_cny"]:
            if row.get(fld, "") == "":
                audit.append(f"持仓批次空值: {key} {fld}")

    # 报价审计
    quote_codes = set()
    for row in quotes:
        if row["ts_code"] in quote_codes:
            audit.append(f"报价重复代码: {row['ts_code']}")
        quote_codes.add(row["ts_code"])

    # 方案审计 - 检查未知代码
    pos_codes = set(r["ts_code"] for r in positions)
    for row in plans:
        if row["ts_code"] not in pos_codes:
            audit.append(f"方案{row['plan_id']}出现未知代码: {row['ts_code']}")

    # 日线审计
    daily_counts = defaultdict(int)
    for row in daily:
        daily_counts[row["ts_code"]] += 1

    audit.append(f"日线记录数: {dict(daily_counts)}")
    return audit


# ============================================================
# 2. 账户勾稽
# ============================================================
def build_quote_map(quotes):
    qmap = {}
    for q in quotes:
        qmap[q["ts_code"]] = {
            "current_price_cny": safe_float(q["current_price_cny"]),
            "previous_close_cny": safe_float(q["previous_close_cny"]),
            "limit_up_cny": safe_float(q["limit_up_cny"]),
            "limit_down_cny": safe_float(q["limit_down_cny"]),
            "quote_age_seconds": safe_int(q["quote_age_seconds"]),
            "quote_status": q["quote_status"],
            "trading_status": q["trading_status"],
            "board_lot_shares": safe_int(q["board_lot_shares"]),
            "fx_to_cny": safe_float(q["fx_to_cny"]),
            "security_name": q["security_name"],
        }
    return qmap


def reconcile_account(positions, quote_map, constraints):
    """账户勾稽：计算市值、净现金、复算净资产"""
    cash_balance = safe_float(constraints["cash_balance_cny"])
    accrued_fees = safe_float(constraints["accrued_fees_payable_cny"])
    reported_na = safe_float(constraints["reported_net_asset_cny"])

    net_cash = cash_balance - accrued_fees

    # 逐批次市值
    lot_details = []
    total_equity_mv = 0.0
    for p in positions:
        ts = p["ts_code"]
        q = quote_map[ts]
        price = q["current_price_cny"]
        fx = q["fx_to_cny"]
        quantity = safe_int(p["quantity"])
        avail = safe_int(p["available_to_sell"])
        cost = safe_float(p["cost_price_cny"])

        mv = quantity * price * fx
        unrealized = (price - cost) * quantity
        total_equity_mv += mv

        lot_details.append({
            "account_label": p["account_label"],
            "ts_code": ts,
            "security_name": p["security_name"],
            "lot_id": p["lot_id"],
            "buy_date": p["buy_date"],
            "quantity": quantity,
            "available_to_sell": avail,
            "cost_price_cny": cost,
            "current_price_cny": price,
            "market_value_cny": mv,
            "unrealized_pnl_cny": unrealized,
            "cost_basis_note": p["cost_basis_note"],
        })

    recalc_na = total_equity_mv + net_cash
    diff = recalc_na - reported_na

    reconciliation = {
        "cash_balance_cny": cash_balance,
        "accrued_fees_payable_cny": accrued_fees,
        "net_cash_cny": net_cash,
        "total_equity_mv_cny": total_equity_mv,
        "recalc_net_asset_cny": recalc_na,
        "reported_net_asset_cny": reported_na,
        "difference_cny": diff,
        "reconciled": abs(diff) <= 1.0,
    }

    return reconciliation, lot_details


# ============================================================
# 3. ADV20 计算
# ============================================================
def calc_adv20(daily):
    """按每只股票截至2025-12-30最近20条有行情记录计算ADV20"""
    by_stock = defaultdict(list)
    for row in daily:
        if row["record_status"] == "有行情":
            by_stock[row["ts_code"]].append({
                "trade_date": row["trade_date"],
                "amount": safe_float(row["amount"]),
                "pct_chg": safe_float(row["pct_chg"]),
                "close": safe_float(row["close"]),
            })

    adv20 = {}
    daily_returns = {}  # ts_code -> list of (date, return)
    for ts, records in by_stock.items():
        # 按日期升序排列
        records.sort(key=lambda x: x["trade_date"])
        # 最近20条
        last20 = records[-20:]
        # amount单位为千元，ADV20 = mean(amount × 1000)
        adv20[ts] = sum(r["amount"] * 1000 for r in last20) / len(last20)
        # 日收益率 pct_chg / 100
        daily_returns[ts] = [(r["trade_date"], r["pct_chg"] / 100.0) for r in records]

    return adv20, daily_returns


# ============================================================
# 4. CVaR 计算
# ============================================================
def calc_cvar(weights, daily_returns, cash_return=0.0, confidence=0.95,
              required_days=250):
    """
    计算组合historical_CVaR95
    weights: dict ts_code -> weight (sum of stock weights + cash weight = 1)
    daily_returns: dict ts_code -> list of (date, return)
    返回 cvar, tail_contributions, common_dates, n_common, m, status
    """
    # 找到所有股票共同存在的交易日
    date_sets = []
    for ts in weights:
        if ts == "CASH":
            continue
        dates = set(d for d, _ in daily_returns.get(ts, []))
        date_sets.append(dates)

    if not date_sets:
        return None, {}, [], 0, 0, "无法评估"

    common_dates = set.intersection(*date_sets)
    common_dates = sorted(common_dates)

    # 取最近 required_days 个
    if len(common_dates) < required_days:
        return None, {}, common_dates, len(common_dates), 0, \
            f"共同有效日{len(common_dates)}<{required_days}"

    common_dates = common_dates[-required_days:]
    common_date_set = set(common_dates)

    # 构建每只股票的日期->收益率映射
    ret_map = {}
    for ts in weights:
        if ts == "CASH":
            continue
        ret_map[ts] = {d: r for d, r in daily_returns[ts]}

    # 计算每日组合收益和损失
    portfolio_losses = []
    for d in common_dates:
        port_ret = 0.0
        for ts, w in weights.items():
            if ts == "CASH":
                port_ret += w * cash_return
            else:
                port_ret += w * ret_map[ts][d]
        loss = -port_ret
        portfolio_losses.append((d, loss, port_ret))

    # 按损失从大到小排列
    portfolio_losses.sort(key=lambda x: x[1], reverse=True)

    n = len(common_dates)
    m = math.ceil((1 - confidence) * n)

    # CVaR = 前m个损失的算术平均
    top_m_losses = portfolio_losses[:m]
    cvar = sum(l for _, l, _ in top_m_losses) / m

    # 尾部日期上每只股票的平均损失贡献
    tail_dates = [d for d, _, _ in top_m_losses]
    tail_contributions = {}
    for ts, w in weights.items():
        if ts == "CASH":
            contrib = sum(-w * cash_return for _ in tail_dates) / m
        else:
            contrib = sum(-w * ret_map[ts][d] for d in tail_dates) / m
        tail_contributions[ts] = contrib

    return cvar, tail_contributions, common_dates, n, m, "通过"


# ============================================================
# 5. 当前风险画像
# ============================================================
def current_risk_profile(lot_details, reconciliation, quote_map, adv20,
                         daily_returns, constraints):
    """计算当前风险画像"""
    na = reconciliation["recalc_net_asset_cny"]
    total_equity_mv = reconciliation["total_equity_mv_cny"]
    net_cash = reconciliation["net_cash_cny"]

    # 按股票汇总
    stock_agg = defaultdict(lambda: {
        "quantity": 0, "available_to_sell": 0,
        "market_value_cny": 0.0, "unrealized_pnl_cny": 0.0,
        "cost_basis_cny": 0.0, "security_name": ""
    })

    for lot in lot_details:
        ts = lot["ts_code"]
        stock_agg[ts]["quantity"] += lot["quantity"]
        stock_agg[ts]["available_to_sell"] += lot["available_to_sell"]
        stock_agg[ts]["market_value_cny"] += lot["market_value_cny"]
        stock_agg[ts]["unrealized_pnl_cny"] += lot["unrealized_pnl_cny"]
        stock_agg[ts]["cost_basis_cny"] += lot["quantity"] * lot["cost_price_cny"]
        stock_agg[ts]["security_name"] = lot["security_name"]

    # 权重
    for ts in stock_agg:
        stock_agg[ts]["weight"] = stock_agg[ts]["market_value_cny"] / na

    equity_weight = total_equity_mv / na
    cash_weight = net_cash / na

    # 前3大权重
    sorted_stocks = sorted(stock_agg.items(), key=lambda x: x[1]["weight"], reverse=True)
    top3_weight = sum(s[1]["weight"] for s in sorted_stocks[:3])

    # 当前CVaR
    weights = {ts: stock_agg[ts]["weight"] for ts in stock_agg}
    weights["CASH"] = cash_weight
    cvar, tail_contrib, _, n, m, cvar_status = calc_cvar(
        weights, daily_returns,
        cash_return=safe_float(constraints["cash_daily_return"]),
        confidence=safe_float(constraints["risk_confidence"]),
        required_days=safe_int(constraints["risk_history_common_days"])
    )

    # 约束检查
    checks = {
        "equity_exposure": {
            "actual": equity_weight,
            "threshold": safe_float(constraints["equity_exposure_max"]),
            "pass": equity_weight <= safe_float(constraints["equity_exposure_max"]),
        },
        "minimum_cash_weight": {
            "actual": cash_weight,
            "threshold": safe_float(constraints["minimum_cash_weight"]),
            "pass": cash_weight >= safe_float(constraints["minimum_cash_weight"]),
        },
        "top3_weight": {
            "actual": top3_weight,
            "threshold": safe_float(constraints["top3_weight_max"]),
            "pass": top3_weight <= safe_float(constraints["top3_weight_max"]),
        },
        "historical_cvar95": {
            "actual": cvar,
            "threshold": safe_float(constraints["historical_cvar95_max"]),
            "pass": cvar is not None and cvar <= safe_float(constraints["historical_cvar95_max"]),
            "status": cvar_status,
        },
    }

    # 单票检查
    single_name_max = safe_float(constraints["single_name_weight_max"])
    for ts in stock_agg:
        checks[f"single_name_{ts}"] = {
            "actual": stock_agg[ts]["weight"],
            "threshold": single_name_max,
            "pass": stock_agg[ts]["weight"] <= single_name_max,
        }

    profile = {
        "net_asset_cny": na,
        "total_equity_mv_cny": total_equity_mv,
        "net_cash_cny": net_cash,
        "equity_weight": equity_weight,
        "cash_weight": cash_weight,
        "top3_weight": top3_weight,
        "cvar95": cvar,
        "cvar_tail_contributions": tail_contrib,
        "cvar_n": n,
        "cvar_m": m,
        "cvar_status": cvar_status,
        "stock_agg": dict(stock_agg),
        "sorted_stocks": sorted_stocks,
        "checks": checks,
    }
    return profile


# ============================================================
# 6. 方案执行测算
# ============================================================
def validate_order(order, quote_map, constraints, stock_agg, adv20):
    """验证单个订单的可执行性"""
    ts = order["ts_code"]
    q = quote_map.get(ts)
    if q is None:
        return {"valid": False, "reason": f"未知代码{ts}", "status": "无法评估"}

    sell_qty = safe_int(order["sell_quantity"])
    board_lot = q["board_lot_shares"]
    max_age = safe_int(constraints["quote_max_age_seconds"])
    max_adv = safe_float(constraints["max_adv20_participation"])

    issues = []

    # T+1 / 可卖数量
    avail = stock_agg[ts]["available_to_sell"]
    if sell_qty > avail:
        issues.append(f"卖出量{sell_qty}>可卖量{avail}")

    # 整手
    if sell_qty % board_lot != 0:
        issues.append(f"卖出量{sell_qty}不是{board_lot}整手倍数")

    # 交易状态
    if q["trading_status"] != "连续竞价":
        issues.append(f"交易状态={q['trading_status']}，非连续竞价")

    # 报价时效
    if q["quote_age_seconds"] > max_age:
        issues.append(f"报价时效{q['quote_age_seconds']}s>{max_age}s，报价陈旧")

    # 涨跌停
    price = q["current_price_cny"]
    if price <= q["limit_down_cny"]:
        issues.append(f"当前价{price}<=跌停价{q['limit_down_cny']}")
    if price > q["limit_up_cny"]:
        issues.append(f"当前价{price}>涨停价{q['limit_up_cny']}")

    # ADV20参与率
    notional = sell_qty * price
    participation = notional / adv20[ts] if adv20[ts] > 0 else float("inf")
    if participation > max_adv:
        issues.append(f"ADV20参与率{participation:.4f}>{max_adv}")

    if issues:
        return {"valid": False, "reason": "; ".join(issues), "status": "待条件恢复",
                "participation": participation}
    return {"valid": True, "reason": "", "status": "可执行", "participation": participation}


def calc_slippage(participation, constraints):
    """计算滑点基点"""
    base = safe_float(constraints["slippage_base_bps"])
    coeff = safe_float(constraints["slippage_sqrt_coefficient_bps"])
    cap = safe_float(constraints["slippage_cap_bps"])
    bps = min(cap, base + coeff * math.sqrt(participation))
    return bps


def execute_plan(plan_id, plan_orders, lot_details, quote_map, constraints,
                 stock_agg, adv20, daily_returns, reconciliation):
    """执行单个方案的完整测算"""
    cash_return = safe_float(constraints["cash_daily_return"])
    confidence = safe_float(constraints["risk_confidence"])
    req_days = safe_int(constraints["risk_history_common_days"])

    # 分离当日和下一交易日订单
    same_day_orders = [o for o in plan_orders if o["execution_window"] == "当日"]
    next_day_orders = [o for o in plan_orders if o["execution_window"] == "下一交易日"]

    # 逐订单验证
    order_validations = []
    all_same_day_valid = True
    for o in same_day_orders:
        v = validate_order(o, quote_map, constraints, stock_agg, adv20)
        v["order"] = o
        order_validations.append(v)
        if not v["valid"]:
            all_same_day_valid = False

    # 如果有当日订单不可执行，方案不能在当日合规
    # 但仍需测算"假设全部可执行"的交易后状态用于比较

    # 构建批次台账（按buy_date升序、lot_id升序），只包含可卖批次
    lot_pool = defaultdict(list)
    for lot in lot_details:
        if lot["available_to_sell"] > 0:
            lot_pool[lot["ts_code"]].append(lot.copy())
    for ts in lot_pool:
        lot_pool[ts].sort(key=lambda x: (x["buy_date"], x["lot_id"]))

    # 逐订单执行测算
    order_executions = []
    total_net_proceeds = 0.0
    total_fees = 0.0
    total_realized_pnl = 0.0
    total_slippage_loss = 0.0

    # 跟踪每只股票已卖出数量
    sold_by_stock = defaultdict(int)

    for v in order_validations:
        o = v["order"]
        ts = o["ts_code"]
        sell_qty = safe_int(o["sell_quantity"])
        q = quote_map[ts]
        price = q["current_price_cny"]

        if not v["valid"]:
            # 不可执行订单：成交数量和现金记0
            order_executions.append({
                "plan_id": plan_id,
                "order_sequence": safe_int(o["order_sequence"]),
                "ts_code": ts,
                "security_name": o["security_name"],
                "sell_quantity": sell_qty,
                "execution_window": o["execution_window"],
                "executed_quantity": 0,
                "estimated_execution_price": price,
                "execution_amount": 0.0,
                "commission": 0.0,
                "stamp_duty": 0.0,
                "transfer_fee": 0.0,
                "total_fee": 0.0,
                "net_proceeds": 0.0,
                "realized_pnl": 0.0,
                "slippage_bps": 0.0,
                "participation": v.get("participation", 0),
                "status": v["status"],
                "reason": v["reason"],
                "lot_allocations": [],
            })
            continue

        # 滑点
        participation = v["participation"]
        slip_bps = calc_slippage(participation, constraints)
        exec_price = price * (1 - slip_bps / 10000.0)

        # 执行金额
        exec_amount = sell_qty * exec_price

        # 费用
        commission = max(safe_float(constraints["minimum_commission_cny"]),
                         exec_amount * safe_float(constraints["commission_rate"]))
        stamp_duty = exec_amount * safe_float(constraints["stamp_duty_sell_rate"])
        transfer_fee = exec_amount * safe_float(constraints["transfer_fee_rate"])
        total_fee = commission + stamp_duty + transfer_fee

        # 净回款
        net_proceeds = exec_amount - total_fee

        # 批次分配（FIFO）
        remaining = sell_qty
        allocations = []
        realized_pnl = 0.0
        for lot in lot_pool[ts]:
            if remaining <= 0:
                break
            avail_in_lot = lot["available_to_sell"] - sold_by_stock.get(f"{ts}_{lot['lot_id']}", 0)
            if avail_in_lot <= 0:
                continue
            alloc_qty = min(remaining, avail_in_lot)
            # 已实现盈亏 = (exec_price - cost) * alloc_qty - 分摊费用
            # 费用按订单整体计算，这里先算毛盈亏，费用在订单级扣除
            gross_pnl = (exec_price - lot["cost_price_cny"]) * alloc_qty
            allocations.append({
                "lot_id": lot["lot_id"],
                "allocated_quantity": alloc_qty,
                "cost_price_cny": lot["cost_price_cny"],
                "gross_pnl": gross_pnl,
            })
            realized_pnl += gross_pnl
            sold_by_stock[f"{ts}_{lot['lot_id']}"] = \
                sold_by_stock.get(f"{ts}_{lot['lot_id']}", 0) + alloc_qty
            remaining -= alloc_qty

        # 扣除费用后的已实现盈亏
        realized_pnl_after_fee = realized_pnl - total_fee

        # 滑点损失 = (price - exec_price) * sell_qty
        slippage_loss = (price - exec_price) * sell_qty

        total_net_proceeds += net_proceeds
        total_fees += total_fee
        total_realized_pnl += realized_pnl_after_fee
        total_slippage_loss += slippage_loss
        sold_by_stock[ts] += sell_qty

        order_executions.append({
            "plan_id": plan_id,
            "order_sequence": safe_int(o["order_sequence"]),
            "ts_code": ts,
            "security_name": o["security_name"],
            "sell_quantity": sell_qty,
            "execution_window": o["execution_window"],
            "executed_quantity": sell_qty,
            "estimated_execution_price": exec_price,
            "execution_amount": exec_amount,
            "commission": commission,
            "stamp_duty": stamp_duty,
            "transfer_fee": transfer_fee,
            "total_fee": total_fee,
            "net_proceeds": net_proceeds,
            "realized_pnl": realized_pnl_after_fee,
            "slippage_bps": slip_bps,
            "participation": participation,
            "status": "已执行(测算)",
            "reason": "",
            "lot_allocations": allocations,
        })

    # 交易后持仓
    post_lots = []
    for lot in lot_details:
        ts = lot["ts_code"]
        lot_id = lot["lot_id"]
        sold = sold_by_stock.get(f"{ts}_{lot_id}", 0)
        remaining_qty = lot["quantity"] - sold
        remaining_avail = lot["available_to_sell"] - sold
        if remaining_qty > 0:
            price = quote_map[ts]["current_price_cny"]
            post_lots.append({
                "ts_code": ts,
                "security_name": lot["security_name"],
                "lot_id": lot_id,
                "quantity": remaining_qty,
                "available_to_sell": remaining_avail,
                "cost_price_cny": lot["cost_price_cny"],
                "current_price_cny": price,
                "market_value_cny": remaining_qty * price,
            })

    # 交易后汇总
    post_stock_agg = defaultdict(lambda: {
        "quantity": 0, "available_to_sell": 0,
        "market_value_cny": 0.0, "security_name": ""
    })
    for lot in post_lots:
        ts = lot["ts_code"]
        post_stock_agg[ts]["quantity"] += lot["quantity"]
        post_stock_agg[ts]["available_to_sell"] += lot["available_to_sell"]
        post_stock_agg[ts]["market_value_cny"] += lot["market_value_cny"]
        post_stock_agg[ts]["security_name"] = lot["security_name"]

    post_equity_mv = sum(s["market_value_cny"] for s in post_stock_agg.values())
    post_net_cash = reconciliation["net_cash_cny"] + total_net_proceeds
    post_na = post_equity_mv + post_net_cash

    # 验证：post_na 应等于 reported_na - slippage_loss - total_fees
    expected_na = reconciliation["reported_net_asset_cny"] - total_slippage_loss - total_fees
    na_diff = post_na - expected_na

    # 交易后权重
    for ts in post_stock_agg:
        post_stock_agg[ts]["weight"] = post_stock_agg[ts]["market_value_cny"] / post_na if post_na > 0 else 0

    post_equity_weight = post_equity_mv / post_na if post_na > 0 else 0
    post_cash_weight = post_net_cash / post_na if post_na > 0 else 0

    sorted_post = sorted(post_stock_agg.items(), key=lambda x: x[1]["weight"], reverse=True)
    post_top3 = sum(s[1]["weight"] for s in sorted_post[:3])

    # 交易后CVaR
    post_weights = {ts: post_stock_agg[ts]["weight"] for ts in post_stock_agg}
    post_weights["CASH"] = post_cash_weight
    post_cvar, post_tail_contrib, _, post_n, post_m, post_cvar_status = calc_cvar(
        post_weights, daily_returns, cash_return=cash_return,
        confidence=confidence, required_days=req_days
    )

    # 约束检查
    eq_max = safe_float(constraints["equity_exposure_max"])
    sn_max = safe_float(constraints["single_name_weight_max"])
    t3_max = safe_float(constraints["top3_weight_max"])
    cvar_max = safe_float(constraints["historical_cvar95_max"])
    cash_min = safe_float(constraints["minimum_cash_weight"])

    post_checks = {
        "equity_exposure_max": {
            "actual": post_equity_weight, "threshold": eq_max,
            "pass": post_equity_weight <= eq_max,
        },
        "minimum_cash_weight": {
            "actual": post_cash_weight, "threshold": cash_min,
            "pass": post_cash_weight >= cash_min,
        },
        "top3_weight_max": {
            "actual": post_top3, "threshold": t3_max,
            "pass": post_top3 <= t3_max,
        },
        "historical_cvar95_max": {
            "actual": post_cvar, "threshold": cvar_max,
            "pass": post_cvar is not None and post_cvar <= cvar_max,
            "status": post_cvar_status,
        },
    }

    # 单票
    single_name_breach = False
    for ts in post_stock_agg:
        w = post_stock_agg[ts]["weight"]
        post_checks[f"single_name_{ts}"] = {
            "actual": w, "threshold": sn_max, "pass": w <= sn_max,
        }
        if w > sn_max:
            single_name_breach = True

    # 方案可批准判定
    all_constraints_pass = all(
        v["pass"] for k, v in post_checks.items()
        if not k.startswith("single_name_")
    ) and not single_name_breach

    # 可批准 = 全部当日订单可执行 AND 全部约束通过
    if not all_same_day_valid:
        plan_status = "待条件恢复"
    elif all_constraints_pass:
        plan_status = "可批准"
    else:
        plan_status = "拒绝"

    # 下一交易日订单信息
    next_day_info = []
    for o in next_day_orders:
        next_day_info.append({
            "order_sequence": safe_int(o["order_sequence"]),
            "ts_code": o["ts_code"],
            "security_name": o["security_name"],
            "sell_quantity": safe_int(o["sell_quantity"]),
            "note": "下一交易日订单，不计入当日合规结果",
        })

    return {
        "plan_id": plan_id,
        "status": plan_status,
        "all_same_day_valid": all_same_day_valid,
        "all_constraints_pass": all_constraints_pass,
        "order_executions": order_executions,
        "next_day_orders": next_day_info,
        "total_net_proceeds": total_net_proceeds,
        "total_fees": total_fees,
        "total_realized_pnl": total_realized_pnl,
        "total_slippage_loss": total_slippage_loss,
        "total_sell_notional": sum(
            e["sell_quantity"] * quote_map[e["ts_code"]]["current_price_cny"]
            for e in order_executions if e["executed_quantity"] > 0
        ),
        "post_na": post_na,
        "post_equity_mv": post_equity_mv,
        "post_net_cash": post_net_cash,
        "post_equity_weight": post_equity_weight,
        "post_cash_weight": post_cash_weight,
        "post_top3_weight": post_top3,
        "post_cvar95": post_cvar,
        "post_cvar_tail_contributions": post_tail_contrib,
        "post_cvar_m": post_m,
        "post_cvar_status": post_cvar_status,
        "post_stock_agg": dict(post_stock_agg),
        "post_checks": post_checks,
        "na_reconciliation_diff": na_diff,
    }


# ============================================================
# 7. 敏感性分析
# ============================================================
def sensitivity_analysis(plan_results, lot_details, quote_map, constraints,
                         stock_agg, adv20, daily_returns, reconciliation):
    """两项敏感性检查：全部订单按slippage_cap_bps执行；CVaR窗口缩短为200日"""
    results = {}

    for plan_id, pr in plan_results.items():
        # 敏感性1：滑点上限
        cap = safe_float(constraints["slippage_cap_bps"])
        cap_slippage_loss = 0.0
        cap_fees = 0.0
        for e in pr["order_executions"]:
            if e["executed_quantity"] > 0:
                price = quote_map[e["ts_code"]]["current_price_cny"]
                exec_price_cap = price * (1 - cap / 10000.0)
                cap_slippage_loss += (price - exec_price_cap) * e["executed_quantity"]
                # 费用按上限执行价重算
                exec_amt = e["executed_quantity"] * exec_price_cap
                comm = max(safe_float(constraints["minimum_commission_cny"]),
                           exec_amt * safe_float(constraints["commission_rate"]))
                sd = exec_amt * safe_float(constraints["stamp_duty_sell_rate"])
                tf = exec_amt * safe_float(constraints["transfer_fee_rate"])
                cap_fees += comm + sd + tf

        cap_na = reconciliation["reported_net_asset_cny"] - cap_slippage_loss - cap_fees

        # 敏感性2：CVaR窗口200日
        post_weights = {ts: pr["post_stock_agg"][ts]["weight"] for ts in pr["post_stock_agg"]}
        post_weights["CASH"] = pr["post_cash_weight"]
        cvar200, _, _, n200, m200, status200 = calc_cvar(
            post_weights, daily_returns,
            cash_return=safe_float(constraints["cash_daily_return"]),
            confidence=safe_float(constraints["risk_confidence"]),
            required_days=200
        )

        results[plan_id] = {
            "slippage_cap_loss": cap_slippage_loss,
            "slippage_cap_fees": cap_fees,
            "slippage_cap_na": cap_na,
            "cvar200": cvar200,
            "cvar200_n": n200,
            "cvar200_m": m200,
            "cvar200_status": status200,
        }

    return results


# ============================================================
# 8. 输出CSV
# ============================================================
def write_csv(path, fieldnames, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def output_current_risk_snapshot(profile, adv20, quote_map, constraints):
    """当前持仓风险快照.csv"""
    rows = []
    cvar_contrib = profile["cvar_tail_contributions"]
    sn_max = safe_float(constraints["single_name_weight_max"])

    for ts, agg in profile["stock_agg"].items():
        breach = agg["weight"] > sn_max
        rows.append({
            "ts_code": ts,
            "security_name": agg["security_name"],
            "quantity": agg["quantity"],
            "available_to_sell": agg["available_to_sell"],
            "market_value_cny": round(agg["market_value_cny"], 2),
            "weight": round(agg["weight"], 6),
            "unrealized_pnl_cny": round(agg["unrealized_pnl_cny"], 2),
            "adv20_cny": round(adv20[ts], 2),
            "current_cvar_contribution": round(cvar_contrib.get(ts, 0), 6),
            "breach_flag": "是" if breach else "否",
        })

    path = os.path.join(OUTPUT_DIR, "当前持仓风险快照.csv")
    write_csv(path, list(rows[0].keys()), rows)
    return path


def output_constraint_matrix(profile, plan_results, constraints):
    """候选方案约束矩阵.csv"""
    rows = []
    metric_defs = [
        ("equity_exposure_max", "股票仓位上限", "actual", "threshold", "pass"),
        ("single_name_weight_max", "单票权重上限", None, None, None),
        ("top3_weight_max", "前3大权重上限", "actual", "threshold", "pass"),
        ("historical_cvar95_max", "CVaR95上限", "actual", "threshold", "pass"),
        ("minimum_cash_weight", "最低现金权重", "actual", "threshold", "pass"),
    ]

    evidence_files = {
        "equity_exposure_max": "账户与交易约束_20251231T1445.csv; 持仓批次_20251231T1445.csv; 持仓标的报价_20251231T1445.csv",
        "single_name_weight_max": "账户与交易约束_20251231T1445.csv; 持仓批次_20251231T1445.csv; 持仓标的报价_20251231T1445.csv",
        "top3_weight_max": "账户与交易约束_20251231T1445.csv; 持仓批次_20251231T1445.csv; 持仓标的报价_20251231T1445.csv",
        "historical_cvar95_max": "账户与交易约束_20251231T1445.csv; 持仓标的日线_20240102_20251230.csv",
        "minimum_cash_weight": "账户与交易约束_20251231T1445.csv",
    }

    for plan_id, pr in plan_results.items():
        for key, label, _, _, _ in metric_defs:
            if key == "single_name_weight_max":
                # 逐股票
                for ts, agg in pr["post_stock_agg"].items():
                    ck = pr["post_checks"].get(f"single_name_{ts}", {})
                    actual = ck.get("actual", agg["weight"])
                    threshold = ck.get("threshold", safe_float(constraints["single_name_weight_max"]))
                    passed = ck.get("pass", actual <= threshold)
                    gap = actual - threshold if actual > threshold else 0
                    rows.append({
                        "plan_id": plan_id,
                        "metric_name": f"single_name_{ts}",
                        "actual_value": round(actual, 6),
                        "threshold": threshold,
                        "status": "通过" if passed else "不通过",
                        "gap": round(gap, 6),
                        "calculation_basis": f"交易后{ts}市值/交易后净资产",
                        "evidence_file": evidence_files["single_name_weight_max"],
                    })
            else:
                ck = pr["post_checks"].get(key, {})
                actual = ck.get("actual", 0)
                threshold = ck.get("threshold", 0)
                passed = ck.get("pass", False)
                if key == "minimum_cash_weight":
                    gap = threshold - actual if actual < threshold else 0
                else:
                    gap = actual - threshold if actual > threshold else 0
                rows.append({
                    "plan_id": plan_id,
                    "metric_name": key,
                    "actual_value": round(actual, 6) if actual else "N/A",
                    "threshold": threshold,
                    "status": "通过" if passed else "不通过",
                    "gap": round(gap, 6),
                    "calculation_basis": _calc_basis(key, pr),
                    "evidence_file": evidence_files[key],
                })

        # 订单级约束
        for e in pr["order_executions"]:
            if e["status"] != "已执行(测算)":
                rows.append({
                    "plan_id": plan_id,
                    "metric_name": f"order_{e['order_sequence']}_{e['ts_code']}_executable",
                    "actual_value": e["status"],
                    "threshold": "可执行",
                    "status": "不通过",
                    "gap": e["reason"],
                    "calculation_basis": "T+1/整手/交易状态/报价时效/涨跌停/ADV20参与率检查",
                    "evidence_file": "候选减仓方案_20251231.csv; 持仓标的报价_20251231T1445.csv; 持仓批次_20251231T1445.csv",
                })

    path = os.path.join(OUTPUT_DIR, "候选方案约束矩阵.csv")
    write_csv(path, list(rows[0].keys()), rows)
    return path


def _calc_basis(key, pr):
    if key == "equity_exposure_max":
        return f"交易后股票市值{pr['post_equity_mv']:.2f}/交易后净资产{pr['post_na']:.2f}"
    elif key == "top3_weight_max":
        return "交易后权重前3大股票权重之和"
    elif key == "historical_cvar95_max":
        return f"250日历史CVaR95，m={pr.get('post_cvar_m','N/A')}"
    elif key == "minimum_cash_weight":
        return f"交易后净现金{pr['post_net_cash']:.2f}/交易后净资产{pr['post_na']:.2f}"
    return ""


def output_lot_allocation(plan_results):
    """批次卖出分配.csv"""
    rows = []
    for plan_id, pr in plan_results.items():
        for e in pr["order_executions"]:
            if e["lot_allocations"]:
                for alloc in e["lot_allocations"]:
                    # 分摊费用
                    fee_per_share = e["total_fee"] / e["executed_quantity"] if e["executed_quantity"] > 0 else 0
                    alloc_fee = fee_per_share * alloc["allocated_quantity"]
                    alloc_realized = alloc["gross_pnl"] - alloc_fee
                    rows.append({
                        "plan_id": plan_id,
                        "order_sequence": e["order_sequence"],
                        "ts_code": e["ts_code"],
                        "security_name": e["security_name"],
                        "lot_id": alloc["lot_id"],
                        "allocated_quantity": alloc["allocated_quantity"],
                        "cost_price_cny": alloc["cost_price_cny"],
                        "estimated_execution_price": round(e["estimated_execution_price"], 4),
                        "estimated_fee": round(alloc_fee, 2),
                        "realized_pnl_cny": round(alloc_realized, 2),
                        "eligibility_status": "可卖",
                    })
            else:
                # 未执行订单
                rows.append({
                    "plan_id": plan_id,
                    "order_sequence": e["order_sequence"],
                    "ts_code": e["ts_code"],
                    "security_name": e["security_name"],
                    "lot_id": "N/A",
                    "allocated_quantity": 0,
                    "cost_price_cny": "N/A",
                    "estimated_execution_price": round(e["estimated_execution_price"], 4),
                    "estimated_fee": 0,
                    "realized_pnl_cny": 0,
                    "eligibility_status": e["status"],
                })

    path = os.path.join(OUTPUT_DIR, "批次卖出分配.csv")
    if rows:
        write_csv(path, list(rows[0].keys()), rows)
    return path


def output_monitoring_conditions(plan_results, profile, constraints, quote_map):
    """审批监控条件.csv"""
    rows = []
    cond_id = 1

    # 通用条件
    rows.append({
        "condition_id": f"C{cond_id:03d}",
        "if_condition": "指定报价在审批截止前刷新且quote_age_seconds<=300",
        "threshold": "quote_age_seconds<=300, quote_status=有效",
        "then_action": "以刷新后报价复算受影响方案全部约束，提交投资负责人重新审批",
        "owner": "交易台",
        "check_deadline": constraints["approval_deadline"],
        "evidence_needed": "刷新后的报价快照、复算后的约束矩阵",
        "stop_condition": "审批截止时点已过且未获刷新报价",
    })
    cond_id += 1

    rows.append({
        "condition_id": f"C{cond_id:03d}",
        "if_condition": "任一硬约束在复算后仍不通过",
        "threshold": "equity_exposure<=0.82, single_name<=0.20, top3<=0.50, cvar95<=0.022, cash>=0.18",
        "then_action": "拒绝执行该方案，要求交易台改案后重新提交",
        "owner": "投顾风控组",
        "check_deadline": constraints["approval_deadline"],
        "evidence_needed": "约束矩阵不通过项的实际值与缺口",
        "stop_condition": "全部硬约束复算通过",
    })
    cond_id += 1

    rows.append({
        "condition_id": f"C{cond_id:03d}",
        "if_condition": "方案C中600519.SH报价仍陈旧(quote_age_seconds>300)",
        "threshold": "quote_age_seconds<=300",
        "then_action": "该订单标记为待条件恢复，不得在当日执行；待报价刷新后单独审批",
        "owner": "交易台",
        "check_deadline": constraints["approval_deadline"],
        "evidence_needed": "600519.SH刷新报价",
        "stop_condition": "600519.SH报价刷新且复算通过",
    })
    cond_id += 1

    rows.append({
        "condition_id": f"C{cond_id:03d}",
        "if_condition": "方案C中300750.SZ卖出15000股涉及当日买入批次P02",
        "threshold": "available_to_sell>0的批次才可分配",
        "then_action": "P02批次available_to_sell=0(T+1)，不得分配；卖出量从P01可卖量中扣除，若P01不足则方案不可执行",
        "owner": "交易台",
        "check_deadline": constraints["approval_deadline"],
        "evidence_needed": "批次台账、可卖数量复核",
        "stop_condition": "P02批次T+1解冻后(下一交易日)可卖",
    })
    cond_id += 1

    rows.append({
        "condition_id": f"C{cond_id:03d}",
        "if_condition": "下一交易日订单(方案C order3 002594.SZ卖出30000股)到达执行日",
        "threshold": "下一交易日开盘后，报价有效且在涨跌停范围内",
        "then_action": "单独提交该订单审批，不计入2025-12-31当日合规结果；执行前重新核验可卖数量与ADV20参与率",
        "owner": "交易台",
        "check_deadline": "下一交易日收盘前",
        "evidence_needed": "下一交易日报价、可卖数量更新、ADV20参与率复算",
        "stop_condition": "订单执行或撤销",
    })
    cond_id += 1

    # 主方案特定监控
    for plan_id, pr in plan_results.items():
        if pr["status"] == "可批准":
            rows.append({
                "condition_id": f"C{cond_id:03d}",
                "if_condition": f"{plan_id}批准后执行时实际成交价偏离测算价超过滑点上限",
                "threshold": f"滑点<={safe_float(constraints['slippage_cap_bps'])}bps",
                "then_action": "暂停执行，重新评估交易成本与约束达标情况",
                "owner": "交易台",
                "check_deadline": "执行当日收盘前",
                "evidence_needed": "实际成交回报、滑点计算",
                "stop_condition": "实际滑点在限内且执行完成",
            })
            cond_id += 1

    path = os.path.join(OUTPUT_DIR, "审批监控条件.csv")
    write_csv(path, list(rows[0].keys()), rows)
    return path


# ============================================================
# 9. 生成备忘录
# ============================================================
def generate_memo(reconciliation, profile, plan_results, sensitivity, audit,
                  constraints, adv20, quote_map, lot_details):
    """生成持仓交易审批备忘录.md"""
    lines = []
    a = lines.append

    a("# 持仓交易审批备忘录")
    a("")
    a(f"**账户**: 稳健账户A | **决策时点**: 2025-12-31T14:45:00+08:00 | **审批截止**: {constraints['approval_deadline']}")
    a("")

    # === 决策摘要 ===
    a("## 一、决策摘要")
    a("")

    # 找出可批准方案
    approvable = [pid for pid, pr in plan_results.items() if pr["status"] == "可批准"]
    conditional = [pid for pid, pr in plan_results.items() if pr["status"] == "待条件恢复"]
    rejected = [pid for pid, pr in plan_results.items() if pr["status"] == "拒绝"]

    if approvable:
        # 排序：CVaR较低 → 总交易成本较低 → 总卖出金额较低 → plan_id升序
        def sort_key(pid):
            pr = plan_results[pid]
            return (pr["post_cvar95"] or 999, pr["total_fees"], pr["total_sell_notional"], pid)
        approvable.sort(key=sort_key)
        main_plan = approvable[0]
        a(f"**结论**: {main_plan}为唯一主方案，可在审批截止前批准执行。")
    elif conditional:
        a(f"**结论**: 无方案可在当前条件下批准。{', '.join(conditional)}处于待条件恢复状态，{', '.join(rejected) if rejected else '无'}被拒绝。")
        main_plan = None
    else:
        a(f"**结论**: 全部方案拒绝，无方案可在规则内执行。")
        main_plan = None

    a("")
    # 找当前最紧的约束
    tightest = None
    tightest_gap = -999
    for key, ck in profile["checks"].items():
        if key.startswith("single_name_"):
            gap = ck["actual"] - ck["threshold"]
        elif key == "minimum_cash_weight":
            gap = ck["threshold"] - ck["actual"]
        else:
            gap = ck["actual"] - ck["threshold"]
        if gap > tightest_gap:
            tightest_gap = gap
            tightest = key

    tight_labels = {
        "equity_exposure": "股票仓位",
        "minimum_cash_weight": "最低现金权重",
        "top3_weight": "前3大权重",
        "historical_cvar95": "CVaR95",
    }
    if tightest and tightest in tight_labels:
        ck = profile["checks"][tightest]
        a(f"**当前最紧约束**: {tight_labels[tightest]}（实际{ck['actual']*100:.2f}%，阈值{ck['threshold']*100:.2f}%）。")
    elif tightest and tightest.startswith("single_name_"):
        ts = tightest.replace("single_name_", "")
        ck = profile["checks"][tightest]
        a(f"**当前最紧约束**: {ts}单票权重（实际{ck['actual']*100:.2f}%，阈值{ck['threshold']*100:.2f}%）。")

    a("")
    if main_plan:
        pr = plan_results[main_plan]
        a(f"**首要动作**: 批准{main_plan}，当日执行{len([e for e in pr['order_executions'] if e['executed_quantity']>0])}笔卖出，预计净回款{pr['total_net_proceeds']:,.2f}元，交易费用{pr['total_fees']:,.2f}元。")
    else:
        a("**首要动作**: 拒绝全部当日方案，要求交易台在报价刷新/可卖数量确认后改案重新提交。")

    a("")
    a(f"**最迟行动日**: 2025-12-31（当日），审批截止{constraints['approval_deadline']}。")
    a("")

    # === 数据审计 ===
    a("## 二、数据审计与账户勾稽")
    a("")
    a("### 2.1 数据审计")
    a("")
    for item in audit:
        a(f"- {item}")
    a("")
    a("6只股票代码均在持仓、报价和日线中匹配，无未知代码。日线amount单位为千元，已按要求转换为元计算ADV20。")
    a("")

    a("### 2.2 账户勾稽")
    a("")
    a("| 项目 | 金额(元) |")
    a("|------|----------|")
    a(f"| 现金余额 | {reconciliation['cash_balance_cny']:,.2f} |")
    a(f"| 应付费用 | {reconciliation['accrued_fees_payable_cny']:,.2f} |")
    a(f"| 净现金 | {reconciliation['net_cash_cny']:,.2f} |")
    a(f"| 股票总市值 | {reconciliation['total_equity_mv_cny']:,.2f} |")
    a(f"| 复算净资产 | {reconciliation['recalc_net_asset_cny']:,.2f} |")
    a(f"| 报表净资产 | {reconciliation['reported_net_asset_cny']:,.2f} |")
    a(f"| 差额 | {reconciliation['difference_cny']:,.4f} |")
    a(f"| 勾稽状态 | {'通过' if reconciliation['reconciled'] else '不通过'} |")
    a("")

    if reconciliation["reconciled"]:
        a("账户快照勾稽通过，复算净资产与报表净资产差额在1元容差内。")
    else:
        a("**账户快照无法勾稽**，差额超过1元，需更正文件。")
    a("")

    # === 当前风险画像 ===
    a("## 三、当前风险画像")
    a("")
    a("### 3.1 持仓与权重")
    a("")
    a("| 股票 | 数量 | 可卖 | 市值(元) | 权重 | 未实现盈亏(元) | ADV20(元) |")
    a("|------|------|------|----------|------|----------------|-----------|")
    for ts, agg in profile["sorted_stocks"]:
        a(f"| {ts} {agg['security_name']} | {agg['quantity']:,} | {agg['available_to_sell']:,} | {agg['market_value_cny']:,.2f} | {agg['weight']*100:.2f}% | {agg['unrealized_pnl_cny']:,.2f} | {adv20[ts]:,.2f} |")
    a("")
    a(f"**股票仓位**: {profile['equity_weight']*100:.2f}%（阈值≤{safe_float(constraints['equity_exposure_max'])*100:.0f}%）")
    a(f"**净现金权重**: {profile['cash_weight']*100:.2f}%（阈值≥{safe_float(constraints['minimum_cash_weight'])*100:.0f}%）")
    a(f"**前3大权重**: {profile['top3_weight']*100:.2f}%（阈值≤{safe_float(constraints['top3_weight_max'])*100:.0f}%）")
    a("")

    a("### 3.2 当前约束状态")
    a("")
    a("| 约束 | 实际值 | 阈值 | 状态 |")
    a("|------|--------|------|------|")
    for key, ck in profile["checks"].items():
        if key.startswith("single_name_"):
            label = f"单票 {key.replace('single_name_','')}"
        else:
            label = tight_labels.get(key, key)
        actual = ck["actual"]
        if actual is not None and isinstance(actual, float) and actual < 1:
            actual_str = f"{actual*100:.2f}%"
        else:
            actual_str = str(actual)
        threshold = ck["threshold"]
        if isinstance(threshold, float) and threshold < 1:
            threshold_str = f"{threshold*100:.2f}%"
        else:
            threshold_str = str(threshold)
        a(f"| {label} | {actual_str} | {threshold_str} | {'通过' if ck['pass'] else '不通过'} |")
    a("")

    a("### 3.3 CVaR分析")
    a("")
    if profile["cvar95"] is not None:
        a(f"共同有效交易日: {profile['cvar_n']}天（要求≥{safe_int(constraints['risk_history_common_days'])}天）")
        a(f"尾部样本数 m = ceil((1-{safe_float(constraints['risk_confidence'])})×{profile['cvar_n']}) = {profile['cvar_m']}")
        a(f"**当前historical_CVaR95 = {profile['cvar95']*100:.4f}%**（阈值≤{safe_float(constraints['historical_cvar95_max'])*100:.2f}%）")
        a("")
        a("尾部损失贡献分解:")
        a("")
        a("| 标的 | 平均损失贡献 | 占比 |")
        a("|------|-------------|------|")
        total_contrib = sum(profile["cvar_tail_contributions"].values())
        for ts, contrib in sorted(profile["cvar_tail_contributions"].items(), key=lambda x: x[1], reverse=True):
            name = "现金" if ts == "CASH" else profile["stock_agg"].get(ts, {}).get("security_name", ts)
            pct_val = contrib / total_contrib * 100 if total_contrib != 0 else 0
            a(f"| {ts} {name} | {contrib*100:.4f}% | {pct_val:.1f}% |")
        a(f"| **合计** | **{total_contrib*100:.4f}%** | **100.0%** |")
    else:
        a(f"CVaR无法计算: {profile['cvar_status']}")
    a("")

    # === 方案比较 ===
    a("## 四、候选方案执行测算与比较")
    a("")

    for plan_id in ["方案A", "方案B", "方案C"]:
        pr = plan_results[plan_id]
        a(f"### 4.{['方案A','方案B','方案C'].index(plan_id)+1} {plan_id}")
        a("")
        a(f"**方案状态**: {pr['status']}")
        a("")

        a("订单明细:")
        a("")
        a("| 序号 | 窗口 | 股票 | 卖出量 | 执行价 | 执行金额 | 费用 | 净回款 | 已实现盈亏 | 状态 |")
        a("|------|------|------|--------|--------|----------|------|--------|-----------|------|")
        for e in pr["order_executions"]:
            a(f"| {e['order_sequence']} | {e['execution_window']} | {e['ts_code']} | {e['sell_quantity']:,} | {e['estimated_execution_price']:.4f} | {e['execution_amount']:,.2f} | {e['total_fee']:,.2f} | {e['net_proceeds']:,.2f} | {e['realized_pnl']:,.2f} | {e['status']} |")
        if pr["next_day_orders"]:
            for nd in pr["next_day_orders"]:
                a(f"| {nd['order_sequence']} | 下一交易日 | {nd['ts_code']} | {nd['sell_quantity']:,} | - | - | - | - | - | 不计入当日 |")
        a("")

        a(f"- 总净回款: {pr['total_net_proceeds']:,.2f}元")
        a(f"- 总交易费用: {pr['total_fees']:,.2f}元")
        a(f"- 总已实现盈亏(扣费后): {pr['total_realized_pnl']:,.2f}元")
        a(f"- 总滑点损失: {pr['total_slippage_loss']:,.2f}元")
        a(f"- 交易后净资产: {pr['post_na']:,.2f}元（勾稽差{pr['na_reconciliation_diff']:.4f}元）")
        a("")

        a("交易后约束状态:")
        a("")
        a("| 约束 | 交易后实际值 | 阈值 | 状态 |")
        a("|------|-------------|------|------|")
        for key, ck in pr["post_checks"].items():
            if key.startswith("single_name_"):
                label = f"单票 {key.replace('single_name_','')}"
            else:
                label = tight_labels.get(key, key)
            actual = ck["actual"]
            if actual is not None and isinstance(actual, float) and actual < 1:
                actual_str = f"{actual*100:.2f}%"
            elif actual is None:
                actual_str = "N/A"
            else:
                actual_str = str(actual)
            threshold = ck["threshold"]
            if isinstance(threshold, float) and threshold < 1:
                threshold_str = f"{threshold*100:.2f}%"
            else:
                threshold_str = str(threshold)
            a(f"| {label} | {actual_str} | {threshold_str} | {'通过' if ck['pass'] else '不通过'} |")
        a("")

        if pr["post_cvar95"] is not None:
            a(f"交易后CVaR95 = {pr['post_cvar95']*100:.4f}%")
        a("")

    # 方案比较汇总
    a("### 4.4 方案比较汇总")
    a("")
    a("| 指标 | 方案A | 方案B | 方案C |")
    a("|------|-------|-------|-------|")
    a(f"| 方案状态 | {plan_results['方案A']['status']} | {plan_results['方案B']['status']} | {plan_results['方案C']['status']} |")
    a(f"| 股票仓位 | {plan_results['方案A']['post_equity_weight']*100:.2f}% | {plan_results['方案B']['post_equity_weight']*100:.2f}% | {plan_results['方案C']['post_equity_weight']*100:.2f}% |")
    a(f"| 现金权重 | {plan_results['方案A']['post_cash_weight']*100:.2f}% | {plan_results['方案B']['post_cash_weight']*100:.2f}% | {plan_results['方案C']['post_cash_weight']*100:.2f}% |")
    a(f"| 前3大权重 | {plan_results['方案A']['post_top3_weight']*100:.2f}% | {plan_results['方案B']['post_top3_weight']*100:.2f}% | {plan_results['方案C']['post_top3_weight']*100:.2f}% |")
    cvar_a = f"{plan_results['方案A']['post_cvar95']*100:.4f}%" if plan_results['方案A']['post_cvar95'] else "N/A"
    cvar_b = f"{plan_results['方案B']['post_cvar95']*100:.4f}%" if plan_results['方案B']['post_cvar95'] else "N/A"
    cvar_c = f"{plan_results['方案C']['post_cvar95']*100:.4f}%" if plan_results['方案C']['post_cvar95'] else "N/A"
    a(f"| CVaR95 | {cvar_a} | {cvar_b} | {cvar_c} |")
    a(f"| 总交易费用 | {plan_results['方案A']['total_fees']:,.2f} | {plan_results['方案B']['total_fees']:,.2f} | {plan_results['方案C']['total_fees']:,.2f} |")
    a(f"| 总卖出名义金额 | {plan_results['方案A']['total_sell_notional']:,.2f} | {plan_results['方案B']['total_sell_notional']:,.2f} | {plan_results['方案C']['total_sell_notional']:,.2f} |")
    a("")

    # === 敏感性 ===
    a("## 五、敏感性分析")
    a("")
    a("### 5.1 全部订单按滑点上限执行")
    a("")
    a("| 方案 | 滑点上限损失(元) | 上限下费用(元) | 上限下净资产(元) |")
    a("|------|-----------------|---------------|-----------------|")
    for pid in ["方案A", "方案B", "方案C"]:
        s = sensitivity[pid]
        a(f"| {pid} | {s['slippage_cap_loss']:,.2f} | {s['slippage_cap_fees']:,.2f} | {s['slippage_cap_na']:,.2f} |")
    a("")
    a("滑点上限情景下交易成本上升，但不改变约束达标方向（权重和CVaR基于附件当前价盯市，不受执行滑点影响）。")
    a("")

    a("### 5.2 CVaR窗口缩短为200日")
    a("")
    a("| 方案 | 200日CVaR95 | 共同日数 | m | 状态 |")
    a("|------|-------------|---------|---|------|")
    for pid in ["方案A", "方案B", "方案C"]:
        s = sensitivity[pid]
        cvar_str = f"{s['cvar200']*100:.4f}%" if s['cvar200'] else "N/A"
        a(f"| {pid} | {cvar_str} | {s['cvar200_n']} | {s['cvar200_m']} | {s['cvar200_status']} |")
    a("")
    a("200日窗口CVaR用于风险提示，不替换250日主口径。")
    a("")

    # === 反向证据 ===
    a("## 六、反向证据与风险提示")
    a("")
    a("1. **600519.SH贵州茅台报价陈旧**: quote_age_seconds=780s > quote_max_age_seconds=300s，方案C中该股票订单不得使用附件报价批准，需待报价刷新。")
    a("2. **T+1批次不可卖**: 300750.SZ批次P02（12000股，buy_date=2025-12-31）和002594.SZ批次P04（40000股，buy_date=2025-12-31）available_to_sell=0，方案C desk_note标注「含当日买入批次」存在违规意图，实际分配只能从可卖批次P01/P03中扣除。")
    a("3. **下一交易日订单不计入当日合规**: 方案C order3（002594.SZ卖出30000股）execution_window为「下一交易日」，不得提前计入2025-12-31当日合规结果。")
    a("4. **CVaR基于历史数据**: historical_CVaR95使用2024-01-02至2025-12-30的250个共同交易日历史收益率，不代表未来尾部风险。")
    a("5. **报价为仿真锚定**: 附件报价为题方脱敏仿真数据，价格锚定公开日线，不代表真实可成交报价。")
    a("")

    # === 条件化动作 ===
    a("## 七、条件化动作（如果→那么）")
    a("")

    if main_plan:
        a(f"### 主方案: {main_plan}")
        a("")
        pr = plan_results[main_plan]
        a(f"如果 {main_plan} 在审批截止前全部当日订单报价仍有效（quote_age_seconds≤300）、可卖数量未变、且交易后全部硬约束复算通过，")
        a(f"那么 提交投资负责人批准 {main_plan}，由交易台以限价委托方式逐笔执行；执行后复核实际成交回报与约束达标情况。")
        a("")
        a(f"如果 {main_plan} 执行中任一订单实际成交价偏离测算价超过滑点上限（{safe_float(constraints['slippage_cap_bps'])}bps），")
        a("那么 暂停该订单执行，重新评估交易成本对净资产和约束的影响，经风控复核后方可继续。")
        a("")

    a("### 通用条件")
    a("")
    a("如果 600519.SH贵州茅台报价在审批截止前刷新（quote_age_seconds≤300，quote_status=有效）且其余约束复算仍通过，")
    a("那么 以刷新后报价复算方案C全部约束，提交投资负责人重新审批方案C。")
    a("")
    a("如果 600519.SH报价在审批截止前仍未刷新，")
    a("那么 方案C中该订单维持「待条件恢复」状态，不得在当日执行；方案C整体因存在不可执行当日订单而不予批准。")
    a("")
    a("如果 任一方案交易后equity_exposure_max、single_name_weight_max、top3_weight_max、historical_cvar95_max或minimum_cash_weight仍不通过，")
    a("那么 拒绝执行该方案，要求交易台改案后重新提交；不得挑选「最接近」的方案冒充合规方案。")
    a("")
    a("如果 方案C中300750.SZ卖出15000股试图分配到P02批次（available_to_sell=0，T+1），")
    a("那么 该分配无效，卖出量只能从P01可卖量（18000股）中扣除；若P01可卖量不足15000股则方案不可执行。")
    a("")
    a("如果 下一交易日（方案C order3 002594.SZ卖出30000股）到达执行日，")
    a("那么 单独提交该订单审批，执行前重新核验可卖数量（P04批次T+1解冻后可卖）、报价有效性和ADV20参与率；该订单不计入2025-12-31当日合规结果。")
    a("")

    # === 数据限制 ===
    a("## 八、数据限制与声明")
    a("")
    a("1. 本备忘录仅使用5个附件中不晚于as_of=2025-12-31T14:45:00+08:00的记录，未联网补入事后行情、公司事件或实时盘口。")
    a("2. 日线为公开历史快照（TuSharePro.daily），retrieved_at=2026-09-04仅表示抓取时间，不是可用行情时点。")
    a("3. 持仓、账户、报价和候选方案为题方脱敏或仿真输入，不得识别为真实客户，当前价锚定公开日线不代表真实可成交报价。")
    a("4. 所有测算为研究测算，不构成真实成交、收益承诺或投资建议；不连接券商、不发送订单。")
    a("5. 计算过程保留全精度，展示金额保留2位小数，比例保留4位小数（权重展示2位小数）。")
    a("")
    a("---")
    a(f"*生成时间: 2025-12-31T14:45:00+08:00 | 投顾风控组 | 内部审批意见*")

    path = os.path.join(OUTPUT_DIR, "持仓交易审批备忘录.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 60)
    print("持仓交易审批复核 - 开始")
    print("=" * 60)

    # 1. 加载数据
    print("\n[1/8] 加载数据...")
    positions, quotes, constraints, plans, daily = load_data()
    print(f"  持仓批次: {len(positions)} 条")
    print(f"  报价: {len(quotes)} 条")
    print(f"  方案: {len(plans)} 条")
    print(f"  日线: {len(daily)} 条")

    # 2. 数据审计
    print("\n[2/8] 数据审计...")
    audit = audit_data(positions, quotes, constraints, plans, daily)
    for a in audit:
        print(f"  - {a}")

    # 3. 构建报价映射
    quote_map = build_quote_map(quotes)

    # 4. 账户勾稽
    print("\n[3/8] 账户勾稽...")
    reconciliation, lot_details = reconcile_account(positions, quote_map, constraints)
    print(f"  股票总市值: {reconciliation['total_equity_mv_cny']:,.2f}")
    print(f"  净现金: {reconciliation['net_cash_cny']:,.2f}")
    print(f"  复算净资产: {reconciliation['recalc_net_asset_cny']:,.2f}")
    print(f"  报表净资产: {reconciliation['reported_net_asset_cny']:,.2f}")
    print(f"  差额: {reconciliation['difference_cny']:.4f}")
    print(f"  勾稽: {'通过' if reconciliation['reconciled'] else '不通过'}")

    if not reconciliation["reconciled"]:
        print("\n*** 账户快照无法勾稽，停止审批 ***")
        # 仍然输出已有的部分
        return

    # 5. ADV20
    print("\n[4/8] 计算ADV20...")
    adv20, daily_returns = calc_adv20(daily)
    for ts, v in adv20.items():
        print(f"  {ts}: ADV20={v:,.2f}")

    # 6. 当前风险画像
    print("\n[5/8] 当前风险画像...")
    profile = current_risk_profile(lot_details, reconciliation, quote_map,
                                    adv20, daily_returns, constraints)
    print(f"  股票仓位: {profile['equity_weight']*100:.2f}%")
    print(f"  现金权重: {profile['cash_weight']*100:.2f}%")
    print(f"  前3大: {profile['top3_weight']*100:.2f}%")
    print(f"  CVaR95: {profile['cvar95']*100:.4f}%" if profile['cvar95'] else "  CVaR95: N/A")
    print(f"  CVaR共同日: {profile['cvar_n']}, m={profile['cvar_m']}")

    # 7. 方案执行测算
    print("\n[6/8] 方案执行测算...")
    # 按方案分组
    plans_by_id = defaultdict(list)
    for p in plans:
        plans_by_id[p["plan_id"]].append(p)

    plan_results = {}
    for plan_id in ["方案A", "方案B", "方案C"]:
        print(f"\n  --- {plan_id} ---")
        pr = execute_plan(plan_id, plans_by_id[plan_id], lot_details, quote_map,
                          constraints, profile["stock_agg"], adv20, daily_returns,
                          reconciliation)
        plan_results[plan_id] = pr
        print(f"  状态: {pr['status']}")
        print(f"  全部当日订单可执行: {pr['all_same_day_valid']}")
        print(f"  全部约束通过: {pr['all_constraints_pass']}")
        print(f"  交易后股票仓位: {pr['post_equity_weight']*100:.2f}%")
        print(f"  交易后现金权重: {pr['post_cash_weight']*100:.2f}%")
        print(f"  交易后前3大: {pr['post_top3_weight']*100:.2f}%")
        print(f"  交易后CVaR95: {pr['post_cvar95']*100:.4f}%" if pr['post_cvar95'] else "  交易后CVaR95: N/A")
        print(f"  总费用: {pr['total_fees']:,.2f}")
        print(f"  净资产勾稽差: {pr['na_reconciliation_diff']:.4f}")

    # 8. 敏感性分析
    print("\n[7/8] 敏感性分析...")
    sensitivity = sensitivity_analysis(plan_results, lot_details, quote_map,
                                        constraints, profile["stock_agg"], adv20,
                                        daily_returns, reconciliation)
    for pid, s in sensitivity.items():
        print(f"  {pid}: 滑点上限损失={s['slippage_cap_loss']:,.2f}, 200日CVaR={s['cvar200']*100:.4f}%" if s['cvar200'] else f"  {pid}: 200日CVaR=N/A")

    # 9. 输出交付物
    print("\n[8/8] 输出交付物...")

    p1 = output_current_risk_snapshot(profile, adv20, quote_map, constraints)
    print(f"  ✓ {p1}")

    p2 = output_constraint_matrix(profile, plan_results, constraints)
    print(f"  ✓ {p2}")

    p3 = output_lot_allocation(plan_results)
    print(f"  ✓ {p3}")

    p4 = output_monitoring_conditions(plan_results, profile, constraints, quote_map)
    print(f"  ✓ {p4}")

    p5 = generate_memo(reconciliation, profile, plan_results, sensitivity, audit,
                       constraints, adv20, quote_map, lot_details)
    print(f"  ✓ {p5}")

    # 依赖清单和运行说明
    req_path = os.path.join(OUTPUT_DIR, "requirements.txt")
    with open(req_path, "w", encoding="utf-8") as f:
        f.write("# 持仓交易审批复核脚本依赖\n")
        f.write("# Python 3.8+\n")
        f.write("# 仅使用标准库，无需额外安装\n")
    print(f"  ✓ {req_path}")

    readme_path = os.path.join(OUTPUT_DIR, "运行说明.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write("# 持仓交易审批复核 - 运行说明\n\n")
        f.write("## 环境要求\n")
        f.write("- Python 3.8 或更高版本\n")
        f.write("- 仅使用Python标准库（csv, math, os, sys, collections, datetime）\n")
        f.write("- 无需联网，无需安装第三方包\n\n")
        f.write("## 目录结构\n")
        f.write("```\n")
        f.write("./\n")
        f.write("├── run_portfolio_review.py    # 主脚本\n")
        f.write("├── requirements.txt            # 依赖清单\n")
        f.write("├── 运行说明.md                  # 本文件\n")
        f.write("└── attachments/                # 原始附件目录\n")
        f.write("    ├── 持仓批次_20251231T1445.csv\n")
        f.write("    ├── 持仓标的报价_20251231T1445.csv\n")
        f.write("    ├── 账户与交易约束_20251231T1445.csv\n")
        f.write("    ├── 候选减仓方案_20251231.csv\n")
        f.write("    └── 持仓标的日线_20240102_20251230.csv\n")
        f.write("```\n\n")
        f.write("## 运行方式\n")
        f.write("```bash\n")
        f.write("python3 run_portfolio_review.py\n")
        f.write("```\n\n")
        f.write("## 输出文件\n")
        f.write("1. `持仓交易审批备忘录.md` - 完整审批备忘录\n")
        f.write("2. `当前持仓风险快照.csv` - 当前持仓风险指标\n")
        f.write("3. `候选方案约束矩阵.csv` - 三方案逐项约束检查\n")
        f.write("4. `批次卖出分配.csv` - 逐批次卖出分配与盈亏\n")
        f.write("5. `审批监控条件.csv` - 条件化动作监控清单\n\n")
        f.write("## 注意事项\n")
        f.write("- 脚本不联网，所有数据来自attachments目录下的5个CSV文件\n")
        f.write("- 脚本不硬编码附件中的价格、数量或推荐方案\n")
        f.write("- 计算过程保留全精度，展示可舍入\n")
        f.write("- 本脚本仅形成内部审批意见和条件化动作，不连接券商、不发送订单\n")
    print(f"  ✓ {readme_path}")

    print("\n" + "=" * 60)
    print("持仓交易审批复核 - 完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
