#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_portfolio_review.py
稳健账户A 持仓交易审批复核脚本（2025-12-31 14:45）

用法:
    python run_portfolio_review.py [附件目录] [输出目录]
默认:
    附件目录 = ./attachments
    输出目录 = ./output

脚本不联网、不硬编码任何价格/数量/推荐方案，全部从5个CSV原始附件读取并重建。
"""

import sys, os, csv, math
from datetime import datetime, timedelta
from collections import defaultdict

# ---------------------------------------------------------------------------
# 0. 路径
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ATTACH_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(SCRIPT_DIR, "attachments")
OUT_DIR    = sys.argv[2] if len(sys.argv) > 2 else os.path.join(SCRIPT_DIR, "output")
os.makedirs(OUT_DIR, exist_ok=True)

F_DAILY = os.path.join(ATTACH_DIR, "持仓标的日线_20240102_20251230.csv")
F_LOTS  = os.path.join(ATTACH_DIR, "持仓批次_20251231T1445.csv")
F_QUOTE = os.path.join(ATTACH_DIR, "持仓标的报价_20251231T1445.csv")
F_ACCT  = os.path.join(ATTACH_DIR, "账户与交易约束_20251231T1445.csv")
F_PLANS = os.path.join(ATTACH_DIR, "候选减仓方案_20251231.csv")

AS_OF = datetime(2025,12,31,14,45,0)

# ---------------------------------------------------------------------------
# 1. 读 CSV
# ---------------------------------------------------------------------------
def read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

daily_raw = read_csv(F_DAILY)
lots_raw  = read_csv(F_LOTS)
quote_raw = read_csv(F_QUOTE)
acct_raw  = read_csv(F_ACCT)
plans_raw = read_csv(F_PLANS)

# ---------------------------------------------------------------------------
# 2. 账户约束（单值）
# ---------------------------------------------------------------------------
a = acct_raw[0]
CASH_BAL       = float(a["cash_balance_cny"])
ACCRUED_FEES   = float(a["accrued_fees_payable_cny"])
REPORTED_NA    = float(a["reported_net_asset_cny"])
EQ_MAX         = float(a["equity_exposure_max"])
SINGLE_MAX     = float(a["single_name_weight_max"])
TOP3_MAX       = float(a["top3_weight_max"])
CVAR_MAX       = float(a["historical_cvar95_max"])
MIN_CASH_W     = float(a["minimum_cash_weight"])
COMM_RATE      = float(a["commission_rate"])
COMM_MIN       = float(a["minimum_commission_cny"])
STAMP_RATE     = float(a["stamp_duty_sell_rate"])
TRANSFER_RATE  = float(a["transfer_fee_rate"])
SLIP_BASE      = float(a["slippage_base_bps"])
SLIP_SQRT      = float(a["slippage_sqrt_coefficient_bps"])
SLIP_CAP       = float(a["slippage_cap_bps"])
MAX_ADV_PART   = float(a["max_adv20_participation"])
RISK_DAYS      = int(a["risk_history_common_days"])
CONFIDENCE     = float(a["risk_confidence"])
CASH_D_RET     = float(a["cash_daily_return"])
QUOTE_MAX_AGE  = float(a["quote_max_age_seconds"])
APPROVAL_DDL   = a["approval_deadline"]

# ---------------------------------------------------------------------------
# 3. 报价表
# ---------------------------------------------------------------------------
quotes = {}
for r in quote_raw:
    quotes[r["ts_code"]] = {
        "price": float(r["current_price_cny"]),
        "prev_close": float(r["previous_close_cny"]),
        "limit_up": float(r["limit_up_cny"]),
        "limit_down": float(r["limit_down_cny"]),
        "age": float(r["quote_age_seconds"]),
        "status": r["quote_status"],
        "trading_status": r["trading_status"],
        "board_lot": int(r["board_lot_shares"]),
        "fx": float(r["fx_to_cny"]),
        "ts": r["quote_timestamp"],
    }

# ---------------------------------------------------------------------------
# 4. 批次台账
# ---------------------------------------------------------------------------
# lots_by_code[ts_code] = list of lot dicts (sorted buy_date asc, lot_id asc later)
lots_by_code = defaultdict(list)
all_lots = []
for r in lots_raw:
    lot = {
        "lot_id": r["lot_id"],
        "ts_code": r["ts_code"],
        "security_name": r["security_name"],
        "buy_date": r["buy_date"],
        "quantity": int(r["quantity"]),
        "available": int(r["available_to_sell"]),
        "cost": float(r["cost_price_cny"]),
    }
    lots_by_code[r["ts_code"]].append(lot)
    all_lots.append(lot)

# 按 buy_date 升序、lot_id 升序
for c in lots_by_code:
    lots_by_code[c].sort(key=lambda x: (x["buy_date"], x["lot_id"]))

# ---------------------------------------------------------------------------
# 5. 日线处理：按 ts_code 收集 (trade_date, pct_chg, amount, record_status)
# ---------------------------------------------------------------------------
daily = defaultdict(list)
for r in daily_raw:
    daily[r["ts_code"]].append({
        "date": r["trade_date"],
        "pct_chg": float(r["pct_chg"]) if r["pct_chg"] not in ("", None) else None,
        "amount": float(r["amount"]) if r["amount"] not in ("", None) else None,
        "status": r["record_status"],
    })
for c in daily:
    daily[c].sort(key=lambda x: x["date"])

# 校验代码集
codes_in_lots = set(lots_by_code.keys())
codes_in_quotes = set(quotes.keys())
codes_in_daily = set(daily.keys())
unknown_in_lots = codes_in_lots - codes_in_quotes
# 任何方案中出现的未知代码 -> 方案不可评估
plan_codes = set(r["ts_code"] for r in plans_raw)
unknown_in_plans = plan_codes - codes_in_quotes
assert not unknown_in_lots, f"持仓存在无报价代码: {unknown_in_lots}"
assert not unknown_in_plans, f"方案存在未知代码: {unknown_in_plans}"

# ADV20: 每只股票截至2025-12-30最近20条"有行情"记录
adv20 = {}
for c in codes_in_lots:
    valid = [d for d in daily[c] if d["status"]=="有行情" and d["amount"] is not None]
    last20 = valid[-20:]
    adv20[c] = sum(d["amount"]*1000 for d in last20)/len(last20)  # 千元->元

# ---------------------------------------------------------------------------
# 6. 当前账户勾稽
# ---------------------------------------------------------------------------
# 每批次市值 = quantity * current_price * fx
stock_mv = 0.0
pos_rows = []
for lot in all_lots:
    c = lot["ts_code"]
    q = quotes[c]["price"]
    fx = quotes[c]["fx"]
    mv = lot["quantity"] * q * fx
    upnl = (q - lot["cost"]) * lot["quantity"]  # 未实现盈亏（不补分红）
    stock_mv += mv
    pos_rows.append({
        "ts_code": c, "lot_id": lot["lot_id"], "quantity": lot["quantity"],
        "available": lot["available"], "cost": lot["cost"], "price": q,
        "mv": mv, "upnl": upnl, "buy_date": lot["buy_date"],
    })

net_cash = CASH_BAL - ACCRUED_FEES
calc_na = stock_mv + net_cash
na_diff = calc_na - REPORTED_NA

# 按 ts_code 汇总
pos_by_code = defaultdict(lambda: {"quantity":0,"available":0,"mv":0.0,"upnl":0.0})
for r in pos_rows:
    p = pos_by_code[r["ts_code"]]
    p["quantity"] += r["quantity"]
    p["available"] += r["available"]
    p["mv"] += r["mv"]
    p["upnl"] += r["upnl"]

# 权重（以复算净资产为分母）
for c in pos_by_code:
    pos_by_code[c]["weight"] = pos_by_code[c]["mv"]/calc_na
    pos_by_code[c]["adv20"] = adv20[c]

cash_weight = net_cash/calc_na
equity_weight = stock_mv/calc_na

# 前三大
sorted_codes = sorted(pos_by_code.keys(), key=lambda c: pos_by_code[c]["weight"], reverse=True)
top3_weight = sum(pos_by_code[c]["weight"] for c in sorted_codes[:3])

# ---------------------------------------------------------------------------
# 7. CVaR 计算：6只股票共同存在且状态有效的最近N个交易日
# ---------------------------------------------------------------------------
# 构建 date -> {ts_code: pct_chg/100}
all_dates = sorted(set(d["date"] for c in daily for d in daily[c]))
common = []
for dt in all_dates:
    row = {}
    ok = True
    for c in codes_in_lots:
        rec = next((d for d in daily[c] if d["date"]==dt), None)
        if rec is None or rec["status"]!="有行情" or rec["pct_chg"] is None:
            ok = False; break
        row[c] = rec["pct_chg"]/100.0
    if ok:
        row["_date"] = dt
        common.append(row)

N_common = len(common)
# 取最近 RISK_DAYS 个
common_window = common[-RISK_DAYS:]
N = len(common_window)
m = math.ceil((1-CONFIDENCE)*N)

def compute_cvar(weights, window=None):
    """weights: dict ts_code->weight (仅股票); 现金日收益=CASH_D_RET
    返回 (cvar95, tail_contrib_dict, N, m)"""
    if window is None:
        window = common_window
    rets = []
    for row in window:
        rp = sum(weights.get(c,0.0)*row[c] for c in weights)
        # 现金权重不参与（cash_daily_return=0），组合日收益=sum(w_i*r_i)
        rets.append(rp)
    losses = [-r for r in rets]
    losses_sorted = sorted(losses, reverse=True)
    tail = losses_sorted[:m]
    cvar = sum(tail)/len(tail)
    # 尾部贡献：在尾部日期上，每只股票平均损失贡献 = mean(-w_i*r_i)
    tail_losses_set = set(tail)  # 注意浮点；改用索引
    # 重新定位尾部日期索引
    indexed = sorted(enumerate(losses), key=lambda x: x[1], reverse=True)
    tail_idx = set(idx for idx,_ in indexed[:m])
    contrib = {}
    for c in weights:
        vals = [-weights[c]*window[i][c] for i in tail_idx]
        contrib[c] = sum(vals)/len(vals)
    contrib["_cvar"] = cvar
    contrib["_sum_contrib"] = sum(contrib[c] for c in weights)
    return cvar, contrib, len(window), m

# 当前权重（股票部分归一化到净资产权重，即直接用 mv/NA）
cur_weights = {c: pos_by_code[c]["weight"] for c in pos_by_code}
cur_cvar, cur_contrib, cur_N, cur_m = compute_cvar(cur_weights)

# ---------------------------------------------------------------------------
# 8. 交易函数
# ---------------------------------------------------------------------------
def check_quote_usable(c):
    """返回 (usable: bool, reason: str)"""
    q = quotes[c]
    if q["trading_status"] != "连续竞价":
        return False, f"交易状态={q['trading_status']}"
    if q["age"] > QUOTE_MAX_AGE:
        return False, f"报价陈旧 age={q['age']:.0f}s>{QUOTE_MAX_AGE:.0f}s"
    if not (q["limit_down"] < q["price"] <= q["limit_up"]):
        return False, f"价格不在涨跌停内 price={q['price']} limit_down={q['limit_down']} limit_up={q['limit_up']}"
    return True, "OK"

def allocate_sell(c, sell_qty):
    """按 buy_date升序, lot_id升序从可卖批次分配。返回 list of (lot, alloc_qty, cost)"""
    alloc = []
    remaining = sell_qty
    for lot in lots_by_code[c]:
        if remaining <= 0: break
        avail = lot["available"]
        if avail <= 0: continue
        take = min(avail, remaining)
        alloc.append((lot, take, lot["cost"]))
        remaining -= take
    if remaining > 0:
        return None, remaining  # 分配不足
    return alloc, 0

def calc_order_fees(exec_amt):
    commission = max(COMM_MIN, exec_amt*COMM_RATE)
    stamp = exec_amt*STAMP_RATE
    transfer = exec_amt*TRANSFER_RATE
    return commission, stamp, transfer

def evaluate_plan(plan_id, orders):
    """
    从原始账户快照独立测算。
    orders: 该 plan 的全部订单行。
    返回 dict:
      same_day_orders, next_day_orders, ineligible_orders,
      post weights/cvar, fee breakdown, allocation rows, checks
    """
    result = {"plan_id": plan_id, "orders": [], "ineligible": [],
              "same_day_executable": True, "blockers": []}

    # 逐订单校验
    same_day_sells = [o for o in orders if o["execution_window"]=="当日" and o["side"]=="卖出"]
    next_day_sells = [o for o in orders if o["execution_window"]=="下一交易日" and o["side"]=="卖出"]

    # 当日订单逐笔校验
    total_exec_amt = 0.0
    total_commission=total_stamp=total_transfer=0.0
    total_slippage_cost = 0.0
    total_realized = 0.0
    alloc_rows = []
    post_qty = {c: pos_by_code[c]["quantity"] for c in pos_by_code}
    # 当日累计卖出 per code
    cum_sold = defaultdict(int)

    for o in sorted(same_day_sells, key=lambda x: x["order_sequence"]):
        c = o["ts_code"]; qty = int(o["sell_quantity"])
        q = quotes[c]
        usable, reason = check_quote_usable(c)
        # 可卖数量校验
        max_avail = pos_by_code[c]["available"]
        # 整手校验
        lot = q["board_lot"]
        lot_ok = (qty % lot == 0)
        # 参与率
        notional = qty * q["price"]
        particip = notional/adv20[c]
        adv_ok = particip <= MAX_ADV_PART

        order_status = []
        if not usable:
            order_status.append(f"报价不可用({reason})")
        if qty > max_avail:
            order_status.append(f"可卖不足(申报{qty}>可卖{max_avail})")
        if not lot_ok:
            order_status.append(f"非整手({qty}非{lot}整数倍)")
        if not adv_ok:
            order_status.append(f"ADV20参与率超限({particip*100:.3f}%>{MAX_ADV_PART*100:.1f}%)")

        # 分配批次
        alloc, short = allocate_sell(c, qty)
        if alloc is None:
            order_status.append(f"可卖批次分配不足(差{short})")

        # 滑点与执行估价（即使报价陈旧也只用于名义参考？——题面：报价不可用时订单列为待条件恢复）
        slip_bps = min(SLIP_CAP, SLIP_BASE + SLIP_SQRT*math.sqrt(particip))
        exec_price = q["price"]*(1 - slip_bps/10000.0)

        row = {
            "seq": o["order_sequence"], "code": c, "qty": qty,
            "window": o["execution_window"], "notional": notional,
            "participation": particip, "slip_bps": slip_bps,
            "exec_price": exec_price, "status": order_status,
            "allocations": [],
        }

        if not order_status and alloc is not None:
            # 执行
            exec_amt = qty*exec_price
            comm, stamp, trans = calc_order_fees(exec_amt)
            fee_total = comm+stamp+trans
            # 滑点成本（相对名义）
            slip_cost = notional - exec_amt
            realized = 0.0
            for lot_obj, take, cost in alloc:
                lp = (exec_price - cost)*take - 0  # 费用按订单整体扣减
                realized += lp
            realized -= fee_total  # 实现盈亏=扣费后
            # 注意：费用扣减一次即可；上面 lp 未扣费用，这里统一减 fee_total
            # 重新算：realized = sum((exec_price-cost)*take) - fee_total
            realized = sum((exec_price-lot_obj["cost"])*take for lot_obj,take,_ in alloc) - fee_total

            row["exec_amt"]=exec_amt; row["commission"]=comm
            row["stamp"]=stamp; row["transfer"]=trans
            row["fee_total"]=fee_total; row["slip_cost"]=slip_cost
            row["net_proceeds"]=exec_amt-fee_total
            row["realized_pnl"]=realized
            for lot_obj, take, cost in alloc:
                row["allocations"].append({
                    "lot_id": lot_obj["lot_id"], "qty": take,
                    "exec_price": exec_price, "cost": cost,
                })
                alloc_rows.append({
                    "plan_id": plan_id, "order_sequence": o["order_sequence"],
                    "ts_code": c, "lot_id": lot_obj["lot_id"],
                    "allocated_quantity": take,
                    "estimated_execution_price": round(exec_price,4),
                    "estimated_fee": round(fee_total,6),  # 订单级费用，下面按比例分摊
                    "realized_pnl_cny": round(realized,2),
                    "eligibility_status": "当日可执行",
                })
            total_exec_amt += exec_amt
            total_commission += comm; total_stamp += stamp; total_transfer += trans
            total_slippage_cost += slip_cost
            total_realized += realized
            post_qty[c] -= qty
            cum_sold[c] += qty
        else:
            # 不可执行：记录，标记
            row["exec_amt"]=0; row["fee_total"]=0; row["net_proceeds"]=0; row["realized_pnl"]=0
            result["blockers"].append(f"订单{o['order_sequence']}({c} {qty}股): {'; '.join(order_status)}")
            for lot_obj, take, cost in (alloc or []):
                alloc_rows.append({
                    "plan_id": plan_id, "order_sequence": o["order_sequence"],
                    "ts_code": c, "lot_id": lot_obj["lot_id"],
                    "allocated_quantity": take,
                    "estimated_execution_price": "",
                    "estimated_fee": "",
                    "realized_pnl_cny": "",
                    "eligibility_status": "待条件恢复:" + ";".join(order_status),
                })

        result["orders"].append(row)

    # 下一交易日订单：仅记录，不计入当日合规
    for o in next_day_sells:
        result["orders"].append({
            "seq": o["order_sequence"], "code": o["ts_code"],
            "qty": int(o["sell_quantity"]), "window": "下一交易日",
            "status": ["下一交易日订单，不计入当日合规"],
            "allocations": [],
        })
        alloc_rows.append({
            "plan_id": plan_id, "order_sequence": o["order_sequence"],
            "ts_code": o["ts_code"], "lot_id": "",
            "allocated_quantity": int(o["sell_quantity"]),
            "estimated_execution_price": "", "estimated_fee": "",
            "realized_pnl_cny": "",
            "eligibility_status": "下一交易日条件动作（不计入当日合规）",
        })

    # ---- 交易后状态（仅当日可执行订单）----
    # 如果有不可执行订单，则交易后状态按"仅执行成功订单"估算并标注无法评估
    post_mv = sum(post_qty[c]*quotes[c]["price"]*quotes[c]["fx"] for c in post_qty)
    post_cash = net_cash + sum(o.get("net_proceeds",0) for o in result["orders"] if o["window"]=="当日")
    post_na = post_mv + post_cash
    # 校验：post_na 应 = REPORTED_NA - 总费用 - 总滑点
    na_should_be = REPORTED_NA - (total_commission+total_stamp+total_transfer) - total_slippage_cost

    post_weights = {}
    for c in post_qty:
        post_weights[c] = post_qty[c]*quotes[c]["price"]*quotes[c]["fx"]/post_na if post_na>0 else 0
    post_equity = post_mv/post_na
    post_cash_w = post_cash/post_na
    post_sorted = sorted(post_weights.keys(), key=lambda c: post_weights[c], reverse=True)
    post_top3 = sum(post_weights[c] for c in post_sorted[:3])
    post_max_single = max(post_weights.values())

    # CVaR（用交易后权重）
    if any(o["status"] for o in result["orders"] if o["window"]=="当日"):
        # 有不可执行订单
        post_cvar = None; post_contrib = None
    else:
        post_cvar, post_contrib, _, _ = compute_cvar(post_weights)

    result.update({
        "post_qty": post_qty, "post_mv": post_mv, "post_cash": post_cash,
        "post_na": post_na, "na_should_be": na_should_be,
        "post_weights": post_weights, "post_equity": post_equity,
        "post_cash_w": post_cash_w, "post_top3": post_top3,
        "post_max_single": post_max_single, "post_cvar": post_cvar,
        "post_contrib": post_contrib,
        "total_exec_amt": total_exec_amt, "total_commission": total_commission,
        "total_stamp": total_stamp, "total_transfer": total_transfer,
        "total_fee": total_commission+total_stamp+total_transfer,
        "total_slip_cost": total_slippage_cost,
        "total_cost_incl_slip": total_commission+total_stamp+total_transfer+total_slippage_cost,
        "total_realized": total_realized,
        "alloc_rows": alloc_rows,
    })
    return result

# ---------------------------------------------------------------------------
# 9. 逐方案
# ---------------------------------------------------------------------------
plans_by_id = defaultdict(list)
for r in plans_raw:
    plans_by_id[r["plan_id"]].append(r)

plan_results = {}
for pid in ["方案A","方案B","方案C"]:
    plan_results[pid] = evaluate_plan(pid, plans_by_id[pid])

# ---------------------------------------------------------------------------
# 10. 硬约束判定
# ---------------------------------------------------------------------------
def judge(value, threshold, mode="max"):
    if value is None: return "无法评估"
    if mode=="max":
        return "通过" if value <= threshold + 1e-9 else "不通过"
    else:
        return "通过" if value >= threshold - 1e-9 else "不通过"

def build_constraint_matrix(res):
    rows = []
    checks = [
        ("equity_exposure", res["post_equity"], EQ_MAX, "max", "股票仓位"),
        ("single_name_weight", res["post_max_single"], SINGLE_MAX, "max", "单票最大权重"),
        ("top3_weight", res["post_top3"], TOP3_MAX, "max", "前三大权重"),
        ("historical_cvar95", res["post_cvar"], CVAR_MAX, "max", "250日historical_CVaR95"),
        ("minimum_cash_weight", res["post_cash_w"], MIN_CASH_W, "min", "最低现金权重"),
    ]
    for name, val, thr, mode, cn in checks:
        status = judge(val, thr, mode)
        gap = ""
        if val is not None and status=="不通过":
            if mode=="max":
                gap = val - thr
            else:
                gap = thr - val
        rows.append({
            "plan_id": res["plan_id"], "metric_name": cn,
            "actual_value": round(val,6) if val is not None else "无法评估",
            "threshold": thr, "status": status,
            "gap": round(gap,6) if gap!="" else "",
            "calculation_basis": "交易后盯市×current_price_cny / 交易后净资产",
            "evidence_file": "持仓批次_20251231T1445.csv;持仓标的报价_20251231T1445.csv;账户与交易约束_20251231T1445.csv",
        })
    # 订单执行层面硬约束
    has_blocker = len(res["blockers"])>0
    rows.append({
        "plan_id": res["plan_id"], "metric_name": "当日订单可执行性(T+1/整手/报价时效/涨跌停/ADV20)",
        "actual_value": "存在不可执行订单" if has_blocker else "全部当日订单可执行",
        "threshold": "全部当日订单可执行",
        "status": "不通过" if has_blocker else "通过",
        "gap": "; ".join(res["blockers"]) if has_blocker else "",
        "calculation_basis": "逐订单校验available_to_sell/board_lot/trading_status/quote_age/limit/ADV20",
        "evidence_file": "候选减仓方案_20251231.csv;持仓标的报价_20251231T1445.csv;持仓批次_20251231T1445.csv",
    })
    # 净资产勾稽
    na_diff_post = res["post_na"] - res["na_should_be"]
    rows.append({
        "plan_id": res["plan_id"], "metric_name": "交易后净资产勾稽(=reported_NA-费用-滑点)",
        "actual_value": round(res["post_na"],2),
        "threshold": round(res["na_should_be"],2),
        "status": "通过" if abs(na_diff_post)<=1.0 else "不通过",
        "gap": round(na_diff_post,2),
        "calculation_basis": "post_mv+post_cash vs reported_NV-fees-slippage",
        "evidence_file": "账户与交易约束_20251231T1445.csv",
    })
    return rows

constraint_rows = []
for pid, res in plan_results.items():
    constraint_rows.extend(build_constraint_matrix(res))

# ---------------------------------------------------------------------------
# 11. 敏感性
# ---------------------------------------------------------------------------
# (a) 全部订单按 slippage_cap_bps 执行：重算费用/现金/NA/权重/CVaR
#     （权重变化极小，因为股价不变，仅现金侧变化；这里重算NA与权重）
# (b) CVaR 窗口缩短为最近200个共同有效日
def sensitivity_cap(res):
    """重新按 cap 滑点计算（对同一组可执行订单）"""
    # 找可执行订单
    cash_adj = net_cash
    mv_after = {c: pos_by_code[c]["quantity"] for c in pos_by_code}
    for o in res["orders"]:
        if o["window"]!="当日" or o["status"]: continue  # 不可执行跳过
        c=o["code"]; qty=o["qty"]; q=quotes[c]
        exec_price = q["price"]*(1 - SLIP_CAP/10000.0)
        exec_amt = qty*exec_price
        comm,stamp,trans = calc_order_fees(exec_amt)
        cash_adj += exec_amt - comm - stamp - trans
        mv_after[c] -= qty
    post_mv = sum(mv_after[c]*quotes[c]["price"] for c in mv_after)
    post_na = post_mv+cash_adj
    w = {c: mv_after[c]*quotes[c]["price"]/post_na for c in mv_after}
    cvar,contrib,_,_ = compute_cvar(w)
    return {"post_na":post_na,"post_cash_w":cash_adj/post_na,"post_equity":post_mv/post_na,
            "post_cvar":cvar,"post_top3":sum(sorted(w.values(),reverse=True)[:3]),
            "post_max_single":max(w.values())}

def sensitivity_window200(res):
    window = common_window[-200:]
    # 用交易后权重（主口径）
    w = res["post_weights"]
    cvar,contrib,_,_ = compute_cvar(w, window=window)
    return {"cvar200":cvar}

sens = {}
for pid, res in plan_results.items():
    if res["post_cvar"] is not None:
        sens[pid] = {"cap": sensitivity_cap(res), "w200": sensitivity_window200(res)}
    else:
        sens[pid] = {"cap": None, "w200": None}

# ---------------------------------------------------------------------------
# 12. 当前持仓风险快照 CSV
# ---------------------------------------------------------------------------
snapshot_rows = []
for c in sorted(pos_by_code.keys()):
    p = pos_by_code[c]
    breach = ""
    flags = []
    if p["weight"] > SINGLE_MAX: flags.append("单票超限")
    if c in cur_contrib:
        pass
    snapshot_rows.append({
        "ts_code": c,
        "security_name": quotes[c] and lots_by_code[c][0]["security_name"],
        "quantity": p["quantity"],
        "available_to_sell": p["available"],
        "market_value_cny": round(p["mv"],2),
        "weight": round(p["weight"],6),
        "unrealized_pnl_cny": round(p["upnl"],2),
        "adv20_cny": round(adv20[c],2),
        "current_cvar_contribution": round(cur_contrib.get(c,0.0),6),
        "breach_flag": ";".join(flags),
    })

# ---------------------------------------------------------------------------
# 13. 写出 CSV
# ---------------------------------------------------------------------------
def write_csv(path, rows, fieldnames):
    with open(path,"w",encoding="utf-8-sig",newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

# 当前持仓风险快照
write_csv(os.path.join(OUT_DIR,"当前持仓风险快照.csv"), snapshot_rows,
    ["ts_code","security_name","quantity","available_to_sell","market_value_cny",
     "weight","unrealized_pnl_cny","adv20_cny","current_cvar_contribution","breach_flag"])

# 候选方案约束矩阵
write_csv(os.path.join(OUT_DIR,"候选方案约束矩阵.csv"), constraint_rows,
    ["plan_id","metric_name","actual_value","threshold","status","gap",
     "calculation_basis","evidence_file"])

# 批次卖出分配
alloc_all = []
for pid in ["方案A","方案B","方案C"]:
    alloc_all.extend(plan_results[pid]["alloc_rows"])
write_csv(os.path.join(OUT_DIR,"批次卖出分配.csv"), alloc_all,
    ["plan_id","order_sequence","ts_code","lot_id","allocated_quantity",
     "estimated_execution_price","estimated_fee","realized_pnl_cny","eligibility_status"])

# 审批监控条件（后面填）
# 先占位，最后填

# ---------------------------------------------------------------------------
# 14. 打印关键结果到 stdout（供核对）
# ---------------------------------------------------------------------------
print("="*70)
print("账户勾稽")
print(f"  股票总市值: {stock_mv:,.2f}")
print(f"  净现金:     {net_cash:,.2f}")
print(f"  复算净资产: {calc_na:,.2f}")
print(f"  报表净资产: {REPORTED_NA:,.2f}")
print(f"  差额:       {na_diff:,.2f}")
print(f"  股票仓位:   {equity_weight*100:.3f}%  (阈值 {EQ_MAX*100:.0f}%)")
print(f"  现金权重:   {cash_weight*100:.3f}%  (阈值 >= {MIN_CASH_W*100:.0f}%)")
print(f"  最大单票:   {max(pos_by_code[c]['weight'] for c in pos_by_code)*100:.3f}% (阈值 {SINGLE_MAX*100:.0f}%)")
print(f"  前三大:     {top3_weight*100:.3f}% (阈值 {TOP3_MAX*100:.0f}%)")
print(f"  共同有效日: {N_common}, 采用窗口 N={N}, m={m}")
print(f"  当前CVaR95: {cur_cvar*100:.4f}%  (阈值 {CVAR_MAX*100:.1f}%)")
print(f"  CVaR尾部贡献之和: {cur_contrib['_sum_contrib']*100:.4f}% (应=CVaR)")
print()
print("当前持仓:")
for c in sorted(pos_by_code.keys(), key=lambda x:-pos_by_code[x]["weight"]):
    p=pos_by_code[c]
    print(f"  {c} {lots_by_code[c][0]['security_name']:6s} 量={p['quantity']:>7d} 可卖={p['available']:>7d} "
          f"市值={p['mv']:>14,.2f} 权重={p['weight']*100:6.3f}% 浮盈={p['upnl']:>14,.2f} "
          f"ADV20={p['adv20']/1e8:6.3f}亿 CVaR贡献={cur_contrib.get(c,0)*100:.4f}%")
print()

for pid in ["方案A","方案B","方案C"]:
    r = plan_results[pid]
    print("="*70)
    print(f"{pid}:")
    print(f"  当日执行金额: {r['total_exec_amt']:,.2f}  总费用(佣+印+过): {r['total_fee']:,.2f}  滑点成本: {r['total_slip_cost']:,.2f}")
    print(f"  交易后NA: {r['post_na']:,.2f}  应NA: {r['na_should_be']:,.2f}  差: {r['post_na']-r['na_should_be']:,.2f}")
    print(f"  交易后股票仓位: {r['post_equity']*100:.3f}%  现金权重: {r['post_cash_w']*100:.3f}%")
    print(f"  交易后最大单票: {r['post_max_single']*100:.3f}%  前三大: {r['post_top3']*100:.3f}%")
    print(f"  交易后CVaR95: {r['post_cvar']*100:.4f}%" if r['post_cvar'] is not None else "  交易后CVaR: 无法评估")
    print(f"  blockers: {r['blockers']}")
    for o in r["orders"]:
        st = ";".join(o["status"]) if o["status"] else "OK"
        print(f"    seq{o['seq']} {o['code']} {o['qty']}股 [{o['window']}] {st}")
    if sens[pid]["cap"]:
        sc = sens[pid]["cap"]
        print(f"  敏感性[cap滑点50bps]: 仓位={sc['post_equity']*100:.3f}% 现金={sc['post_cash_w']*100:.3f}% "
              f"单票={sc['post_max_single']*100:.3f}% top3={sc['post_top3']*100:.3f}% CVaR={sc['post_cvar']*100:.4f}%")
    if sens[pid]["w200"]:
        print(f"  敏感性[200日窗口]: CVaR95={sens[pid]['w200']['cvar200']*100:.4f}%")
    print()

# 暴露给后续写报告的对象
import json
summary = {
    "stock_mv": stock_mv, "net_cash": net_cash, "calc_na": calc_na,
    "reported_na": REPORTED_NA, "na_diff": na_diff,
    "equity_weight": equity_weight, "cash_weight": cash_weight,
    "top3_weight": top3_weight, "cur_cvar": cur_cvar,
    "N_common": N_common, "N": N, "m": m,
    "pos_by_code": {c: dict(pos_by_code[c]) for c in pos_by_code},
    "cur_contrib": cur_contrib,
}
with open(os.path.join(OUT_DIR,"_summary.json"),"w",encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

print("DONE")
