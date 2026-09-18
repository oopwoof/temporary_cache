#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_portfolio_review.py
稳健账户A 持仓交易审批客观测算（as_of = 2025-12-31T14:45:00+08:00）

用法:
    python3 run_portfolio_review.py \
        --daily <持仓标的日线.csv> \
        --lots  <持仓批次.csv> \
        --quote <持仓标的报价.csv> \
        --acct  <账户与交易约束.csv> \
        --plans <候选减仓方案.csv> \
        --outdir <输出目录>

不联网；不硬编码价格/数量/推荐方案；全部数字由五个原始附件重算。
"""
import argparse, csv, math, os, sys
from collections import defaultdict, OrderedDict

def read_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def fnum(x):
    return float(x) if x not in (None, '') else float('nan')

# ---------------- 输入 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--daily', required=True)
    ap.add_argument('--lots', required=True)
    ap.add_argument('--quote', required=True)
    ap.add_argument('--acct', required=True)
    ap.add_argument('--plans', required=True)
    ap.add_argument('--outdir', required=True)
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)

    acct_row = read_csv(a.acct)[0]
    lots = read_csv(a.lots)
    quotes = {r['ts_code']: r for r in read_csv(a.quote)}
    plans = read_csv(a.plans)
    daily_rows = read_csv(a.daily)

    # 账户参数
    cash_balance = fnum(acct_row['cash_balance_cny'])
    accrued_fees = fnum(acct_row['accrued_fees_payable_cny'])
    reported_na = fnum(acct_row['reported_net_asset_cny'])
    net_cash0 = cash_balance - accrued_fees
    equity_max = fnum(acct_row['equity_exposure_max'])
    single_max = fnum(acct_row['single_name_weight_max'])
    top3_max = fnum(acct_row['top3_weight_max'])
    cvar_max = fnum(acct_row['historical_cvar95_max'])
    cash_w_min = fnum(acct_row['minimum_cash_weight'])
    comm_rate = fnum(acct_row['commission_rate'])
    min_comm = fnum(acct_row['minimum_commission_cny'])
    stamp = fnum(acct_row['stamp_duty_sell_rate'])
    transfer = fnum(acct_row['transfer_fee_rate'])
    slip_base = fnum(acct_row['slippage_base_bps'])
    slip_sqrt = fnum(acct_row['slippage_sqrt_coefficient_bps'])
    slip_cap = fnum(acct_row['slippage_cap_bps'])
    max_part = fnum(acct_row['max_adv20_participation'])
    common_days_req = int(acct_row['risk_history_common_days'])
    conf = fnum(acct_row['risk_confidence'])
    cash_ret = fnum(acct_row['cash_daily_return'])
    quote_max_age = int(acct_row['quote_max_age_seconds'])

    # 批次 -> 汇总
    by_code = defaultdict(list)
    for L in lots:
        by_code[L['ts_code']].append(L)
    codes = sorted(by_code.keys())

    # 当前市值 / 可卖汇总
    cur = {}
    for c in codes:
        q = quotes[c]
        px = fnum(q['current_price_cny']); fx = fnum(q['fx_to_cny'])
        tot_qty = sum(int(L['quantity']) for L in by_code[c])
        avail = sum(int(L['available_to_sell']) for L in by_code[c])
        mv = tot_qty * px * fx
        unreal = sum((px - fnum(L['cost_price_cny']))*int(L['quantity']) for L in by_code[c])
        cur[c] = dict(px=px, fx=fx, qty=tot_qty, avail=avail, mv=mv, unreal=unreal,
                      board_lot=int(q['board_lot_shares']),
                      age=int(q['quote_age_seconds']), qstatus=q['quote_status'],
                      tstatus=q['trading_status'],
                      lup=fnum(q['limit_up_cny']), ldn=fnum(q['limit_down_cny']))

    stock_mv0 = sum(cur[c]['mv'] for c in codes)
    na_calc = stock_mv0 + net_cash0
    recon_diff = na_calc - reported_na

    # ADV20: 每只股票最近20条"有行情" amount(千元)*1000 的均值
    adv20 = {}
    for c in codes:
        rows = [r for r in daily_rows if r['ts_code']==c and r['record_status']=='有行情']
        rows.sort(key=lambda r: r['trade_date'], reverse=True)
        last20 = rows[:20]
        adv20[c] = sum(fnum(r['amount'])*1000 for r in last20)/len(last20)

    # 共同有效日：6只股票都有"有行情"记录的交易日，按日期升序
    by_date = defaultdict(dict)
    for r in daily_rows:
        if r['record_status'] != '有行情':
            continue
        try:
            pc = fnum(r['pct_chg'])
        except Exception:
            continue
        by_date[r['trade_date']][r['ts_code']] = pc/100.0
    common_dates = sorted(d for d,m in by_date.items() if all(c in m for c in codes))
    n_common = len(common_dates)
    cvar_block = dict(ok=(n_common >= common_days_req), n=n_common,
                      need=common_days_req)

    def portfolio_cvar(weights_by_code, dates):
        """返回 (cvar, tail_contrib dict, m, N)。weights 为 dict[code]=w, 现金权重×cash_ret。"""
        N = len(dates)
        rets = []
        for d in dates:
            rp = sum(weights_by_code[c]*by_date[d][c] for c in codes)
            rp += (1.0 - sum(weights_by_code.values())) * cash_ret
            rets.append(-rp)  # loss
        order = sorted(range(N), key=lambda i: rets[i], reverse=True)
        m = math.ceil((1.0-conf)*N)
        tail_idx = order[:m]
        cvar = sum(rets[i] for i in tail_idx)/m
        contrib = {}
        for c in codes:
            contrib[c] = sum(-weights_by_code[c]*by_date[dates[i]][c] for i in tail_idx)/m
        # 现金贡献
        wcash = 1.0 - sum(weights_by_code.values())
        contrib['CASH'] = sum(-wcash*cash_ret for i in tail_idx)/m
        return cvar, contrib, m, N

    # 当前权重 / CVaR
    w0 = {c: cur[c]['mv']/na_calc for c in codes}
    cvar0, contrib0, m0, N0 = (None,None,None,None)
    if cvar_block['ok']:
        dates0 = common_dates[-common_days_req:]
        cvar0, contrib0, m0, N0 = portfolio_cvar(w0, dates0)

    # ---------------- 订单级测算 ----------------
    def order_eligible(o):
        """返回 (可执行?, 原因, 报价可用px)"""
        c = o['ts_code']
        q = quotes.get(c)
        if q is None:
            return False, '未知代码', None
        if q['trading_status'] != '连续竞价':
            return False, f"交易状态={q['trading_status']}", None
        age = int(q['quote_age_seconds'])
        if age > quote_max_age:
            return False, f'报价陈旧(age={age}s>{quote_max_age}s)', None
        px = fnum(q['current_price_cny'])
        if not (fnum(q['limit_down_cny']) < px <= fnum(q['limit_up_cny'])):
            return False, '不在涨跌停可成交区间', None
        qty = int(o['sell_quantity'])
        lot = int(q['board_lot_shares'])
        if qty % lot != 0:
            return False, f'非整手({qty}%{lot}!=0)', px
        return True, '可执行', px

    def allocate(c, sell_qty):
        """按 buy_date升序, lot_id升序, 只分配available_to_sell>0批次。返回分配列表。"""
        batches = sorted(by_code[c], key=lambda L:(L['buy_date'], L['lot_id']))
        remaining = sell_qty; alloc=[]
        for L in batches:
            avail = int(L['available_to_sell'])
            if avail <= 0:
                continue
            take = min(remaining, avail)
            if take>0:
                alloc.append(dict(lot_id=L['lot_id'], buy_date=L['buy_date'],
                                  cost=fnum(L['cost_price_cny']), qty=take))
                remaining -= take
            if remaining<=0: break
        return alloc, remaining

    def eval_plan(plan_orders, slip_override_bps=None, cvar_dates=None):
        """从原始快照独立测算一个方案。"""
        # 当日订单
        today_orders = [o for o in plan_orders if o['execution_window']=='当日']
        next_orders  = [o for o in plan_orders if o['execution_window']!='当日']

        # 可卖量逐票检查
        per_code_sell = defaultdict(int)
        order_results = []
        all_executable = True
        total_fee = 0.0; total_slippage_loss=0.0; total_notional=0.0
        remaining_qty = {c: cur[c]['qty'] for c in codes}
        remaining_avail = {c: cur[c]['avail'] for c in codes}
        allocation_rows = []
        for o in sorted(today_orders, key=lambda x:int(x['order_sequence'])):
            c=o['ts_code']; sell=int(o['sell_quantity'])
            eligible, reason, px = order_eligible(o)
            per_code_sell[c]+=sell
            rec = dict(seq=int(o['order_sequence']), code=c, sell=sell,
                       eligible=eligible, reason=reason)
            if not eligible:
                all_executable=False
                order_results.append(rec); continue
            # 可卖量检查
            if per_code_sell[c] > cur[c]['avail']:
                rec.update(eligible=False, reason=f"当日累计{per_code_sell[c]}>可卖{cur[c]['avail']}")
                all_executable=False
                order_results.append(rec); continue
            notional = sell*px
            total_notional += notional
            part = notional/adv20[c]
            if slip_override_bps is not None:
                slip_bps = slip_override_bps
            else:
                slip_bps = min(slip_cap, slip_base + slip_sqrt*math.sqrt(part))
            exec_px = px*(1-slip_bps/10000.0)
            exec_amt = sell*exec_px
            comm = max(min_comm, exec_amt*comm_rate)
            stamp_fee = exec_amt*stamp
            tr_fee = exec_amt*transfer
            fee = comm+stamp_fee+tr_fee
            total_fee += fee
            slip_loss = sell*(px-exec_px)
            total_slippage_loss += slip_loss
            net_proceeds = exec_amt - fee
            # 批次分配
            alloc, leftover = allocate(c, sell)
            realized = 0.0
            for a in alloc:
                realized += (exec_px - a['cost'])*a['qty']
                allocation_rows.append(dict(
                    plan_id=plan_orders[0]['plan_id'], seq=int(o['order_sequence']),
                    code=c, lot_id=a['lot_id'], alloc_qty=a['qty'],
                    exec_px=exec_px, est_fee=fee*a['qty']/sell if sell else 0.0,
                    realized=(exec_px-a['cost'])*a['qty']))
            realized -= fee  # 扣费后口径
            remaining_qty[c]-=sell
            remaining_avail[c]-=sell
            rec.update(notional=notional, adv20=adv20[c], part=part, part_ok=(part<=max_part),
                       slip_bps=slip_bps, exec_px=exec_px, exec_amt=exec_amt,
                       comm=comm, stamp=stamp_fee, transfer=tr_fee, fee=fee,
                       net_proceeds=net_proceeds, realized=realized,
                       leftover_after_alloc=leftover, alloc=alloc)
            order_results.append(rec)
            if part > max_part:
                all_executable=False; rec['part_ok']=False
        # 交易后
        post_mv = sum(remaining_qty[c]*cur[c]['px']*cur[c]['fx'] for c in codes)
        post_cash = net_cash0 + sum(r.get('net_proceeds',0.0) for r in order_results if r['eligible'])
        post_na = post_mv + post_cash
        # 校验: post_na ≈ reported_na - slip_loss - fee
        na_check = post_na - (reported_na - total_slippage_loss - total_fee)
        w_post = {c: (remaining_qty[c]*cur[c]['px']*cur[c]['fx'])/post_na for c in codes}
        # CVaR
        cvar_post=None; contrib_post=None; m_post=None; N_post=None
        if cvar_dates is not None:
            cvar_post, contrib_post, m_post, N_post = portfolio_cvar(w_post, cvar_dates)
        return dict(order_results=order_results, next_orders=next_orders,
                    post_mv=post_mv, post_cash=post_cash, post_na=post_na,
                    na_check=na_check, w_post=w_post,
                    equity=post_mv/post_na, cash_w=post_cash/post_na,
                    singles={c:w_post[c] for c in codes},
                    top3=sum(sorted(w_post.values(), reverse=True)[:3]),
                    cvar_post=cvar_post, contrib_post=contrib_post,
                    m_post=m_post, N_post=N_post,
                    total_fee=total_fee, total_slippage=total_slippage_loss,
                    total_cost=total_fee+total_slippage_loss,
                    total_notional=sum(r.get('notional',0.0) for r in order_results if r['eligible']),
                    all_executable=all_executable, remaining_qty=remaining_qty)

    # 主口径：共同250日
    main_dates = common_dates[-common_days_req:] if cvar_block['ok'] else []
    # 敏感性：200日
    sens_dates = common_dates[-200:] if len(common_dates)>=200 else common_dates

    plan_ids = []
    for p in plans:
        if p['plan_id'] not in plan_ids: plan_ids.append(p['plan_id'])
    plan_orders = {pid:[p for p in plans if p['plan_id']==pid] for pid in plan_ids}

    results = {}
    for pid in plan_ids:
        r_main = eval_plan(plan_orders[pid], slip_override_bps=None, cvar_dates=main_dates)
        r_slipcap = eval_plan(plan_orders[pid], slip_override_bps=slip_cap, cvar_dates=main_dates)
        r_200 = eval_plan(plan_orders[pid], slip_override_bps=None, cvar_dates=sens_dates)
        results[pid] = dict(main=r_main, slipcap=r_slipcap, d200=r_200)

    # ---------------- 输出 CSV ----------------
    # 1) 当前持仓风险快照
    snap_path = os.path.join(a.outdir, '当前持仓风险快照.csv')
    with open(snap_path,'w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f)
        w.writerow(['ts_code','security_name','quantity','available_to_sell','market_value_cny',
                    'weight','unrealized_pnl_cny','adv20_cny','current_cvar_contribution','breach_flag'])
        for c in codes:
            breach=[]
            if w0[c]>single_max: breach.append(f'单票>{single_max}')
            cc = contrib0.get(c) if contrib0 else None
            w.writerow([c, quotes[c]['security_name'], cur[c]['qty'], cur[c]['avail'],
                        round(cur[c]['mv'],2), round(w0[c],6), round(cur[c]['unreal'],2),
                        round(adv20[c],2),
                        (round(cc,6) if cc is not None else 'NA'),
                        ';'.join(breach) if breach else ''])

    # 2) 候选方案约束矩阵
    mat_path = os.path.join(a.outdir, '候选方案约束矩阵.csv')
    rows=[]
    def add_metric(pid, name, actual, thr, basis, evidence):
        if actual is None or (isinstance(actual,float) and math.isnan(actual)):
            status='无法评估'; gap='NA'
        else:
            if name in ('equity_exposure','single_name_weight','single_name_weight_max','top3_weight','historical_cvar95'):
                status='通过' if actual<=thr+1e-9 else '不通过'
                gap=round(actual-thr,6)
            elif name=='minimum_cash_weight':
                status='通过' if actual>=thr-1e-9 else '不通过'
                gap=round(actual-thr,6)
            else:
                status=''; gap=''
        rows.append([pid, name, actual, thr, status, gap, basis, evidence])

    for pid in plan_ids:
        R=results[pid]['main']
        add_metric(pid,'账户勾稽(复算NA-报告NA)', round(recon_diff,2), 1.0,
                   '股票市值+净现金 vs reported_net_asset_cny', '账户与交易约束/持仓批次/报价')
        add_metric(pid,'当日订单全部可执行', '是' if R['all_executable'] else '否', '是',
                   'T+1/整手/连续竞价/报价时效≤300s/涨跌停/ADV20参与率', '候选减仓方案/报价')
        add_metric(pid,'equity_exposure', round(R['equity'],6), equity_max,
                   '交易后股票市值/交易后净资产', '主口径')
        add_metric(pid,'single_name_weight_max',
                   round(max(R['singles'].values()),6), single_max,
                   '交易后最大单票权重', '主口径')
        add_metric(pid,'top3_weight', round(R['top3'],6), top3_max,
                   '交易后前3大权重之和', '主口径')
        if R['cvar_post'] is not None:
            add_metric(pid,'historical_cvar95', round(R['cvar_post'],6), cvar_max,
                       f'N={R["N_post"]},m={R["m_post"]},最近250共同日', '主口径')
        else:
            add_metric(pid,'historical_cvar95', None, cvar_max, '共同日不足', '主口径')
        add_metric(pid,'minimum_cash_weight', round(R['cash_w'],6), cash_w_min,
                   '交易后净现金/交易后净资产', '主口径')
    with open(mat_path,'w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f)
        w.writerow(['plan_id','metric_name','actual_value','threshold','status','gap','calculation_basis','evidence_file'])
        for r in rows: w.writerow(r)

    # 3) 批次卖出分配
    alloc_path=os.path.join(a.outdir,'批次卖出分配.csv')
    with open(alloc_path,'w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f)
        w.writerow(['plan_id','order_sequence','ts_code','lot_id','allocated_quantity',
                    'estimated_execution_price','estimated_fee','realized_pnl_cny','eligibility_status'])
        for pid in plan_ids:
            R=results[pid]['main']
            seen=set()
            for r in R['order_results']:
                if not r['eligible']:
                    w.writerow([pid, r['seq'], r['code'], 'NA', r['sell'], 'NA','NA','NA',
                                '不可执行:'+r['reason']])
                    continue
                for a in r['alloc']:
                    fee_share = r['fee']*a['qty']/r['sell']
                    realized = (r['exec_px']-a['cost'])*a['qty']
                    w.writerow([pid, r['seq'], r['code'], a['lot_id'], a['qty'],
                                round(r['exec_px'],4), round(fee_share,2), round(realized,2),
                                '已分配'])
            # 下一交易日订单
            for o in R['next_orders']:
                w.writerow([pid, int(o['order_sequence']), o['ts_code'], 'NA',
                            int(o['sell_quantity']), 'NA','NA','NA','下一交易日,不计入当日合规'])

    # 控制台摘要（供报告引用）
    out = dict(
        recon=dict(stock_mv=stock_mv0, net_cash=net_cash0, na_calc=na_calc,
                   reported=reported_na, diff=recon_diff),
        cur=cur, adv20=adv20, w0=w0, cvar0=cvar0, contrib0=contrib0, m0=m0, N0=N0,
        cvar_block=cvar_block, n_common=n_common,
        results={pid: {
            'main': {k:v for k,v in results[pid]['main'].items()
                     if k not in ('order_results','next_orders','w_post','singles','contrib_post','remaining_qty','allocation_rows')},
            'slipcap_cost': results[pid]['slipcap']['total_cost'],
            'slipcap_cvar': results[pid]['slipcap']['cvar_post'],
            'd200_cvar': results[pid]['d200']['cvar_post'],
            'order_feed': [
                {kk:(round(vv,4) if isinstance(vv,float) else vv) for kk,vv in r.items()
                 if kk not in ('alloc',)}
                for r in results[pid]['main']['order_results']]
        } for pid in plan_ids},
    )
    import json
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))

if __name__=='__main__':
    main()
