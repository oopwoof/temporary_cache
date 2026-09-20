#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_portfolio_review.py
稳健账户A 持仓减仓方案审批复核 — 一键重建全部客观CSV与报告数字。

输入（同目录或通过 --input-dir 指定）：
  1. 持仓标的日线_20240102_20251230.csv
  2. 持仓批次_20251231T1445.csv
  3. 持仓标的报价_20251231T1445.csv
  4. 账户与交易约束_20251231T1445.csv
  5. 候选减仓方案_20251231.csv

输出（同目录或通过 --output-dir 指定）：
  - 当前持仓风险快照.csv
  - 候选方案约束矩阵.csv
  - 批次卖出分配.csv
  - 审批监控条件.csv
  - review_results.json（供备忘录引用的全部数字）

本脚本不联网，不硬编码价格/数量/推荐方案。
"""

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def read_csv(path):
    """读取CSV，处理BOM，返回list[dict]。"""
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            # 去除None键（尾部空列）
            cleaned = {k.strip(): (v.strip() if isinstance(v, str) else v)
                       for k, v in row.items() if k is not None}
            rows.append(cleaned)
    return rows


def to_float(v, default=None):
    if v is None or v == '':
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def to_int(v, default=None):
    if v is None or v == '':
        return default
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def fmt(v, digits=4):
    """展示用舍入，计算保留全精度。"""
    if v is None:
        return ''
    return round(v, digits)


# ---------------------------------------------------------------------------
# 1. 数据加载与审计
# ---------------------------------------------------------------------------

def load_data(input_dir):
    files = {
        'daily': '持仓标的日线_20240102_20251230.csv',
        'lots': '持仓批次_20251231T1445.csv',
        'quotes': '持仓标的报价_20251231T1445.csv',
        'constraints': '账户与交易约束_20251231T1445.csv',
        'plans': '候选减仓方案_20251231.csv',
    }
    data = {}
    for key, fname in files.items():
        path = os.path.join(input_dir, fname)
        if not os.path.exists(path):
            print(f"[ERROR] 找不到输入文件: {path}", file=sys.stderr)
            sys.exit(1)
        data[key] = read_csv(path)
    return data


def audit_data(data):
    """字段、主键、日期、空值、重复值审计。返回审计报告。"""
    report = {}

    # 日线
    daily = data['daily']
    daily_keys = set()
    daily_dup = 0
    daily_null = 0
    daily_status_counts = defaultdict(int)
    for r in daily:
        k = (r.get('ts_code'), r.get('trade_date'))
        if k in daily_keys:
            daily_dup += 1
        daily_keys.add(k)
        if not r.get('ts_code') or not r.get('trade_date'):
            daily_null += 1
        daily_status_counts[r.get('record_status', '')] += 1
    report['daily'] = {
        'rows': len(daily),
        'unique_ts_code_trade_date': len(daily_keys),
        'duplicates': daily_dup,
        'null_key_rows': daily_null,
        'status_counts': dict(daily_status_counts),
        'ts_codes': sorted(set(r.get('ts_code', '') for r in daily)),
    }

    # 持仓批次
    lots = data['lots']
    lot_keys = set()
    lot_dup = 0
    for r in lots:
        k = (r.get('ts_code'), r.get('lot_id'))
        if k in lot_keys:
            lot_dup += 1
        lot_keys.add(k)
    report['lots'] = {
        'rows': len(lots),
        'unique_ts_code_lot_id': len(lot_keys),
        'duplicates': lot_dup,
        'ts_codes': sorted(set(r.get('ts_code', '') for r in lots)),
    }

    # 报价
    quotes = data['quotes']
    report['quotes'] = {
        'rows': len(quotes),
        'ts_codes': sorted(set(r.get('ts_code', '') for r in quotes)),
    }

    # 约束
    report['constraints'] = {'rows': len(data['constraints'])}

    # 方案
    plans = data['plans']
    plan_ids = sorted(set(r.get('plan_id', '') for r in plans))
    report['plans'] = {
        'rows': len(plans),
        'plan_ids': plan_ids,
    }

    # 代码一致性检查
    lot_codes = set(r.get('ts_code') for r in lots)
    quote_codes = set(r.get('ts_code') for r in quotes)
    daily_codes = set(r.get('ts_code') for r in daily)
    plan_codes = set(r.get('ts_code') for r in plans)

    report['code_consistency'] = {
        'lot_not_in_quote': list(lot_codes - quote_codes),
        'lot_not_in_daily': list(lot_codes - daily_codes),
        'plan_not_in_lot': list(plan_codes - lot_codes),
        'plan_not_in_quote': list(plan_codes - quote_codes),
    }

    return report


# ---------------------------------------------------------------------------
# 2. 账户勾稽
# ---------------------------------------------------------------------------

def reconcile_account(data):
    """按批次汇总市值、净现金、复算净资产，与reported比较。"""
    constraints = data['constraints'][0]
    lots = data['lots']
    quotes = {r['ts_code']: r for r in data['quotes']}

    cash_balance = to_float(constraints['cash_balance_cny'])
    accrued_fees = to_float(constraints['accrued_fees_payable_cny'])
    reported_na = to_float(constraints['reported_net_asset_cny'])

    # 按批次计算市值
    lot_details = []
    total_stock_mv = 0.0
    for r in lots:
        ts_code = r['ts_code']
        q = quotes.get(ts_code, {})
        price = to_float(q.get('current_price_cny'))
        fx = to_float(q.get('fx_to_cny'), 1.0)
        quantity = to_int(r['quantity'])
        avail = to_int(r['available_to_sell'])
        cost = to_float(r['cost_price_cny'])

        mv = quantity * price * fx if price is not None else 0.0
        unrealized = (price - cost) * quantity if price is not None else 0.0
        total_stock_mv += mv

        lot_details.append({
            'ts_code': ts_code,
            'security_name': r.get('security_name', ''),
            'lot_id': r['lot_id'],
            'buy_date': r['buy_date'],
            'quantity': quantity,
            'available_to_sell': avail,
            'cost_price_cny': cost,
            'current_price_cny': price,
            'fx_to_cny': fx,
            'market_value_cny': mv,
            'unrealized_pnl_cny': unrealized,
            'is_t1_locked': (r['buy_date'] == '2025-12-31' and avail == 0),
        })

    net_cash = cash_balance - accrued_fees
    recalc_na = total_stock_mv + net_cash
    diff = recalc_na - reported_na

    return {
        'cash_balance_cny': cash_balance,
        'accrued_fees_payable_cny': accrued_fees,
        'net_cash_cny': net_cash,
        'total_stock_mv_cny': total_stock_mv,
        'reported_net_asset_cny': reported_na,
        'recalc_net_asset_cny': recalc_na,
        'difference_cny': diff,
        'reconciled': abs(diff) <= 1.0,
        'lot_details': lot_details,
    }


# ---------------------------------------------------------------------------
# 3. ADV20 与 共同交易日
# ---------------------------------------------------------------------------

def compute_adv20(daily, as_of='2025-12-30'):
    """每只股票截至as_of最近20条有行情记录的ADV20（元）。amount单位千元→×1000。"""
    by_code = defaultdict(list)
    for r in daily:
        if r.get('record_status') == '有行情':
            by_code[r['ts_code']].append(r)

    adv20 = {}
    for code, rows in by_code.items():
        # 按日期降序，取<=as_of的最近20条
        valid = [r for r in rows if r['trade_date'] <= as_of]
        valid.sort(key=lambda x: x['trade_date'], reverse=True)
        recent = valid[:20]
        amounts = [to_float(r['amount'], 0) * 1000 for r in recent]
        adv20[code] = sum(amounts) / len(amounts) if amounts else 0.0
    return adv20


def find_common_trading_days(daily, n_days=250):
    """取6只股票共同存在且状态有效的最近n_days个交易日。"""
    by_code_date = defaultdict(dict)
    codes = set()
    for r in daily:
        if r.get('record_status') == '有行情':
            by_code_date[r['ts_code']][r['trade_date']] = r
            codes.add(r['ts_code'])

    # 共同日期
    common_dates = None
    for code in codes:
        dates = set(by_code_date[code].keys())
        common_dates = dates if common_dates is None else common_dates & dates

    common_dates = sorted(common_dates, reverse=True)
    selected = common_dates[:n_days]
    selected.sort()  # 升序

    return selected, by_code_date, len(common_dates)


# ---------------------------------------------------------------------------
# 4. CVaR 计算
# ---------------------------------------------------------------------------

def compute_historical_cvar(weights, daily, common_dates, by_code_date,
                            cash_daily_return=0.0, confidence=0.95):
    """
    给定权重dict{ts_code: weight}（现金权重隐含=1-sum），计算historical CVaR95。
    r_i,t = pct_chg / 100；现金日收益=cash_daily_return。
    返回 cvar, tail_contributions(dict), portfolio_losses(list), tail_indices。
    """
    codes = sorted(weights.keys())
    n = len(common_dates)
    m = math.ceil((1 - confidence) * n)

    # 构建收益矩阵
    returns = {}
    for code in codes:
        returns[code] = []
        for d in common_dates:
            r = by_code_date[code].get(d)
            returns[code].append(to_float(r['pct_chg'], 0) / 100.0 if r else 0.0)

    # 组合日收益与损失
    cash_weight = 1.0 - sum(weights.values())
    port_returns = []
    losses = []
    for i in range(n):
        rp = cash_weight * cash_daily_return
        for code in codes:
            rp += weights[code] * returns[code][i]
        port_returns.append(rp)
        losses.append(-rp)

    # 损失从大到小排列，取前m个
    indexed_losses = sorted(enumerate(losses), key=lambda x: x[1], reverse=True)
    tail_indices = [idx for idx, _ in indexed_losses[:m]]
    tail_losses = [losses[idx] for idx in tail_indices]
    cvar = sum(tail_losses) / m if m > 0 else 0.0

    # 尾部贡献：每只股票 mean(-w_i * r_i,t) over tail dates
    tail_contrib = {}
    for code in codes:
        contrib = sum(-weights[code] * returns[code][i] for i in tail_indices) / m
        tail_contrib[code] = contrib
    # 现金贡献
    cash_contrib = sum(-cash_weight * cash_daily_return for _ in tail_indices) / m
    tail_contrib['CASH'] = cash_contrib

    return cvar, tail_contrib, losses, tail_indices, m


# ---------------------------------------------------------------------------
# 5. 当前风险画像
# ---------------------------------------------------------------------------

def current_risk_profile(recon, constraints, adv20):
    """计算当前仓位、权重、前3大、未实现盈亏。"""
    na = recon['recalc_net_asset_cny']
    lot_details = recon['lot_details']

    # 按股票汇总
    by_stock = defaultdict(lambda: {
        'quantity': 0, 'available_to_sell': 0,
        'market_value_cny': 0.0, 'unrealized_pnl_cny': 0.0,
        'security_name': '', 'current_price_cny': 0.0,
    })
    for lot in lot_details:
        s = by_stock[lot['ts_code']]
        s['quantity'] += lot['quantity']
        s['available_to_sell'] += lot['available_to_sell']
        s['market_value_cny'] += lot['market_value_cny']
        s['unrealized_pnl_cny'] += lot['unrealized_pnl_cny']
        s['security_name'] = lot['security_name']
        s['current_price_cny'] = lot['current_price_cny']

    stocks = []
    for code, s in by_stock.items():
        weight = s['market_value_cny'] / na if na else 0
        stocks.append({
            'ts_code': code,
            'security_name': s['security_name'],
            'quantity': s['quantity'],
            'available_to_sell': s['available_to_sell'],
            'current_price_cny': s['current_price_cny'],
            'market_value_cny': s['market_value_cny'],
            'weight': weight,
            'unrealized_pnl_cny': s['unrealized_pnl_cny'],
            'adv20_cny': adv20.get(code, 0.0),
        })

    stocks.sort(key=lambda x: x['weight'], reverse=True)
    equity_exposure = sum(s['market_value_cny'] for s in stocks) / na
    cash_weight = recon['net_cash_cny'] / na
    top3_weight = sum(s['weight'] for s in stocks[:3])
    total_unrealized = sum(s['unrealized_pnl_cny'] for s in stocks)

    return {
        'stocks': stocks,
        'equity_exposure': equity_exposure,
        'cash_weight': cash_weight,
        'top3_weight': top3_weight,
        'total_unrealized_pnl_cny': total_unrealized,
        'net_asset_cny': na,
    }


# ---------------------------------------------------------------------------
# 6. 方案执行测算
# ---------------------------------------------------------------------------

def evaluate_plan(plan_id, plan_orders, recon, constraints, quotes, adv20,
                  daily, common_dates, by_code_date, common_days_count):
    """
    评估单个方案。返回完整测算结果。
    只把execution_window为"当日"的订单计入即时修复。
    """
    c = constraints
    na = recon['recalc_net_asset_cny']
    lot_details = recon['lot_details']
    cash_daily_return = to_float(c['cash_daily_return'], 0.0)
    confidence = to_float(c['risk_confidence'], 0.95)
    risk_common_days = to_int(c['risk_history_common_days'], 250)

    # 费率参数
    commission_rate = to_float(c['commission_rate'])
    min_commission = to_float(c['minimum_commission_cny'])
    stamp_rate = to_float(c['stamp_duty_sell_rate'])
    transfer_rate = to_float(c['transfer_fee_rate'])
    slip_base = to_float(c['slippage_base_bps'])
    slip_sqrt = to_float(c['slippage_sqrt_coefficient_bps'])
    slip_cap = to_float(c['slippage_cap_bps'])
    max_participation = to_float(c['max_adv20_participation'])
    quote_max_age = to_float(c['quote_max_age_seconds'])

    # 按股票分组的可卖批次（buy_date升序、lot_id升序）
    sellable_lots = defaultdict(list)
    for lot in lot_details:
        if lot['available_to_sell'] > 0:
            sellable_lots[lot['ts_code']].append(lot)
    for code in sellable_lots:
        sellable_lots[code].sort(key=lambda x: (x['buy_date'], x['lot_id']))

    # 累计可卖
    total_avail_by_code = defaultdict(int)
    for lot in lot_details:
        total_avail_by_code[lot['ts_code']] += lot['available_to_sell']

    # 当日订单
    same_day_orders = [o for o in plan_orders if o['execution_window'] == '当日']
    next_day_orders = [o for o in plan_orders if o['execution_window'] != '当日']

    # 按股票累计当日卖出量
    cum_sell_by_code = defaultdict(int)
    for o in same_day_orders:
        cum_sell_by_code[o['ts_code']] += to_int(o['sell_quantity'])

    # 订单级校验与执行
    order_results = []
    allocation_rows = []
    plan_feasible = True
    blockers = []

    total_net_proceeds = 0.0
    total_fees = 0.0
    total_slippage_loss = 0.0
    total_realized_pnl = 0.0
    total_exec_amount = 0.0

    # 跟踪每只股票已分配数量（用于同股票多订单）
    allocated_by_code = defaultdict(int)

    for o in same_day_orders:
        ts_code = o['ts_code']
        seq = to_int(o['order_sequence'])
        sell_qty = to_int(o['sell_quantity'])
        q = quotes.get(ts_code, {})

        order_status = '通过'
        order_issues = []

        # 未知代码
        if ts_code not in quotes or not q:
            order_status = '无法评估'
            order_issues.append('未知代码，报价缺失')
            plan_feasible = False
            blockers.append(f'订单{seq}: {ts_code} 未知代码')

        price = to_float(q.get('current_price_cny')) if q else None
        board_lot = to_int(q.get('board_lot_shares'), 100) if q else 100

        # 整手检查
        if sell_qty % board_lot != 0:
            order_status = '不通过'
            order_issues.append(f'卖出量{sell_qty}不是{board_lot}整数倍')
            plan_feasible = False
            blockers.append(f'订单{seq}: {ts_code} 非整手')

        # 可卖数量检查（累计）
        cum_sell = cum_sell_by_code[ts_code]
        avail = total_avail_by_code.get(ts_code, 0)
        if cum_sell > avail:
            order_status = '不通过'
            order_issues.append(f'当日累计卖出{cum_sell}超过可卖{avail}')
            plan_feasible = False
            blockers.append(f'订单{seq}: {ts_code} 累计卖出超可卖量')

        # 报价时效
        quote_age = to_float(q.get('quote_age_seconds')) if q else None
        trading_status = q.get('trading_status', '') if q else ''
        quote_status = q.get('quote_status', '') if q else ''

        quote_usable = True
        if trading_status != '连续竞价':
            quote_usable = False
            order_issues.append(f'交易状态={trading_status}，非连续竞价')
        if quote_age is not None and quote_age > quote_max_age:
            quote_usable = False
            order_issues.append(f'报价时效{quote_age}s超过上限{quote_max_age}s')
        if price is not None:
            lu = to_float(q.get('limit_up_cny'))
            ld = to_float(q.get('limit_down_cny'))
            if lu is not None and price > lu:
                quote_usable = False
                order_issues.append(f'价格{price}高于涨停{lu}')
            if ld is not None and price <= ld:
                quote_usable = False
                order_issues.append(f'价格{price}不高于跌停{ld}')

        if not quote_usable:
            order_status = '待条件恢复' if order_status == '通过' else order_status
            plan_feasible = False
            blockers.append(f'订单{seq}: {ts_code} 报价不可用({";".join(order_issues)})')

        # ADV20参与率
        notional = sell_qty * price if price else 0
        adv = adv20.get(ts_code, 0)
        participation = notional / adv if adv > 0 else float('inf')
        if participation > max_participation:
            order_status = '不通过'
            order_issues.append(f'参与率{participation:.6f}超过上限{max_participation}')
            plan_feasible = False
            blockers.append(f'订单{seq}: {ts_code} ADV20参与率超标')

        # 滑点与执行估价（仅在报价可用时计算）
        exec_price = None
        slip_bps = None
        exec_amount = 0.0
        commission = 0.0
        stamp = 0.0
        transfer = 0.0
        net_proceeds = 0.0
        order_fee = 0.0

        if quote_usable and price is not None and order_status != '无法评估':
            slip_bps = min(slip_cap, slip_base + slip_sqrt * math.sqrt(participation))
            exec_price = price * (1 - slip_bps / 10000)
            exec_amount = sell_qty * exec_price
            commission = max(min_commission, exec_amount * commission_rate)
            stamp = exec_amount * stamp_rate
            transfer = exec_amount * transfer_rate
            order_fee = commission + stamp + transfer
            net_proceeds = exec_amount - order_fee

            total_net_proceeds += net_proceeds
            total_fees += order_fee
            total_exec_amount += exec_amount
            slippage_loss = sell_qty * (price - exec_price)
            total_slippage_loss += slippage_loss

        # 批次分配（仅在可执行时）
        order_realized = 0.0
        if quote_usable and price is not None and exec_price is not None and order_status in ('通过',):
            remaining = sell_qty
            already = allocated_by_code[ts_code]
            # 从可卖批次按顺序分配
            for lot in sellable_lots[ts_code]:
                if remaining <= 0:
                    break
                lot_avail = lot['available_to_sell']
                # 该批次已被前面同股票订单分配了多少
                lot_already = max(0, already - sum(
                    l['available_to_sell'] for l in sellable_lots[ts_code]
                    if (l['buy_date'], l['lot_id']) < (lot['buy_date'], lot['lot_id'])
                ))
                lot_remaining_avail = lot_avail - lot_already
                if lot_remaining_avail <= 0:
                    continue
                alloc = min(remaining, lot_remaining_avail)
                lot_realized = (exec_price - lot['cost_price_cny']) * alloc
                order_realized += lot_realized

                allocation_rows.append({
                    'plan_id': plan_id,
                    'order_sequence': seq,
                    'ts_code': ts_code,
                    'security_name': lot['security_name'],
                    'lot_id': lot['lot_id'],
                    'buy_date': lot['buy_date'],
                    'allocated_quantity': alloc,
                    'cost_price_cny': lot['cost_price_cny'],
                    'estimated_execution_price': exec_price,
                    'estimated_fee': order_fee * (alloc / sell_qty) if sell_qty > 0 else 0,
                    'realized_pnl_cny': lot_realized - order_fee * (alloc / sell_qty) if sell_qty > 0 else 0,
                    'eligibility_status': '可卖',
                })
                remaining -= alloc
                already += alloc

            allocated_by_code[ts_code] += sell_qty
            order_realized -= order_fee  # 减去该订单全部费用
            total_realized_pnl += order_realized

        order_results.append({
            'plan_id': plan_id,
            'order_sequence': seq,
            'execution_window': o['execution_window'],
            'ts_code': ts_code,
            'security_name': o.get('security_name', ''),
            'sell_quantity': sell_qty,
            'current_price_cny': price,
            'notional_cny': notional,
            'adv20_cny': adv,
            'participation_rate': participation,
            'slippage_bps': slip_bps,
            'estimated_execution_price': exec_price,
            'execution_amount_cny': exec_amount,
            'commission_cny': commission,
            'stamp_duty_cny': stamp,
            'transfer_fee_cny': transfer,
            'total_fee_cny': order_fee,
            'net_proceeds_cny': net_proceeds,
            'realized_pnl_cny': order_realized if order_status == '通过' else None,
            'status': order_status,
            'issues': '; '.join(order_issues) if order_issues else '',
        })

    # 下一交易日订单（仅记录，不计入当日合规）
    next_day_results = []
    for o in next_day_orders:
        next_day_results.append({
            'plan_id': plan_id,
            'order_sequence': to_int(o['order_sequence']),
            'execution_window': o['execution_window'],
            'ts_code': o['ts_code'],
            'security_name': o.get('security_name', ''),
            'sell_quantity': to_int(o['sell_quantity']),
            'status': '后续条件动作（不计入当日合规）',
            'note': o.get('desk_note', ''),
        })

    # 交易后持仓（仅当日订单影响）
    post_lots = []
    for lot in lot_details:
        code = lot['ts_code']
        sold = allocated_by_code.get(code, 0)
        # 从该批次扣减
        lot_sold = 0
        if code in sellable_lots:
            cum_before = 0
            for sl in sellable_lots[code]:
                if (sl['buy_date'], sl['lot_id']) == (lot['buy_date'], lot['lot_id']):
                    lot_sold = max(0, min(sl['available_to_sell'], sold - cum_before))
                    break
                cum_before += sl['available_to_sell']
        remaining_qty = lot['quantity'] - lot_sold
        remaining_avail = lot['available_to_sell'] - lot_sold
        post_lots.append({
            **lot,
            'quantity': remaining_qty,
            'available_to_sell': remaining_avail,
            'sold_quantity': lot_sold,
        })

    # 交易后市值（按附件current_price盯市剩余数量）
    post_stock_mv = 0.0
    post_by_stock = defaultdict(lambda: {'quantity': 0, 'market_value_cny': 0.0, 'security_name': ''})
    for lot in post_lots:
        mv = lot['quantity'] * lot['current_price_cny'] * lot['fx_to_cny']
        post_stock_mv += mv
        post_by_stock[lot['ts_code']]['quantity'] += lot['quantity']
        post_by_stock[lot['ts_code']]['market_value_cny'] += mv
        post_by_stock[lot['ts_code']]['security_name'] = lot['security_name']

    post_net_cash = recon['net_cash_cny'] + total_net_proceeds
    post_na = post_stock_mv + post_net_cash

    # 验证：post_na 应等于 reported_na - slippage_loss - total_fees
    na_check = post_na - (recon['reported_net_asset_cny'] - total_slippage_loss - total_fees)

    # 交易后权重
    post_weights = {}
    post_stock_list = []
    for code, s in post_by_stock.items():
        w = s['market_value_cny'] / post_na if post_na else 0
        post_weights[code] = w
        post_stock_list.append({
            'ts_code': code,
            'security_name': s['security_name'],
            'quantity': s['quantity'],
            'market_value_cny': s['market_value_cny'],
            'weight': w,
        })
    post_stock_list.sort(key=lambda x: x['weight'], reverse=True)

    post_equity = post_stock_mv / post_na if post_na else 0
    post_cash_w = post_net_cash / post_na if post_na else 0
    post_top3 = sum(s['weight'] for s in post_stock_list[:3])

    # CVaR（仅在共同日数足够时）
    cvar_ok = common_days_count >= risk_common_days
    if cvar_ok and plan_feasible:
        cvar, tail_contrib, _, _, m = compute_historical_cvar(
            post_weights, daily, common_dates, by_code_date,
            cash_daily_return, confidence)
        # 当前CVaR（用于快照）
        cur_weights = {s['ts_code']: s['weight'] for s in current_risk_profile(recon, constraints, adv20)['stocks']}
        cur_cvar, cur_tail_contrib, _, _, _ = compute_historical_cvar(
            cur_weights, daily, common_dates, by_code_date,
            cash_daily_return, confidence)
    else:
        cvar = None
        tail_contrib = {}
        cur_cvar = None
        cur_tail_contrib = {}
        m = math.ceil((1 - confidence) * len(common_dates)) if common_dates else 0

    # 约束逐项判定
    eq_max = to_float(c['equity_exposure_max'])
    single_max = to_float(c['single_name_weight_max'])
    top3_max = to_float(c['top3_weight_max'])
    cvar_max = to_float(c['historical_cvar95_max'])
    cash_min = to_float(c['minimum_cash_weight'])

    metrics = []

    def add_metric(name, actual, threshold, direction, basis, evidence):
        """direction: 'max'=actual<=threshold pass, 'min'=actual>=threshold pass"""
        if actual is None:
            status = '无法评估'
            gap = ''
        elif direction == 'max':
            status = '通过' if actual <= threshold else '不通过'
            gap = actual - threshold
        else:
            status = '通过' if actual >= threshold else '不通过'
            gap = threshold - actual
        metrics.append({
            'plan_id': plan_id,
            'metric_name': name,
            'actual_value': actual,
            'threshold': threshold,
            'status': status,
            'gap': gap,
            'calculation_basis': basis,
            'evidence_file': evidence,
        })

    add_metric('equity_exposure', post_equity, eq_max, 'max',
               f'交易后股票市值{post_stock_mv:.2f}/交易后净资产{post_na:.2f}',
               '持仓批次+报价+候选方案')
    add_metric('single_name_weight_max', max(s['weight'] for s in post_stock_list) if post_stock_list else 0,
               single_max, 'max',
               f'最大单票{post_stock_list[0]["ts_code"] if post_stock_list else ""}权重',
               '持仓批次+报价+候选方案')
    add_metric('top3_weight_max', post_top3, top3_max, 'max',
               f'前3大权重之和', '持仓批次+报价+候选方案')
    add_metric('historical_cvar95_max', cvar, cvar_max, 'max',
               f'{len(common_dates)}共同日historical CVaR95，m={m}',
               '持仓标的日线')
    add_metric('minimum_cash_weight', post_cash_w, cash_min, 'min',
               f'交易后净现金{post_net_cash:.2f}/交易后净资产{post_na:.2f}',
               '账户约束+候选方案')

    # 订单级约束也加入矩阵
    for or_ in order_results:
        metrics.append({
            'plan_id': plan_id,
            'metric_name': f'order_{or_["order_sequence"]}_{or_["ts_code"]}_sellable',
            'actual_value': or_['sell_quantity'],
            'threshold': total_avail_by_code.get(or_['ts_code'], 0),
            'status': or_['status'],
            'gap': or_['issues'],
            'calculation_basis': f'当日累计卖出vs可卖数量; 整手={or_["sell_quantity"] % 100 == 0}',
            'evidence_file': '持仓批次+候选方案',
        })
        metrics.append({
            'plan_id': plan_id,
            'metric_name': f'order_{or_["order_sequence"]}_{or_["ts_code"]}_quote',
            'actual_value': or_['current_price_cny'],
            'threshold': f'quote_age<={quote_max_age}s, 连续竞价, 跌停<price<=涨停',
            'status': or_['status'],
            'gap': or_['issues'],
            'calculation_basis': f'quote_age={to_float(q.get("quote_age_seconds")) if q else "N/A"}s, trading_status={q.get("trading_status","N/A") if q else "N/A"}',
            'evidence_file': '持仓标的报价',
        })
        if or_['participation_rate'] is not None and or_['participation_rate'] != float('inf'):
            metrics.append({
                'plan_id': plan_id,
                'metric_name': f'order_{or_["order_sequence"]}_{or_["ts_code"]}_adv20_participation',
                'actual_value': or_['participation_rate'],
                'threshold': max_participation,
                'status': '通过' if or_['participation_rate'] <= max_participation else '不通过',
                'gap': or_['participation_rate'] - max_participation,
                'calculation_basis': f'名义金额{or_["notional_cny"]:.2f}/ADV20{or_["adv20_cny"]:.2f}',
                'evidence_file': '持仓标的日线+报价',
            })

    all_pass = all(m['status'] == '通过' for m in metrics)

    # 敏感性1：全部订单按slippage_cap执行
    sens_cap_net_proceeds = 0.0
    sens_cap_fees = 0.0
    for or_ in order_results:
        if or_['status'] == '通过' and or_['current_price_cny']:
            cap_exec_price = or_['current_price_cny'] * (1 - slip_cap / 10000)
            cap_exec = or_['sell_quantity'] * cap_exec_price
            cap_comm = max(min_commission, cap_exec * commission_rate)
            cap_stamp = cap_exec * stamp_rate
            cap_trans = cap_exec * transfer_rate
            cap_fee = cap_comm + cap_stamp + cap_trans
            sens_cap_net_proceeds += cap_exec - cap_fee
            sens_cap_fees += cap_fee
    sens_cap_cash = recon['net_cash_cny'] + sens_cap_net_proceeds
    sens_cap_na = post_stock_mv + sens_cap_cash

    # 敏感性2：CVaR窗口缩短为最近200共同有效日
    sens_200_dates = common_dates[-200:] if len(common_dates) >= 200 else common_dates
    if cvar_ok and plan_feasible and len(sens_200_dates) >= 200:
        cvar_200, _, _, _, m200 = compute_historical_cvar(
            post_weights, daily, sens_200_dates, by_code_date,
            cash_daily_return, confidence)
    else:
        cvar_200 = None
        m200 = 0

    return {
        'plan_id': plan_id,
        'plan_feasible': plan_feasible,
        'all_constraints_pass': all_pass and plan_feasible,
        'order_results': order_results,
        'next_day_orders': next_day_results,
        'allocation_rows': allocation_rows,
        'post_stock_mv_cny': post_stock_mv,
        'post_net_cash_cny': post_net_cash,
        'post_net_asset_cny': post_na,
        'na_check_diff': na_check,
        'post_equity_exposure': post_equity,
        'post_cash_weight': post_cash_w,
        'post_top3_weight': post_top3,
        'post_stocks': post_stock_list,
        'post_cvar95': cvar,
        'post_tail_contributions': tail_contrib,
        'current_cvar95': cur_cvar,
        'current_tail_contributions': cur_tail_contrib,
        'total_net_proceeds_cny': total_net_proceeds,
        'total_fees_cny': total_fees,
        'total_slippage_loss_cny': total_slippage_loss,
        'total_realized_pnl_cny': total_realized_pnl,
        'total_exec_amount_cny': total_exec_amount,
        'metrics': metrics,
        'blockers': blockers,
        'sensitivity': {
            'cap_slippage_net_asset_cny': sens_cap_na,
            'cap_slippage_cash_weight': sens_cap_cash / sens_cap_na if sens_cap_na else 0,
            'cvar_200day': cvar_200,
            'cvar_200day_m': m200,
        },
    }


def compute_correlation_matrix(daily, common_dates, by_code_date):
    """计算250日样本的Pearson相关系数矩阵（ddof=1）。"""
    codes = sorted(by_code_date.keys())
    returns = {}
    for code in codes:
        returns[code] = [to_float(by_code_date[code][d]['pct_chg'], 0) / 100.0
                         for d in common_dates]

    n = len(common_dates)
    # 均值
    means = {c: sum(returns[c]) / n for c in codes}
    # 标准差 ddof=1
    stds = {}
    for c in codes:
        variance = sum((r - means[c]) ** 2 for r in returns[c]) / (n - 1)
        stds[c] = math.sqrt(variance)

    corr = {}
    for c1 in codes:
        corr[c1] = {}
        for c2 in codes:
            cov = sum((returns[c1][i] - means[c1]) * (returns[c2][i] - means[c2])
                      for i in range(n)) / (n - 1)
            corr[c1][c2] = cov / (stds[c1] * stds[c2]) if stds[c1] > 0 and stds[c2] > 0 else 0.0
    return corr, codes


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='稳健账户A持仓减仓方案审批复核')
    parser.add_argument('--input-dir', default=os.path.dirname(os.path.abspath(__file__)),
                        help='输入CSV所在目录')
    parser.add_argument('--output-dir', default=os.path.dirname(os.path.abspath(__file__)),
                        help='输出CSV/JSON所在目录')
    args = parser.parse_args()

    input_dir = args.input_dir
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 70)
    print("稳健账户A 持仓减仓方案审批复核")
    print(f"决策时点: 2025-12-31T14:45:00+08:00")
    print("=" * 70)

    # 加载数据
    data = load_data(input_dir)

    # 审计
    audit = audit_data(data)
    print("\n[1] 数据审计")
    print(f"  日线: {audit['daily']['rows']}行, {audit['daily']['unique_ts_code_trade_date']}唯一键, 重复{audit['daily']['duplicates']}")
    print(f"  批次: {audit['lots']['rows']}行, 重复{audit['lots']['duplicates']}")
    print(f"  代码一致性: lot_not_in_quote={audit['code_consistency']['lot_not_in_quote']}, "
          f"plan_not_in_lot={audit['code_consistency']['plan_not_in_lot']}")

    # 勾稽
    recon = reconcile_account(data)
    print(f"\n[2] 账户勾稽")
    print(f"  股票总市值: {recon['total_stock_mv_cny']:,.2f}")
    print(f"  净现金: {recon['net_cash_cny']:,.2f}")
    print(f"  复算净资产: {recon['recalc_net_asset_cny']:,.2f}")
    print(f"  上报净资产: {recon['reported_net_asset_cny']:,.2f}")
    print(f"  差额: {recon['difference_cny']:,.2f} → {'勾稽通过' if recon['reconciled'] else '勾稽失败'}")

    if not recon['reconciled']:
        print("\n[FATAL] 账户快照无法勾稽，停止审批。")
        # 仍输出基础信息
        return

    constraints = data['constraints'][0]
    quotes = {r['ts_code']: r for r in data['quotes']}

    # ADV20
    adv20 = compute_adv20(data['daily'])
    print(f"\n[3] ADV20 (最近20个有行情日, 截至2025-12-30)")
    for code in sorted(adv20):
        print(f"  {code}: {adv20[code]:,.2f} 元")

    # 共同交易日
    risk_common_days = to_int(constraints['risk_history_common_days'], 250)
    common_dates, by_code_date, total_common = find_common_trading_days(data['daily'], risk_common_days)
    print(f"\n[4] 共同有效交易日: 总计{total_common}天, 取用{len(common_dates)}天 (要求>={risk_common_days})")
    print(f"  区间: {common_dates[0]} ~ {common_dates[-1]}")

    cvar_available = total_common >= risk_common_days
    if not cvar_available:
        print(f"  [WARN] 共同有效日不足，CVaR审批停止")

    # 当前风险画像
    profile = current_risk_profile(recon, constraints, adv20)
    print(f"\n[5] 当前风险画像")
    print(f"  股票仓位: {profile['equity_exposure']:.4%} (上限{to_float(constraints['equity_exposure_max']):.0%})")
    print(f"  净现金权重: {profile['cash_weight']:.4%} (下限{to_float(constraints['minimum_cash_weight']):.0%})")
    print(f"  前3大权重: {profile['top3_weight']:.4%} (上限{to_float(constraints['top3_weight_max']):.0%})")
    print(f"  未实现盈亏: {profile['total_unrealized_pnl_cny']:,.2f}")
    for s in profile['stocks']:
        print(f"  {s['ts_code']} {s['security_name']}: 权重{s['weight']:.4%}, "
              f"市值{s['market_value_cny']:,.2f}, 可卖{s['available_to_sell']}/{s['quantity']}")

    # 当前CVaR
    if cvar_available:
        cur_weights = {s['ts_code']: s['weight'] for s in profile['stocks']}
        cur_cvar, cur_tail, _, _, cur_m = compute_historical_cvar(
            cur_weights, data['daily'], common_dates, by_code_date,
            to_float(constraints['cash_daily_return'], 0),
            to_float(constraints['risk_confidence'], 0.95))
        print(f"\n  当前CVaR95: {cur_cvar:.6f} (上限{to_float(constraints['historical_cvar95_max'])})")
        print(f"  尾部样本数m={cur_m}")
    else:
        cur_cvar = None
        cur_tail = {}

    # 相关性矩阵
    corr_matrix, corr_codes = compute_correlation_matrix(data['daily'], common_dates, by_code_date)
    print(f"\n  相关性矩阵(Pearson, ddof=1, {len(common_dates)}日):")
    for c1 in corr_codes:
        row = "  ".join(f"{corr_matrix[c1][c2]:+.3f}" for c2 in corr_codes)
        print(f"    {c1}: {row}")

    # 方案评估
    plans_by_id = defaultdict(list)
    for p in data['plans']:
        plans_by_id[p['plan_id']].append(p)

    plan_results = {}
    print(f"\n[6] 候选方案评估")
    for plan_id in sorted(plans_by_id.keys()):
        orders = sorted(plans_by_id[plan_id], key=lambda x: to_int(x['order_sequence']))
        print(f"\n  --- {plan_id} ---")
        result = evaluate_plan(plan_id, orders, recon, constraints, quotes, adv20,
                               data['daily'], common_dates, by_code_date, total_common)
        plan_results[plan_id] = result

        print(f"  可行性: {'是' if result['plan_feasible'] else '否'}")
        print(f"  全部约束通过: {'是' if result['all_constraints_pass'] else '否'}")
        print(f"  交易后净资产: {result['post_net_asset_cny']:,.2f} (校验差{result['na_check_diff']:.4f})")
        print(f"  交易后股票仓位: {result['post_equity_exposure']:.4%}")
        print(f"  交易后现金权重: {result['post_cash_weight']:.4%}")
        print(f"  交易后前3大: {result['post_top3_weight']:.4%}")
        print(f"  交易后CVaR95: {result['post_cvar95']}")
        print(f"  总费用: {result['total_fees_cny']:,.2f}")
        print(f"  总滑点损失: {result['total_slippage_loss_cny']:,.2f}")
        if result['blockers']:
            for b in result['blockers']:
                print(f"  [BLOCKER] {b}")
        for m in result['metrics']:
            if m['metric_name'].startswith('order_'):
                continue
            print(f"    {m['metric_name']}: actual={m['actual_value']}, threshold={m['threshold']}, status={m['status']}")

    # 主方案选择
    approved = [pid for pid, r in plan_results.items() if r['all_constraints_pass']]
    if len(approved) == 1:
        main_plan = approved[0]
        main_reason = '唯一满足全部硬约束的方案'
    elif len(approved) > 1:
        # 排序：CVaR低 → 总交易成本低 → 总卖出金额低 → plan_id升序
        approved.sort(key=lambda pid: (
            plan_results[pid]['post_cvar95'] if plan_results[pid]['post_cvar95'] is not None else float('inf'),
            plan_results[pid]['total_fees_cny'],
            plan_results[pid]['total_exec_amount_cny'],
            pid
        ))
        main_plan = approved[0]
        main_reason = '多方案通过，按CVaR→成本→卖出金额排序选定'
    else:
        main_plan = None
        main_reason = '无方案满足全部硬约束'

    print(f"\n[7] 主方案: {main_plan} ({main_reason})")

    # -----------------------------------------------------------------------
    # 输出 CSV
    # -----------------------------------------------------------------------

    # 1. 当前持仓风险快照.csv
    snapshot_path = os.path.join(output_dir, '当前持仓风险快照.csv')
    with open(snapshot_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['ts_code', 'security_name', 'quantity', 'available_to_sell',
                         'current_price_cny', 'market_value_cny', 'weight',
                         'unrealized_pnl_cny', 'adv20_cny', 'current_cvar_contribution',
                         'breach_flag'])
        single_max = to_float(constraints['single_name_weight_max'])
        for s in profile['stocks']:
            contrib = cur_tail.get(s['ts_code'], '')
            breach = '是' if s['weight'] > single_max else '否'
            writer.writerow([
                s['ts_code'], s['security_name'], s['quantity'], s['available_to_sell'],
                s['current_price_cny'], round(s['market_value_cny'], 2),
                round(s['weight'], 6), round(s['unrealized_pnl_cny'], 2),
                round(s['adv20_cny'], 2),
                round(contrib, 8) if contrib != '' else '',
                breach
            ])
    print(f"\n[输出] {snapshot_path}")

    # 2. 候选方案约束矩阵.csv
    matrix_path = os.path.join(output_dir, '候选方案约束矩阵.csv')
    all_metrics = []
    for pid in sorted(plan_results.keys()):
        all_metrics.extend(plan_results[pid]['metrics'])
    with open(matrix_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['plan_id', 'metric_name', 'actual_value', 'threshold',
                         'status', 'gap', 'calculation_basis', 'evidence_file'])
        for m in all_metrics:
            av = m['actual_value']
            if isinstance(av, float):
                av = round(av, 8)
            gap = m['gap']
            if isinstance(gap, float):
                gap = round(gap, 8)
            writer.writerow([m['plan_id'], m['metric_name'], av, m['threshold'],
                             m['status'], gap, m['calculation_basis'], m['evidence_file']])
    print(f"[输出] {matrix_path}")

    # 3. 批次卖出分配.csv
    alloc_path = os.path.join(output_dir, '批次卖出分配.csv')
    all_allocs = []
    for pid in sorted(plan_results.keys()):
        all_allocs.extend(plan_results[pid]['allocation_rows'])
        # 对不可执行订单也记录一行
        for or_ in plan_results[pid]['order_results']:
            if or_['status'] != '通过':
                all_allocs.append({
                    'plan_id': pid,
                    'order_sequence': or_['order_sequence'],
                    'ts_code': or_['ts_code'],
                    'security_name': or_['security_name'],
                    'lot_id': '',
                    'buy_date': '',
                    'allocated_quantity': 0,
                    'cost_price_cny': '',
                    'estimated_execution_price': or_['estimated_execution_price'] if or_['estimated_execution_price'] else '',
                    'estimated_fee': or_['total_fee_cny'] if or_['total_fee_cny'] else '',
                    'realized_pnl_cny': '',
                    'eligibility_status': or_['status'] + (': ' + or_['issues'] if or_['issues'] else ''),
                })
    with open(alloc_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['plan_id', 'order_sequence', 'ts_code', 'security_name',
                         'lot_id', 'buy_date', 'allocated_quantity', 'cost_price_cny',
                         'estimated_execution_price', 'estimated_fee',
                         'realized_pnl_cny', 'eligibility_status'])
        for a in sorted(all_allocs, key=lambda x: (x['plan_id'], x['order_sequence'], x.get('lot_id', ''))):
            writer.writerow([
                a['plan_id'], a['order_sequence'], a['ts_code'], a['security_name'],
                a['lot_id'], a['buy_date'], a['allocated_quantity'],
                a['cost_price_cny'],
                round(a['estimated_execution_price'], 4) if isinstance(a.get('estimated_execution_price'), (int, float)) else a.get('estimated_execution_price', ''),
                round(a['estimated_fee'], 2) if isinstance(a.get('estimated_fee'), (int, float)) else a.get('estimated_fee', ''),
                round(a['realized_pnl_cny'], 2) if isinstance(a.get('realized_pnl_cny'), (int, float)) else a.get('realized_pnl_cny', ''),
                a['eligibility_status'],
            ])
    print(f"[输出] {alloc_path}")

    # 4. 审批监控条件.csv
    monitor_path = os.path.join(output_dir, '审批监控条件.csv')
    monitor_conditions = build_monitor_conditions(main_plan, plan_results, constraints, recon, quotes)
    with open(monitor_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['condition_id', 'if_condition', 'threshold', 'then_action',
                         'owner', 'check_deadline', 'evidence_needed', 'stop_condition'])
        for mc in monitor_conditions:
            writer.writerow([mc[c] for c in ['condition_id', 'if_condition', 'threshold',
                                              'then_action', 'owner', 'check_deadline',
                                              'evidence_needed', 'stop_condition']])
    print(f"[输出] {monitor_path}")

    # 5. review_results.json（供备忘录引用）
    json_path = os.path.join(output_dir, 'review_results.json')
    # 转换不可序列化类型
    def default_serializer(o):
        if isinstance(o, (set,)):
            return list(o)
        return str(o)

    output_json = {
        'audit': audit,
        'reconciliation': {k: v for k, v in recon.items() if k != 'lot_details'},
        'reconciliation_lot_count': len(recon['lot_details']),
        'adv20': adv20,
        'common_days': {
            'total': total_common,
            'used': len(common_dates),
            'required': risk_common_days,
            'cvar_available': cvar_available,
            'range': [common_dates[0], common_dates[-1]] if common_dates else [],
        },
        'current_profile': {
            'equity_exposure': profile['equity_exposure'],
            'cash_weight': profile['cash_weight'],
            'top3_weight': profile['top3_weight'],
            'total_unrealized_pnl_cny': profile['total_unrealized_pnl_cny'],
            'net_asset_cny': profile['net_asset_cny'],
            'stocks': profile['stocks'],
            'current_cvar95': cur_cvar,
            'current_tail_contributions': cur_tail,
            'correlation_matrix': corr_matrix,
            'correlation_codes': corr_codes,
        },
        'plan_results': {},
        'main_plan': main_plan,
        'main_plan_reason': main_reason,
        'approved_plans': approved,
    }
    for pid, r in plan_results.items():
        output_json['plan_results'][pid] = {
            'plan_feasible': r['plan_feasible'],
            'all_constraints_pass': r['all_constraints_pass'],
            'post_net_asset_cny': r['post_net_asset_cny'],
            'post_equity_exposure': r['post_equity_exposure'],
            'post_cash_weight': r['post_cash_weight'],
            'post_top3_weight': r['post_top3_weight'],
            'post_cvar95': r['post_cvar95'],
            'post_tail_contributions': r['post_tail_contributions'],
            'total_net_proceeds_cny': r['total_net_proceeds_cny'],
            'total_fees_cny': r['total_fees_cny'],
            'total_slippage_loss_cny': r['total_slippage_loss_cny'],
            'total_realized_pnl_cny': r['total_realized_pnl_cny'],
            'total_exec_amount_cny': r['total_exec_amount_cny'],
            'blockers': r['blockers'],
            'post_stocks': r['post_stocks'],
            'order_results': r['order_results'],
            'next_day_orders': r['next_day_orders'],
            'sensitivity': r['sensitivity'],
            'na_check_diff': r['na_check_diff'],
        }

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(output_json, f, ensure_ascii=False, indent=2, default=default_serializer)
    print(f"[输出] {json_path}")

    print("\n" + "=" * 70)
    print("复核完成。全部输出已生成。")
    print("=" * 70)


def build_monitor_conditions(main_plan, plan_results, constraints, recon, quotes):
    """构建审批监控条件列表。"""
    conditions = []
    cid = 1
    approval_deadline = constraints.get('approval_deadline', '2025-12-31T14:50:00+08:00')
    quote_max_age = to_float(constraints['quote_max_age_seconds'])

    if main_plan:
        r = plan_results[main_plan]
        conditions.append({
            'condition_id': f'MC{cid:03d}',
            'if_condition': f'如果{main_plan}全部当日订单在审批截止前报价仍有效且可卖量未变',
            'threshold': f'quote_age<={quote_max_age}s, 连续竞价, 累计卖出<=可卖',
            'then_action': f'那么提交投资负责人批准{main_plan}并由交易台按限价委托执行',
            'owner': '投顾风控组',
            'check_deadline': approval_deadline,
            'evidence_needed': '刷新后的报价快照、可卖数量确认、订单执行回执',
            'stop_condition': '任一硬约束复算不通过则停止执行',
        })
        cid += 1

    # 方案C的茅台报价问题
    if '方案C' in plan_results:
        conditions.append({
            'condition_id': f'MC{cid:03d}',
            'if_condition': '如果贵州茅台(600519.SH)报价在审批截止前刷新至quote_age<=300s且交易状态为连续竞价',
            'threshold': f'quote_age<={quote_max_age}s, 连续竞价, 跌停<price<=涨停',
            'then_action': '那么方案C订单2可重新评估；若其余约束复算仍通过，提交投资负责人重新审批',
            'owner': '交易台+投顾风控组',
            'check_deadline': approval_deadline,
            'evidence_needed': '刷新后的600519.SH报价快照',
            'stop_condition': '审批截止前报价未刷新则方案C当日不可批准',
        })
        cid += 1

    # 通用：硬约束不通过
    conditions.append({
        'condition_id': f'MC{cid:03d}',
        'if_condition': '如果任一方案的equity_exposure、single_name_weight、top3_weight、historical_cvar95或minimum_cash_weight在复算后仍不通过',
        'threshold': '账户约束表中对应阈值',
        'then_action': '那么拒绝该方案执行，要求交易台修改卖出数量或标的后重新提交',
        'owner': '投顾风控组',
        'check_deadline': approval_deadline,
        'evidence_needed': '约束矩阵复算结果',
        'stop_condition': '全部硬约束通过前不得批准',
    })
    cid += 1

    # T+1锁定
    conditions.append({
        'condition_id': f'MC{cid:03d}',
        'if_condition': '如果方案试图卖出buy_date=2025-12-31且available_to_sell=0的批次（宁德时代P02或比亚迪P04）',
        'threshold': 'available_to_sell=0的批次不得分配',
        'then_action': '那么该订单判为不可执行，需从可卖批次重新分配或减少卖出量',
        'owner': '交易台',
        'check_deadline': approval_deadline,
        'evidence_needed': '持仓批次表available_to_sell字段',
        'stop_condition': 'T+1批次解禁前（下一交易日）不得卖出',
    })
    cid += 1

    # 下一交易日订单
    conditions.append({
        'condition_id': f'MC{cid:03d}',
        'if_condition': '如果方案C订单3（比亚迪30000股，下一交易日）需在2026-01-05执行',
        'threshold': '下一交易日开盘后重新校验报价、可卖量和约束',
        'then_action': '那么在下一交易日开盘后重新跑本复核脚本，确认约束通过后再提交审批',
        'owner': '交易台+投顾风控组',
        'check_deadline': '2026-01-05T09:30:00+08:00',
        'evidence_needed': '下一交易日最新报价、更新后的持仓批次、复算约束矩阵',
        'stop_condition': '下一交易日约束不通过则取消该订单',
    })
    cid += 1

    # 参与率监控
    conditions.append({
        'condition_id': f'MC{cid:03d}',
        'if_condition': '如果实际成交导致单订单参与率超过max_adv20_participation=5%',
        'threshold': '订单名义金额/ADV20 <= 0.05',
        'then_action': '那么暂停该订单剩余部分执行，拆分到后续交易日',
        'owner': '交易台',
        'check_deadline': '执行当日收盘',
        'evidence_needed': '成交回报、ADV20更新值',
        'stop_condition': '参与率回到阈值内',
    })
    cid += 1

    # 账户勾稽
    conditions.append({
        'condition_id': f'MC{cid:03d}',
        'if_condition': '如果复算净资产与reported_net_asset_cny绝对差超过1元',
        'threshold': '|复算净资产 - 上报净资产| <= 1元',
        'then_action': '那么停止全部审批，要求运营提供更正后的持仓或现金快照',
        'owner': '运营+投顾风控组',
        'check_deadline': approval_deadline,
        'evidence_needed': '更正后的持仓批次表、账户约束表',
        'stop_condition': '勾稽差额<=1元前不得审批',
    })

    return conditions


if __name__ == '__main__':
    main()
