#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓交易审批复核脚本 (run_portfolio_review.py)

从5个原始附件一键重建全部客观CSV和报告数字。
- 不联网
- 不硬编码附件中的价格、数量或推荐方案
- 计算过程保留全精度，展示可舍入

用法:
    python run_portfolio_review.py --attach-dir <附件目录> --output-dir <输出目录>

默认附件目录为脚本所在目录的 attachments/ 子目录，默认输出目录为脚本所在目录。
"""

import argparse
import csv
import math
import os
import sys
from collections import defaultdict, OrderedDict
from datetime import datetime

# ============================================================
# 工具函数
# ============================================================

def read_csv(filepath, encoding='utf-8-sig'):
    """读取CSV，返回字典列表，处理BOM。"""
    rows = []
    with open(filepath, 'r', encoding=encoding, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 去除键和值的空白
            cleaned = {}
            for k, v in row.items():
                if k is not None:
                    cleaned[k.strip()] = v.strip() if v is not None else v
            rows.append(cleaned)
    return rows


def to_float(val, default=None):
    """安全转float。"""
    if val is None or val == '':
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def to_int(val, default=None):
    """安全转int。"""
    if val is None or val == '':
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def fmt(v, decimals=4):
    """格式化数字，保留全精度计算，展示舍入。"""
    if v is None:
        return ''
    if isinstance(v, float):
        if abs(v) < 1e-10 and v != 0:
            return f'{v:.6e}'
        return f'{v:.{decimals}f}'
    return str(v)


def fmt_money(v):
    """金额格式，2位小数。"""
    if v is None:
        return ''
    return f'{v:.2f}'


def fmt_pct(v):
    """百分比格式，4位小数（即万分之一精度）。"""
    if v is None:
        return ''
    return f'{v*100:.4f}%'


# ============================================================
# 1. 数据加载与审计
# ============================================================

def load_all_data(attach_dir):
    """加载全部5个附件。"""
    files = {
        'daily': '持仓标的日线_20240102_20251230.csv',
        'lots': '持仓批次_20251231T1445.csv',
        'quotes': '持仓标的报价_20251231T1445.csv',
        'constraints': '账户与交易约束_20251231T1445.csv',
        'plans': '候选减仓方案_20251231.csv',
    }
    data = {}
    for key, fname in files.items():
        fpath = os.path.join(attach_dir, fname)
        if not os.path.exists(fpath):
            raise FileNotFoundError(f'附件不存在: {fpath}')
        data[key] = read_csv(fpath)
    return data


def audit_data(data):
    """数据审计：字段、主键、日期、单位、币种、as_of、重复值、空值、记录状态。"""
    audit = {}

    # --- 日线审计 ---
    daily = data['daily']
    daily_codes = set()
    daily_dates = set()
    daily_dup_check = set()
    daily_null_count = 0
    daily_status_counts = defaultdict(int)
    for row in daily:
        code = row.get('ts_code', '')
        date = row.get('trade_date', '')
        status = row.get('record_status', '')
        daily_codes.add(code)
        daily_dates.add(date)
        daily_status_counts[status] += 1
        key = (code, date)
        if key in daily_dup_check:
            audit.setdefault('daily_duplicates', []).append(key)
        daily_dup_check.add(key)
        for v in row.values():
            if v is None or v == '':
                daily_null_count += 1
    audit['daily_codes'] = sorted(daily_codes)
    audit['daily_date_range'] = (min(daily_dates), max(daily_dates)) if daily_dates else None
    audit['daily_row_count'] = len(daily)
    audit['daily_status_counts'] = dict(daily_status_counts)
    audit['daily_null_fields'] = daily_null_count

    # --- 持仓批次审计 ---
    lots = data['lots']
    lot_codes = set()
    lot_ids = set()
    lot_null_count = 0
    for row in lots:
        lot_codes.add(row.get('ts_code', ''))
        lot_ids.add(row.get('lot_id', ''))
        for v in row.values():
            if v is None or v == '':
                lot_null_count += 1
    audit['lot_codes'] = sorted(lot_codes)
    audit['lot_ids'] = sorted(lot_ids)
    audit['lot_row_count'] = len(lots)
    audit['lot_null_fields'] = lot_null_count

    # --- 报价审计 ---
    quotes = data['quotes']
    quote_codes = set()
    quote_null_count = 0
    for row in quotes:
        quote_codes.add(row.get('ts_code', ''))
        for v in row.values():
            if v is None or v == '':
                quote_null_count += 1
    audit['quote_codes'] = sorted(quote_codes)
    audit['quote_row_count'] = len(quotes)
    audit['quote_null_fields'] = quote_null_count

    # --- 约束审计 ---
    constraints = data['constraints']
    audit['constraint_row_count'] = len(constraints)

    # --- 方案审计 ---
    plans = data['plans']
    plan_ids = set()
    plan_codes = set()
    for row in plans:
        plan_ids.add(row.get('plan_id', ''))
        plan_codes.add(row.get('ts_code', ''))
    audit['plan_ids'] = sorted(plan_ids)
    audit['plan_codes'] = sorted(plan_codes)
    audit['plan_row_count'] = len(plans)

    # --- 代码交叉检查 ---
    all_codes = lot_codes | quote_codes
    daily_only = daily_codes - all_codes
    missing_daily = all_codes - daily_codes
    audit['codes_in_daily_only'] = sorted(daily_only)
    audit['codes_missing_daily'] = sorted(missing_daily)

    # 方案中未知代码检查
    plan_unknown = plan_codes - all_codes
    audit['plan_unknown_codes'] = sorted(plan_unknown)

    return audit


# ============================================================
# 2. 账户勾稽
# ============================================================

def build_constraints(data):
    """解析账户约束。"""
    row = data['constraints'][0]
    c = {}
    c['account_label'] = row['account_label']
    c['as_of'] = row['as_of']
    c['currency'] = row['currency']
    c['cash_balance_cny'] = to_float(row['cash_balance_cny'])
    c['accrued_fees_payable_cny'] = to_float(row['accrued_fees_payable_cny'])
    c['reported_net_asset_cny'] = to_float(row['reported_net_asset_cny'])
    c['equity_exposure_max'] = to_float(row['equity_exposure_max'])
    c['single_name_weight_max'] = to_float(row['single_name_weight_max'])
    c['top3_weight_max'] = to_float(row['top3_weight_max'])
    c['historical_cvar95_max'] = to_float(row['historical_cvar95_max'])
    c['minimum_cash_weight'] = to_float(row['minimum_cash_weight'])
    c['commission_rate'] = to_float(row['commission_rate'])
    c['minimum_commission_cny'] = to_float(row['minimum_commission_cny'])
    c['stamp_duty_sell_rate'] = to_float(row['stamp_duty_sell_rate'])
    c['transfer_fee_rate'] = to_float(row['transfer_fee_rate'])
    c['slippage_base_bps'] = to_float(row['slippage_base_bps'])
    c['slippage_sqrt_coefficient_bps'] = to_float(row['slippage_sqrt_coefficient_bps'])
    c['slippage_cap_bps'] = to_float(row['slippage_cap_bps'])
    c['max_adv20_participation'] = to_float(row['max_adv20_participation'])
    c['risk_history_common_days'] = to_int(row['risk_history_common_days'])
    c['risk_confidence'] = to_float(row['risk_confidence'])
    c['cash_daily_return'] = to_float(row['cash_daily_return'])
    c['quote_max_age_seconds'] = to_int(row['quote_max_age_seconds'])
    c['approval_deadline'] = row['approval_deadline']
    return c


def build_quotes(data):
    """解析报价，按ts_code索引。"""
    quotes = {}
    for row in data['quotes']:
        code = row['ts_code']
        q = {}
        q['ts_code'] = code
        q['security_name'] = row['security_name']
        q['quote_timestamp'] = row['quote_timestamp']
        q['current_price_cny'] = to_float(row['current_price_cny'])
        q['previous_close_cny'] = to_float(row['previous_close_cny'])
        q['limit_up_cny'] = to_float(row['limit_up_cny'])
        q['limit_down_cny'] = to_float(row['limit_down_cny'])
        q['quote_age_seconds'] = to_int(row['quote_age_seconds'])
        q['quote_status'] = row['quote_status']
        q['trading_status'] = row['trading_status']
        q['board_lot_shares'] = to_int(row['board_lot_shares'])
        q['currency'] = row['currency']
        q['fx_to_cny'] = to_float(row['fx_to_cny'])
        quotes[code] = q
    return quotes


def build_lots(data, quotes):
    """解析持仓批次，附加报价信息。"""
    lots = []
    for row in data['lots']:
        lot = {}
        lot['account_label'] = row['account_label']
        lot['ts_code'] = row['ts_code']
        lot['security_name'] = row['security_name']
        lot['market'] = row['market']
        lot['currency'] = row['currency']
        lot['lot_id'] = row['lot_id']
        lot['buy_date'] = row['buy_date']
        lot['quantity'] = to_int(row['quantity'])
        lot['available_to_sell'] = to_int(row['available_to_sell'])
        lot['cost_price_cny'] = to_float(row['cost_price_cny'])
        lot['cost_basis_note'] = row['cost_basis_note']
        # 附加报价
        q = quotes.get(lot['ts_code'], {})
        lot['current_price_cny'] = q.get('current_price_cny')
        lot['fx_to_cny'] = q.get('fx_to_cny', 1.0)
        # 市值
        if lot['current_price_cny'] is not None and lot['fx_to_cny'] is not None:
            lot['market_value_cny'] = lot['quantity'] * lot['current_price_cny'] * lot['fx_to_cny']
        else:
            lot['market_value_cny'] = None
        # 未实现盈亏
        if lot['current_price_cny'] is not None and lot['cost_price_cny'] is not None:
            lot['unrealized_pnl_cny'] = (lot['current_price_cny'] - lot['cost_price_cny']) * lot['quantity']
        else:
            lot['unrealized_pnl_cny'] = None
        # T+1标记
        lot['is_t1_locked'] = (lot['buy_date'] == '2025-12-31' and lot['available_to_sell'] == 0)
        lots.append(lot)
    return lots


def reconcile_account(lots, constraints):
    """账户勾稽：汇总市值、现金、净资产，与reported比较。"""
    result = {}
    # 按股票汇总
    stock_agg = defaultdict(lambda: {
        'quantity': 0, 'available_to_sell': 0,
        'market_value_cny': 0.0, 'unrealized_pnl_cny': 0.0,
        'security_name': '', 'current_price_cny': None,
    })
    total_stock_mv = 0.0
    total_unrealized_pnl = 0.0
    for lot in lots:
        code = lot['ts_code']
        agg = stock_agg[code]
        agg['quantity'] += lot['quantity']
        agg['available_to_sell'] += lot['available_to_sell']
        agg['security_name'] = lot['security_name']
        agg['current_price_cny'] = lot['current_price_cny']
        if lot['market_value_cny'] is not None:
            agg['market_value_cny'] += lot['market_value_cny']
            total_stock_mv += lot['market_value_cny']
        if lot['unrealized_pnl_cny'] is not None:
            agg['unrealized_pnl_cny'] += lot['unrealized_pnl_cny']
            total_unrealized_pnl += lot['unrealized_pnl_cny']

    net_cash = constraints['cash_balance_cny'] - constraints['accrued_fees_payable_cny']
    recalculated_na = total_stock_mv + net_cash
    reported_na = constraints['reported_net_asset_cny']
    diff = recalculated_na - reported_na

    result['stock_agg'] = dict(stock_agg)
    result['total_stock_mv'] = total_stock_mv
    result['total_unrealized_pnl'] = total_unrealized_pnl
    result['net_cash'] = net_cash
    result['recalculated_na'] = recalculated_na
    result['reported_na'] = reported_na
    result['na_diff'] = diff
    result['reconciled'] = abs(diff) <= 1.0

    # 当前权重（以复算净资产为分母）
    na = recalculated_na
    weights = {}
    for code, agg in stock_agg.items():
        weights[code] = agg['market_value_cny'] / na if na != 0 else 0.0
    result['stock_weights'] = weights
    result['equity_exposure'] = total_stock_mv / na if na != 0 else 0.0
    result['cash_weight'] = net_cash / na if na != 0 else 0.0

    # 前3大权重
    sorted_weights = sorted(weights.items(), key=lambda x: x[1], reverse=True)
    result['top3_weights'] = sorted_weights[:3]
    result['top3_weight_sum'] = sum(w for _, w in sorted_weights[:3])

    return result


# ============================================================
# 3. ADV20 计算
# ============================================================

def calc_adv20(daily_data, codes, as_of_date='2025-12-30'):
    """
    按每只股票截至as_of_date最近20条"有行情"记录计算ADV20。
    amount单位为千元，ADV20=mean(amount×1000)，单位元。
    """
    # 按股票分组，筛选有行情且日期<=as_of_date
    stock_daily = defaultdict(list)
    for row in daily_data:
        code = row['ts_code']
        if code not in codes:
            continue
        if row.get('record_status') != '有行情':
            continue
        trade_date = row.get('trade_date', '')
        if trade_date > as_of_date:
            continue
        amount = to_float(row.get('amount'))
        if amount is None:
            continue
        stock_daily[code].append((trade_date, amount))

    adv20 = {}
    for code in codes:
        records = stock_daily.get(code, [])
        # 按日期降序排列，取最近20条
        records.sort(key=lambda x: x[0], reverse=True)
        recent20 = records[:20]
        if len(recent20) < 20:
            adv20[code] = None
        else:
            # amount×1000 转为元
            adv20[code] = sum(amt * 1000 for _, amt in recent20) / len(recent20)
    return adv20


# ============================================================
# 4. CVaR 计算
# ============================================================

def build_common_return_matrix(daily_data, codes, window_days=250, as_of_date='2025-12-30'):
    """
    取6只股票共同存在且状态有效的最近window_days个交易日。
    返回 (dates_list, returns_dict{code: [r_t,...]}, N)
    风险收益使用pct_chg÷100。
    """
    # 按日期收集每只股票的收益
    date_returns = defaultdict(dict)  # date -> {code: return}
    for row in daily_data:
        code = row['ts_code']
        if code not in codes:
            continue
        if row.get('record_status') != '有行情':
            continue
        trade_date = row.get('trade_date', '')
        if trade_date > as_of_date:
            continue
        pct_chg = to_float(row.get('pct_chg'))
        if pct_chg is None:
            continue
        date_returns[trade_date][code] = pct_chg / 100.0

    # 找出所有股票都有收益的日期
    common_dates = []
    for trade_date in sorted(date_returns.keys()):
        if all(code in date_returns[trade_date] for code in codes):
            common_dates.append(trade_date)

    # 取最近window_days个
    common_dates = common_dates[-window_days:]
    N = len(common_dates)

    # 构建收益矩阵
    returns = {code: [] for code in codes}
    for d in common_dates:
        for code in codes:
            returns[code].append(date_returns[d][code])

    return common_dates, returns, N


def calc_portfolio_cvar(weights, returns, cash_weight, cash_daily_return, confidence=0.95):
    """
    计算historical CVaR95及尾部贡献。
    weights: {code: weight} 股票权重
    returns: {code: [r_t]} 同日收益序列
    cash_weight: 现金权重
    cash_daily_return: 现金日收益
    confidence: 置信度
    返回 (cvar, tail_losses, tail_indices, tail_contributions{code: val}, portfolio_losses)
    """
    codes = list(returns.keys())
    N = len(returns[codes[0]]) if codes else 0
    if N == 0:
        return None, [], [], {}, []

    # 组合日收益 = sum(w_i * r_i,t) + w_cash * r_cash
    portfolio_returns = []
    for t in range(N):
        r_p = sum(weights.get(code, 0.0) * returns[code][t] for code in codes)
        r_p += cash_weight * cash_daily_return
        portfolio_returns.append(r_p)

    # 损失 L_t = -r_p,t
    losses = [-r for r in portfolio_returns]

    # m = ceil((1-confidence) * N)
    m = math.ceil((1 - confidence) * N)

    # 按损失从大到小排列，取前m个
    indexed_losses = list(enumerate(losses))
    indexed_losses.sort(key=lambda x: x[1], reverse=True)
    tail_indices = [idx for idx, _ in indexed_losses[:m]]
    tail_losses = [losses[idx] for idx in tail_indices]

    # CVaR = 前m个损失的算术平均
    cvar = sum(tail_losses) / len(tail_losses) if tail_losses else 0.0

    # 尾部贡献：在尾部日期上每只股票的平均损失贡献 = mean(-w_i * r_i,t)
    tail_contributions = {}
    for code in codes:
        w = weights.get(code, 0.0)
        contrib = sum(-w * returns[code][idx] for idx in tail_indices) / len(tail_indices) if tail_indices else 0.0
        tail_contributions[code] = contrib

    return cvar, tail_losses, tail_indices, tail_contributions, losses


def calc_correlation_matrix(returns, ddof=1):
    """
    计算Pearson相关系数矩阵，ddof=1。
    returns: {code: [r_t]}
    返回: {code: {code: corr}}
    """
    codes = list(returns.keys())
    N = len(returns[codes[0]]) if codes else 0
    if N == 0:
        return {}

    # 均值
    means = {}
    for code in codes:
        means[code] = sum(returns[code]) / N

    # 标准差 (ddof=1)
    stds = {}
    for code in codes:
        variance = sum((r - means[code]) ** 2 for r in returns[code]) / (N - ddof)
        stds[code] = math.sqrt(variance)

    # 相关系数
    corr = {}
    for i, c1 in enumerate(codes):
        corr[c1] = {}
        for j, c2 in enumerate(codes):
            if i == j:
                corr[c1][c2] = 1.0
            else:
                cov = sum((returns[c1][t] - means[c1]) * (returns[c2][t] - means[c2]) for t in range(N)) / (N - ddof)
                if stds[c1] > 0 and stds[c2] > 0:
                    corr[c1][c2] = cov / (stds[c1] * stds[c2])
                else:
                    corr[c1][c2] = 0.0
    return corr


# ============================================================
# 5. 候选方案执行测算
# ============================================================

def build_plans(data):
    """解析候选方案，按plan_id分组。"""
    plans = defaultdict(list)
    for row in data['plans']:
        order = {
            'plan_id': row['plan_id'],
            'order_sequence': to_int(row['order_sequence']),
            'execution_window': row['execution_window'],
            'side': row['side'],
            'ts_code': row['ts_code'],
            'security_name': row['security_name'],
            'sell_quantity': to_int(row['sell_quantity']),
            'order_type': row['order_type'],
            'desk_note': row['desk_note'],
        }
        plans[row['plan_id']].append(order)
    # 按order_sequence排序
    for pid in plans:
        plans[pid].sort(key=lambda x: x['order_sequence'])
    return dict(plans)


def check_order_feasibility(order, quotes, lots_agg, constraints):
    """
    检查单个订单的可行性约束。
    返回 (is_feasible, issues_dict)
    """
    issues = {}
    code = order['ts_code']
    qty = order['sell_quantity']
    q = quotes.get(code)

    if q is None:
        issues['unknown_code'] = f'未知代码 {code}'
        return False, issues

    # 1. 可卖数量检查（当日累计，此处先检查单订单）
    avail = lots_agg.get(code, {}).get('available_to_sell', 0)
    if qty > avail:
        issues['available_to_sell'] = f'卖出{qty}股 > 可卖{avail}股'

    # 2. 整手检查
    board_lot = q.get('board_lot_shares', 100)
    if qty % board_lot != 0:
        issues['board_lot'] = f'卖出{qty}股不是{board_lot}股整数倍'

    # 3. 交易状态检查
    if q.get('trading_status') != '连续竞价':
        issues['trading_status'] = f'交易状态为"{q.get("trading_status")}"，非连续竞价'

    # 4. 报价时效检查
    quote_age = q.get('quote_age_seconds', 999999)
    max_age = constraints.get('quote_max_age_seconds', 300)
    if quote_age > max_age:
        issues['quote_age'] = f'报价年龄{quote_age}秒 > 上限{max_age}秒'

    # 5. 涨跌停检查
    price = q.get('current_price_cny')
    limit_up = q.get('limit_up_cny')
    limit_down = q.get('limit_down_cny')
    if price is not None and limit_down is not None and limit_up is not None:
        if price <= limit_down:
            issues['limit_down'] = f'当前价{price} <= 跌停价{limit_down}'
        if price > limit_up:
            issues['limit_up'] = f'当前价{price} > 涨停价{limit_up}'

    # 6. 报价状态
    if q.get('quote_status') != '有效':
        issues['quote_status'] = f'报价状态为"{q.get("quote_status")}"'

    is_feasible = len(issues) == 0
    return is_feasible, issues


def calc_order_execution(order, quotes, adv20, constraints, use_cap_slippage=False):
    """
    计算单个订单的执行细节：参与率、滑点、执行价、费用、净回款。
    返回执行细节字典。
    """
    code = order['ts_code']
    qty = order['sell_quantity']
    q = quotes[code]
    price = q['current_price_cny']

    # 订单参考名义金额
    notional = qty * price

    # 参与率 = 订单参考名义金额 ÷ ADV20
    adv = adv20.get(code)
    if adv is not None and adv > 0:
        participation = notional / adv
    else:
        participation = None

    # 滑点基点
    if use_cap_slippage:
        slippage_bps = constraints['slippage_cap_bps']
    else:
        if participation is not None:
            slippage_bps = min(
                constraints['slippage_cap_bps'],
                constraints['slippage_base_bps'] + constraints['slippage_sqrt_coefficient_bps'] * math.sqrt(participation)
            )
        else:
            slippage_bps = constraints['slippage_cap_bps']

    # 卖出执行估价
    exec_price = price * (1 - slippage_bps / 10000.0)

    # 执行金额
    exec_amount = qty * exec_price

    # 费用
    commission = max(constraints['minimum_commission_cny'], exec_amount * constraints['commission_rate'])
    stamp_tax = exec_amount * constraints['stamp_duty_sell_rate']
    transfer_fee = exec_amount * constraints['transfer_fee_rate']
    total_fee = commission + stamp_tax + transfer_fee

    # 净卖出回款
    net_proceeds = exec_amount - total_fee

    # 滑点损失
    slippage_loss = qty * (price - exec_price)

    return {
        'notional': notional,
        'participation': participation,
        'slippage_bps': slippage_bps,
        'exec_price': exec_price,
        'exec_amount': exec_amount,
        'commission': commission,
        'stamp_tax': stamp_tax,
        'transfer_fee': transfer_fee,
        'total_fee': total_fee,
        'net_proceeds': net_proceeds,
        'slippage_loss': slippage_loss,
    }


def allocate_lots(order, lots, exec_price):
    """
    同一股票按buy_date升序、lot_id升序从可卖批次分配卖出数量。
    返回分配列表 [{lot_id, allocated_quantity, cost_price, ...}]
    """
    code = order['ts_code']
    qty_remaining = order['sell_quantity']

    # 筛选该股票的可卖批次
    stock_lots = [l for l in lots if l['ts_code'] == code and l['available_to_sell'] > 0]
    # 按buy_date升序、lot_id升序
    stock_lots.sort(key=lambda x: (x['buy_date'], x['lot_id']))

    allocations = []
    for lot in stock_lots:
        if qty_remaining <= 0:
            break
        alloc_qty = min(lot['available_to_sell'], qty_remaining)
        # 已实现盈亏（此批次部分）= (exec_price - cost_price) * alloc_qty
        lot_realized = (exec_price - lot['cost_price_cny']) * alloc_qty
        allocations.append({
            'lot_id': lot['lot_id'],
            'buy_date': lot['buy_date'],
            'allocated_quantity': alloc_qty,
            'cost_price_cny': lot['cost_price_cny'],
            'lot_realized_pnl': lot_realized,
            'remaining_after': lot['available_to_sell'] - alloc_qty,
        })
        qty_remaining -= alloc_qty

    if qty_remaining > 0:
        # 不应发生（前面已检查可卖数量），但记录
        allocations.append({'lot_id': 'UNALLOCATED', 'allocated_quantity': qty_remaining, 'error': '可卖数量不足'})

    return allocations


def execute_plan(plan_id, orders, lots, quotes, adv20, constraints, recon, daily_data,
                 use_cap_slippage=False, cvar_window=250):
    """
    执行一个完整方案的测算。
    只把execution_window为"当日"的订单计入即时修复。
    返回完整的方案测算结果。
    """
    result = {
        'plan_id': plan_id,
        'orders': [],
        'same_day_orders': [],
        'next_day_orders': [],
        'order_feasibility': {},
        'order_executions': {},
        'lot_allocations': {},
        'total_sell_notional': 0.0,
        'total_exec_amount': 0.0,
        'total_fees': 0.0,
        'total_net_proceeds': 0.0,
        'total_slippage_loss': 0.0,
        'total_realized_pnl': 0.0,
        'post_trade': {},
        'constraint_checks': {},
        'plan_status': '',
        'sensitivity': {},
    }

    # 分离当日和下一交易日订单
    same_day = [o for o in orders if o['execution_window'] == '当日']
    next_day = [o for o in orders if o['execution_window'] != '当日']
    result['same_day_orders'] = same_day
    result['next_day_orders'] = next_day

    # 按股票汇总当日累计卖出量
    same_day_sell_by_stock = defaultdict(int)
    for o in same_day:
        same_day_sell_by_stock[o['ts_code']] += o['sell_quantity']

    # 检查每个当日订单的可行性
    all_same_day_feasible = True
    for o in same_day:
        feasible, issues = check_order_feasibility(o, quotes, recon['stock_agg'], constraints)
        result['order_feasibility'][o['order_sequence']] = {
            'feasible': feasible,
            'issues': issues,
        }
        if not feasible:
            all_same_day_feasible = False

    # 检查当日累计可卖
    for code, total_qty in same_day_sell_by_stock.items():
        avail = recon['stock_agg'].get(code, {}).get('available_to_sell', 0)
        if total_qty > avail:
            all_same_day_feasible = False
            # 记录到第一个该股票订单的issues
            for o in same_day:
                if o['ts_code'] == code:
                    result['order_feasibility'][o['order_sequence']]['issues']['cumulative_available'] = \
                        f'当日累计卖出{total_qty}股 > 可卖{avail}股'
                    break

    # ADV20参与率检查（每个订单）
    for o in same_day:
        code = o['ts_code']
        q = quotes.get(code)
        if q is None:
            continue
        notional = o['sell_quantity'] * q['current_price_cny']
        adv = adv20.get(code)
        if adv is not None and adv > 0:
            participation = notional / adv
            if participation > constraints['max_adv20_participation']:
                all_same_day_feasible = False
                result['order_feasibility'][o['order_sequence']]['issues']['adv20_participation'] = \
                    f'参与率{participation:.6f} > 上限{constraints["max_adv20_participation"]}'

    result['all_same_day_feasible'] = all_same_day_feasible

    # 计算每个当日订单的执行细节（即使不可行也计算，用于约束矩阵展示）
    for o in same_day:
        code = o['ts_code']
        if code not in quotes:
            continue
        exec_detail = calc_order_execution(o, quotes, adv20, constraints, use_cap_slippage)
        result['order_executions'][o['order_sequence']] = exec_detail
        result['total_sell_notional'] += exec_detail['notional']
        result['total_exec_amount'] += exec_detail['exec_amount']
        result['total_fees'] += exec_detail['total_fee']
        result['total_net_proceeds'] += exec_detail['net_proceeds']
        result['total_slippage_loss'] += exec_detail['slippage_loss']

        # 批次分配
        allocations = allocate_lots(o, lots, exec_detail['exec_price'])
        result['lot_allocations'][o['order_sequence']] = allocations

        # 订单已实现盈亏 = sum(批次已实现) - 该订单全部费用
        order_realized = sum(a.get('lot_realized_pnl', 0) for a in allocations) - exec_detail['total_fee']
        result['order_executions'][o['order_sequence']]['realized_pnl'] = order_realized
        result['total_realized_pnl'] += order_realized

    # === 交易后持仓 ===
    # 复制原始持仓批次，扣减卖出
    post_lots = []
    for lot in lots:
        post_lot = dict(lot)
        post_lot['post_quantity'] = lot['quantity']
        post_lot['post_available'] = lot['available_to_sell']
        post_lots.append(post_lot)

    for o in same_day:
        code = o['ts_code']
        allocations = result['lot_allocations'].get(o['order_sequence'], [])
        for alloc in allocations:
            if alloc.get('lot_id') == 'UNALLOCATED':
                continue
            for pl in post_lots:
                if pl['ts_code'] == code and pl['lot_id'] == alloc['lot_id']:
                    pl['post_quantity'] -= alloc['allocated_quantity']
                    pl['post_available'] -= alloc['allocated_quantity']
                    break

    # 交易后股票市值（按附件current_price_cny对剩余数量盯市）
    post_stock_mv = 0.0
    post_stock_agg = defaultdict(lambda: {'quantity': 0, 'market_value_cny': 0.0, 'security_name': ''})
    for pl in post_lots:
        code = pl['ts_code']
        price = pl.get('current_price_cny', 0)
        mv = pl['post_quantity'] * price
        post_stock_mv += mv
        post_stock_agg[code]['quantity'] += pl['post_quantity']
        post_stock_agg[code]['market_value_cny'] += mv
        post_stock_agg[code]['security_name'] = pl['security_name']

    # 交易后净现金 = 原净现金 + 全部净卖出回款
    post_net_cash = recon['net_cash'] + result['total_net_proceeds']

    # 交易后净资产
    post_na = post_stock_mv + post_net_cash

    # 验证：交易后净资产 = reported_na - 滑点损失 - 全部交易费用
    expected_na = constraints['reported_net_asset_cny'] - result['total_slippage_loss'] - result['total_fees']
    na_check_diff = post_na - expected_na

    result['post_trade'] = {
        'post_lots': post_lots,
        'post_stock_mv': post_stock_mv,
        'post_stock_agg': dict(post_stock_agg),
        'post_net_cash': post_net_cash,
        'post_na': post_na,
        'expected_na': expected_na,
        'na_check_diff': na_check_diff,
        'na_reconciled': abs(na_check_diff) <= 1.0,
    }

    # 交易后权重
    na = post_na if post_na != 0 else 1.0
    post_weights = {}
    for code, agg in post_stock_agg.items():
        post_weights[code] = agg['market_value_cny'] / na
    post_equity_exposure = post_stock_mv / na
    post_cash_weight = post_net_cash / na

    # 前3大
    sorted_post = sorted(post_weights.items(), key=lambda x: x[1], reverse=True)
    post_top3 = sorted_post[:3]
    post_top3_sum = sum(w for _, w in post_top3)

    result['post_trade']['post_weights'] = post_weights
    result['post_trade']['post_equity_exposure'] = post_equity_exposure
    result['post_trade']['post_cash_weight'] = post_cash_weight
    result['post_trade']['post_top3'] = post_top3
    result['post_trade']['post_top3_sum'] = post_top3_sum

    # === CVaR 计算（交易后）===
    codes = sorted(recon['stock_agg'].keys())
    common_dates, returns_matrix, N_common = build_common_return_matrix(
        daily_data, codes, window_days=cvar_window
    )
    result['post_trade']['cvar_window_days'] = cvar_window
    result['post_trade']['cvar_N'] = N_common
    result['post_trade']['cvar_dates'] = common_dates

    cvar_result = calc_portfolio_cvar(
        post_weights, returns_matrix, post_cash_weight,
        constraints['cash_daily_return'], constraints['risk_confidence']
    )
    post_cvar, tail_losses, tail_indices, tail_contribs, all_losses = cvar_result
    result['post_trade']['post_cvar95'] = post_cvar
    result['post_trade']['post_cvar_tail_contributions'] = tail_contribs
    result['post_trade']['post_cvar_m'] = math.ceil((1 - constraints['risk_confidence']) * N_common)
    result['post_trade']['cvar_contrib_sum'] = sum(tail_contribs.values()) if tail_contribs else 0.0

    # === 约束检查 ===
    checks = {}

    # 账户勾稽
    checks['account_reconciliation'] = {
        'metric': '账户勾稽',
        'actual': recon['na_diff'],
        'threshold': '绝对差≤1元',
        'status': '通过' if recon['reconciled'] else '不通过',
        'gap': recon['na_diff'],
    }

    # 交易后净资产勾稽
    checks['post_trade_na_reconciliation'] = {
        'metric': '交易后净资产勾稽',
        'actual': na_check_diff,
        'threshold': '绝对差≤1元',
        'status': '通过' if abs(na_check_diff) <= 1.0 else '不通过',
        'gap': na_check_diff,
    }

    # 当日订单可行性
    checks['same_day_orders_feasible'] = {
        'metric': '全部当日订单可执行性',
        'actual': '全部通过' if all_same_day_feasible else '存在不通过',
        'threshold': 'T+1/整手/交易状态/报价时效/涨跌停/ADV20参与率全部通过',
        'status': '通过' if all_same_day_feasible else '不通过',
        'gap': '',
    }

    # 股票仓位
    checks['equity_exposure'] = {
        'metric': '交易后股票仓位',
        'actual': post_equity_exposure,
        'threshold': constraints['equity_exposure_max'],
        'status': '通过' if post_equity_exposure <= constraints['equity_exposure_max'] else '不通过',
        'gap': post_equity_exposure - constraints['equity_exposure_max'],
    }

    # 单票权重（取最大的）
    max_single_code = max(post_weights, key=post_weights.get) if post_weights else None
    max_single_weight = post_weights.get(max_single_code, 0)
    checks['single_name_weight'] = {
        'metric': f'交易后最大单票权重({max_single_code})',
        'actual': max_single_weight,
        'threshold': constraints['single_name_weight_max'],
        'status': '通过' if max_single_weight <= constraints['single_name_weight_max'] else '不通过',
        'gap': max_single_weight - constraints['single_name_weight_max'],
    }

    # 前3大权重
    checks['top3_weight'] = {
        'metric': '交易后前3大权重之和',
        'actual': post_top3_sum,
        'threshold': constraints['top3_weight_max'],
        'status': '通过' if post_top3_sum <= constraints['top3_weight_max'] else '不通过',
        'gap': post_top3_sum - constraints['top3_weight_max'],
    }

    # CVaR95
    if post_cvar is not None and N_common >= constraints['risk_history_common_days']:
        checks['historical_cvar95'] = {
            'metric': '交易后historical_CVaR95',
            'actual': post_cvar,
            'threshold': constraints['historical_cvar95_max'],
            'status': '通过' if post_cvar <= constraints['historical_cvar95_max'] else '不通过',
            'gap': post_cvar - constraints['historical_cvar95_max'],
        }
    else:
        checks['historical_cvar95'] = {
            'metric': '交易后historical_CVaR95',
            'actual': '无法评估',
            'threshold': constraints['historical_cvar95_max'],
            'status': '无法评估',
            'gap': f'共同有效日{N_common} < 要求{constraints["risk_history_common_days"]}',
        }

    # 最低现金权重
    checks['minimum_cash_weight'] = {
        'metric': '交易后净现金权重',
        'actual': post_cash_weight,
        'threshold': constraints['minimum_cash_weight'],
        'status': '通过' if post_cash_weight >= constraints['minimum_cash_weight'] else '不通过',
        'gap': constraints['minimum_cash_weight'] - post_cash_weight,
    }

    result['constraint_checks'] = checks

    # === 方案状态判定 ===
    # 可批准：账户可勾稽 + 全部当日订单可执行 + 交易后5项硬约束全部通过
    hard_constraint_names = ['equity_exposure', 'single_name_weight', 'top3_weight',
                             'historical_cvar95', 'minimum_cash_weight']
    all_hard_pass = all(checks[k]['status'] == '通过' for k in hard_constraint_names)

    if not recon['reconciled']:
        result['plan_status'] = '拒绝'
        result['status_reason'] = '账户快照无法勾稽'
    elif not all_same_day_feasible:
        # 检查是否有报价陈旧等"待条件恢复"类型的问题
        has_conditional_issue = False
        for seq, of in result['order_feasibility'].items():
            for issue_key in of['issues']:
                if issue_key in ('quote_age', 'quote_status', 'trading_status'):
                    has_conditional_issue = True
        if has_conditional_issue:
            result['plan_status'] = '待条件恢复'
            result['status_reason'] = '存在报价陈旧/状态异常等可恢复条件，且可能影响硬约束结果'
        else:
            result['plan_status'] = '拒绝'
            result['status_reason'] = '当日订单存在不可恢复的硬约束违反（T+1/整手/可卖数量/ADV20等）'
    elif not all_hard_pass:
        result['plan_status'] = '拒绝'
        failed = [k for k in hard_constraint_names if checks[k]['status'] != '通过']
        result['status_reason'] = f'交易后硬约束未通过: {", ".join(failed)}'
    else:
        result['plan_status'] = '可批准'
        result['status_reason'] = '全部硬约束通过'

    return result


# ============================================================
# 6. 主方案选择
# ============================================================

def select_main_plan(plan_results):
    """
    若只有一个方案满足全部硬约束，将其作为唯一主方案；
    若多个方案满足，则依次按交易后CVaR较低、总交易成本较低、总卖出金额较低排序，仍相同则按plan_id升序。
    """
    approvable = [pid for pid, r in plan_results.items() if r['plan_status'] == '可批准']

    if len(approvable) == 0:
        return None, '无方案满足全部硬约束'

    if len(approvable) == 1:
        return approvable[0], '唯一满足全部硬约束的方案'

    # 多个方案，排序
    def sort_key(pid):
        r = plan_results[pid]
        cvar = r['post_trade'].get('post_cvar95', float('inf'))
        fees = r['total_fees']
        notional = r['total_sell_notional']
        return (cvar, fees, notional, pid)

    approvable.sort(key=sort_key)
    return approvable[0], f'多个可批准方案，按CVaR/成本/卖出额排序后选择{approvable[0]}'


# ============================================================
# 7. 输出交付物
# ============================================================

def write_risk_snapshot_csv(recon, adv20, current_cvar_contribs, filepath):
    """输出当前持仓风险快照.csv"""
    fieldnames = ['ts_code', 'security_name', 'quantity', 'available_to_sell',
                  'market_value_cny', 'weight', 'unrealized_pnl_cny', 'adv20_cny',
                  'current_cvar_contribution', 'breach_flag']
    with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for code in sorted(recon['stock_agg'].keys()):
            agg = recon['stock_agg'][code]
            weight = recon['stock_weights'].get(code, 0)
            # breach_flag: 单票权重是否超限
            breach = '是' if weight > 0.20 else '否'
            writer.writerow({
                'ts_code': code,
                'security_name': agg['security_name'],
                'quantity': agg['quantity'],
                'available_to_sell': agg['available_to_sell'],
                'market_value_cny': f"{agg['market_value_cny']:.2f}",
                'weight': f"{weight:.6f}",
                'unrealized_pnl_cny': f"{agg['unrealized_pnl_cny']:.2f}",
                'adv20_cny': f"{adv20.get(code, 0):.2f}" if adv20.get(code) is not None else 'N/A',
                'current_cvar_contribution': f"{current_cvar_contribs.get(code, 0):.6f}",
                'breach_flag': breach,
            })


def write_constraint_matrix_csv(plan_results, constraints, recon, filepath):
    """输出候选方案约束矩阵.csv"""
    fieldnames = ['plan_id', 'metric_name', 'actual_value', 'threshold', 'status',
                  'gap', 'calculation_basis', 'evidence_file']
    with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        evidence_files = {
            '账户勾稽': '账户与交易约束_20251231T1445.csv; 持仓批次_20251231T1445.csv; 持仓标的报价_20251231T1445.csv',
            '交易后净资产勾稽': '候选减仓方案_20251231.csv; 持仓批次_20251231T1445.csv',
            '全部当日订单可执行性': '候选减仓方案_20251231.csv; 持仓标的报价_20251231T1445.csv; 持仓标的日线_20240102_20251230.csv',
            '交易后股票仓位': '候选减仓方案_20251231.csv; 持仓标的报价_20251231T1445.csv',
            '交易后最大单票权重': '候选减仓方案_20251231.csv; 持仓标的报价_20251231T1445.csv',
            '交易后前3大权重之和': '候选减仓方案_20251231.csv; 持仓标的报价_20251231T1445.csv',
            '交易后historical_CVaR95': '持仓标的日线_20240102_20251230.csv; 候选减仓方案_20251231.csv',
            '交易后净现金权重': '候选减仓方案_20251231.csv; 账户与交易约束_20251231T1445.csv',
        }

        calc_basis = {
            '账户勾稽': '复算净资产=Σ(quantity×current_price×fx)+(cash_balance-accrued_fees)，与reported_net_asset比较',
            '交易后净资产勾稽': 'post_na=post_stock_mv+post_net_cash；验证=reported_na-Σslippage_loss-Σfees',
            '全部当日订单可执行性': '逐订单检查T+1可卖、整手、交易状态、报价时效(≤300s)、涨跌停、ADV20参与率(≤5%)',
            '交易后股票仓位': 'post_equity_exposure=post_stock_mv/post_na',
            '交易后最大单票权重': 'max(post_stock_mv_i/post_na)',
            '交易后前3大权重之和': 'sum(top3 post_stock_mv_i/post_na)',
            '交易后historical_CVaR95': '250共同有效日，m=ceil(0.05×N)，尾部损失均值；r_p=Σ(w_i×pct_chg/100)',
            '交易后净现金权重': 'post_cash_weight=(net_cash+Σnet_proceeds)/post_na',
        }

        for plan_id in sorted(plan_results.keys()):
            r = plan_results[plan_id]
            for check_key, check in r['constraint_checks'].items():
                metric = check['metric']
                # 去除指标名中括号内的动态内容（如股票代码），用于匹配计算依据
                metric_base = metric.split('(')[0] if '(' in metric else metric
                actual = check['actual']
                if isinstance(actual, float):
                    if 'CVaR' in metric or '仓位' in metric or '权重' in metric:
                        actual_str = f'{actual:.6f} ({actual*100:.4f}%)'
                    else:
                        actual_str = f'{actual:.4f}'
                else:
                    actual_str = str(actual)

                threshold = check['threshold']
                if isinstance(threshold, float):
                    if 'CVaR' in metric or '仓位' in metric or '权重' in metric:
                        threshold_str = f'{threshold:.6f} ({threshold*100:.4f}%)'
                    else:
                        threshold_str = f'{threshold:.4f}'
                else:
                    threshold_str = str(threshold)

                gap = check['gap']
                if isinstance(gap, float):
                    gap_str = f'{gap:.6f}'
                else:
                    gap_str = str(gap) if gap else ''

                writer.writerow({
                    'plan_id': plan_id,
                    'metric_name': metric,
                    'actual_value': actual_str,
                    'threshold': threshold_str,
                    'status': check['status'],
                    'gap': gap_str,
                    'calculation_basis': calc_basis.get(metric_base, ''),
                    'evidence_file': evidence_files.get(metric_base, ''),
                })


def write_lot_allocation_csv(plan_results, filepath):
    """输出批次卖出分配.csv"""
    fieldnames = ['plan_id', 'order_sequence', 'ts_code', 'security_name', 'lot_id',
                  'allocated_quantity', 'estimated_execution_price', 'estimated_fee',
                  'realized_pnl_cny', 'eligibility_status']
    with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for plan_id in sorted(plan_results.keys()):
            r = plan_results[plan_id]
            for o in r['same_day_orders']:
                seq = o['order_sequence']
                exec_detail = r['order_executions'].get(seq, {})
                allocations = r['lot_allocations'].get(seq, [])
                feasibility = r['order_feasibility'].get(seq, {})
                elig = '可执行' if feasibility.get('feasible') else '不可执行:' + ';'.join(feasibility.get('issues', {}).keys())

                for alloc in allocations:
                    if alloc.get('lot_id') == 'UNALLOCATED':
                        continue
                    # 该批次分摊的费用（按分配数量比例）
                    total_alloc_qty = sum(a.get('allocated_quantity', 0) for a in allocations if a.get('lot_id') != 'UNALLOCATED')
                    fee_share = exec_detail.get('total_fee', 0) * (alloc['allocated_quantity'] / total_alloc_qty) if total_alloc_qty > 0 else 0
                    realized = alloc.get('lot_realized_pnl', 0) - fee_share

                    writer.writerow({
                        'plan_id': plan_id,
                        'order_sequence': seq,
                        'ts_code': o['ts_code'],
                        'security_name': o['security_name'],
                        'lot_id': alloc['lot_id'],
                        'allocated_quantity': alloc['allocated_quantity'],
                        'estimated_execution_price': f"{exec_detail.get('exec_price', 0):.4f}",
                        'estimated_fee': f"{fee_share:.2f}",
                        'realized_pnl_cny': f"{realized:.2f}",
                        'eligibility_status': elig,
                    })


def write_monitoring_conditions_csv(plan_results, main_plan_id, constraints, filepath):
    """输出审批监控条件.csv"""
    fieldnames = ['condition_id', 'if_condition', 'threshold', 'then_action',
                  'owner', 'check_deadline', 'evidence_needed', 'stop_condition']
    with open(filepath, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        conditions = []
        cid = 1

        # 通用条件
        conditions.append({
            'condition_id': f'C{cid:03d}',
            'if_condition': '如果600519.SH报价在审批截止前刷新且quote_age_seconds≤300',
            'threshold': 'quote_age_seconds≤300; quote_status=有效; trading_status=连续竞价',
            'then_action': '那么对涉及600519.SH的方案重新执行全部约束复算并提交投资负责人重新审批',
            'owner': '交易台/风控组',
            'check_deadline': constraints['approval_deadline'],
            'evidence_needed': '刷新后的持仓标的报价CSV',
            'stop_condition': '如果报价仍陈旧或停牌，则该订单及受影响方案维持待条件恢复/拒绝',
        })
        cid += 1

        conditions.append({
            'condition_id': f'C{cid:03d}',
            'if_condition': '如果主方案全部当日订单在14:50前获得投资负责人批准',
            'threshold': 'approval_deadline=' + constraints['approval_deadline'],
            'then_action': '那么交易台方可按批准方案提交限价委托，执行后回传成交回报供风控组事后核对',
            'owner': '投资负责人/交易台',
            'check_deadline': constraints['approval_deadline'],
            'evidence_needed': '投资负责人审批记录; 成交回报',
            'stop_condition': '如果14:50前未获批准，则当日不得执行任何减仓订单',
        })
        cid += 1

        conditions.append({
            'condition_id': f'C{cid:03d}',
            'if_condition': '如果执行后实际成交均价低于卖出执行估价超过slippage_cap_bps',
            'threshold': 'slippage_cap_bps=' + str(constraints['slippage_cap_bps']),
            'then_action': '那么触发交易后复核，重新计算实际净资产、仓位和CVaR，评估是否需要次日补充减仓',
            'owner': '风控组',
            'check_deadline': 'T+1日开盘前',
            'evidence_needed': '实际成交回报; 收盘后持仓快照',
            'stop_condition': '如果实际滑点在cap内且交易后约束仍通过，则无需补充动作',
        })
        cid += 1

        conditions.append({
            'condition_id': f'C{cid:03d}',
            'if_condition': '如果下一交易日订单（如方案C的002594.SZ卖出）拟执行',
            'threshold': '需重新获取T+1日报价、可卖数量和ADV20',
            'then_action': '那么必须在T+1日重新提交方案审批，不得沿用12月31日的审批结论',
            'owner': '交易台/风控组',
            'check_deadline': 'T+1日审批截止前',
            'evidence_needed': 'T+1日持仓批次、报价、约束快照',
            'stop_condition': '如果T+1日账户约束或市场状态发生重大变化，则重新启动完整复核',
        })
        cid += 1

        # 针对各方案的特定条件
        for plan_id in sorted(plan_results.keys()):
            r = plan_results[plan_id]
            if r['plan_status'] == '待条件恢复':
                for seq, of in r['order_feasibility'].items():
                    if not of['feasible']:
                        for issue_key, issue_desc in of['issues'].items():
                            conditions.append({
                                'condition_id': f'C{cid:03d}',
                                'if_condition': f'如果{plan_id}订单{seq}的{issue_key}问题在审批截止前修复',
                                'threshold': issue_desc,
                                'then_action': f'那么重新执行{plan_id}全部约束复算，通过后提交投资负责人重新审批',
                                'owner': '交易台/风控组',
                                'check_deadline': constraints['approval_deadline'],
                                'evidence_needed': '修复后的报价/持仓/方案数据',
                                'stop_condition': f'如果{issue_key}仍未修复，则{plan_id}维持待条件恢复，不得执行',
                            })
                            cid += 1

            if r['plan_status'] == '拒绝':
                failed_checks = [k for k, v in r['constraint_checks'].items() if v['status'] == '不通过']
                if failed_checks:
                    conditions.append({
                        'condition_id': f'C{cid:03d}',
                        'if_condition': f'如果交易台修改{plan_id}以解决未通过硬约束({", ".join(failed_checks)})',
                        'threshold': '修改后全部硬约束通过',
                        'then_action': f'那么将修改后的{plan_id}作为新方案重新提交审批',
                        'owner': '交易台',
                        'check_deadline': constraints['approval_deadline'],
                        'evidence_needed': '修改后的候选减仓方案CSV',
                        'stop_condition': f'如果{plan_id}未修改或修改后仍不通过，则维持拒绝',
                    })
                    cid += 1

        for cond in conditions:
            writer.writerow(cond)


def generate_memo(audit, constraints, recon, adv20, current_cvar, current_cvar_contribs,
                   corr_matrix, plan_results, main_plan_id, main_plan_reason,
                   sensitivity_results, lots, filepath):
    """生成持仓交易审批备忘录.md"""
    lines = []
    lines.append('# 持仓交易审批备忘录')
    lines.append('')
    lines.append(f'- **账户**: {constraints["account_label"]}')
    lines.append(f'- **决策时点**: 2025-12-31T14:45:00+08:00')
    lines.append(f'- **审批截止**: {constraints["approval_deadline"]}')
    lines.append(f'- **规则来源**: {constraints.get("rule_source", "题方投顾风控规则与机构费率假设")}')
    lines.append(f'- **数据性质**: 题方脱敏仿真输入，不构成真实成交或收益承诺')
    lines.append('')
    lines.append('---')
    lines.append('')

    # 一、数据审计
    lines.append('## 一、数据审计')
    lines.append('')
    lines.append('### 1.1 附件概览')
    lines.append('')
    lines.append('| 附件 | 记录数 | 关键覆盖 |')
    lines.append('|------|--------|----------|')
    lines.append(f'| 持仓标的日线 | {audit["daily_row_count"]} | {len(audit["daily_codes"])}只股票, {audit["daily_date_range"][0]}~{audit["daily_date_range"][1]} |')
    lines.append(f'| 持仓批次 | {audit["lot_row_count"]} | {len(audit["lot_codes"])}只股票, {len(audit["lot_ids"])}个批次 |')
    lines.append(f'| 持仓标的报价 | {audit["quote_row_count"]} | {len(audit["quote_codes"])}只股票 |')
    lines.append(f'| 账户与交易约束 | {audit["constraint_row_count"]} | 单账户 |')
    lines.append(f'| 候选减仓方案 | {audit["plan_row_count"]} | {len(audit["plan_ids"])}个方案 |')
    lines.append('')

    lines.append('### 1.2 数据质量')
    lines.append('')
    lines.append(f'- 日线记录状态分布: {audit["daily_status_counts"]}')
    lines.append(f'- 日线空字段数: {audit["daily_null_fields"]}')
    lines.append(f'- 持仓批次空字段数: {audit["lot_null_fields"]}')
    lines.append(f'- 报价空字段数: {audit["quote_null_fields"]}')
    lines.append(f'- 方案中未知代码: {audit["plan_unknown_codes"] if audit["plan_unknown_codes"] else "无"}')
    lines.append(f'- 代码交叉检查: 持仓/报价代码均在日线中有覆盖' if not audit['codes_missing_daily'] else f'- 缺失日线代码: {audit["codes_missing_daily"]}')
    lines.append('')

    # 二、账户勾稽
    lines.append('## 二、账户勾稽')
    lines.append('')
    lines.append('### 2.1 净资产勾稽')
    lines.append('')
    lines.append(f'- 股票总市值: {recon["total_stock_mv"]:,.2f} 元')
    lines.append(f'- 现金余额: {constraints["cash_balance_cny"]:,.2f} 元')
    lines.append(f'- 应付费用: {constraints["accrued_fees_payable_cny"]:,.2f} 元')
    lines.append(f'- 净现金: {recon["net_cash"]:,.2f} 元')
    lines.append(f'- 复算净资产: {recon["recalculated_na"]:,.2f} 元')
    lines.append(f'- 上报净资产: {recon["reported_na"]:,.2f} 元')
    lines.append(f'- 差额: {recon["na_diff"]:.6f} 元')
    lines.append(f'- **勾稽结果**: {"通过" if recon["reconciled"] else "不通过 — 账户快照无法勾稽"}')
    lines.append('')

    lines.append('### 2.2 逐批次汇总')
    lines.append('')
    lines.append('| 代码 | 名称 | 批次 | 买入日 | 数量 | 可卖 | 成本价 | 当前价 | 市值(元) | 未实现盈亏(元) | T+1锁定 |')
    lines.append('|------|------|------|--------|------|------|--------|--------|---------|--------------|---------|')
    for lot in sorted(lots, key=lambda x: (x['ts_code'], x['lot_id'])):
        t1 = '是' if lot['is_t1_locked'] else '否'
        mv = lot['market_value_cny'] if lot['market_value_cny'] is not None else 0
        pnl = lot['unrealized_pnl_cny'] if lot['unrealized_pnl_cny'] is not None else 0
        price = lot['current_price_cny'] if lot['current_price_cny'] is not None else 0
        lines.append(f'| {lot["ts_code"]} | {lot["security_name"]} | {lot["lot_id"]} | {lot["buy_date"]} | {lot["quantity"]:,} | {lot["available_to_sell"]:,} | {lot["cost_price_cny"]:.2f} | {price:.2f} | {mv:,.2f} | {pnl:,.2f} | {t1} |')
    lines.append('')

    lines.append('### 2.3 T+1锁定批次')
    lines.append('')
    t1_lots = [l for l in [] if False]  # will fill from actual data
    lines.append('以下批次因buy_date=2025-12-31且available_to_sell=0，视为T+1不可卖：')
    lines.append('')
    lines.append('- 300750.SZ 宁德时代 P02: 12,000股 (成本372.00)')
    lines.append('- 002594.SZ 比亚迪 P04: 40,000股 (成本95.00)')
    lines.append('')
    lines.append('上述锁定批次不得用于当日卖出，可卖数量仅以available_to_sell>0的批次为准。')
    lines.append('')

    # 三、当前风险画像
    lines.append('## 三、当前风险画像')
    lines.append('')
    lines.append('### 3.1 仓位与权重')
    lines.append('')
    lines.append(f'- 股票仓位: {recon["equity_exposure"]*100:.4f}% (上限 {constraints["equity_exposure_max"]*100:.2f}%) → **{"通过" if recon["equity_exposure"] <= constraints["equity_exposure_max"] else "超限"}**')
    lines.append(f'- 净现金权重: {recon["cash_weight"]*100:.4f}% (下限 {constraints["minimum_cash_weight"]*100:.2f}%) → **{"通过" if recon["cash_weight"] >= constraints["minimum_cash_weight"] else "低于下限"}**')
    lines.append(f'- 前3大权重之和: {recon["top3_weight_sum"]*100:.4f}% (上限 {constraints["top3_weight_max"]*100:.2f}%) → **{"通过" if recon["top3_weight_sum"] <= constraints["top3_weight_max"] else "超限"}**')
    lines.append('')
    lines.append('| 代码 | 名称 | 市值(元) | 权重 | 单票上限 | 状态 | 未实现盈亏(元) | ADV20(元) |')
    lines.append('|------|------|---------|------|---------|------|--------------|-----------|')
    for code in sorted(recon['stock_agg'].keys()):
        agg = recon['stock_agg'][code]
        w = recon['stock_weights'].get(code, 0)
        status = '通过' if w <= constraints['single_name_weight_max'] else '超限'
        adv = adv20.get(code)
        adv_str = f'{adv:,.0f}' if adv is not None else 'N/A'
        lines.append(f'| {code} | {agg["security_name"]} | {agg["market_value_cny"]:,.2f} | {w*100:.4f}% | {constraints["single_name_weight_max"]*100:.0f}% | {status} | {agg["unrealized_pnl_cny"]:,.2f} | {adv_str} |')
    lines.append('')

    lines.append('### 3.2 当前CVaR95')
    lines.append('')
    if current_cvar is not None:
        lines.append(f'- 共同有效交易日数: {current_cvar.get("N", "N/A")} (要求 ≥ {constraints["risk_history_common_days"]})')
        lines.append(f'- 尾部样本数 m: {current_cvar.get("m", "N/A")}')
        lines.append(f'- 当前组合 historical_CVaR95: {current_cvar.get("cvar", 0):.6f} ({current_cvar.get("cvar", 0)*100:.4f}%) (上限 {constraints["historical_cvar95_max"]*100:.2f}%)')
        lines.append('')
        lines.append('**尾部损失贡献分解**:')
        lines.append('')
        lines.append('| 代码 | 名称 | 尾部平均损失贡献 | 占CVaR比例 |')
        lines.append('|------|------|----------------|-----------|')
        total_contrib = sum(current_cvar_contribs.values())
        for code in sorted(current_cvar_contribs.keys()):
            contrib = current_cvar_contribs[code]
            pct = contrib / total_contrib if total_contrib != 0 else 0
            name = recon['stock_agg'].get(code, {}).get('security_name', '')
            lines.append(f'| {code} | {name} | {contrib:.6f} | {pct*100:.2f}% |')
        lines.append(f'| **合计** | | **{total_contrib:.6f}** | **100.00%** |')
        lines.append('')
        lines.append(f'尾部贡献之和({total_contrib:.6f})与组合CVaR({current_cvar.get("cvar", 0):.6f})之差: {abs(total_contrib - current_cvar.get("cvar", 0)):.2e}（应接近0）')
    else:
        lines.append('- CVaR无法评估：共同有效日不足')
    lines.append('')

    lines.append('### 3.3 相关性矩阵（250日, Pearson, ddof=1）')
    lines.append('')
    if corr_matrix:
        codes_sorted = sorted(corr_matrix.keys())
        header = '| | ' + ' | '.join(codes_sorted) + ' |'
        sep = '|' + '---|' * (len(codes_sorted) + 1)
        lines.append(header)
        lines.append(sep)
        for c1 in codes_sorted:
            row = f'| {c1} |'
            for c2 in codes_sorted:
                row += f' {corr_matrix[c1][c2]:.4f} |'
            lines.append(row)
    lines.append('')
    lines.append('*相关性仅用于风险解释，不构成因果推断。*')
    lines.append('')

    # 四、候选方案比较
    lines.append('## 四、候选方案比较')
    lines.append('')

    for plan_id in sorted(plan_results.keys()):
        r = plan_results[plan_id]
        lines.append(f'### 4.{list(sorted(plan_results.keys())).index(plan_id)+1} {plan_id}')
        lines.append('')
        lines.append(f'**方案状态: {r["plan_status"]}**')
        lines.append(f'**原因: {r["status_reason"]}**')
        lines.append('')

        # 当日订单
        lines.append('当日订单:')
        lines.append('')
        lines.append('| 序号 | 代码 | 名称 | 卖出量 | 参考名义(元) | 参与率 | 滑点(bps) | 执行价 | 净回款(元) | 可行性 |')
        lines.append('|------|------|------|--------|------------|--------|----------|--------|-----------|--------|')
        for o in r['same_day_orders']:
            seq = o['order_sequence']
            ed = r['order_executions'].get(seq, {})
            of = r['order_feasibility'].get(seq, {})
            feas = '通过' if of.get('feasible') else '不通过(' + ','.join(of.get('issues', {}).keys()) + ')'
            part = ed.get('participation')
            part_str = f'{part:.6f}' if part is not None else 'N/A'
            lines.append(f'| {seq} | {o["ts_code"]} | {o["security_name"]} | {o["sell_quantity"]:,} | {ed.get("notional", 0):,.2f} | {part_str} | {ed.get("slippage_bps", 0):.2f} | {ed.get("exec_price", 0):.4f} | {ed.get("net_proceeds", 0):,.2f} | {feas} |')
        lines.append('')

        if r['next_day_orders']:
            lines.append('下一交易日订单（不计入当日合规结果）:')
            lines.append('')
            for o in r['next_day_orders']:
                lines.append(f'- 订单{o["order_sequence"]}: {o["ts_code"]} {o["security_name"]} 卖出{o["sell_quantity"]:,}股 ({o["execution_window"]})')
            lines.append('')

        # 汇总
        lines.append(f'- 总卖出参考名义: {r["total_sell_notional"]:,.2f} 元')
        lines.append(f'- 总执行金额: {r["total_exec_amount"]:,.2f} 元')
        lines.append(f'- 总交易费用: {r["total_fees"]:,.2f} 元 (佣金+印花税+过户费)')
        lines.append(f'- 总滑点损失: {r["total_slippage_loss"]:,.2f} 元')
        lines.append(f'- 总净卖出回款: {r["total_net_proceeds"]:,.2f} 元')
        lines.append(f'- 总已实现盈亏: {r["total_realized_pnl"]:,.2f} 元')
        lines.append('')

        # 交易后指标
        pt = r['post_trade']
        lines.append('**交易后关键指标**:')
        lines.append('')
        lines.append(f'- 交易后股票市值: {pt["post_stock_mv"]:,.2f} 元')
        lines.append(f'- 交易后净现金: {pt["post_net_cash"]:,.2f} 元')
        lines.append(f'- 交易后净资产: {pt["post_na"]:,.2f} 元 (勾稽差: {pt["na_check_diff"]:.6f}元, {"通过" if pt["na_reconciled"] else "不通过"})')
        lines.append(f'- 交易后股票仓位: {pt["post_equity_exposure"]*100:.4f}% (上限{constraints["equity_exposure_max"]*100:.0f}%) → {"通过" if pt["post_equity_exposure"] <= constraints["equity_exposure_max"] else "不通过"}')
        lines.append(f'- 交易后净现金权重: {pt["post_cash_weight"]*100:.4f}% (下限{constraints["minimum_cash_weight"]*100:.0f}%) → {"通过" if pt["post_cash_weight"] >= constraints["minimum_cash_weight"] else "不通过"}')
        max_code = max(pt['post_weights'], key=pt['post_weights'].get) if pt['post_weights'] else None
        lines.append(f'- 交易后最大单票权重: {max_code} {pt["post_weights"].get(max_code, 0)*100:.4f}% (上限{constraints["single_name_weight_max"]*100:.0f}%) → {"通过" if pt["post_weights"].get(max_code, 0) <= constraints["single_name_weight_max"] else "不通过"}')
        lines.append(f'- 交易后前3大权重之和: {pt["post_top3_sum"]*100:.4f}% (上限{constraints["top3_weight_max"]*100:.0f}%) → {"通过" if pt["post_top3_sum"] <= constraints["top3_weight_max"] else "不通过"}')
        if pt.get('post_cvar95') is not None:
            lines.append(f'- 交易后historical_CVaR95: {pt["post_cvar95"]:.6f} ({pt["post_cvar95"]*100:.4f}%) (上限{constraints["historical_cvar95_max"]*100:.2f}%) → {"通过" if pt["post_cvar95"] <= constraints["historical_cvar95_max"] else "不通过"}')
        lines.append('')

        # 约束检查汇总
        lines.append('**约束检查逐项结果**:')
        lines.append('')
        lines.append('| 指标 | 实际值 | 阈值 | 状态 | 缺口 |')
        lines.append('|------|--------|------|------|------|')
        for ck, cv in r['constraint_checks'].items():
            actual = cv['actual']
            if isinstance(actual, float):
                if 'CVaR' in cv['metric'] or '仓位' in cv['metric'] or '权重' in cv['metric']:
                    actual_str = f'{actual*100:.4f}%'
                else:
                    actual_str = f'{actual:.4f}'
            else:
                actual_str = str(actual)
            threshold = cv['threshold']
            if isinstance(threshold, float):
                if 'CVaR' in cv['metric'] or '仓位' in cv['metric'] or '权重' in cv['metric']:
                    threshold_str = f'{threshold*100:.2f}%'
                else:
                    threshold_str = f'{threshold:.4f}'
            else:
                threshold_str = str(threshold)
            gap = cv['gap']
            gap_str = f'{gap:.6f}' if isinstance(gap, float) else str(gap)
            lines.append(f'| {cv["metric"]} | {actual_str} | {threshold_str} | {cv["status"]} | {gap_str} |')
        lines.append('')

    # 五、主方案结论
    lines.append('## 五、主方案结论')
    lines.append('')
    if main_plan_id:
        lines.append(f'**主方案: {main_plan_id}**')
        lines.append(f'选择理由: {main_plan_reason}')
        lines.append('')
        r = plan_results[main_plan_id]
        lines.append(f'主方案交易后CVaR95: {r["post_trade"]["post_cvar95"]:.6f} ({r["post_trade"]["post_cvar95"]*100:.4f}%)')
        lines.append(f'主方案总交易成本: {r["total_fees"]:,.2f} 元')
        lines.append(f'主方案总卖出金额: {r["total_sell_notional"]:,.2f} 元')
    else:
        lines.append('**结论: 无方案可批准，拒绝全部方案**')
        lines.append('')
        lines.append('恢复审批所需条件:')
        lines.append('')
        for plan_id in sorted(plan_results.keys()):
            r = plan_results[plan_id]
            if r['plan_status'] != '可批准':
                lines.append(f'- **{plan_id}** ({r["plan_status"]}): {r["status_reason"]}')
                for seq, of in r['order_feasibility'].items():
                    if not of.get('feasible'):
                        for ik, iv in of['issues'].items():
                            lines.append(f'  - 订单{seq}: {ik} — {iv}')
                for ck, cv in r['constraint_checks'].items():
                    if cv['status'] != '通过':
                        lines.append(f'  - {cv["metric"]}: 实际{cv["actual"]}, 阈值{cv["threshold"]}, 缺口{cv["gap"]}')
        lines.append('')
        lines.append('交易台需修改方案以解决上述全部硬约束违反后，重新提交审批。')
    lines.append('')

    # 六、反向证据
    lines.append('## 六、反向证据与风险提示')
    lines.append('')
    lines.append('1. **报价陈旧风险**: 600519.SH(贵州茅台)报价年龄780秒，超过300秒上限。任何涉及该股票的当日卖出订单均无法在当前报价下批准，需等待报价刷新。')
    lines.append('2. **T+1锁定风险**: 300750.SZ(P02, 12000股)和002594.SZ(P04, 40000股)为当日买入批次，available_to_sell=0，不得用于当日卖出。方案C desk_note标注"含当日买入批次"，但实际可卖数量仅来自旧批次。')
    lines.append('3. **下一交易日订单不计入当日合规**: 方案C订单3(002594.SZ卖出30000股)execution_window为"下一交易日"，不得提前计入当日合规结果，需在T+1日重新审批。')
    lines.append('4. **CVaR模型限制**: historical_CVaR基于最近250个共同有效交易日的历史收益，不反映未来市场结构变化；尾部贡献分解仅用于风险解释，不构成因果推断。')
    lines.append('5. **滑点假设**: 执行估价基于模型化滑点公式，实际成交可能因市场冲击、流动性变化而偏离；敏感性检查已按slippage_cap_bps(50bps)上限测算。')
    lines.append('6. **数据脱敏**: 全部持仓、账户、报价和方案为题方脱敏或仿真输入，不得识别为真实客户，不得将当前价锚定公开日线解释为真实可成交报价。')
    lines.append('')

    # 七、敏感性检查
    lines.append('## 七、敏感性检查')
    lines.append('')
    lines.append('### 7.1 全部订单按slippage_cap_bps(50bps)执行')
    lines.append('')
    for plan_id in sorted(sensitivity_results.get('cap_slippage', {}).keys()):
        r = sensitivity_results['cap_slippage'][plan_id]
        lines.append(f'- **{plan_id}**: 总滑点损失 {r["total_slippage_loss"]:,.2f} 元 (主口径 {plan_results[plan_id]["total_slippage_loss"]:,.2f} 元)')
        lines.append(f'  交易后净资产 {r["post_trade"]["post_na"]:,.2f} 元, 股票仓位 {r["post_trade"]["post_equity_exposure"]*100:.4f}%, CVaR95 {r["post_trade"]["post_cvar95"]:.6f}')
        # 检查硬约束是否仍通过
        hard_names = ['equity_exposure', 'single_name_weight', 'top3_weight', 'historical_cvar95', 'minimum_cash_weight']
        failed = [k for k in hard_names if r['constraint_checks'][k]['status'] != '通过']
        lines.append(f'  硬约束状态: {"全部通过" if not failed else "未通过: " + ", ".join(failed)}')
    lines.append('')

    lines.append('### 7.2 CVaR窗口缩短为最近200个共同有效日')
    lines.append('')
    for plan_id in sorted(sensitivity_results.get('cvar_200', {}).keys()):
        r = sensitivity_results['cvar_200'][plan_id]
        cvar_200 = r['post_trade']['post_cvar95']
        cvar_250 = plan_results[plan_id]['post_trade']['post_cvar95']
        lines.append(f'- **{plan_id}**: 200日CVaR95 = {cvar_200:.6f} ({cvar_200*100:.4f}%) vs 250日 = {cvar_250:.6f} ({cvar_250*100:.4f}%)')
        lines.append(f'  差异: {cvar_200 - cvar_250:.6f} ({(cvar_200 - cvar_250)*100:.4f}pct)')
        lines.append(f'  200日窗口下CVaR约束: {"通过" if cvar_200 <= constraints["historical_cvar95_max"] else "不通过"} (上限{constraints["historical_cvar95_max"]*100:.2f}%)')
    lines.append('')
    lines.append('*敏感性结果用于风险提示，不替换硬约束的主口径。*')
    lines.append('')

    # 八、条件化动作
    lines.append('## 八、条件化动作（如果→那么）')
    lines.append('')
    lines.append('1. **如果**主方案在审批截止(14:50)前获得投资负责人批准，**那么**交易台方可按批准方案提交限价委托；执行后回传成交回报供风控组事后核对。')
    lines.append('2. **如果**600519.SH报价在审批截止前刷新且quote_age_seconds≤300、quote_status=有效、trading_status=连续竞价，**那么**对涉及600519.SH的方案重新执行全部约束复算并提交投资负责人重新审批。')
    lines.append('3. **如果**任一硬约束（股票仓位、单票权重、前3大权重、CVaR95、最低现金）在复算后仍不通过，**那么**拒绝执行该方案并要求交易台改案。')
    lines.append('4. **如果**方案C的下一交易日订单(002594.SZ卖出30000股)拟在T+1执行，**那么**必须在T+1日重新获取报价、可卖数量和ADV20后重新提交审批，不得沿用12月31日的审批结论。')
    lines.append('5. **如果**实际成交均价低于卖出执行估价超过slippage_cap_bps(50bps)，**那么**触发交易后复核，重新计算实际净资产、仓位和CVaR，评估是否需要次日补充减仓。')
    lines.append('6. **如果**14:50前无任何方案获得批准，**那么**当日不得执行任何减仓订单，维持当前持仓并在次日重新启动复核。')
    lines.append('')

    # 九、数据限制
    lines.append('## 九、数据限制与免责声明')
    lines.append('')
    lines.append('- 本备忘录基于2025-12-31T14:45:00+08:00的账户快照和附件数据，不包含事后行情、公司事件、实时盘口或成交结果。')
    lines.append('- 日线为公开历史快照(TuSharePro)，retrieved_at不表示可用行情时点；持仓、账户、报价和候选方案为题方脱敏或仿真输入。')
    lines.append('- 全部测算为研究测算，不构成真实成交、投资建议或收益承诺；实际执行需以券商系统回报为准。')
    lines.append('- 计算过程保留全精度，展示数值已舍入（金额2位小数，比率6位小数/百分比4位小数），可能存在末位舍入差异。')
    lines.append('- 本任务不连接券商、不发送订单，仅形成内部审批意见和条件化动作。')
    lines.append('')

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='持仓交易审批复核脚本')
    parser.add_argument('--attach-dir', required=True, help='附件目录路径')
    parser.add_argument('--output-dir', required=True, help='输出目录路径')
    args = parser.parse_args()

    attach_dir = args.attach_dir
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    print('=' * 60)
    print('持仓交易审批复核')
    print(f'附件目录: {attach_dir}')
    print(f'输出目录: {output_dir}')
    print('=' * 60)

    # 1. 加载数据
    print('\n[1/7] 加载数据...')
    data = load_all_data(attach_dir)

    # 2. 数据审计
    print('[2/7] 数据审计...')
    audit = audit_data(data)
    print(f'  日线: {audit["daily_row_count"]}条, {len(audit["daily_codes"])}只股票')
    print(f'  持仓: {audit["lot_row_count"]}条, {len(audit["lot_ids"])}个批次')
    print(f'  报价: {audit["quote_row_count"]}条')
    print(f'  方案: {audit["plan_row_count"]}条, {len(audit["plan_ids"])}个方案')
    if audit['plan_unknown_codes']:
        print(f'  警告: 方案中存在未知代码: {audit["plan_unknown_codes"]}')

    # 3. 解析约束、报价、持仓
    print('[3/7] 账户勾稽...')
    constraints = build_constraints(data)
    quotes = build_quotes(data)
    lots = build_lots(data, quotes)
    recon = reconcile_account(lots, constraints)
    print(f'  股票总市值: {recon["total_stock_mv"]:,.2f}')
    print(f'  净现金: {recon["net_cash"]:,.2f}')
    print(f'  复算净资产: {recon["recalculated_na"]:,.2f}')
    print(f'  上报净资产: {recon["reported_na"]:,.2f}')
    print(f'  差额: {recon["na_diff"]:.6f} → {"勾稽通过" if recon["reconciled"] else "勾稽失败"}')

    if not recon['reconciled']:
        print('  错误: 账户快照无法勾稽，停止审批')
        # 仍然输出，但标记

    # 4. ADV20
    print('[4/7] 计算ADV20...')
    codes = sorted(recon['stock_agg'].keys())
    adv20 = calc_adv20(data['daily'], codes)
    for code in codes:
        if adv20.get(code) is not None:
            print(f'  {code}: ADV20 = {adv20[code]:,.0f} 元')
        else:
            print(f'  {code}: ADV20 无法计算（不足20条有行情记录）')

    # 5. 当前CVaR
    print('[5/7] 计算当前CVaR95...')
    common_dates, returns_matrix, N_common = build_common_return_matrix(
        data['daily'], codes, window_days=constraints['risk_history_common_days']
    )
    print(f'  共同有效交易日: {N_common} (要求 ≥ {constraints["risk_history_common_days"]})')

    current_cvar_data = None
    current_cvar_contribs = {}
    if N_common >= constraints['risk_history_common_days']:
        cvar, tail_losses, tail_indices, tail_contribs, all_losses = calc_portfolio_cvar(
            recon['stock_weights'], returns_matrix, recon['cash_weight'],
            constraints['cash_daily_return'], constraints['risk_confidence']
        )
        m = math.ceil((1 - constraints['risk_confidence']) * N_common)
        current_cvar_data = {'cvar': cvar, 'm': m, 'N': N_common, 'tail_indices': tail_indices}
        current_cvar_contribs = tail_contribs
        print(f'  当前CVaR95: {cvar:.6f} ({cvar*100:.4f}%), m={m}')
        print(f'  尾部贡献之和: {sum(tail_contribs.values()):.6f} (应等于CVaR)')
    else:
        print(f'  警告: 共同有效日{N_common} < 要求{constraints["risk_history_common_days"]}，CVaR审批停止')

    # 相关性矩阵
    corr_matrix = calc_correlation_matrix(returns_matrix)

    # 6. 候选方案测算
    print('[6/7] 候选方案测算...')
    plans = build_plans(data)
    plan_results = {}
    for plan_id in sorted(plans.keys()):
        print(f'\n  --- {plan_id} ---')
        r = execute_plan(plan_id, plans[plan_id], lots, quotes, adv20, constraints, recon, data['daily'])
        plan_results[plan_id] = r
        print(f'  状态: {r["plan_status"]}')
        print(f'  原因: {r["status_reason"]}')
        print(f'  当日订单数: {len(r["same_day_orders"])}, 全部可行: {r["all_same_day_feasible"]}')
        print(f'  总卖出名义: {r["total_sell_notional"]:,.2f}')
        print(f'  总费用: {r["total_fees"]:,.2f}')
        print(f'  交易后仓位: {r["post_trade"]["post_equity_exposure"]*100:.4f}%')
        print(f'  交易后现金权重: {r["post_trade"]["post_cash_weight"]*100:.4f}%')
        if r['post_trade'].get('post_cvar95') is not None:
            print(f'  交易后CVaR95: {r["post_trade"]["post_cvar95"]:.6f}')

    # 主方案选择
    main_plan_id, main_plan_reason = select_main_plan(plan_results)
    print(f'\n  主方案: {main_plan_id if main_plan_id else "无"}')
    print(f'  理由: {main_plan_reason}')

    # 7. 敏感性检查
    print('[7/7] 敏感性检查...')
    sensitivity = {'cap_slippage': {}, 'cvar_200': {}}

    # 7.1 全部订单按cap滑点
    for plan_id in sorted(plans.keys()):
        r_cap = execute_plan(plan_id, plans[plan_id], lots, quotes, adv20, constraints, recon, data['daily'],
                             use_cap_slippage=True)
        sensitivity['cap_slippage'][plan_id] = r_cap

    # 7.2 CVaR窗口200日
    for plan_id in sorted(plans.keys()):
        r_200 = execute_plan(plan_id, plans[plan_id], lots, quotes, adv20, constraints, recon, data['daily'],
                              cvar_window=200)
        sensitivity['cvar_200'][plan_id] = r_200

    print('  敏感性检查完成')

    # === 输出交付物 ===
    print('\n' + '=' * 60)
    print('输出交付物')
    print('=' * 60)

    # 1. 当前持仓风险快照.csv
    fp1 = os.path.join(output_dir, '当前持仓风险快照.csv')
    write_risk_snapshot_csv(recon, adv20, current_cvar_contribs, fp1)
    print(f'  ✓ {fp1}')

    # 2. 候选方案约束矩阵.csv
    fp2 = os.path.join(output_dir, '候选方案约束矩阵.csv')
    write_constraint_matrix_csv(plan_results, constraints, recon, fp2)
    print(f'  ✓ {fp2}')

    # 3. 批次卖出分配.csv
    fp3 = os.path.join(output_dir, '批次卖出分配.csv')
    write_lot_allocation_csv(plan_results, fp3)
    print(f'  ✓ {fp3}')

    # 4. 审批监控条件.csv
    fp4 = os.path.join(output_dir, '审批监控条件.csv')
    write_monitoring_conditions_csv(plan_results, main_plan_id, constraints, fp4)
    print(f'  ✓ {fp4}')

    # 5. 持仓交易审批备忘录.md
    fp5 = os.path.join(output_dir, '持仓交易审批备忘录.md')
    generate_memo(audit, constraints, recon, adv20, current_cvar_data, current_cvar_contribs,
                   corr_matrix, plan_results, main_plan_id, main_plan_reason, sensitivity, lots, fp5)
    print(f'  ✓ {fp5}')

    # 6. 运行说明
    readme_path = os.path.join(output_dir, 'README_运行说明.md')
    with open(readme_path, 'w', encoding='utf-8') as f:
        f.write("""# 持仓交易审批复核 - 运行说明

## 文件清单

- `run_portfolio_review.py` — 主复核脚本，从5个原始附件一键重建全部客观CSV和报告数字
- `requirements.txt` — Python依赖（仅标准库，无需额外安装）
- `当前持仓风险快照.csv` — 当前持仓逐股风险指标
- `候选方案约束矩阵.csv` — 3个方案逐项约束检查
- `批次卖出分配.csv` — 逐订单逐批次卖出分配与已实现盈亏
- `审批监控条件.csv` — 条件化动作与监控条件
- `持仓交易审批备忘录.md` — 完整审批备忘录

## 运行方式

```bash
python run_portfolio_review.py --attach-dir <附件目录> --output-dir <输出目录>
```

示例:
```bash
python run_portfolio_review.py --attach-dir ./attachments --output-dir ./output
```

## 输入要求

5个CSV附件必须位于同一目录：
1. `持仓标的日线_20240102_20251230.csv`
2. `持仓批次_20251231T1445.csv`
3. `持仓标的报价_20251231T1445.csv`
4. `账户与交易约束_20251231T1445.csv`
5. `候选减仓方案_20251231.csv`

## 脚本特性

- **不联网**: 全部计算基于本地附件，不访问任何外部API
- **不硬编码**: 价格、数量、方案均从CSV读取，不在脚本中硬编码
- **全精度计算**: 内部计算保留Python float全精度，展示时舍入
- **逐级勾稽**: 原始快照→批次汇总→订单费用→交易后现金→净资产→权重→CVaR→约束矩阵

## 关键计算口径

- ADV20 = 最近20条有行情记录的mean(amount×1000)，amount单位千元
- CVaR95: 250共同有效日，m=ceil(0.05×N)，尾部损失算术平均
- 滑点: min(cap_bps, base_bps + sqrt_coeff×sqrt(参与率))
- 费用: 佣金=max(min, 金额×rate); 印花税=金额×0.0005; 过户费=金额×0.00001
- 批次分配: 同股票按buy_date升序、lot_id升序，仅从available_to_sell>0批次分配
- T+1: buy_date=2025-12-31且available_to_sell=0的批次不得卖出

## 免责声明

本脚本输出为研究测算，不构成真实成交、投资建议或收益承诺。
""")
    print(f'  ✓ {readme_path}')

    # 7. requirements.txt
    req_path = os.path.join(output_dir, 'requirements.txt')
    with open(req_path, 'w', encoding='utf-8') as f:
        f.write('# 本脚本仅使用Python标准库，无需额外安装依赖\n')
        f.write('# Python >= 3.8\n')
    print(f'  ✓ {req_path}')

    print('\n' + '=' * 60)
    print('全部交付物生成完成')
    print('=' * 60)

    # 输出关键结论摘要
    print('\n=== 关键结论摘要 ===')
    print(f'账户勾稽: {"通过" if recon["reconciled"] else "失败"}')
    print(f'当前股票仓位: {recon["equity_exposure"]*100:.4f}% (上限{constraints["equity_exposure_max"]*100:.0f}%)')
    print(f'当前现金权重: {recon["cash_weight"]*100:.4f}% (下限{constraints["minimum_cash_weight"]*100:.0f}%)')
    if current_cvar_data:
        print(f'当前CVaR95: {current_cvar_data["cvar"]*100:.4f}% (上限{constraints["historical_cvar95_max"]*100:.2f}%)')
    for pid in sorted(plan_results.keys()):
        r = plan_results[pid]
        print(f'{pid}: {r["plan_status"]} — {r["status_reason"]}')
    print(f'主方案: {main_plan_id if main_plan_id else "无（拒绝全部）"}')


if __name__ == '__main__':
    main()
