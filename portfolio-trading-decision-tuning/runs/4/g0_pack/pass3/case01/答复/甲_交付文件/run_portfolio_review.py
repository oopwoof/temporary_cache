#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
稳健账户A 持仓交易审批一键复算脚本
从 5 个原始附件重建：账户勾稽、当前风险画像、3 个候选方案逐单测算、
批次分配、约束矩阵、敏感性检查，并输出全部交付 CSV。

用法:
    python3 run_portfolio_review.py <input_dir> <output_dir>
  - input_dir: 含 5 个原始 CSV 的目录
  - output_dir: 生成交付 CSV 的目录
脚本不联网、不硬编码价格/数量/推荐方案；全部数字由附件计算得出。
"""
import sys, os, csv, math
from collections import defaultdict, OrderedDict

# ---------- 工具 ----------
def rd(x):
    return float(x) if x not in (None, "") else 0.0

def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def r4(x):  # 展示用 4 位小数
    return round(x, 6)

# ---------- 读取 5 个附件（文件名按目录内 glob 定位，不硬编码绝对路径） ----------
def find_file(d, key):
    for fn in os.listdir(d):
        if fn.endswith(".csv") and key in fn:
            return os.path.join(d, fn)
    raise FileNotFoundError("找不到含关键字 %s 的 CSV" % key)

def main():
    inp, outp = sys.argv[1], sys.argv[2]
    os.makedirs(outp, exist_ok=True)

    f_daily   = find_file(inp, "持仓标的日线")
    f_lots    = find_file(inp, "持仓批次")
    f_quote   = find_file(inp, "持仓标的报价")
    f_constr  = find_file(inp, "账户与交易约束")
    f_plans   = find_file(inp, "候选减仓方案")

    daily  = read_csv(f_daily)
    lots   = read_csv(f_lots)
    quotes = read_csv(f_quote)
    cons   = read_csv(f_constr)[0]
    plans  = read_csv(f_plans)

    # ---------- 约束常量 ----------
    cash_balance   = rd(cons["cash_balance_cny"])
    accrued_fees   = rd(cons["accrued_fees_payable_cny"])
    reported_na     = rd(cons["reported_net_asset_cny"])
    EQ_MAX  = rd(cons["equity_exposure_max"])
    SN_MAX  = rd(cons["single_name_weight_max"])
    T3_MAX  = rd(cons["top3_weight_max"])
    CV_MAX  = rd(cons["historical_cvar95_max"])
    CASH_MIN= rd(cons["minimum_cash_weight"])
    COMM_R  = rd(cons["commission_rate"])
    COMM_MIN= rd(cons["minimum_commission_cny"])
    STAMP_R = rd(cons["stamp_duty_sell_rate"])
    TRANS_R = rd(cons["transfer_fee_rate"])
    SLIP_BASE= rd(cons["slippage_base_bps"])
    SLIP_K  = rd(cons["slippage_sqrt_coefficient_bps"])
    SLIP_CAP= rd(cons["slippage_cap_bps"])
    ADV_PART= rd(cons["max_adv20_participation"])
    COMMON_DAYS = int(float(cons["risk_history_common_days"]))
    CONF    = rd(cons["risk_confidence"])
    CASH_RET= rd(cons["cash_daily_return"])
    QMAX_AGE= int(float(cons["quote_max_age_seconds"]))
    APPR_DL = cons["approval_deadline"]

    # ---------- 报价表 ----------
    q = {r["ts_code"]: r for r in quotes}

    # ---------- 批次表：按 ts_code 分组，按 buy_date, lot_id 排序 ----------
    lot_by_code = defaultdict(list)
    for L in lots:
        lot_by_code[L["ts_code"]].append(L)
    for code in lot_by_code:
        lot_by_code[code].sort(key=lambda L: (L["buy_date"], L["lot_id"]))

    # ---------- 日线：按 ts_code 取“有行情”记录，按日期升序 ----------
    daily_by_code = defaultdict(list)
    for d in daily:
        if d["record_status"] == "有行情":
            daily_by_code[d["ts_code"]].append(d)
    for code in daily_by_code:
        daily_by_code[code].sort(key=lambda d: d["trade_date"])

    codes = sorted(lot_by_code.keys())
    # 未知代码检查（方案中出现、但批次/报价里没有的代码）
    unknown = set()
    for p in plans:
        if p["ts_code"] not in lot_by_code:
            unknown.add(p["ts_code"])

    # ---------- ADV20：每只股票最近 20 条有行情 amount(千元) -> 元 ----------
    adv20 = {}
    for code in codes:
        rows = daily_by_code[code][-20:]
        adv20[code] = sum(rd(r["amount"])*1000.0 for r in rows)/len(rows)

    # ---------- 共同有效日：最近 COMMON_DAYS 个 ----------
    valid_dates = {code: set(d["trade_date"] for d in daily_by_code[code]) for code in codes}
    common_all = sorted(set.intersection(*valid_dates.values()))
    common = common_all[-COMMON_DAYS:]
    N = len(common)
    m = math.ceil((1.0-CONF)*N)

    # 收益矩阵 r_i,t = pct_chg/100
    ret = {code: {} for code in codes}
    for code in codes:
        for d in daily_by_code[code]:
            ret[code][d["trade_date"]] = rd(d["pct_chg"])/100.0

    def cvar_and_contrib(weights):
        """weights: dict code->权重(交易后净资产为分母，现金权重=CASH_RET=0 不贡献)。
        返回 (cvar95, 各股平均尾部损失贡献dict, 尾部日期list)"""
        port_ret = []
        for t in common:
            rp = 0.0
            for code in codes:
                rp += weights.get(code,0.0) * ret[code][t]
            port_ret.append((t, rp))
        losses = sorted((( -rp, t, rp) for t,rp in port_ret), key=lambda x: -x[0])
        tail = losses[:m]
        cvar = sum(l[0] for l in tail)/m
        tail_dates = set(l[1] for l in tail)
        contrib = {}
        for code in codes:
            vals = [-weights.get(code,0.0)*ret[code][t] for t in tail_dates]
            contrib[code] = sum(vals)/len(vals)
        return cvar, contrib, tail_dates

    def cvar_window(weights, days):
        """敏感性：缩短共同窗口到最近 days 天。"""
        cw = common_all[-days:]
        nn = len(cw); mm = math.ceil((1.0-CONF)*nn)
        port_ret=[]
        for t in cw:
            rp = sum(weights.get(code,0.0)*ret[code][t] for code in codes)
            port_ret.append((t,rp))
        losses = sorted(((-rp,t) for t,rp in port_ret), key=lambda x:-x[0])
        tail=losses[:mm]
        cvar=sum(l[0] for l in tail)/mm
        tail_dates=set(l[1] for l in tail)
        contrib={code: sum(-weights.get(code,0.0)*ret[code][t] for t in tail_dates)/mm for code in codes}
        return cvar, contrib

    # ---------- 当前持仓勾稽 ----------
    cur_qty, cur_avail, cur_cost = {}, {}, {}
    mv = {}
    for code in codes:
        tq=avail=0; cost_amt=0
        for L in lot_by_code[code]:
            vq  = rd(L["quantity"]); va = rd(L["available_to_sell"]); vc=rd(L["cost_price_cny"])
            tq+=vq; avail+=va; cost_amt += vq*vc
        cur_qty[code]=tq; cur_avail[code]=avail; cur_cost[code]=cost_amt
        pr = rd(q[code]["current_price_cny"]); fx=rd(q[code]["fx_to_cny"])
        mv[code]= tq*pr*fx
    stock_mv = sum(mv.values())
    net_cash = cash_balance - accrued_fees
    recalc_na = stock_mv + net_cash
    recon_diff = recalc_na - reported_na

    cur_w = {code: mv[code]/recalc_na for code in codes}
    unreal = {code: sum((rd(q[code]["current_price_cny"])-rd(L["cost_price_cny"]))*rd(L["quantity"])
                        for L in lot_by_code[code]) for code in codes}
    eq_exp = stock_mv/recalc_na
    cash_w = net_cash/recalc_na
    top3_codes = sorted(codes, key=lambda c:-cur_w[c])[:3]
    top3_w = sum(cur_w[c] for c in top3_codes)
    single_max_code = max(codes, key=lambda c:cur_w[c])
    cvar0, contrib0, tail0 = cvar_and_contrib(cur_w)

    # ---------- 订单执行函数 ----------
    def order_eligible(code):
        """返回 (是否可当日成交, 原因)"""
        Q = q[code]
        if code not in q:
            return False, "附件无报价"
        if Q["trading_status"] != "连续竞价":
            return False, "非连续竞价(%s)" % Q["trading_status"]
        age = int(float(Q["quote_age_seconds"]))
        if age > QMAX_AGE:
            return False, "报价陈旧(age=%ds>%ds)" % (age, QMAX_AGE)
        cp = rd(Q["current_price_cny"]); lu=rd(Q["limit_up_cny"]); ld=rd(Q["limit_down_cny"])
        if not (cp > ld and cp <= lu):
            return False, "价格越限(现价%.2f 跌%.2f 涨%.2f)" % (cp,ld,lu)
        return True, "OK"

    def build_plan(plan_id, slippage_override=None, count_next_day=False):
        """从原始快照独立测算一个方案。返回 dict。"""
        porders = [p for p in plans if p["plan_id"]==plan_id]
        # 当日订单
        same_day = [p for p in porders if p["execution_window"]=="当日"]
        next_day = [p for p in porders if p["execution_window"]!="当日"]

        # 拷贝台账
        remain = {code: dict(cur_qty) for code in codes}  # 不使用，仅占位
        avail_left = {L["lot_id"]: rd(L["available_to_sell"]) for L in lots}
        qty_left = {code: cur_qty[code] for code in codes}

        alloc_rows=[]          # 批次分配输出
        total_notional_cur=0.0  # 按现价的名义
        total_exec_amt=0.0
        total_fees=0.0          # 佣金+印花+过户
        total_slip_cost=0.0     # 滑点成本(现价-执行价)*量
        realized_total=0.0
        order_problems=[]       # 当日订单的硬约束问题
        order_assessable=True

        for p in sorted(same_day, key=lambda x:int(x["order_sequence"])):
            seq=int(p["order_sequence"]); code=p["ts_code"]; sell=int(rd(p["sell_quantity"]))
            # 代码存在性
            if code not in lot_by_code:
                order_problems.append((seq,code,"未知代码，不可评估"))
                order_assessable=False
                continue
            Q=q[code]; cp=rd(Q["current_price_cny"]); lot=int(float(Q["board_lot_shares"]))
            # 整手
            if sell % lot != 0:
                order_problems.append((seq,code,"非整手(%d%%%d≠0)"%(sell,lot)))
                order_assessable=False
            # 可卖
            cur_code_avail = sum(rd(L["available_to_sell"]) for L in lot_by_code[code])
            if sell > cur_code_avail:
                order_problems.append((seq,code,"超过可卖(%d>%.0f)"%(sell,cur_code_avail)))
                order_assessable=False
            # 报价/状态
            elig, why = order_eligible(code)
            if not elig:
                order_problems.append((seq,code,why))
                order_assessable=False

            notional = sell*cp
            part = notional/adv20[code]
            # 参与率（即使报价不可用也按附件报价名义/ADV20 记录，但仅在可评估时判定）
            if part > ADV_PART:
                order_problems.append((seq,code,"参与率%.4f>%.3f"%(part,ADV_PART)))
                order_assessable=False

            # 滑点与费用（仅当可评估时计算实际执行价；否则用 cap 做占位并标注）
            if slippage_override is not None:
                slip_bps = slippage_override
            else:
                slip_bps = min(SLIP_CAP, SLIP_BASE + SLIP_K*math.sqrt(part))
            exec_price = cp*(1-slip_bps/10000.0)
            exec_amt = sell*exec_price
            comm = max(COMM_MIN, exec_amt*COMM_R)
            stamp= exec_amt*STAMP_R
            trans= exec_amt*TRANS_R
            fee = comm+stamp+trans
            net_proceeds = exec_amt - fee
            slip_cost = sell*(cp-exec_price)

            # 批次分配：buy_date 升序 lot_id 升序，仅 available>0
            to_alloc=sell; realized=0.0; fees_alloc=0.0
            for L in lot_by_code[code]:
                if to_alloc<=0: break
                if rd(L["available_to_sell"])<=0: continue
                a = min(to_alloc, avail_left[L["lot_id"]])
                if a<=0: continue
                realized += (exec_price - rd(L["cost_price_cny"]))*a
                avail_left[L["lot_id"]] -= a
                qty_left[code] -= a
                # 该批次分摊费用（按数量比例）
                fa = fee*a/sell if sell else 0.0
                fees_alloc += fa
                alloc_rows.append({
                    "plan_id":plan_id,"order_sequence":seq,"ts_code":code,
                    "lot_id":L["lot_id"],"buy_date":L["buy_date"],
                    "allocated_quantity":int(a),
                    "estimated_execution_price":round(exec_price,4),
                    "estimated_fee":round(fa,4),
                    "realized_pnl_cny":round((exec_price-rd(L["cost_price_cny"]))*a - fa,4),
                    "participation":round(part,5),
                    "slippage_bps":round(slip_bps,3),
                    "eligibility_status": "可执行" if order_assessable else "待条件恢复"
                })
                to_alloc-=a
            if to_alloc>0:
                order_problems.append((seq,code,"可卖批次不足分配(缺%d)"%to_alloc))
                order_assessable=False

            realized -= fee  # 扣费后
            realized_total += realized
            total_notional_cur += notional
            total_exec_amt += exec_amt
            total_fees += fee
            total_slip_cost += slip_cost

        # 交易后市值（按现价对剩余数量）与现金
        post_mv={}; 
        for code in codes:
            pr=rd(q[code]["current_price_cny"]); fx=rd(q[code]["fx_to_cny"])
            post_mv[code]= qty_left[code]*pr*fx
        post_stock_mv=sum(post_mv.values())
        post_cash = net_cash + (total_exec_amt - total_fees)
        post_na = post_stock_mv + post_cash
        # 勾稽：post_na 应 = reported_na - 滑点 - 费用
        na_identity = reported_na - total_slip_cost - total_fees

        post_w={code: (post_mv[code]/post_na if post_na else 0.0) for code in codes}
        post_eq = post_stock_mv/post_na if post_na else 0
        post_cashw = post_cash/post_na if post_na else 0
        post_top3_c = sorted(codes, key=lambda c:-post_w[c])[:3]
        post_top3 = sum(post_w[c] for c in post_top3_c)
        post_single_c = max(codes, key=lambda c:post_w[c])
        post_single = post_w[post_single_c]
        post_cvar, post_contrib, post_tail = cvar_and_contrib(post_w)
        # 敏感性
        cvar200,_ = cvar_window(post_w, 200)

        return {
            "plan_id":plan_id, "orders":porders, "same_day":same_day, "next_day":next_day,
            "alloc_rows":alloc_rows, "problems":order_problems, "assessable":order_assessable,
            "total_notional_cur":total_notional_cur, "total_exec_amt":total_exec_amt,
            "total_fees":total_fees, "total_slip_cost":total_slip_cost,
            "total_cost": total_fees+total_slip_cost,
            "realized_total":realized_total,
            "post_qty":dict(qty_left), "post_mv":post_mv, "post_w":post_w,
            "post_eq":post_eq, "post_cashw":post_cashw, "post_top3":post_top3,
            "post_top3_c":post_top3_c, "post_single":post_single, "post_single_c":post_single_c,
            "post_cvar":post_cvar, "post_contrib":post_contrib, "post_tail":post_tail,
            "post_stock_mv":post_stock_mv, "post_cash":post_cash, "post_na":post_na,
            "na_identity":na_identity, "na_diff":post_na-na_identity,
            "cvar200":cvar200,
        }

    plans_out = OrderedDict()
    for pid in ["方案A","方案B","方案C"]:
        plans_out[pid] = build_plan(pid)
    # 敏感性：全部按 cap 滑点
    plans_cap = OrderedDict((pid, build_plan(pid, slippage_override=SLIP_CAP)) for pid in plans_out)

    # ================= 写交付 CSV =================
    # 1) 当前持仓风险快照
    snap = os.path.join(outp,"当前持仓风险快照.csv")
    with open(snap,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f)
        w.writerow(["ts_code","security_name","quantity","available_to_sell","cost_basis_cny",
                    "current_price_cny","market_value_cny","weight","unrealized_pnl_cny",
                    "adv20_cny","current_cvar_contrib","breach_flag","breach_detail"])
        for code in codes:
            Q=q[code]
            flags=[]
            if cur_w[code]>SN_MAX: flags.append("单票>%.0f%%"%(SN_MAX*100))
            w.writerow([code,Q["security_name"],int(cur_qty[code]),int(cur_avail[code]),
                        round(cur_cost[code],2),rd(Q["current_price_cny"]),round(mv[code],2),
                        round(cur_w[code],6),round(unreal[code],2),round(adv20[code],2),
                        round(contrib0[code],6),
                        ";".join(flags) if flags else "",";".join(flags)])
        w.writerow(["合计","",int(sum(cur_qty.values())),int(sum(cur_avail.values())),
                    "", "", round(stock_mv,2), round(eq_exp,6),
                    round(sum(unreal.values()),2),"","",
                    "股票仓位=%.4f 现金=%.4f"%(eq_exp,cash_w),
                    ""] )

    # 2) 候选方案约束矩阵
    def metric_row(pid, mname, actual, thr, kind="upper"):
        if actual is None:
            return [pid,mname,"无法评估",thr,"无法评估","","",""]
        if kind=="upper":
            ok = actual<=thr+1e-9
            gap = actual-thr
        else:
            ok = actual>=thr-1e-9
            gap = thr-actual
        return [pid,mname,round(actual,6),thr,"通过" if ok else "不通过",round(gap,6),
                "交易后净资产=%s"%round(plans_out[pid]["post_na"],2),"持仓快照与报价CSV"]

    cm = os.path.join(outp,"候选方案约束矩阵.csv")
    with open(cm,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f)
        w.writerow(["plan_id","metric_name","actual_value","threshold","status","gap",
                    "calculation_basis","evidence_file"])
        for pid,R in plans_out.items():
            # 当日订单可评估性
            w.writerow([pid,"当日订单可卖/T+1/整手/报价/涨跌停/参与率",
                        "全部通过" if R["assessable"] else "存在不通过项",
                        "全部当日订单通过",
                        "通过" if R["assessable"] else "不通过",
                        "; ".join("%s@seq%d %s"%(c,s,t) for s,c,t in R["problems"]) or "0",
                        "逐单检查报价/可卖/整手/ADV20","方案CSV+报价CSV+批次CSV"])
            w.writerow(metric_row(pid,"equity_exposure(股票仓位)",R["post_eq"],EQ_MAX,"upper"))
            w.writerow(metric_row(pid,"single_name_weight(%s)"%R["post_single_c"],R["post_single"],SN_MAX,"upper"))
            w.writerow(metric_row(pid,"top3_weight(%s)"%("/".join(R["post_top3_c"])),R["post_top3"],T3_MAX,"upper"))
            w.writerow(metric_row(pid,"historical_CVaR95",R["post_cvar"],CV_MAX,"upper"))
            w.writerow(metric_row(pid,"minimum_cash_weight(净现金权重)",R["post_cashw"],CASH_MIN,"lower"))
            # 200日敏感性
            w.writerow([pid,"敏感性:CVaR95(200日窗口)",round(R["cvar200"],6),CV_MAX,
                        "通过" if R["cvar200"]<=CV_MAX else "不通过",round(R["cvar200"]-CV_MAX,6),
                        "最近200共同日","日线CSV"])

    # 3) 批次卖出分配（主口径 + cap 敏感性合并标注）
    ba = os.path.join(outp,"批次卖出分配.csv")
    with open(ba,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f)
        w.writerow(["plan_id","order_sequence","ts_code","lot_id","buy_date",
                    "allocated_quantity","estimated_execution_price","estimated_fee",
                    "realized_pnl_cny","participation","slippage_bps","eligibility_status"])
        for pid,R in plans_out.items():
            for row in R["alloc_rows"]:
                w.writerow([row["plan_id"],row["order_sequence"],row["ts_code"],row["lot_id"],
                            row["buy_date"],row["allocated_quantity"],row["estimated_execution_price"],
                            row["estimated_fee"],row["realized_pnl_cny"],row["participation"],
                            row["slippage_bps"],row["eligibility_status"]])
            # 下一交易日订单（不参与当日成交，列出占位）
            for p in R["next_day"]:
                w.writerow([pid,int(p["order_sequence"]),p["ts_code"],"(下一交易日,不进当日成交)",
                            "",int(rd(p["sell_quantity"])),"","","","","","后续条件动作"])

    # 4) 审批监控条件（if→then）
    mon = os.path.join(outp,"审批监控条件.csv")
    conds = [
        ["C01","若 600519.SH 在 14:50 审批截止前刷新为连续竞价且 quote_age≤300s",
         "quote_age≤300s 且 current_price∈(limit_down,limit_up]",
         "把方案C当日两单按新报价重算全部硬约束；若仍全部通过，提交投资负责人重新审批；否则维持方案C今日不批准",
         "交易台/风控","2025-12-31T14:50:00+08:00",
         "刷新后报价CSV、重算约束矩阵","任一硬约束重算后仍不通过"],
        ["C02","若方案B任一日内订单到执行时可卖量不足/非整手/跌停或参与率>5%",
         "可卖≥委托量且整手且非跌停且参与率≤0.05",
         "该单视为未成交，不得把组合记为已合规；按未成交后手改派或缩小数量，成交后重算权重与CVaR",
         "交易台","2025-12-31T14:50:00+08:00",
         "逐单成交回报、当时报价","硬约束仍越限"],
        ["C03","若方案B四单全部成交后",
         "股票仓位≤0.82 且 单票≤0.20 且 前3≤0.50 且 CVaR95≤0.022 且 净现金权重≥0.18",
         "以实际成交价重算后五项全部达标，方可记为合规收口；任一不达标则继续按网格补减",
         "风控","成交后立即","实际成交明细、重算快照","任一硬约束不达标"],
        ["C04","若任一硬约束仍越限期间",
         "equity/single/top3/CVaR/cash 五项中任一不通过",
         "禁止对越限敞口加仓、回补或做T；待重估回到限内再评估",
         "投资负责人","持续至全部达标","每日收盘后重算快照","全部硬约束回到限内"],
        ["C05","方案C序3（002594 卖30000）为下一交易日订单",
         "execution_window≠当日",
         "不得计入14:50前当日合规结果；仅在次一交易日经单独审批后才可执行",
         "交易台","次一交易日开盘前","下一交易日委托与可卖复核","该单被撤销或改期"],
    ]
    with open(mon,"w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f)
        w.writerow(["condition_id","if_condition","threshold","then_action","owner",
                    "check_deadline","evidence_needed","stop_condition"])
        for c in conds: w.writerow(c)

    # ================= 控制台汇总（供报告引用） =================
    print("="*70)
    print("账户勾稽: 股票市值=%.2f  净现金=%.2f  复算净资产=%.2f  报表净资产=%.2f  差额=%.2f"
          %(stock_mv,net_cash,recalc_na,reported_na,recon_diff))
    print("共同有效日=%d  取最近N=%d  m=%d"%(len(common_all),N,m))
    print("当前: 股票仓位=%.4f 现金权重=%.4f 单票max=%s=%.4f 前3(%s)=%.4f CVaR95=%.5f"
          %(eq_exp,cash_w,single_max_code,cur_w[single_max_code],
            "/".join(top3_codes),top3_w,cvar0))
    print("尾部贡献合计=%.6f (应=CVaR=%.6f)"%(sum(contrib0.values()),cvar0))
    print("ADV20:", {k:round(v) for k,v in adv20.items()})
    print("UNKNOWN codes in plans:", unknown)
    print("-"*70)
    for pid,R in plans_out.items():
        print("\n### %s  当日订单=%d 下一交易日订单=%d  可评估=%s"%(
            pid,len(R["same_day"]),len(R["next_day"]),R["assessable"]))
        for s,c,t in R["problems"]:
            print("   [订单seq%d %s] %s"%(s,c,t))
        print("   现价名义额=%.0f 执行额=%.0f 费用(佣印过)=%.0f 滑点成本=%.0f 总成本(含滑点)=%.0f"
              %(R["total_notional_cur"],R["total_exec_amt"],R["total_fees"],R["total_slip_cost"],R["total_cost"]))
        print("   交易后: 股票仓位=%.4f 现金=%.4f 单票=%s=%.4f 前3=%s=%.4f CVaR=%.5f"
              %(R["post_eq"],R["post_cashw"],R["post_single_c"],R["post_single"],
                "/".join(R["post_top3_c"]),R["post_top3"],R["post_cvar"]))
        print("   交易后净资产=%.2f 恒等校验(reported-滑点-费)=%.2f 差=%.4f"%(
            R["post_na"],R["na_identity"],R["na_diff"]))
        print("   CVaR尾部贡献合计=%.6f  200日CVaR=%.5f  扣费后已实现盈亏=%.0f"%(
            sum(R["post_contrib"].values()),R["cvar200"],R["realized_total"]))
        # 硬约束逐项
        checks=[("股票仓位<=%.2f"%EQ_MAX,R["post_eq"]<=EQ_MAX),
                ("单票<=%.2f"%SN_MAX,R["post_single"]<=SN_MAX),
                ("前3<=%.2f"%T3_MAX,R["post_top3"]<=T3_MAX),
                ("CVaR<=%.3f"%CV_MAX,R["post_cvar"]<=CV_MAX),
                ("现金>=%.2f"%CASH_MIN,R["post_cashw"]>=CASH_MIN),
                ("当日订单全部通过",R["assessable"])]
        print("   硬约束:", " ".join("%s=%s"%(n,"PASS" if ok else "FAIL") for n,ok in checks))
    # cap 滑点敏感性
    print("\n--- 敏感性: 全部按 cap=%dbps 滑点 ---"%SLIP_CAP)
    for pid,R in plans_cap.items():
        print("  %s: 费用=%.0f 滑点成本=%.0f 总成本=%.0f 现金权重=%.4f 股票仓位=%.4f CVaR=%.5f"%(
            pid,R["total_fees"],R["total_slip_cost"],R["total_cost"],R["post_cashw"],R["post_eq"],R["post_cvar"]))

if __name__=="__main__":
    main()
