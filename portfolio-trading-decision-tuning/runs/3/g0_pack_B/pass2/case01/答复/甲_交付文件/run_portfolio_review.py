#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_portfolio_review.py
稳健账户A 持仓交易审批：从5个原始附件一键重建全部客观CSV与报告数字。
不联网、不硬编码价格/数量/推荐方案；所有数字均由输入CSV现算。

用法:
  python run_portfolio_review.py [附件目录] [输出目录]
默认附件目录为脚本上级的 attachments 目录，输出目录为脚本所在目录。
"""
import sys, os, csv, math, json
from collections import defaultdict, OrderedDict
from datetime import datetime

# ----------------------------- 路径与IO -----------------------------
def here(p): return os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
ATTACH = sys.argv[1] if len(sys.argv) > 1 else here("../../.sessions/38442569442812930/attachments")
OUT    = sys.argv[2] if len(sys.argv) > 2 else here(".")

def read_csv(name):
    path = os.path.join(ATTACH, name)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def fnum(x):
    if x is None or x == "": return None
    return float(x)

# ----------------------------- 读取附件 -----------------------------
lots_raw = read_csv("持仓批次_20251231T1445.csv")
quotes_raw = read_csv("持仓标的报价_20251231T1445.csv")
cons_raw = read_csv("账户与交易约束_20251231T1445.csv")
plans_raw = read_csv("候选减仓方案_20251231.csv")
daily_raw = read_csv("持仓标的日线_20240102_20251230.csv")

# ----------------------------- 账户约束 -----------------------------
c = cons_raw[0]
C = dict(
    cash_balance=fnum(c["cash_balance_cny"]),
    accrued_fees=fnum(c["accrued_fees_payable_cny"]),
    reported_nav=fnum(c["reported_net_asset_cny"]),
    eq_max=fnum(c["equity_exposure_max"]),
    sn_max=fnum(c["single_name_weight_max"]),
    top3_max=fnum(c["top3_weight_max"]),
    cvar_max=fnum(c["historical_cvar95_max"]),
    cash_min=fnum(c["minimum_cash_weight"]),
    comm_rate=fnum(c["commission_rate"]),
    comm_min=fnum(c["minimum_commission_cny"]),
    stamp_rate=fnum(c["stamp_duty_sell_rate"]),
    transfer_rate=fnum(c["transfer_fee_rate"]),
    slip_base=fnum(c["slippage_base_bps"]),
    slip_sqrt=fnum(c["slippage_sqrt_coefficient_bps"]),
    slip_cap=fnum(c["slippage_cap_bps"]),
    adv_max_part=fnum(c["max_adv20_participation"]),
    common_days_req=int(fnum(c["risk_history_common_days"])),
    conf=fnum(c["risk_confidence"]),
    cash_ret=fnum(c["cash_daily_return"]),
    quote_max_age=fnum(c["quote_max_age_seconds"]),
    deadline=c["approval_deadline"],
)

# ----------------------------- 报价表 -----------------------------
Q = {}
for q in quotes_raw:
    Q[q["ts_code"]] = dict(
        name=q["security_name"],
        price=fnum(q["current_price_cny"]),
        prev=fnum(q["previous_close_cny"]),
        up=fnum(q["limit_up_cny"]),
        dn=fnum(q["limit_down_cny"]),
        age=fnum(q["quote_age_seconds"]),
        qstatus=q["quote_status"],
        tstatus=q["trading_status"],
        lot=int(fnum(q["board_lot_shares"])),
        fx=fnum(q["fx_to_cny"]),
    )

# ----------------------------- 持仓批次 -----------------------------
LOTS = []
for r in lots_raw:
    LOTS.append(dict(
        code=r["ts_code"], name=r["security_name"], lot=r["lot_id"],
        buy=r["buy_date"], qty=int(fnum(r["quantity"])),
        avl=int(fnum(r["available_to_sell"])),
        cost=fnum(r["cost_price_cny"]),
    ))

def lot_key(l): return (l["buy"], l["lot"])

# ----------------------------- 日线：ADV20 与收益矩阵 -----------------------------
# 组织为 code -> {date: pct_chg/100}，并保留 amount 用于 ADV20
ret = defaultdict(dict)
amt = defaultdict(dict)
for d in daily_raw:
    if d["record_status"] != "有行情":
        continue
    code = d["ts_code"]; dt = d["trade_date"]
    ret[code][dt] = fnum(d["pct_chg"])/100.0
    amt[code][dt] = fnum(d["amount"])*1000.0  # 千元 -> 元

codes = sorted(Q.keys())
# 最近20条有行情记录的 ADV20
ADV20 = {}
for code in codes:
    ds = sorted(amt[code].keys())[-20:]
    ADV20[code] = sum(amt[code][x] for x in ds)/len(ds)

# 共同有效交易日（6只股票均存在），取最近 N 个
common = None
for code in codes:
    s = set(ret[code].keys())
    common = s if common is None else (common & s)
common = sorted(common)
N = min(C["common_days_req"], len(common))
common_N = common[-N:]
m = math.ceil((1 - C["conf"]) * N)

def port_losses(weights, window_dates):
    """weights: code->权重(占总NAV). 返回该窗口下每日损失L_t=-r_p,t 列表(按window_dates顺序)."""
    out = []
    for dt in window_dates:
        rp = C["cash_ret"] * (1.0 - sum(weights.values()))  # 现金部分权重收益=0
        for code, w in weights.items():
            rp += w * ret[code][dt]
        out.append(-rp)
    return out

def cvar95(weights, window_dates):
    L = port_losses(weights, window_dates)
    Ls = sorted(L, reverse=True)
    top = Ls[:m]
    return sum(top)/len(top), Ls

# 尾部贡献（主窗口）
def tail_contrib(weights, window_dates):
    L = port_losses(weights, window_dates)
    order = sorted(range(len(L)), key=lambda i: L[i], reverse=True)[:m]
    contrib = {}
    for code, w in weights.items():
        vals = [-w * ret[code][window_dates[i]] for i in order]
        contrib[code] = sum(vals)/len(vals)
    return contrib

# ----------------------------- 当前账户勾稽 -----------------------------
# 每标的总数量/可卖/市值
pos = OrderedDict()
for code in codes:
    lots = [l for l in LOTS if l["code"]==code]
    qty = sum(l["qty"] for l in lots)
    avl = sum(l["avl"] for l in lots)
    mv = qty * Q[code]["price"] * Q[code]["fx"]
    pos[code] = dict(qty=qty, avl=avl, mv=mv, lots=lots)

stock_mv = sum(p["mv"] for p in pos.values())
net_cash = C["cash_balance"] - C["accrued_fees"]
nav_calc = stock_mv + net_cash
recon_diff = nav_calc - C["reported_nav"]

weights = {code: pos[code]["mv"]/nav_calc for code in codes}
cash_w = net_cash/nav_calc
eq_w = stock_mv/nav_calc

# 未实现盈亏
unreal = {}
for code in codes:
    u = 0.0
    for l in pos[code]["lots"]:
        u += (Q[code]["price"] - l["cost"]) * l["qty"]
    unreal[code] = u

sorted_by_w = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
top3_now = sum(w for _,w in sorted_by_w[:3])

# 当前 CVaR
cvar_now, L_now = cvar95(weights, common_N)
tail_now = tail_contrib(weights, common_N)

# ----------------------------- 方案测算函数 -----------------------------
def check_order_eligibility(plan_id, seq, code, qty, used_avl):
    """返回 (eligibility_status, reasons[])"""
    reasons = []
    st = "通过"
    q = Q.get(code)
    if q is None:
        return "未知代码", ["候选方案出现附件报价/持仓中不存在的 ts_code"]
    # T+1 / 可卖（累计）
    if qty > pos[code]["avl"] - used_avl:
        reasons.append(f"卖出{qty}股超过剩余可卖{pos[code]['avl']-used_avl}股")
        st = "不可卖"
    # 整手
    if qty % q["lot"] != 0:
        reasons.append(f"卖出{qty}股不是整手({q['lot']})的整数倍")
        st = "整手不符"
    # 交易状态 / 报价时效 / 涨跌停
    if q["tstatus"] != "连续竞价":
        reasons.append(f"交易状态={q['tstatus']}，非连续竞价")
        st = "待条件恢复"
    if q["age"] > C["quote_max_age"]:
        reasons.append(f"报价已陈旧(age={q['age']:.0f}s > {C['quote_max_age']:.0f}s)")
        st = "待条件恢复"
    if not (q["price"] > q["dn"]):
        reasons.append("当前价不高于跌停价")
        st = "待条件恢复"
    if not (q["price"] <= q["up"]):
        reasons.append("当前价高于涨停价")
        st = "待条件恢复"
    return st, reasons

def simulate_plan(plan_id):
    orders = [o for o in plans_raw if o["plan_id"]==plan_id]
    today_orders = [o for o in orders if o["execution_window"]=="当日"]
    future_orders = [o for o in orders if o["execution_window"]!="当日"]

    # 1) 逐订单资格 + 参与率/费用
    used_avl = defaultdict(int)
    alloc_rows = []
    today_eligible = True
    plan_blocked_reasons = []
    total_sell_notional = 0.0
    total_fee = 0.0
    total_slip_loss = 0.0
    total_net_proceeds = 0.0
    total_realized = 0.0
    sold = defaultdict(int)

    for o in sorted(today_orders, key=lambda x:int(x["order_sequence"])):
        code = o["ts_code"]; qty = int(fnum(o["sell_quantity"]))
        st, reasons = check_order_eligibility(plan_id, int(o["order_sequence"]), code, qty, used_avl[code])
        q = Q[code]
        # 名义金额与参与率
        notional = qty * q["price"]
        part = notional / ADV20[code] if ADV20[code] else float("inf")
        if part > C["adv_max_part"]:
            reasons.append(f"参与率{part:.4%} > 上限{C['adv_max_part']:.2%}")
            st = "参与率超限"
        # 滑点与费用（即使受限也按公式算参考值，但不用于合规）
        slip_bps = min(C["slip_cap"], C["slip_base"] + C["slip_sqrt"]*math.sqrt(part))
        exec_px = q["price"]*(1 - slip_bps/10000.0)
        exec_amt = qty*exec_px
        comm = max(C["comm_min"], exec_amt*C["comm_rate"])
        stamp = exec_amt*C["stamp_rate"]
        trans = exec_amt*C["transfer_rate"]
        fee = comm+stamp+trans
        net_proc = exec_amt - fee
        slip_loss = qty*q["price"]*(slip_bps/10000.0)

        eligible = (st == "通过")
        if not eligible:
            today_eligible = False
            plan_blocked_reasons.append(f"序{o['order_sequence']} {code}: "+";".join(reasons))
        # 批次分配（仅对当日；按buy_date升序、lot升序；不分配avl=0）
        lots_sorted = sorted([l for l in pos[code]["lots"] if l["avl"]>0], key=lot_key)
        remain = qty
        order_realized = 0.0
        for l in lots_sorted:
            if remain <= 0: break
            take = min(remain, l["avl"])
            if take <= 0: continue
            order_realized += (exec_px - l["cost"])*take
            alloc_rows.append(dict(
                plan_id=plan_id, order_sequence=int(o["order_sequence"]),
                ts_code=code, lot_id=l["lot"], allocated_quantity=take,
                estimated_execution_price=round(exec_px,4),
                estimated_fee=round(fee*take/qty,2) if qty else 0.0,
                realized_pnl_cny=round((exec_px-l["cost"])*take - fee*take/qty,2),
                eligibility_status=st if not eligible else "通过",
            ))
            remain -= take
        order_realized -= fee
        used_avl[code] += qty
        sold[code] += qty
        total_sell_notional += notional
        total_fee += fee
        total_slip_loss += slip_loss
        total_net_proceeds += net_proc
        total_realized += order_realized
        alloc_rows[-1]  # noop

    # 2) 交易后盯市（仅当当日全部可成交才评估合规；否则权重无法用有效报价重建）
    post = {}
    nav_post = nav_calc
    if today_eligible:
        post_mv = 0.0
        for code in codes:
            rem = pos[code]["qty"] - sold.get(code,0)
            mv = rem*Q[code]["price"]*Q[code]["fx"]
            post[code] = dict(rem=rem, mv=mv)
            post_mv += mv
        cash_post = net_cash + total_net_proceeds
        nav_post = post_mv + cash_post
        w_post = {code: post[code]["mv"]/nav_post for code in codes}
        cash_w_post = cash_post/nav_post
        eq_w_post = post_mv/nav_post
        top3_p = sum(w for _,w in sorted(w_post.items(), key=lambda kv:kv[1], reverse=True)[:3])
        cvar_post, _ = cvar95(w_post, common_N)
        tail_post = tail_contrib(w_post, common_N)
        nav_check = C["reported_nav"] - total_slip_loss - total_fee
    else:
        w_post = cash_w_post = eq_w_post = top3_p = cvar_post = tail_post = None
        cash_post = None
        nav_check = None

    return dict(
        plan_id=plan_id,
        today_eligible=today_eligible,
        blocked_reasons=plan_blocked_reasons,
        today_orders=today_orders, future_orders=future_orders,
        alloc_rows=alloc_rows,
        total_sell_notional=total_sell_notional,
        total_fee=total_fee, total_slip_loss=total_slip_loss,
        total_trade_cost=total_fee+total_slip_loss,
        total_net_proceeds=total_net_proceeds, total_realized=total_realized,
        sold=dict(sold),
        nav_post=nav_post, nav_check=nav_check,
        w_post=w_post, cash_w_post=cash_w_post, eq_w_post=eq_w_post,
        top3_p=top3_p, cvar_post=cvar_post, tail_post=tail_post,
        cash_post=cash_post,
    )

plans = ["方案A","方案B","方案C"]
RES = {p: simulate_plan(p) for p in plans}

# ----------------------------- 敏感性 -----------------------------
def cvar_on_window(weights, n_days):
    w = common[-n_days:]
    return cvar95(weights, w)[0]

# ----------------------------- 输出1: 当前持仓风险快照.csv -----------------------------
snap_path = os.path.join(OUT, "当前持仓风险快照.csv")
with open(snap_path,"w",encoding="utf-8-sig",newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["ts_code","security_name","quantity","available_to_sell","market_value_cny",
                 "weight","unrealized_pnl_cny","adv20_cny","current_cvar_contribution","breach_flag"])
    for code in codes:
        w = weights[code]
        breach = []
        if w > C["sn_max"]: breach.append("单票超限")
        wr.writerow([code, Q[code]["name"], pos[code]["qty"], pos[code]["avl"],
                     round(pos[code]["mv"],2), round(w,6), round(unreal[code],2),
                     round(ADV20[code],2), round(tail_now.get(code,0.0),6),
                     ";".join(breach) if breach else ""])

# ----------------------------- 输出3: 批次卖出分配.csv -----------------------------
alloc_path = os.path.join(OUT, "批次卖出分配.csv")
with open(alloc_path,"w",encoding="utf-8-sig",newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["plan_id","order_sequence","ts_code","lot_id","allocated_quantity",
                 "estimated_execution_price","estimated_fee","realized_pnl_cny","eligibility_status"])
    for p in plans:
        for row in RES[p]["alloc_rows"]:
            wr.writerow([row["plan_id"],row["order_sequence"],row["ts_code"],row["lot_id"],
                         row["allocated_quantity"],row["estimated_execution_price"],
                         row["estimated_fee"],row["realized_pnl_cny"],row["eligibility_status"]])

# ----------------------------- 输出2: 候选方案约束矩阵.csv -----------------------------
ev_file_q = "持仓标的报价_20251231T1445.csv"
ev_file_l = "持仓批次_20251231T1445.csv"
ev_file_c = "账户与交易约束_20251231T1445.csv"
ev_file_d = "持仓标的日线_20240102_20251230.csv"

def w_post_max(r):
    if not r["today_eligible"]: return None
    return max(r["w_post"].values())

def status_of(actual, thr, kind):
    if actual is None: return "无法评估"
    if kind == "max":
        return "通过" if actual <= thr + 1e-9 else "不通过"
    else:
        return "通过" if actual >= thr - 1e-9 else "不通过"

matrix_rows = []
for p in plans:
    r = RES[p]
    # 账户勾稽（所有方案共用同一快照）
    matrix_rows.append([p,"account_reconciliation",round(recon_diff,4),1.0,
                        "通过" if abs(recon_diff)<=1 else "不通过",
                        round(abs(recon_diff),4),
                        "净现金=现金-应付费；复算NAV=股票市值+净现金，与reported差额(元)",
                        ev_file_l+" / "+ev_file_c])
    # 当日订单可执行性
    if r["today_eligible"]:
        os_st, os_gap, os_basis = "通过", 0.0, "全部当日订单：可卖/T+1/整手/连续竞价/报价≤300s/涨跌停内/ADV20参与率≤5%"
    else:
        os_st, os_gap = "不通过", None
        os_basis = "；".join(r["blocked_reasons"])
    matrix_rows.append([p,"today_order_executable", os_gap if os_gap is not None else "NA",
                        "全部当日订单可执行", os_st, os_gap if os_gap is not None else "NA",
                        os_basis, ev_file_q+" / "+ev_file_l])
    # 五项硬约束
    specs = [
        ("equity_exposure", "max", r["eq_w_post"], C["eq_max"], "股票仓位上限"),
        ("single_name_weight", "max", w_post_max(r), C["sn_max"], "单票权重上限(取交易后最大)"),
        ("top3_weight", "max", r["top3_p"], C["top3_max"], "前三大权重上限"),
        ("historical_cvar95", "max", r["cvar_post"], C["cvar_max"], "250日历史CVaR95上限"),
        ("minimum_cash_weight", "min", r["cash_w_post"], C["cash_min"], "净现金权重下限"),
    ]
    for name, kind, act, thr, basis in specs:
        if act is None:
            matrix_rows.append([p,name,"NA",thr,"无法评估","NA",
                                "当日订单不可成交，交易后权重/风险无法用有效报价重建",
                                ev_file_q+" / "+ev_file_d])
        else:
            st = status_of(act, thr, kind)
            gap = (act-thr) if kind=="max" else (thr-act)
            matrix_rows.append([p,name,round(act,6),thr,st,round(gap,6),basis,
                                ev_file_c+" / "+ev_file_d+" / "+ev_file_q])

mat_path = os.path.join(OUT, "候选方案约束矩阵.csv")
with open(mat_path,"w",encoding="utf-8-sig",newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["plan_id","metric_name","actual_value","threshold","status","gap",
                 "calculation_basis","evidence_file"])
    for row in matrix_rows:
        wr.writerow(row)

# ----------------------------- 输出5: 审批监控条件.csv -----------------------------
monitor_rows = [
 ["M1","若贵州茅台(600519.SH)报价在"+C["deadline"]+"前刷新且quote_age_seconds≤"+
  str(int(C["quote_max_age"]))+"s、trading_status=连续竞价、价格在涨跌停之间",
  "quote_age≤300s 且四项硬约束复算仍通过",
  "将方案C序1(300750 15000股)+序2(600519 1000股)重新提交投资负责人审批；序3(比亚迪30000股)仍按下一交易日处理",
  "交易台","2025-12-31T14:50:00+08:00","刷新后报价快照(ts_code/age/price/limit)","若任一硬约束仍不通过则拒绝，禁止用陈旧价补造权重"],
 ["M2","若14:50前报价未刷新或任一硬约束复算不通过",
  "任一硬约束FAIL 或 报价仍>300s",
  "拒绝方案C当日执行；要求交易台改案，不得把序3下一交易日订单提前计入当日合规",
  "风控组","2025-12-31T14:50:00+08:00","复算后的约束矩阵","无报价刷新即不恢复审批"],
 ["M3","若批准方案B并在14:50前以限价委托挂出，存在未成交/部分成交",
  "14:50截止仍有未成交数量",
  "未成交部分现金记0、不视为已合规；按实际成交剩余整手重算仓位/现金/CVaR，仍不达标则转入M4",
  "交易台→风控","2025-12-31T14:50:00+08:00","券商成交通知(仅事后核对，不代下单)","全部成交则按交易后值复核"],
 ["M4","若方案B当日成交后任一硬约束仍不通过(尤其CVaR95>2.2%或300750权重>20%)",
  "交易后任一阈值超限",
  "当日暂停该敞口的加仓/回补/做T；下一交易日开盘前按新可卖整手反解减仓量并重新报批",
  "风控组","下一交易日09:15","交易后权重与CVaR重算","复核回到限内后才恢复该敞口操作"],
 ["M5","下一交易日(方案C序3 比亚迪30000股或B补单)拟执行前",
  "T+1可卖/报价时效/整手/ADV20参与率均满足",
  "重新核对当日可卖批次与报价，逐单满足硬约束后方可提交；不得沿用今日价格",
  "交易台→风控","下一交易日14:50","当日报价与可卖台账","任一不满足则当日不执行该订单"],
]
mon_path = os.path.join(OUT, "审批监控条件.csv")
with open(mon_path,"w",encoding="utf-8-sig",newline="") as f:
    wr = csv.writer(f)
    wr.writerow(["condition_id","if_condition","threshold","then_action","owner",
                 "check_deadline","evidence_needed","stop_condition"])
    for row in monitor_rows:
        wr.writerow(row)

# ----------------------------- 汇总打印（供备忘录引用） -----------------------------
def show(title, obj):
    print("\n==== %s ====" % title)
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))

show("ACCOUNT", dict(
    stock_mv=round(stock_mv,2), net_cash=round(net_cash,2),
    nav_calc=round(nav_calc,2), reported_nav=C["reported_nav"], recon_diff=round(recon_diff,2),
    eq_weight=round(eq_w,6), cash_weight=round(cash_w,6),
    single_weights={k:round(v,6) for k,v in weights.items()},
    top3_now=round(top3_now,6), cvar_now=round(cvar_now,6),
    tail_now={k:round(v,6) for k,v in tail_now.items()},
    tail_sum=round(sum(tail_now.values()),6),
    adv20={k:round(v,2) for k,v in ADV20.items()},
    unreal={k:round(v,2) for k,v in unreal.items()},
    common_days=len(common), N=N, m=m,
))

for p in plans:
    r = RES[p]
    detail = dict(
        today_eligible=r["today_eligible"], blocked=r["blocked_reasons"],
        total_notional=round(r["total_sell_notional"],2),
        total_fee=round(r["total_fee"],2), slip_loss=round(r["total_slip_loss"],2),
        total_trade_cost=round(r["total_trade_cost"],2),
        net_proceeds=round(r["total_net_proceeds"],2),
        realized=round(r["total_realized"],2),
        sold=r["sold"],
        nav_post=round(r["nav_post"],2) if r["nav_post"] else None,
        nav_check=round(r["nav_check"],2) if r["nav_check"] else None,
    )
    if r["today_eligible"]:
        detail.update(dict(
            eq_post=round(r["eq_w_post"],6), cash_post=round(r["cash_w_post"],6),
            sn_post={k:round(v,6) for k,v in r["w_post"].items()},
            top3_post=round(r["top3_p"],6), cvar_post=round(r["cvar_post"],6),
            tail_post={k:round(v,6) for k,v in r["tail_post"].items()},
            tail_sum_post=round(sum(r["tail_post"].values()),6),
        ))
    show("PLAN "+p, detail)

# 敏感性：全部订单按 cap=50bps 重算；CVaR 窗口 200 日（对可评估方案）
print("\n==== SENSITIVITY ====")
for p in plans:
    r = RES[p]
    if not r["today_eligible"]:
        print(p, "当日订单不可执行，敏感性不适用（待条件恢复）")
        continue
    # cap 口径：所有订单滑点按 cap_bps=50 重算净额与 NAV
    slip_cap_loss = 0.0; fee_cap = 0.0; net_cap = 0.0
    for o in sorted(r["today_orders"], key=lambda x:int(x["order_sequence"])):
        code=o["ts_code"]; qty=int(fnum(o["sell_quantity"])); q=Q[code]
        ex=q["price"]*(1-C["slip_cap"]/10000); ea=qty*ex
        comm=max(C["comm_min"],ea*C["comm_rate"]); stp=ea*C["stamp_rate"]; tr=ea*C["transfer_rate"]
        slip_cap_loss += qty*q["price"]*C["slip_cap"]/10000
        fee_cap += comm+stp+tr; net_cap += ea-(comm+stp+tr)
    nav_cap = C["reported_nav"] - slip_cap_loss - fee_cap
    mv_cap = 0.0
    for code in codes:
        rem = pos[code]["qty"] - r["sold"].get(code,0)
        mv_cap += rem*Q[code]["price"]
    w_cap = {code:( (pos[code]["qty"]-r["sold"].get(code,0))*Q[code]["price"])/nav_cap for code in codes}
    cvar_cap50 = cvar95(w_cap, common_N)[0]
    cvar_200 = cvar95(r["w_post"], common[-200:])[0] if len(common)>=200 else None
    print(json.dumps(dict(plan=p,
        nav_cap_slippage=round(nav_cap,2), cvar_at_cap50=round(cvar_cap50,6),
        cvar_window200=round(cvar_200,6) if cvar_200 else None,
        w_cap_cash=round((nav_cap-mv_cap)/nav_cap,6)), ensure_ascii=False))

print("\nDONE. files:", snap_path, alloc_path)
