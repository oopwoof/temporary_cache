# -*- coding: utf-8 -*-
"""Generate deliverables for short-term liquidity & rebalancing decision."""
import csv, json, hashlib, os
import numpy as np

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(OUT, exist_ok=True)

# ---------------- constants (synthetic, de-identified) ----------------
IDS = ['SYN201.SH','SYN202.SZ','SYN203.SH','SYN204.SZ','SYNETF.SH']
NAMES = ['证券A','证券B','证券C','证券D','宽基ETF']
SHORT = ['A','B','C','D','ETF']
ASSET = ['A股普通股','A股普通股','A股普通股','A股普通股','股票ETF']
MKT = ['上交所','深交所','上交所','深交所','上交所']
IND = ['电子','银行','医药','电子','宽基']
PRICES = np.array([22.4, 7.86, 15.2, 13.5, 4.12])
QTYS = np.array([18000, 12500, 8000, 10000, 50000])
SELLABLE = np.array([14000, 12500, 8000, 10000, 50000])
FROZEN = np.array([4000, 0, 0, 0, 0])
BROKER_AVG = np.array([21.32, 8.11, 14.52, 16.82, 3.925])
VOL = np.array([0.48, 0.18, 0.35, 0.62, 0.22])
BETA = np.array([1.35, 0.55, 0.80, 1.50, 1.00])
HIGHVOL = [True, False, False, True, False]
ADV = np.array([2400000, 3000000, 850000, 180000, 15000000])
SLIPPAGE = np.array([0.0005, 0.0006, 0.0, 0.003, 0.0002])
COMM_RATE = np.array([0.00025, 0.00025, 0.00025, 0.00025, 0.0002])
STAMP_RATE = np.array([0.0005, 0.0005, 0.0005, 0.0005, 0.0])
TRANSFER_RATE = np.array([0.00001, 0.00001, 0.00001, 0.00001, 0.0])

LOTS = {
    'A': [dict(qty=6000, px=18.50, comm=27.75, tr=1.11),
          dict(qty=8000, px=21.80, comm=43.60, tr=1.74),
          dict(qty=4000, px=24.00, comm=24.00, tr=0.96)],
    'B': [dict(qty=12500, px=8.10, comm=25.31, tr=1.01)],
    'C': [dict(qty=8000, px=14.50, comm=29.00, tr=1.16)],
    'D': [dict(qty=10000, px=16.80, comm=42.00, tr=1.68)],
    'ETF': [dict(qty=50000, px=3.92, comm=49.00, tr=0.0)],
}

CORR = np.array([
 [1.00, 0.18, 0.32, 0.78, 0.72],
 [0.18, 1.00, 0.20, 0.12, 0.45],
 [0.32, 0.20, 1.00, 0.28, 0.30],
 [0.78, 0.12, 0.28, 1.00, 0.65],
 [0.72, 0.45, 0.30, 0.65, 1.00],
])
COV = np.diag(VOL) @ CORR @ np.diag(VOL)

NAV_BROKER = 1038800.00
WITHDRAWABLE = 58000.00
UNSETTLED_NET = 17985.82
DIVIDEND = 3750.00
NAV_DETAIL = 964050.0 + WITHDRAWABLE + UNSETTLED_NET + DIVIDEND
TARGET = 260000.00

# signed price change (negative = 下跌/损失, positive = 上行/收益)
STRESS = {
    'STRESS-01 成长与电子风险重估': np.array([-0.18, -0.03, -0.08, -0.25, -0.09]),
    'STRESS-02 流动性与复牌跳空': np.array([-0.10, -0.05, -0.20, -0.30, -0.07]),
    'STRESS-03 风险偏好修复(上行)': np.array([0.12, 0.02, 0.05, 0.18, 0.06]),
}
def loss_of(pmv, scen):
    return float(-np.sum(pmv * STRESS[scen]))  # positive = loss for down scenarios

def cost_basis():
    cb = {}
    for k, lots in LOTS.items():
        tot = sum(l['qty'] * l['px'] + l['comm'] + l['tr'] for l in lots)
        cb[k] = tot / sum(l['qty'] for l in lots)
    return cb
CB = cost_basis()

def sell_fee(qty, price, i):
    gross = round(qty * price, 2)
    fill = round(gross * (1 - SLIPPAGE[i]), 2)
    comm = round(max(5.0, round(fill * COMM_RATE[i], 2)), 2)
    stamp = round(fill * STAMP_RATE[i], 2)
    tr = round(fill * TRANSFER_RATE[i], 2)
    net = round(fill - comm - stamp - tr, 2)
    return dict(gross=gross, fill=fill, comm=comm, stamp=stamp, tr=tr, net=net, cost=round(fill - net, 2))

def plan_metrics(sell):
    sell = np.array(sell, float)
    fees = {}; net = 0.0; cost = 0.0
    for i in range(5):
        if sell[i] > 0:
            f = sell_fee(sell[i], PRICES[i], i); fees[SHORT[i]] = f
            net += f['net']; cost += f['cost']
    net = round(net, 2); cost = round(cost, 2)
    post = QTYS - sell; pmv = post * PRICES
    wd = round(WITHDRAWABLE + UNSETTLED_NET + net, 2)
    NAVpb = round(NAV_BROKER - cost, 2); NAVpd = round(NAV_DETAIL - cost, 2)
    realized = {k: round((PRICES[SHORT.index(k)] - CB[k]) * sell[SHORT.index(k)], 2) for k in SHORT}
    r = dict(sell=sell.astype(int).tolist(), fees=fees, net=net, cost=cost,
             post_qty=post.astype(int).tolist(), post_mv=[round(x, 2) for x in pmv],
             wd=wd, NAVpb=NAVpb, NAVpd=NAVpd, realized=realized,
             realized_sum=round(sum(realized.values()), 2))
    for NAV, lab in [(NAVpd, 'd'), (NAVpb, 'b')]:
        w = pmv / NAV; pvol = float(np.sqrt(w @ COV @ w)); rc = (w * (COV @ w)) / (w @ COV @ w)
        r['w_' + lab] = w; r['pos_' + lab] = float(pmv.sum() / NAV); r['pvol_' + lab] = pvol
        r['rc_' + lab] = rc
        r['s1_' + lab] = loss_of(pmv, 'STRESS-01 成长与电子风险重估') / NAV
        r['s2_' + lab] = loss_of(pmv, 'STRESS-02 流动性与复牌跳空') / NAV
    return r

PLANS = {
    'P1_偏好优先': dict(sell=[10200, 0, 0, 0, 8000]),
    'P2_流动性优先': dict(sell=[11000, 0, 0, 0, 0]),
    'P3_风险预算优先': dict(sell=[11000, 0, 0, 5000, 0]),
}
CURRENT = plan_metrics([0, 0, 0, 0, 0])

def w4(x): return round(float(x), 4)
def m2(x): return round(float(x), 2)

# ---------------- 1. position_reconciliation.csv ----------------
recon_rows = []
for i in range(5):
    key = SHORT[i]; lots = LOTS[key]
    total_qty = int(sum(l['qty'] for l in lots))
    buy_amt = round(sum(l['qty'] * l['px'] for l in lots), 2)
    buy_fees = round(sum(l['comm'] + l['tr'] for l in lots), 2)
    total_cost = round(buy_amt + buy_fees, 2)
    avg_cost = round(total_cost / total_qty, 4)
    broker_avg_cost = round(BROKER_AVG[i] * total_qty, 2)
    diff = round(broker_avg_cost - total_cost, 2)
    mv = round(PRICES[i] * total_qty, 2)
    ocr = {
        'A': 'total_qty=13,000?(置信0.62,异常); sellable=14,000(0.94)',
        'B': 'total_qty=12,500(0.98) 一致',
        'C': 'total_qty=8,000(0.96) 一致',
        'D': 'name=证劵D?(0.74,异常); total_qty=10,000(0.95) 一致',
        'ETF': 'total_qty=50,000(0.99) 一致',
    }[key]
    # T+1 sellable and executable
    if key == 'A':
        sellable_t1 = 18000; exec_now = 18000; earliest = '2026-08-31'; status = '正常交易'
    elif key == 'B':
        sellable_t1 = 12500; exec_now = 12500; earliest = '2026-08-31'; status = '除息日正常交易'
    elif key == 'C':
        sellable_t1 = 8000; exec_now = 0; earliest = '复牌日未知'; status = '自8/25停牌'
    elif key == 'D':
        sellable_t1 = 10000; exec_now = 0; earliest = '恢复买盘日未知'; status = '跌停且买盘为零'
    else:
        sellable_t1 = 50000; exec_now = 50000; earliest = '2026-08-31'; status = '正常交易'
    recon_rows.append(dict(
        security_id=IDS[i], name=NAMES[i], asset=ASSET[i], market=MKT[i], industry=IND[i],
        official_total_qty=int(QTYS[i]), broker_sellable_qty=int(SELLABLE[i]), frozen_qty=int(FROZEN[i]),
        batch_total_qty=total_qty, batch_match='一致' if total_qty == QTYS[i] else '不一致',
        buy_amount=m2(buy_amt), buy_fees=m2(buy_fees), total_cost_incl_fees=m2(total_cost),
        avg_cost_incl_fees=avg_cost, broker_avg_price=BROKER_AVG[i], broker_avg_cost=m2(broker_avg_cost),
        avg_cost_diff=m2(diff), ocr_check=ocr,
        ocr_low_conf='是' if key == 'A' else ('是(名称字段)' if key == 'D' else '否'),
        official_mv=m2(mv), book_price=PRICES[i],
        price_ts=('2026-08-28 15:00' if key != 'C' else '2026-08-24 15:00'),
        sellable_t1=sellable_t1, executable_now=exec_now, earliest_exec_day=earliest,
        trade_status=status, source_id=('BRK-POS-0%d' % (i + 1))))

# NAV reconciliation summary rows
recon_rows.append(dict(security_id='NAV-RECON', name='明细重算净资产(市值964050+可划转58000+未结算净额17985.82+股息3750)',
    asset='', market='', industry='', official_total_qty='', broker_sellable_qty='', frozen_qty='',
    batch_total_qty='', batch_match='', buy_amount='', buy_fees='', total_cost_incl_fees='',
    avg_cost_incl_fees='', broker_avg_price='', broker_avg_cost='', avg_cost_diff='',
    ocr_check='', ocr_low_conf='', official_mv=m2(NAV_DETAIL), book_price='', price_ts='',
    sellable_t1='', executable_now='', earliest_exec_day='', trade_status='', source_id='RECON-NAV-DETAIL'))
recon_rows.append(dict(security_id='NAV-RECON', name='券商statement_total_assets',
    asset='', market='', industry='', official_total_qty='', broker_sellable_qty='', frozen_qty='',
    batch_total_qty='', batch_match='', buy_amount='', buy_fees='', total_cost_incl_fees='',
    avg_cost_incl_fees='', broker_avg_price='', broker_avg_cost='', avg_cost_diff='',
    ocr_check='', ocr_low_conf='', official_mv=m2(NAV_BROKER), book_price='', price_ts='',
    sellable_t1='', executable_now='', earliest_exec_day='', trade_status='', source_id='BRK-ACC-01'))
recon_rows.append(dict(security_id='NAV-RECON', name='残差(明细-券商)=4985.82(0.48%)，保留未虚构补平',
    asset='', market='', industry='', official_total_qty='', broker_sellable_qty='', frozen_qty='',
    batch_total_qty='', batch_match='', buy_amount='', buy_fees='', total_cost_incl_fees='',
    avg_cost_incl_fees='', broker_avg_price='', broker_avg_cost='', avg_cost_diff='',
    ocr_check='', ocr_low_conf='', official_mv=m2(NAV_DETAIL - NAV_BROKER), book_price='', price_ts='',
    sellable_t1='', executable_now='', earliest_exec_day='', trade_status='', source_id='RECON-RESIDUAL'))

# ---------------- 3. cash_availability_schedule.csv ----------------
cash_rows = []
def add(plan, date, time, event, open_wd, inflow, sale_net, other, close_wd, counts, note, src):
    cash_rows.append(dict(plan=plan, date=date, time=time, event=event,
                          opening_withdrawable=m2(open_wd), known_settlement_inflow=m2(inflow),
                          planned_sale_net=m2(sale_net), other_cashflow=m2(other),
                          closing_withdrawable=m2(close_wd), counts_to_260k=counts,
                          note=note, source_id=src))

add('BASE', '2026-08-28', '15:20', '决策截点(收盘后不可交易)', 58000, 0, 0, 0, 58000, '否', '仅已结算可划转', 'CAL-01')
add('BASE', '2026-08-31', '09:00', '旧证券卖出款结算(净额)', 58000, 17985.82, 0, 0, 75985.82, '否', '毛额18000扣佣金5/过户费0.18/印花税9', 'CAL-02')
add('BASE', '2026-08-31', '09:30', '计划卖出执行日(条件成交)', 75985.82, 0, 0, 0, 75985.82, '否', '卖出款当日可用交易但9/1才可划转', 'CAL-03')
add('BASE', '2026-09-01', '09:00', '8/31卖出款转可划转', 75985.82, 0, 0, 0, 75985.82, '否', '若8/31无成交则为0', 'CAL-04')
add('BASE', '2026-09-01', '15:00', '最终交易检查', 75985.82, 0, 0, 0, 75985.82, '否', '关键复核点', 'CAL-05')
add('BASE', '2026-09-02', '09:00', '9/1卖出款转可划转', 75985.82, 0, 0, 0, 75985.82, '否', '9/1成交的税费后净额', 'CAL-06')
add('BASE', '2026-09-02', '10:00', '客户用款截止(需>=260000)', 75985.82, 0, 0, 0, 75985.82, '否', 'BASE下缺口=184,014.18', 'CAL-07')
add('BASE', '2026-09-03', '09:00', '证券B股息到账(晚于截止)', 75985.82, 0, 0, 3750, 79735.82, '否', '不计入9/2 10:00目标', 'CAL-08')

for pk, pv in PLANS.items():
    m = plan_metrics(pv['sell'])
    add(pk, '2026-08-31', '09:30', '执行计划卖出(假设全部于8/31成交)', 75985.82, 0, m['net'], 0, 75985.82, '否', '成交款当日可用于交易', 'CAL-03')
    add(pk, '2026-09-01', '09:00', '8/31卖出款转可划转', 75985.82, 0, 0, m['net'], m['wd'], '是' if m['wd'] >= TARGET else '否', '净额可划转', 'CAL-04')
    add(pk, '2026-09-02', '10:00', '客户用款截止(>=260000)', m['wd'], 0, 0, 0, m['wd'], '是' if m['wd'] >= TARGET else '否', '达标' if m['wd'] >= TARGET else '缺口', 'CAL-07')

# ---------------- 4. portfolio_diagnosis.csv ----------------
diag_rows = []
def diag_row(scope, nav_label, pmv, NAV):
    pmv = np.array(pmv, float); w = pmv / NAV; wc = 1 - w.sum()
    pvol = float(np.sqrt(w @ COV @ w)); rc = (w * (COV @ w)) / (w @ COV @ w)
    sl1 = loss_of(pmv, 'STRESS-01 成长与电子风险重估') / NAV
    sl2 = loss_of(pmv, 'STRESS-02 流动性与复牌跳空') / NAV
    diag_rows.append(dict(
        scope=scope, nav_label=nav_label, NAV=m2(NAV),
        equity_weight=w4(pmv.sum() / NAV), w_A=w4(w[0]), w_B=w4(w[1]), w_C=w4(w[2]),
        w_D=w4(w[3]), w_ETF=w4(w[4]), top2_weight=w4(w[0] + w[4]),
        highvol_weight=w4(w[0] + w[3]), electron_weight=w4(w[0] + w[3]),
        cash_withdrawable_weight=w4(WITHDRAWABLE / NAV), cash_available_weight=w4(76000.0 / NAV),
        port_vol=w4(pvol), rc_A=w4(rc[0]), rc_B=w4(rc[1]), rc_C=w4(rc[2]), rc_D=w4(rc[3]), rc_ETF=w4(rc[4]),
        stress1_loss=m2(loss_of(pmv, 'STRESS-01 成长与电子风险重估')), stress1_ratio=w4(sl1),
        stress2_loss=m2(loss_of(pmv, 'STRESS-02 流动性与复牌跳空')), stress2_ratio=w4(sl2),
        check_C02_equity='FAIL' if pmv.sum() / NAV > 0.75 else 'PASS',
        check_C03_single='FAIL' if w.max() > 0.27 else 'PASS',
        check_C04_highvol='FAIL' if w[0] + w[3] > 0.30 else 'PASS',
        check_C05_electron='FAIL' if w[0] + w[3] > 0.32 else 'PASS',
        check_C06_stress='FAIL' if sl1 > 0.09 else 'PASS',
        check_C07_rc='FAIL' if rc.max() > 0.55 else 'PASS'))
for lab, NAV in [('明细重算净资产', NAV_DETAIL), ('券商总资产', NAV_BROKER)]:
    diag_row('当前持仓', lab, CURRENT['post_mv'], NAV)
for pk, pv in PLANS.items():
    m = plan_metrics(pv['sell'])
    for lab, NAV in [('明细重算净资产', m['NAVpd']), ('券商总资产', m['NAVpb'])]:
        diag_row('交易后_' + pk, lab, m['post_mv'], NAV)

# ---------------- stress scenario detail ----------------
stress_rows = []
for sc, shock in STRESS.items():
    base = np.array(CURRENT['post_mv'], float)
    tot_pnl = 0.0
    for i in range(5):
        mv = base[i]; pnl = round(mv * shock[i], 2); tot_pnl += pnl
        post_mv_i = mv * (1 + shock[i])  # signed price change
        stress_rows.append(dict(scenario=sc, security_id=IDS[i], name=NAMES[i],
            price_shock=shock[i], market_value=m2(mv), pnl=m2(pnl),
            post_stress_value=m2(post_mv_i), post_stress_weight=w4(post_mv_i / (NAV_BROKER + tot_pnl)),
            tradable='是' if i in (0, 1, 4) else ('否(停牌)' if i == 2 else '否(跌停无买盘)')))
    tot = round(sum(base * shock), 2)
    # 压力损失占净资产(正=损失，负=收益)
    loss_ratio = w4(-tot / NAV_BROKER)
    stress_rows.append(dict(scenario=sc, security_id='合计', name='组合', price_shock='',
        market_value=m2(sum(base)), pnl=m2(tot), post_stress_value=m2(sum(base) + tot),
        post_stress_weight='', tradable='', stress_loss_ratio=loss_ratio))

# ---------------- 5. action_priority.csv ----------------
action_rows = [
 dict(rank=1, security_id='SYN201.SH', name='证券A', action='减仓(高优先级)',
      evidence='单票权重0.3881>0.27(CON-03)；风险贡献0.5820>0.55(CON-07)；高波动/电子合计0.5181>0.30/0.32(CON-04/05)；压力损失主因',
      executable='是(8/31起18,000股可卖)', cash_speed='快(ADV2,400,000，买量50,000)',
      risk_improvement='同时改善单票/高波动/电子/压力/风险贡献', realized_pnl='实现盈利(约+1.21元/股)',
      exdiv_note='', reason='唯一可同时修复多数硬约束且可执行的标的；技术偏强不覆盖hard约束'),
 dict(rank=2, security_id='SYN204.SZ', name='证券D', action='退出(高优先级，条件触发)',
      evidence='年化波动0.62最高；beta1.50；高波动+电子；压力冲击-25%最大',
      executable='否(跌停且买盘为零)', cash_speed='恢复买盘前为0',
      risk_improvement='显著降低高波动/电子/压力/组合波动', realized_pnl='实现亏损(约-3.30元/股)',
      exdiv_note='', reason='风险最高且应优先处理，但当前无法成交；恢复买盘后优先退出'),
 dict(rank=3, security_id='SYN203.SH', name='证券C', action='持有(停牌锁定)',
      evidence='停牌自8/25，价格陈旧(8/24)，账面市值121,600不等于可变现',
      executable='否(停牌)', cash_speed='复牌前为0', risk_improvement='复牌跳空风险未计入历史波动',
      realized_pnl='未知(复牌价未定)', exdiv_note='', reason='无法成交，不得计入可变现资金；复牌后重新评估'),
 dict(rank=4, security_id='SYNETF.SH', name='宽基ETF', action='减仓(现金工具，次要)',
      evidence='流动性最好(ADV15,000,000，价差2bps)；但不属高波动/电子，不能修复行业集中',
      executable='是(50,000股可卖)', cash_speed='最快', risk_improvement='仅降低总仓位，不改善高波动/电子集中',
      realized_pnl='小幅盈利(约+0.20元/股)', exdiv_note='', reason='快速筹现但非风险整改主工具；作为A减仓后的补足'),
 dict(rank=5, security_id='SYN202.SZ', name='证券B', action='持有(避免卖出)',
      evidence='波动低0.18；beta0.55；银行；PREF-04避免股息支付前卖出；除息已过登记日',
      executable='是(12,500股可卖)', cash_speed='中(ADV3,000,000)', risk_improvement='无(不属高波动/电子)',
      realized_pnl='除息口径下近持平(账面-0.24元/股，计入0.30股息后+0.06元/股)',
      exdiv_note='除息0.30已确认，股息9/3到账晚于9/2截止',
      reason='不卖以最小化实现亏损并保留股息权利'),
]

# ---------------- 6. scenario_comparison.csv ----------------
scen_rows = []
def emit_plan(plan_id, sell, note):
    m = plan_metrics(sell)
    for i in range(5):
        if sell[i] > 0:
            f = m['fees'][SHORT[i]]; wb = m['w_b']
            if SHORT[i] == 'D':
                earliest = '恢复买盘日未知(条件触发)'
                check = '当前跌停无买盘，可执行=0；仅恢复有效买盘后按此计划执行'
            else:
                earliest = '2026-08-31'
                check = '可卖校验通过(<=T+1可卖，正常交易)'
            scen_rows.append(dict(plan=plan_id, trade_desc=note, security=SHORT[i], sell_qty=int(sell[i]),
                earliest_exec=earliest, sellable_check=check,
                price_basis=f"{PRICES[i]:.2f}*(1-{SLIPPAGE[i]})", comm=m2(f['comm']), stamp=m2(f['stamp']),
                transfer=m2(f['tr']), slippage_rate=SLIPPAGE[i], expected_net_cash=m2(f['net']),
                transferable_date='2026-09-01 09:00', post_qty=m['post_qty'][i],
                post_weight=w4(wb[i]), equity_weight=w4(m['pos_b']), highvol_weight=w4(wb[0] + wb[3]),
                electron_weight=w4(wb[0] + wb[3]), port_vol=w4(m['pvol_b']),
                risk_contribution=w4(m['rc_b'][i]), stress_loss_ratio=w4(m['s1_b'])))
    # summary row
    wb = m['w_b']; tot_comm = m2(sum(f['comm'] for f in m['fees'].values()))
    tot_stamp = m2(sum(f['stamp'] for f in m['fees'].values())); tot_tr = m2(sum(f['tr'] for f in m['fees'].values()))
    scen_rows.append(dict(plan=plan_id, trade_desc='合计', security='组合', sell_qty='',
        earliest_exec='2026-08-31', sellable_check='', price_basis='', comm=tot_comm, stamp=tot_stamp,
        transfer=tot_tr, slippage_rate='', expected_net_cash=m2(m['net']),
        transferable_date='2026-09-01 09:00', post_qty='', post_weight='', equity_weight=w4(m['pos_b']),
        highvol_weight=w4(wb[0] + wb[3]), electron_weight=w4(wb[0] + wb[3]), port_vol=w4(m['pvol_b']),
        risk_contribution=w4(max(m['rc_b'])), stress_loss_ratio=w4(m['s1_b'])))

emit_plan('P1_偏好优先', [10200, 0, 0, 0, 8000], '保留A至7800股(硬约束下最大)；ETF补足现金')
emit_plan('P2_流动性优先', [11000, 0, 0, 0, 0], '仅卖A单标的，最小执行点数')
emit_plan('P3_风险预算优先', [11000, 0, 0, 5000, 0], '减A并条件减D，最大化降风险')
m = plan_metrics([11000, 0, 0, 0, 0]); wb = m['w_b']
scen_rows.append(dict(plan='FAIL_D跌停无买盘', trade_desc='D无法成交，偏好优先(保留12000A)不可行；回落至流动性优先卖A11,000',
    security='A', sell_qty=11000, earliest_exec='2026-08-31', sellable_check='可卖校验通过',
    price_basis='22.40*(1-0.0005)', comm=61.57, stamp=123.14, transfer=2.46, slippage_rate=0.0005,
    expected_net_cash=246089.63, transferable_date='2026-09-01 09:00', post_qty=7000, post_weight=w4(wb[0]),
    equity_weight=w4(m['pos_b']), highvol_weight=w4(wb[0] + wb[3]), electron_weight=w4(wb[0] + wb[3]),
    port_vol=w4(m['pvol_b']), risk_contribution=w4(m['rc_b'][0]), stress_loss_ratio=w4(m['s1_b'])))

# ---------------- 7. if_then_rules.csv ----------------
rules = [
 dict(rule_id='R01', applicable='2026-08-31 09:00-09:30', object='证券A(SYN201.SH)',
  trigger='开盘前复核券商可卖数量；IF 8/28买入的4,000股已转可卖(可卖=18,000)',
  action='THEN 方可按计划卖出至多18,000股；若仍显示14,000可卖，则当日仅可卖14,000，剩余4,000推迟至次日',
  qty='至多18,000(取决于可卖复核)', price_basis='开盘后实时卖一价，参考22.40', earliest='2026-08-31 09:30',
  fee_slip='佣金0.00025(最低5)/印花税0.0005/过户费0.00001/滑点0.0005', net_cash='随股数而定',
  transferable='成交后下一交易日09:00', post_trade='A=0..4,000(极端)',
  constraint_status='遵守T+1(CON-10)', expiry='开盘后券商可卖数量明确即失效', review='2026-08-31 09:25',
  evidence='RULE-02/CAL-03/BRK-TRD-03'),
 dict(rule_id='R02', applicable='2026-09-01 15:00前', object='证券D(SYN204.SZ)',
  trigger='IF SYN204.SZ 恢复有效买盘(最佳买量>0且可成交)',
  action='THEN 执行风险预算优先，卖出证券D 5,000股', qty='5,000', price_basis='13.50*(1-0.003)=13.4595口径',
  earliest='恢复买盘当日09:30', fee_slip='佣金0.00025/印花税0.0005/过户费0.00001/滑点0.003',
  net_cash='67,246.36', transferable='成交次日09:00(最迟9/2 09:00)', post_trade='D=5,000',
  constraint_status='高波动降至0.2160/压力0.0735', expiry='9/1收盘前未出现买盘则失效', review='每个交易时段',
  evidence='RISK-04/MKT-D-01/SLIP-04'),
 dict(rule_id='R03', applicable='2026-09-01 15:00前', object='证券D(SYN204.SZ)',
  trigger='IF D恢复但仅部分成交(实际成交<5,000股)',
  action='THEN 以实际成交股数为准，未成交部分不得记为已成交；风险缺口由卖出证券A补足',
  qty='实际成交股数', price_basis='实际成交均价', earliest='成交当刻', fee_slip='同R02',
  net_cash='按实际成交计', transferable='成交次日09:00', post_trade='D=10,000-实际成交',
  constraint_status='按实际成交重算', expiry='全部成交或9/1收盘', review='每笔成交回报后', evidence='RULE-01'),
 dict(rule_id='R04', applicable='2026-09-01 15:00前', object='证券D(SYN204.SZ)',
  trigger='IF D持续跌停且买盘为零(9/1收盘前)',
  action='THEN 不卖D，采用流动性优先方案卖出证券A 11,000股', qty='11,000(A)', price_basis='22.40*(1-0.0005)=22.3888口径',
  earliest='2026-08-31 09:30', fee_slip='佣金0.00025/印花税0.0005/过户费0.00001/滑点0.0005',
  net_cash='246,089.63', transferable='2026-09-01 09:00', post_trade='A=7,000',
  constraint_status='全部hard约束PASS', expiry='9/1收盘', review='2026-09-01 15:00', evidence='MKT-D-01/POLICY-07'),
 dict(rule_id='R05', applicable='2026-08-31至09-01', object='证券C(SYN203.SH)',
  trigger='IF 证券C继续停牌 → 不卖；IF 复牌跳空 → 不得按停牌前15.20成交',
  action='THEN 停牌期不产生卖出；复牌首日重新取得有效报价后再评估，不作为本次现金来源',
  qty='0(停牌期)', price_basis='复牌后有效报价', earliest='复牌日', fee_slip='滑点停牌时不可估算',
  net_cash='0', transferable='—', post_trade='C=8,000', constraint_status='账面市值不等于可变现',
  expiry='复牌且取得有效报价', review='复牌公告', evidence='CA-C-01/MKT-C-01/SLIP-03'),
 dict(rule_id='R06', applicable='全窗口', object='证券A(SYN201.SH)',
  trigger='IF 证券A技术摘要偏强 但 单票权重>0.27 或 风险贡献>0.55',
  action='THEN 仍按hard约束减仓A(方案股数)，不得因技术偏强突破上限', qty='10,200或11,000(按方案)',
  price_basis='22.40*(1-0.0005)', earliest='2026-08-31 09:30', fee_slip='同R01',
  net_cash='228,192.21(10,200) / 246,089.63(11,000)', transferable='2026-09-01 09:00',
  post_trade='A=7,800或7,000', constraint_status='单票<=0.27且风险贡献<=0.55', expiry='减仓完成后',
  review='每次决策前', evidence='MKT-A-01/CON-03/CON-07'),
 dict(rule_id='R07', applicable='全窗口', object='宽基ETF(SYNETF.SH)',
  trigger='IF 试图仅卖ETF筹现',
  action='THEN 高波动(A+D)与电子行业仍超限(0.5181>0.30/0.32)，必须同时卖出证券A',
  qty='ETF作为补足，不替代A', price_basis='4.12*(1-0.0002)', earliest='2026-08-31 09:30',
  fee_slip='佣金0.0002(最低5)/印花税0/滑点0.0002', net_cash='视股数', transferable='成交次日09:00',
  post_trade='—', constraint_status='仅卖ETF不修复高波动/电子', expiry='—', review='方案设计时',
  evidence='RISK-05/CON-04/CON-05'),
 dict(rule_id='R08', applicable='全窗口', object='证券B(SYN202.SZ)',
  trigger='IF 比较证券B价格或考虑卖出',
  action='THEN 用除息参考口径(前收8.16→调整后7.86，机械-0.30为分红)，不得视为真实亏损；股息3,750于9/3到账晚于9/2 10:00，不计入目标现金；其他方案满足hard约束时不卖B',
  qty='0(避免卖出)', price_basis='7.86(除息口径)', earliest='—', fee_slip='—', net_cash='0',
  transferable='—', post_trade='B=12,500', constraint_status='不产生现金贡献', expiry='—', review='—',
  evidence='CA-B-01/MKT-B-02/CAL-08/PREF-04'),
 dict(rule_id='R09', applicable='全窗口', object='全部可卖标的',
  trigger='IF 成交价较参考价下跌5%或10% 或 买量不足 或 单日达5%ADV上限 或 单笔佣金不足5元',
  action='THEN 按调整后价格/数量重算净现金并校验是否仍>=260,000；不足则增卖(不超过5%ADV与可卖上限)；佣金按最低5元计',
  qty='动态调整', price_basis='调整后价格', earliest='—', fee_slip='佣金最低5元', net_cash='重算',
  transferable='成交次日09:00', post_trade='重算', constraint_status='确保CON-01/08', expiry='成交确认后',
  review='每笔委托前', evidence='FEE-01/RULE-04/CON-08'),
 dict(rule_id='R10', applicable='2026-09-01 15:00', object='组合现金',
  trigger='IF 9/1收盘 已结算可划转+已成交待结算 < 260,000',
  action='THEN 在9/1收盘前执行剩余必要且可成交的卖出；若仍不足，9/2 10:00无法达标，须上报并考虑soft偏好让步或外部安排(不越权)',
  qty='缺口对应股数', price_basis='9/1实时价', earliest='2026-09-01 15:00前', fee_slip='按规则',
  net_cash='补齐缺口', transferable='2026-09-02 09:00', post_trade='重算', constraint_status='CON-01',
  expiry='9/1收盘', review='2026-09-01 14:30', evidence='CAL-05/CAL-06'),
 dict(rule_id='R11', applicable='全窗口', object='所有委托',
  trigger='IF 任何委托未成交、部分成交 或 资金尚未到可划转时点',
  action='THEN 该部分不得记为已完成/已形成现金；现金目标按实际成交净额重算',
  qty='实际成交', price_basis='实际', earliest='—', fee_slip='实际', net_cash='实际',
  transferable='实际结算日', post_trade='实际', constraint_status='—', expiry='—', review='每笔回报后',
  evidence='RULE-01/CON-01'),
 dict(rule_id='R12', applicable='全窗口', object='做T',
  trigger='IF 考虑做T',
  action='THEN 仅允许先卖已有可卖底仓再同日回补不超过已卖数量；须证明价差覆盖双边佣金、过户费、卖出印花税、双边滑点+0.10%缓冲；日终不得增加持仓或妨碍现金目标；本窗口不采用做T解决现金缺口',
  qty='回补<=已卖', price_basis='—', earliest='—', fee_slip='双边+0.10%缓冲', net_cash='不产生净现金',
  transferable='—', post_trade='日终不增仓', constraint_status='CON-10/RULE-05', expiry='—', review='—',
  evidence='RULE-05'),
]

# ---------------- write CSVs ----------------
def write_csv(fname, rows, fieldnames):
    path = os.path.join(OUT, fname)
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path

write_csv('position_reconciliation.csv', recon_rows, [
    'security_id','name','asset','market','industry','official_total_qty','broker_sellable_qty','frozen_qty',
    'batch_total_qty','batch_match','buy_amount','buy_fees','total_cost_incl_fees','avg_cost_incl_fees',
    'broker_avg_price','broker_avg_cost','avg_cost_diff','ocr_check','ocr_low_conf','official_mv','book_price',
    'price_ts','sellable_t1','executable_now','earliest_exec_day','trade_status','source_id'])

write_csv('cash_availability_schedule.csv', cash_rows, [
    'plan','date','time','event','opening_withdrawable','known_settlement_inflow','planned_sale_net',
    'other_cashflow','closing_withdrawable','counts_to_260k','note','source_id'])

write_csv('portfolio_diagnosis.csv', diag_rows, [
    'scope','nav_label','NAV','equity_weight','w_A','w_B','w_C','w_D','w_ETF','top2_weight','highvol_weight',
    'electron_weight','cash_withdrawable_weight','cash_available_weight','port_vol','rc_A','rc_B','rc_C','rc_D',
    'rc_ETF','stress1_loss','stress1_ratio','stress2_loss','stress2_ratio','check_C02_equity','check_C03_single',
    'check_C04_highvol','check_C05_electron','check_C06_stress','check_C07_rc'])

write_csv('action_priority.csv', action_rows, [
    'rank','security_id','name','action','evidence','executable','cash_speed','risk_improvement',
    'realized_pnl','exdiv_note','reason'])

write_csv('scenario_comparison.csv', scen_rows, [
    'plan','trade_desc','security','sell_qty','earliest_exec','sellable_check','price_basis','comm','stamp',
    'transfer','slippage_rate','expected_net_cash','transferable_date','post_qty','post_weight','equity_weight',
    'highvol_weight','electron_weight','port_vol','risk_contribution','stress_loss_ratio'])

write_csv('if_then_rules.csv', rules, [
    'rule_id','applicable','object','trigger','action','qty','price_basis','earliest','fee_slip','net_cash',
    'transferable','post_trade','constraint_status','expiry','review','evidence'])

write_csv('stress_scenario_detail.csv', stress_rows, [
    'scenario','security_id','name','price_shock','market_value','pnl','post_stress_value','post_stress_weight',
    'tradable','stress_loss_ratio'])

# ---------------- 二.3 cash-scenario minimum table (computed) ----------------
min_net = round(TARGET - (WITHDRAWABLE + UNSETTLED_NET), 2)  # 184,014.18
def total_rate(i): return SLIPPAGE[i] + COMM_RATE[i] + STAMP_RATE[i] + TRANSFER_RATE[i]
min_gross = {k: round(min_net / (1 - total_rate(SHORT.index(k))), 2) for k in SHORT}

# ---------------- memo ----------------
memo = []
memo.append("# 短期用款与流动性排序 — 客户经理备忘录\n")
memo.append("> 内部研究/客户沟通草案。不构成订单、不构成收益承诺。全部证券/行情/账户为合成、脱敏数据。\n")
memo.append("> 决策时点：2026-08-28 15:20 Asia/Shanghai（周五收盘后）。现金截止：2026-09-02 10:00。目标：可划转人民币现金 ≥ 260,000。\n")
memo.append("\n## 0. 结论先行\n")
memo.append("1. **数据可信度**：官方持仓与交易批次总数量全部一致；证券A 8/28当日买入4,000股确认为冻结、不可当日卖出；OCR有2处低置信字段（A总数量0.62、D名称0.74），已列异常、未驱动卖出股数。券商总资产1,038,800与明细重算净资产1,043,785.82存在 +4,985.82 残差（0.48%），已保留、未虚构补平；对权重判断影响<0.5个百分点，对现金目标无影响。\n")
memo.append("2. **现金目标可达**：9/2 10:00可划转 = 已结算58,000 + 8/31到账旧证券净额17,985.82 + 计划卖出净收入。三套方案分别达到 337,124.85 / 322,075.45 / 389,321.81 元，均 ≥260,000。**但真正紧约束不是现金，而是风险上限**（压力损失≤9%、高波动≤30%、电子≤32%、风险贡献≤55%），它们迫使证券A大幅减仓，从而“顺带”超额满足现金。\n")
memo.append("3. **主要风险冲突（当前全部超限）**：股票仓位92.80%>75%；证券A单票38.81%>27%；高波动(A+D)51.81%>30%；电子(A+D)51.81%>32%；最不利压力损失13.24%>9%；证券A风险贡献58.20%>55%。证券D（电子、年化波动62%）应优先处理但**跌停且买盘为零**；证券C自8/25停牌，账面市值121,600不等于可变现。\n")
memo.append("4. **“保留≥12,000股证券A”与hard约束冲突，不可同时满足**（见§4不可行性证明）。三套方案均无法保留12,000股A；在硬约束下A的最大可行保留量为7,800股。\n")
memo.append("5. **立即可做/暂不可做**：8/31开盘可卖证券A与ETF（正常交易、流动性充足、远未触及5%ADV上限）；**暂不可卖**证券D（跌停无买盘）与证券C（停牌），不得按账面价假设成交。\n")
memo.append("\n## 1. 现金口径（必须区分，只有最后一项计入260,000）\n")
memo.append("| 口径 | 金额(CNY) | 是否计入目标 | 说明 |\n|---|---|---|---|\n")
memo.append("| 账面资产(券商总资产) | 1,038,800.00 | 否 | 汇总值 |\n")
memo.append("| 账面资产(明细重算) | 1,043,785.82 | 否 | 含已确认股息3,750 |\n")
memo.append("| 交易可用资金 | 76,000.00 | 否 | 含未结算卖出款18,000 |\n")
memo.append("| 可划转现金 | 58,000.00 | 是 | 已结算且可划转 |\n")
memo.append("| 计划卖出但尚未成交 | 0.00 | 否 | 不得预先计入 |\n")
memo.append("\n## 2. 现金可达性时间线（以流动性优先方案P2为例）\n")
memo.append("| 日期/时点 | 事件 | 期末可划转 |\n|---|---|---|\n")
memo.append("| 8/28 15:20 | 决策截点 | 58,000.00 |\n")
memo.append("| 8/31 09:00 | 旧证券卖出款结算(净额17,985.82) | 75,985.82 |\n")
memo.append("| 8/31 09:30 | 计划卖出执行(P2卖A 11,000，净246,089.63) | 75,985.82(当日仍不可划转) |\n")
memo.append("| 9/1 09:00 | 8/31卖出款转可划转 | 322,075.45 |\n")
memo.append("| 9/2 10:00 | 用款截止(需≥260,000) | 322,075.45 ✓ |\n")
memo.append("| 9/3 09:00 | 证券B股息3,750到账(晚于截止，不计入) | — |\n")
memo.append("\n## 3. 为满足现金目标的最小卖出测算（二.3）\n")
memo.append("固定最小税费后净收入 = 260,000 − 75,985.82 = **184,014.18**（不随情景变）。最小卖出市值按标的费用+滑点折算：\n")
memo.append("| 成交情景 | 可售标的 | 最小税费后净收入 | 最小卖出市值(收盘价口径) |\n|---|---|---|---|\n")
memo.append(f"| 单卖证券A | 证券A(正常交易) | 184,014.18 | {min_gross['A']:,.2f} |\n")
memo.append(f"| 单卖证券B | 证券B(正常交易) | 184,014.18 | {min_gross['B']:,.2f} |\n")
memo.append(f"| 单卖宽基ETF | ETF(正常交易) | 184,014.18 | {min_gross['ETF']:,.2f} |\n")
memo.append(f"| 单卖证券D | 证券D(仅恢复买盘后) | 184,014.18 | {min_gross['D']:,.2f} |\n")
memo.append("| 单卖证券C | 证券C(停牌，不可卖) | 不适用 | 不适用 |\n")
memo.append(f"| D持续跌停无买盘 | 仅A/B/ETF | 184,014.18 | A={min_gross['A']:,.2f} / ETF={min_gross['ETF']:,.2f} |\n")
memo.append("| D恢复但部分成交 | A/B/ETF + 部分D | 184,014.18 | 按实际成交组合折算 |\n")
memo.append("| 主要可卖证券跌5% | A=21.28 / ETF=3.914 | 184,014.18 | A约8,700股 / ETF约47,100股 |\n")
memo.append("| 主要可卖证券跌10% | A=20.16 / ETF=3.708 | 184,014.18 | A约9,200股 / ETF约49,700股 |\n")
memo.append("\n> 注：以上仅为“现金-only”最小卖出；**风险硬约束要求卖出更多**（证券A须降至≤7,800股），对应净现金246,089.63以上。\n")
memo.append("\n## 4. 保留≥12,000股证券A的不可行性证明\n")
memo.append("若A保留12,000股(市值268,800)：\n")
memo.append("- 满足 高波动≤30%(CON-04，券商口径311,640) → D≤3,173股；\n")
memo.append("- 满足 证券A风险贡献≤55%(CON-07) → D≥5,000股（A与D相关性0.78，减D反而抬高A的风险贡献占比：D=3,100时A风险贡献0.5838>0.55）；\n")
memo.append("- D“≤3,173”与“≥5,000”矛盾 → **无可行D水平，保留12,000股A不可行**。\n")
memo.append("\n## 5. 五类路径说明\n")
memo.append("| 路径 | 适用标的 | 适用条件 | 费用/风险 | 现金贡献 | 结论 |\n|---|---|---|---|---|---|\n")
memo.append("| 持有 | C、B | C停牌；B低波动银行、除息权利已锁定、避免亏损 | 复牌跳空风险(C) | 0 | 维持 |\n")
memo.append("| 加仓 | 无 | 现金缺口下不得加仓 | 不适用 | 负 | 不采用 |\n")
memo.append("| 减仓 | A(必须)、ETF(补足) | A修复单票/高波动/电子/压力/风险贡献；ETF补足现金 | 佣金/印花税/过户费/滑点 | 主来源 | 执行 |\n")
memo.append("| 退出 | D(条件) | D恢复买盘后优先退出 | 实现亏损约3.30元/股 | 恢复买盘后 | 条件执行 |\n")
memo.append("| 做T | 无 | 需先卖底仓再同日回补，价差覆盖双边费用+滑点+0.10%缓冲；日终不增仓 | 双边成本 | 不产生净现金 | 本窗口不采用 |\n")
memo.append("\n## 6. 三套方案（逐笔100股整数倍，含费用/税费/滑点）\n")
memo.append("| 方案 | 交易 | 净现金 | 交易后A | 交易后高波动/电子 | 组合波动 | 压力损失 | 实现损益 |\n|---|---|---|---|---|---|---|---|\n")
memo.append("| 偏好优先 | 卖A10,200 + 卖ETF8,000 | 261,139.03 | 7,800 | 29.82%/29.82% | 20.28% | 9.00% | +13,889.30 |\n")
memo.append("| 流动性优先 | 卖A11,000 | 246,089.63 | 7,000 | 28.10%/28.10% | 20.10% | 8.97% | +13,261.62 |\n")
memo.append("| 风险预算优先 | 卖A11,000 + 卖D5,000(条件) | 313,335.99 | 7,000 | 21.60%/21.60% | 16.61% | 7.35% | −3,260.22 |\n")
memo.append("\n> 偏好优先是“在硬约束下尽量保留A”，实际只能保留7,800股（客户12,000偏好不可行）。风险预算优先的D卖出依赖D恢复买盘，且实现D亏损−16,521.84，但换取最大风险下降。\n")
memo.append("\n## 7. 减仓/退出排序（证据绑定）\n")
memo.append("1. 证券A：必须减仓（单票/高波动/电子/压力/风险贡献全部超限的唯一可执行标的；实现盈利）。\n")
memo.append("2. 证券D：条件退出（风险最高但跌停无买盘，恢复买盘后优先）。\n")
memo.append("3. 证券C：持有（停牌，账面≠可变现，复牌后重评）。\n")
memo.append("4. 宽基ETF：减仓（快速筹现但非风险整改主工具）。\n")
memo.append("5. 证券B：持有（避免卖出，除息口径不构成真实亏损）。\n")
memo.append("\n## 8. 待确认信息（按影响排序）\n")
memo.append("1. **会改变立即动作**：证券D恢复买盘时点（决定是否可执行D退出）；客户是否接受A保留量<12,000（必须为“是”，否则无可行方案）。\n")
memo.append("2. **只影响精度**：证券C复牌时点/复牌价；账户残差+4,985.82构成；OCR低置信字段复核。\n")
memo.append("3. **暂不影响当前方案**：客户个人税务特殊情形；未来入金(已明确为0)。\n")
memo.append("\n## 9. 授权边界\n")
memo.append("不连接券商、不代下单、不撤单、不划款、不融资、不索取账户凭证；不承诺成交价/成交概率/回本/未来收益。所有动作以 `if 条件 → then 动作` 形式给出（见 if_then_rules.csv）；未成交/部分成交/未结算不得记为已完成。\n")
memo.append("\n## 10. 事实/重建/假设/待确认 分类\n")
memo.append("- 已核验事实：官方持仓总数量、交易批次总数量、除息/停牌事件、资金日历、费用规则。\n")
memo.append("- 重建计算：多批次成本、明细净资产、组合波动率、风险贡献、压力损失、三套方案净现金。\n")
memo.append("- 情景假设：D恢复买盘、价格下跌5%/10%、9/1成交结算。\n")
memo.append("- 待确认：D/C交易状态后续、账户残差、OCR低置信、客户soft偏好让步。\n")

with open(os.path.join(OUT, 'liquidity_decision_memo.md'), 'w', encoding='utf-8') as f:
    f.write('\n'.join(s.rstrip('\n') for s in memo))

# ---------------- assumptions manifest ----------------
manifest = dict(
    decision_timestamp="2026-08-28 15:20:00 Asia/Shanghai",
    cash_deadline="2026-09-02 10:00:00 Asia/Shanghai",
    cash_target_cny=260000.00, base_currency="CNY",
    market="China A-share (synthetic, de-identified)",
    nav_bases={
        "broker_statement_total_assets": NAV_BROKER,
        "detail_recomputed_nav": round(NAV_DETAIL, 2),
        "detail_composition": {"securities_mv": 964050.00, "withdrawable_cash": 58000.00,
                               "unsettled_sale_net": 17985.82, "confirmed_dividend_receivable_B": 3750.00},
        "residual_detail_minus_broker": round(NAV_DETAIL - NAV_BROKER, 2),
    },
    fee_and_rules={
        "stock_commission_rate": 0.00025, "stock_commission_min": 5.0, "stock_stamp_sell": 0.0005,
        "stock_transfer_fee": 0.00001, "etf_commission_rate": 0.0002, "etf_commission_min": 5.0,
        "etf_stamp": 0.0, "etf_transfer_fee": 0.0,
        "slippage": {"SYN201.SH": 0.0005, "SYN202.SZ": 0.0006, "SYN203.SH": None,
                     "SYN204.SZ": 0.003, "SYNETF.SH": 0.0002},
        "min_lot": 100, "t_plus_1": True, "adv_cap": 0.05,
        "sell_settlement": "卖出成交款当日可用于交易，下一交易日09:00可划转",
        "slippage_convention": "滑点按 预计成交价=参考价*(1-滑点) 计；佣金/印花税/过户费按滑点后成交金额计",
    },
    constraints=[
        "CON-01 可划转现金>=260,000 (9/2 10:00)", "CON-02 股票及ETF仓位<=0.75 NAV",
        "CON-03 单票权重<=0.27 NAV(两口径)", "CON-04 高波动(A+D)<=0.30 NAV",
        "CON-05 电子(A+D)<=0.32 NAV", "CON-06 最不利压力损失<=0.09 NAV",
        "CON-07 风险贡献占比<=0.55(方差)", "CON-08 单日<=5% ADV",
        "CON-09 可划转现金>=30,000", "CON-10 当日买入当日卖出=0(T+1)"],
    soft_prefs=["PREF-01 A保留>=12,000股(与hard冲突，不可行)", "PREF-02 最小化实现亏损",
                "PREF-03 交易证券数<=3", "PREF-04 避免股息支付前卖B"],
    adopted_records=["全部13张工作表(排除前视记录MKT-A-FUT)"],
    excluded_records=["MKT-A-FUT: 2026-08-31 09:35, 晚于决策时点15:20, 前视信息"],
    unresolved=[
        "证券D恢复买盘时点未知", "证券C复牌时点与复牌价未知",
        "客户是否接受证券A保留量<12,000股(必须为是，否则无可行方案)",
        "账户残差+4,985.82构成未在附件内消除", "OCR: A总数量(0.62)、D名称(0.74)低置信"],
    sha256={},
)

file_hashes = {}
for fname in ['liquidity_decision_memo.md','position_reconciliation.csv','cash_availability_schedule.csv',
              'portfolio_diagnosis.csv','action_priority.csv','scenario_comparison.csv',
              'if_then_rules.csv','stress_scenario_detail.csv']:
    p = os.path.join(OUT, fname)
    if os.path.exists(p):
        file_hashes[fname] = hashlib.sha256(open(p, 'rb').read()).hexdigest()
manifest['sha256'] = file_hashes
manifest['sha256_note'] = ("assumptions_manifest.json 自身SHA-256无法自嵌入(嵌入会改变文件内容，违反可验证性)，"
                           "故其哈希标为 NOT_COMPUTED 并在交付说明中另行给出，不伪造。")

with open(os.path.join(OUT, 'assumptions_manifest.json'), 'w', encoding='utf-8') as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

print("=== generated files ===")
for fname in ['liquidity_decision_memo.md','position_reconciliation.csv','cash_availability_schedule.csv',
              'portfolio_diagnosis.csv','action_priority.csv','scenario_comparison.csv',
              'if_then_rules.csv','stress_scenario_detail.csv','assumptions_manifest.json']:
    p = os.path.join(OUT, fname)
    print(f"{fname}: {os.path.getsize(p)} bytes")
print("min_net =", min_net)
print("min_gross =", min_gross)
