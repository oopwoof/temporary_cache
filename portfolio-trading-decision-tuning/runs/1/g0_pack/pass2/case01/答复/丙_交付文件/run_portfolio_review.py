#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""稳健账户A持仓复核。一键从五个原始附件重建客观CSV和备忘录；不联网。"""
from __future__ import annotations
import argparse, math
from pathlib import Path
import pandas as pd
import numpy as np

FILES={
 'daily':'持仓标的日线_20240102_20251230.csv','lots':'持仓批次_20251231T1445.csv',
 'quotes':'持仓标的报价_20251231T1445.csv','rules':'账户与交易约束_20251231T1445.csv',
 'plans':'候选减仓方案_20251231.csv'}

def cvar(weights, returns, confidence):
    rp=returns.mul(pd.Series(weights),axis=1).sum(axis=1)
    losses=-rp
    m=int(math.ceil((1-confidence)*len(losses)))
    tail=losses.nlargest(m).index
    contrib=(-returns.loc[tail].mul(pd.Series(weights),axis=1)).mean()
    return float(losses.loc[tail].mean()),contrib,tail

def money(x): return f'{x:,.2f}'
def pct(x): return f'{x:.4%}'

def run(inp:Path,out:Path):
    out.mkdir(parents=True,exist_ok=True)
    daily=pd.read_csv(inp/FILES['daily']); lots=pd.read_csv(inp/FILES['lots'])
    quotes=pd.read_csv(inp/FILES['quotes']); rules=pd.read_csv(inp/FILES['rules']).iloc[0]
    plans=pd.read_csv(inp/FILES['plans'])
    # 审计
    audit=[]
    for name,df,key in [('日线',daily,['ts_code','trade_date']),('持仓',lots,['lot_id']),('报价',quotes,['ts_code']),('约束',pd.DataFrame([rules]),['account_label']),('方案',plans,['plan_id','order_sequence'])]:
        audit.append((name,len(df),int(df.duplicated(key).sum()),int(df.isna().sum().sum())))
    q=quotes.set_index('ts_code')
    lot=lots.copy(); lot['market_value_cny']=lot.apply(lambda r:r.quantity*q.loc[r.ts_code,'current_price_cny']*q.loc[r.ts_code,'fx_to_cny'],axis=1)
    lot['unrealized_pnl_cny']=lot.apply(lambda r:(q.loc[r.ts_code,'current_price_cny']-r.cost_price_cny)*r.quantity,axis=1)
    pos=lot.groupby(['ts_code','security_name'],as_index=False).agg(quantity=('quantity','sum'),available_to_sell=('available_to_sell','sum'),market_value_cny=('market_value_cny','sum'),unrealized_pnl_cny=('unrealized_pnl_cny','sum'))
    net_cash=float(rules.cash_balance_cny-rules.accrued_fees_payable_cny)
    equity=float(pos.market_value_cny.sum()); nav=equity+net_cash; reported=float(rules.reported_net_asset_cny); diff=nav-reported
    # 日线风险样本与ADV
    valid=daily[daily.record_status.eq('有行情')].copy(); valid['ret']=valid.pct_chg/100
    adv=valid.sort_values('trade_date').groupby('ts_code').tail(20).groupby('ts_code').amount.mean()*1000
    pivot=valid.pivot(index='trade_date',columns='ts_code',values='ret').dropna().sort_index().tail(int(rules.risk_history_common_days))
    codes=pos.ts_code.tolist()
    if len(pivot)<int(rules.risk_history_common_days): raise ValueError(f'共同有效日不足: {len(pivot)}')
    pos['weight']=pos.market_value_cny/nav; pos['adv20_cny']=pos.ts_code.map(adv)
    weights=pos.set_index('ts_code').weight.to_dict()
    cur_cv,cur_cc,tail=cvar(weights,pivot[codes],float(rules.risk_confidence))
    pos['current_cvar_contribution']=pos.ts_code.map(cur_cc)
    cur_top3=float(pos.nlargest(3,'weight').weight.sum())
    pos['breach_flag']=np.where(pos.weight>rules.single_name_weight_max,'单票超限','通过')
    pos[['ts_code','quantity','available_to_sell','market_value_cny','weight','unrealized_pnl_cny','adv20_cny','current_cvar_contribution','breach_flag']].to_csv(out/'当前持仓风险快照.csv',index=False,float_format='%.10f')
    corr=pivot[codes].corr()
    corr.to_csv(out/'250日相关矩阵.csv',float_format='%.8f')
    # 方案
    matrices=[]; allocs=[]; summaries=[]
    base_qty=pos.set_index('ts_code').quantity.to_dict(); base_av=pos.set_index('ts_code').available_to_sell.to_dict()
    for pid,orders_all in plans.groupby('plan_id',sort=True):
        orders=orders_all[orders_all.execution_window.eq('当日')].sort_values('order_sequence')
        unknown=set(orders.ts_code)-set(q.index); structural=[]; quote_cond=[]
        sells={}; total_cost=total_net=total_exec=0.0
        remaining={k:int(v) for k,v in base_qty.items()}; lots_left=lot.copy(); lots_left['avail_left']=lots_left.available_to_sell
        for _,o in orders.iterrows():
            code=o.ts_code; qty=int(o.sell_quantity)
            if code in unknown:
                structural.append(f'{code}未知代码'); continue
            qr=q.loc[code]; reasons=[]
            if qty>base_av.get(code,0): reasons.append(f'超可卖{base_av.get(code,0)}股')
            if qty%int(qr.board_lot_shares): reasons.append('非整手')
            if qr.trading_status!='连续竞价': quote_cond.append(f'{code}非连续竞价')
            if float(qr.quote_age_seconds)>float(rules.quote_max_age_seconds): quote_cond.append(f'{code}报价陈旧{qr.quote_age_seconds}秒')
            if not(float(qr.current_price_cny)>float(qr.limit_down_cny) and float(qr.current_price_cny)<=float(qr.limit_up_cny)): quote_cond.append(f'{code}价格不在可批准区间')
            ref=qty*float(qr.current_price_cny); participation=ref/float(adv.loc[code])
            if participation>float(rules.max_adv20_participation): reasons.append(f'ADV参与率{participation:.4%}超限')
            structural+= [f'{code}:{x}' for x in reasons]
            slip=min(float(rules.slippage_cap_bps),float(rules.slippage_base_bps)+float(rules.slippage_sqrt_coefficient_bps)*math.sqrt(participation))
            px=float(qr.current_price_cny)*(1-slip/10000); exec_amt=qty*px
            comm=max(float(rules.minimum_commission_cny),exec_amt*float(rules.commission_rate)); stamp=exec_amt*float(rules.stamp_duty_sell_rate); transfer=exec_amt*float(rules.transfer_fee_rate); fee=comm+stamp+transfer; net=exec_amt-fee
            total_cost += qty*(float(qr.current_price_cny)-px)+fee; total_exec+=exec_amt; total_net+=net
            remaining[code]-=qty; sells[code]=sells.get(code,0)+qty
            need=qty; elig='可分配' if not reasons else '结构不合规'
            for ix,r in lots_left[(lots_left.ts_code==code)&(lots_left.avail_left>0)].sort_values(['buy_date','lot_id']).iterrows():
                take=min(need,int(r.avail_left));
                if take<=0: continue
                share_fee=fee*take/qty
                allocs.append(dict(plan_id=pid,order_sequence=int(o.order_sequence),ts_code=code,lot_id=r.lot_id,allocated_quantity=take,estimated_execution_price=px,estimated_fee=share_fee,realized_pnl_cny=(px-float(r.cost_price_cny))*take-share_fee,eligibility_status=elig))
                lots_left.loc[ix,'avail_left']-=take; need-=take
                if need==0: break
            if need>0: allocs.append(dict(plan_id=pid,order_sequence=int(o.order_sequence),ts_code=code,lot_id='UNALLOCATED',allocated_quantity=need,estimated_execution_price=px,estimated_fee=0,realized_pnl_cny=np.nan,eligibility_status='不可卖批次不足'))
        post_mv={c:remaining[c]*float(q.loc[c,'current_price_cny']) for c in remaining}
        post_eq=sum(post_mv.values()); post_cash=net_cash+total_net; post_nav=post_eq+post_cash
        w={c:post_mv[c]/post_nav for c in remaining}; pcv,pcc,ptail=cvar(w,pivot[codes],float(rules.risk_confidence)); pcv200,_,_=cvar(w,pivot[codes].tail(200),float(rules.risk_confidence))
        max_single=max(w.values()); top3=sum(sorted(w.values(),reverse=True)[:3]); eq_exp=post_eq/post_nav; cash_w=post_cash/post_nav
        # cap sensitivity only affects nav/weights, not holdings
        cap_exec=cap_fee=cap_net=0
        for _,o in orders.iterrows():
            if o.ts_code not in q.index: continue
            qr=q.loc[o.ts_code]; qty=int(o.sell_quantity); px=float(qr.current_price_cny)*(1-float(rules.slippage_cap_bps)/10000); amt=qty*px
            fee=max(float(rules.minimum_commission_cny),amt*float(rules.commission_rate))+amt*(float(rules.stamp_duty_sell_rate)+float(rules.transfer_fee_rate)); cap_exec+=amt; cap_fee+=fee; cap_net+=amt-fee
        cap_nav=sum(post_mv.values())+net_cash+cap_net
        cap_cash_w=(net_cash+cap_net)/cap_nav
        metrics=[
          ('账户勾稽',abs(diff),1.0,'通过' if abs(diff)<=1 else '不通过',max(abs(diff)-1,0),'复算NAV与reported差额'),
          ('当日订单结构及可卖性',len(structural),0,'通过' if not structural else '不通过',len(structural),'T+1/整手/ADV/代码检查'),
          ('报价与交易状态',len(quote_cond),0,'通过' if not quote_cond else '无法评估',len(quote_cond),'时效/连续竞价/涨跌停检查'),
          ('股票仓位',eq_exp,float(rules.equity_exposure_max),'通过' if eq_exp<=rules.equity_exposure_max else '不通过',max(eq_exp-rules.equity_exposure_max,0),'交易后市值/交易后NAV'),
          ('最大单票权重',max_single,float(rules.single_name_weight_max),'通过' if max_single<=rules.single_name_weight_max else '不通过',max(max_single-rules.single_name_weight_max,0),'交易后最大单票'),
          ('前3大权重',top3,float(rules.top3_weight_max),'通过' if top3<=rules.top3_weight_max else '不通过',max(top3-rules.top3_weight_max,0),'交易后前三大'),
          ('historical_CVaR95',pcv,float(rules.historical_cvar95_max),'通过' if pcv<=rules.historical_cvar95_max else '不通过',max(pcv-rules.historical_cvar95_max,0),'共同250日历史模拟'),
          ('净现金权重',cash_w,float(rules.minimum_cash_weight),'通过' if cash_w>=rules.minimum_cash_weight else '不通过',max(rules.minimum_cash_weight-cash_w,0),'交易后净现金/NAV'),
          ('cap滑点现金权重',cap_cash_w,float(rules.minimum_cash_weight),'敏感性','', '全部订单50bps'),
          ('200日historical_CVaR95',pcv200,float(rules.historical_cvar95_max),'敏感性','', '最近200共同有效日')]
        for n,a,t,s,g,b in metrics: matrices.append(dict(plan_id=pid,metric_name=n,actual_value=a,threshold=t,status=s,gap=g,calculation_basis=b,evidence_file='五个原始附件'))
        hard_fail=structural or any(s=='不通过' for n,a,t,s,g,b in metrics[3:8]) or abs(diff)>1
        if hard_fail: decision='拒绝'
        elif quote_cond: decision='待条件恢复'
        else: decision='可批准'
        summaries.append(dict(plan_id=pid,decision=decision,post_nav=post_nav,equity_exposure=eq_exp,cash_weight=cash_w,max_single=max_single,top3=top3,cvar95=pcv,cvar200=pcv200,total_trade_cost=total_cost,total_sell_notional=total_exec,structural='；'.join(structural) or '无',quote_conditions='；'.join(quote_cond) or '无'))
    md=pd.DataFrame(matrices); ad=pd.DataFrame(allocs); sd=pd.DataFrame(summaries)
    md.to_csv(out/'候选方案约束矩阵.csv',index=False,float_format='%.10f'); ad.to_csv(out/'批次卖出分配.csv',index=False,float_format='%.10f'); sd.to_csv(out/'方案汇总.csv',index=False,float_format='%.10f')
    eligible=sd[sd.decision.eq('可批准')].sort_values(['cvar95','total_trade_cost','total_sell_notional','plan_id'])
    main=eligible.iloc[0].plan_id if len(eligible) else None
    mon=pd.DataFrame([
      ['C01','如果任一拟卖标的报价年龄超过300秒或状态非连续竞价','quote_age<=300且连续竞价','那么不执行并刷新报价后重新审批','交易台','2025-12-31 14:50+08:00','有效报价快照','截止时仍不满足'],
      ['C02','如果订单累计数量超过批次可卖量或非100股整数倍','数量<=available且整手','那么拒绝该方案并改案','交易台','2025-12-31 14:50+08:00','持仓批次与委托草案','任一结构约束失败'],
      ['C03','如果刷新报价后五项交易后硬约束均通过','全部硬约束通过','那么提交投资负责人重新审批；获批后才可传达交易台','风控/投资负责人','2025-12-31 14:50+08:00','约束矩阵复算','任一硬约束不通过'],
      ['C04','如果下一交易日拟执行方案C第3笔','不得计入今日合规','那么以次日新报价、可卖量和ADV重新完整审批','风控','下一交易日开盘前','新快照','不得提前视为成交']
    ],columns=['condition_id','if_condition','threshold','then_action','owner','check_deadline','evidence_needed','stop_condition'])
    mon.to_csv(out/'审批监控条件.csv',index=False)
    # memo
    lines=['# 持仓交易审批备忘录','','> 决策时点：2025-12-31 14:45（Asia/Shanghai）。本文件仅为内部条件化审批意见，不代表已下单或成交。','',
    '## 一、结论先行',f'- 账户复算净资产 **{money(nav)}元**，报告值 **{money(reported)}元**，差额 **{money(diff)}元**，账户勾稽：**{"通过" if abs(diff)<=1 else "停止审批"}**。']
    for _,r in sd.iterrows(): lines.append(f'- {r.plan_id}：**{r.decision}**；交易后股票仓位{pct(r.equity_exposure)}、现金{pct(r.cash_weight)}、最大单票{pct(r.max_single)}、前3大{pct(r.top3)}、CVaR95 {pct(r.cvar95)}。结构问题：{r.structural}；报价条件：{r.quote_conditions}。')
    lines.append(f'- 主方案：**{main if main else "无；拒绝全部方案"}**。排序只在全部硬约束通过者中按CVaR、成本、卖出金额、plan_id依次进行。')
    lines += ['','## 二、数据审计与当前风险',f'- 五表审计（记录数/主键重复/空值）：'+ '；'.join(f'{a}{b}/{c}/{d}' for a,b,c,d in audit)+f'。日线amount按千元×1000计算ADV20。',f'- 股票市值{money(equity)}元；净现金{money(net_cash)}元；股票仓位{pct(equity/nav)}；净现金权重{pct(net_cash/nav)}；前3大{pct(cur_top3)}；250日historical CVaR95 {pct(cur_cv)}。',f'- 共同有效样本{len(pivot)}日，尾部样本{len(tail)}日；尾部贡献合计{pct(cur_cc.sum())}，与CVaR差{abs(cur_cc.sum()-cur_cv):.10f}。相关性仅作共振风险解释，不代表因果。','',
    '## 三、方案比较与反向证据']
    for _,r in sd.iterrows(): lines += [f'### {r.plan_id}｜{r.decision}',f'- 交易后NAV {money(r.post_nav)}；估计滑点与费用合计 {money(r.total_trade_cost)}；200日CVaR敏感性 {pct(r.cvar200)}。',f'- 反向证据：即使某项风险改善，也不能覆盖结构约束、报价不可用或其他硬约束失败；详见《候选方案约束矩阵.csv》。']
    lines += ['','## 四、敏感性与纪律','- 全部订单按50bps滑点上限的现金权重，以及最近200共同有效日CVaR，已逐方案列入约束矩阵；它们是风险提示，不替代主口径。','- 如果指定报价在14:50前刷新且其余约束复算仍通过，那么提交投资负责人重新审批；如果任一硬约束仍不通过，那么拒绝执行并要求交易台改案。','- 如果方案含“下一交易日”订单，那么今日不计入合规修复；次日必须以新报价、可卖批次和ADV重新审批。','',
    '## 五、数据限制','- 报价为题方脱敏仿真快照；retrieved_at不是行情时点。本分析不联网，不补公司事件或成交结果。','- 展示金额保留2位、比例4位；CSV保留更高精度。未实现盈亏不补回历史分红。']
    (out/'持仓交易审批备忘录.md').write_text('\n'.join(lines),encoding='utf-8')

if __name__=='__main__':
 p=argparse.ArgumentParser(); p.add_argument('--input-dir',required=True); p.add_argument('--output-dir',default='.')
 a=p.parse_args(); run(Path(a.input_dir),Path(a.output_dir))
