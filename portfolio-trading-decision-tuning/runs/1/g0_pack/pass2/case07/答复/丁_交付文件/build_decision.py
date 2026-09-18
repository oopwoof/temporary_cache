#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓交易决策 - 短期用款与流动性排序  可复算主脚本
运行：python3 build_decision.py
仅使用附件数据，不联网。输出 6 个 CSV + 1 个 JSON 到本目录（memo 由同目录生成）。
"""
import os, json, hashlib, math, csv, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
XLSX = "/home/user/.doubao/agent_mode/workspace/.sessions/38442461490630658/attachments/持仓交易决策_短期用款与流动性排序_附件.xlsx"

CASH_TARGET = 260000.0
NAV_BROKER = 1038800.0
WITHDRAWABLE = 58000.0
UNSETTLED_GROSS = 18000.0
DIV_B = 3750.0
OLD_SELL_FEES = 5.0 + 0.18 + 9.0          # TRD-X-01
OLD_SELL_NET = UNSETTLED_GROSS - OLD_SELL_FEES  # 17985.82
TARGET_NET = CASH_TARGET - (WITHDRAWABLE + OLD_SELL_NET)  # 184014.18

pos = {
 "A": dict(sid="SYN201.SH", name="证券A", type="A股", ind="电子", total=18000,
   broker_sellable=14000, px=22.40, mv=403200.0, vol=0.48, beta=1.35, hvol=True,
   adv=2400000, status="正常交易", slip=0.0005, cr=0.00025, lot="沪"),
 "B": dict(sid="SYN202.SZ", name="证券B", type="A股", ind="银行", total=12500,
   broker_sellable=12500, px=7.86, mv=98250.0, vol=0.18, beta=0.55, hvol=False,
   adv=3000000, status="除息日正常", slip=0.0006, cr=0.00025, lot="深"),
 "C": dict(sid="SYN203.SH", name="证券C", type="A股", ind="医药", total=8000,
   broker_sellable=8000, px=15.20, mv=121600.0, vol=0.35, beta=0.80, hvol=False,
   adv=850000, status="自8/25停牌", slip=None, cr=0.00025, lot="沪"),
 "D": dict(sid="SYN204.SZ", name="证券D", type="A股", ind="电子", total=10000,
   broker_sellable=10000, px=13.50, mv=135000.0, vol=0.62, beta=1.50, hvol=True,
   adv=180000, status="跌停零买盘", slip=0.003, cr=0.00025, lot="深"),
 "E": dict(sid="SYNETF.SH", name="宽基ETF", type="股票ETF", ind="宽基", total=50000,
   broker_sellable=50000, px=4.12, mv=206000.0, vol=0.22, beta=1.0, hvol=False,
   adv=15000000, status="正常", slip=0.0002, cr=0.0002, lot="沪"),
}
ids = ["A","B","C","D","E"]
batches = {
 "A":[(6000,18.50,27.75,1.11,"2025-12-03"),(8000,21.80,43.60,1.74,"2026-03-17"),(4000,24.00,24.00,0.96,"2026-08-31")],
 "B":[(12500,8.10,25.31,1.01,"2026-05-06")],
 "C":[(8000,14.50,29.00,1.16,"2026-01-21")],
 "D":[(10000,16.80,42.00,1.68,"2026-06-11")],
 "E":[(50000,3.92,49.00,0.0,"2026-02-11")],
}
for k,bs in batches.items():
    cost=sum(q*p+c+t for (q,p,c,t,_) in bs); qty=sum(q for (q,p,c,t,_) in bs)
    pos[k]["cost"]=round(cost,2); pos[k]["avg"]=round(cost/qty,4)

corr = {("A","A"):1,("A","B"):.18,("A","C"):.32,("A","D"):.78,("A","E"):.72,
        ("B","A"):.18,("B","B"):1,("B","C"):.20,("B","D"):.12,("B","E"):.45,
        ("C","A"):.32,("C","B"):.20,("C","C"):1,("C","D"):.28,("C","E"):.30,
        ("D","A"):.78,("D","B"):.12,("D","C"):.28,("D","D"):1,("D","E"):.65,
        ("E","A"):.72,("E","B"):.45,("E","C"):.30,("E","D"):.65,("E","E"):1}
stress = {"S1":{"A":-.18,"B":-.03,"C":-.08,"D":-.25,"E":-.09},
          "S2":{"A":-.10,"B":-.05,"C":-.20,"D":-.30,"E":-.07},
          "S3":{"A":.12,"B":.02,"C":.05,"D":.18,"E":.06}}

mv_sum=sum(p["mv"] for p in pos.values())
cash_all=WITHDRAWABLE+UNSETTLED_GROSS
nav_detail=mv_sum+cash_all
residual=nav_detail-NAV_BROKER

def sell(k,q,px=None):
    px=px or pos[k]["px"]; tt=q*px; p=pos[k]; etf=p["type"]=="股票ETF"
    comm=max(5.0,tt*p["cr"]); stamp=0.0 if etf else tt*0.0005
    tr=0.0 if etf else tt*0.00001; sl=tt*(p["slip"] or 0)
    fee=comm+stamp+tr+sl; net=tt-fee
    realized=q*(px-p["avg"])-fee
    return dict(tt=tt,comm=comm,stamp=stamp,tr=tr,sl=sl,fee=fee,net=net,realized=realized)

def risk(w):
    sig=[pos[k]["vol"] for k in ids]; ww=[w[k] for k in ids]
    var=sum(ww[i]*ww[j]*sig[i]*sig[j]*corr[(ids[i],ids[j])] for i in range(5) for j in range(5))
    vol=math.sqrt(var); fr={}
    for i,a in enumerate(ids):
        s=sum(ww[j]*sig[j]*corr[(a,ids[j])] for j in range(5))
        mctr=sig[i]*s/vol; fr[a]=ww[i]*mctr/vol
    return vol,fr

def weights(nav):
    return {k:pos[k]["mv"]/nav for k in ids}

def evaluate(sales,label):
    sales={k:int(v) for k,v in sales.items() if v}
    fee=net=real=0.0; lines=[]; rem={k:pos[k]["total"] for k in ids}
    for k,q in sales.items():
        rem[k]-=q; r=sell(k,q); fee+=r["fee"]; net+=r["net"]; real+=r["realized"]
        lines.append((k,q,r))
    nav=nav_detail-fee
    mvp={k:rem[k]*pos[k]["px"] for k in ids}
    eq=sum(mvp.values()); wp={k:mvp[k]/nav for k in ids}
    vol,fr=risk(wp)
    s1=sum(mvp[k]*stress["S1"][k] for k in ids)
    out=dict(label=label,sales=sales,rem=rem,nav=nav,fee=fee,net=net,real=real,lines=lines,
        eq=eq,posn=eq/nav,wA=mvp["A"]/nav,wB=mvp["B"]/nav,wD=mvp["D"]/nav,wE=mvp["E"]/nav,
        hvol=(mvp["A"]+mvp["D"])/nav,elec=(mvp["A"]+mvp["D"])/nav,vol=vol,fr=fr,
        stress=s1/nav,transferable=WITHDRAWABLE+OLD_SELL_NET+net,
        cash_need_gap=CASH_TARGET-(WITHDRAWABLE+OLD_SELL_NET+net))
    return out

# ---- 整手补 ETF 到目标净回笼（基础净已超标则不补） ----
def fill_etf(need,base):
    s=dict(base)
    base_net=sum(sell(k,q)["net"] for k,q in s.items())
    if base_net>=need:
        s["E"]=0; return s
    px=pos["E"]["px"]; rate=1-(pos["E"]["cr"]+0+0+pos["E"]["slip"])
    q=int(math.ceil((need-base_net)/(px*100*rate)))*100
    def netof(eq):
        ss=dict(s); ss["E"]=eq; return sum(sell(k,qq)["net"] for k,qq in ss.items())
    while netof(q)<need and q<=pos["E"]["broker_sellable"]: q+=100
    while q-100>=100 and netof(q-100)>=need: q-=100
    s["E"]=q; return s

# 方案B 流动性优先
sB=fill_etf(TARGET_NET,{"A":5500})
rB=evaluate(sB,"方案B 流动性优先")
# 方案A 偏好优先: A留12500, 若D复牌卖D压高波动
def solve_dq():
    for dq in range(0,pos["D"]["broker_sellable"]+1,100):
        s={"A":5500,"D":dq}; fee=sum(sell(k,q)["fee"] for k,q in s.items()); nav=nav_detail-fee
        remD=pos["D"]["total"]-dq; hv=(280000+remD*13.50)/nav
        if hv<=0.30001 and hv<=0.32001: return dq
    return None
DQ=solve_dq()
sA=fill_etf(TARGET_NET,{"A":5500,"D":DQ})
rA=evaluate(sA,"方案A 偏好优先")
# 方案C 风险预算优先: A压到8000(破软偏好), D卖7000(条件), 不补ETF
sC={"A":10000,"D":7000,"E":0}
rC=evaluate(sC,"方案C 风险预算优先")

schemes=[rA,rB,rC]
for r in schemes:
    print(f"\n=== {r['label']} ===")
    for k,q,rr in r["lines"]:
        print(f"  {pos[k]['name']} {q}股 额{rr['tt']:,.2f} 费{rr['fee']:,.2f} 净{rr['net']:,.2f} 实现盈亏{rr['realized']:,.2f}")
    print(f"  NAV {r['nav']:,.2f} 可划转{r['transferable']:,.2f}(缺口{r['cash_need_gap']:,.2f}) 仓位{r['posn']*100:.4f}% A{r['wA']*100:.4f}% 高波动{r['hvol']*100:.4f}% 波动率{r['vol']*100:.4f}% A风险{r['fr']['A']*100:.4f}% 压力{r['stress']*100:.4f}% 实现盈亏{r['real']:,.2f}")

# ============ 写文件 ============
def wcsv(fname,header,rows):
    with open(os.path.join(HERE,fname),"w",newline="",encoding="utf-8-sig") as f:
        wr=csv.writer(f); wr.writerow(header); wr.writerows(rows)

# 1 position_reconciliation.csv
rows=[]
for k in ids:
    p=pos[k]
    sellable_t1 = p["broker_sellable"]+(4000 if k=="A" else 0)  # 8/31起A+4000
    if k=="C": execq, execday=0,"复牌且有效报价后(不可预估)"
    elif k=="D": execq, execday=0,"恢复有效买盘后(不可预估)"
    else: execq, execday=min(p["broker_sellable"]+(4000 if k=="A" else 0), int(p["adv"]*0.05//100*100)),"2026-08-31 09:30起"
    rows.append([k,p["sid"],p["name"],p["total"],p["broker_sellable"],
                 p["total"]-p["broker_sellable"],sellable_t1,execq,execday,
                 p["avg"],p["cost"],p["mv"],p["status"]])
rows.append(["NAV","","明细重算NAV",mv_sum,"","","","","",round(nav_detail,2),"",round(nav_detail,2),"MV合计+现金"])
rows.append(["NAV","","券商statement_total_assets","","","","","","",NAV_BROKER,"",NAV_BROKER,"BRK-ACC-01"])
rows.append(["NAV","","勾稽残差(明细-券商)","","","","","","",round(residual,2),"",round(residual,2),"0.1202%，不虚构其他资产补平"])
wcsv("position_reconciliation.csv",
  ["key","sid","名称/项目","账面总数量","券商可卖","冻结/当日买","T+1复核可卖(8/31起)","当前可执行量","最早可执行日","重建成本均价","重建成本总额","账面市值","状态/备注"],
  rows)

# 2 cash_availability_schedule.csv
cash_rows=[
 ["2026-08-28 周五 决策截点15:20","收盘后不可交易",0,0,WITHDRAWABLE,UNSETTLED_GROSS,"旧卖出18000毛额待结算","CAL-01"],
 ["2026-08-31 周一 09:00","旧卖出款结算可划转",OLD_SELL_NET,0,WITHDRAWABLE+OLD_SELL_NET,0,"旧证券TRD-X-01净额17985.82到账","CAL-02"],
 ["2026-08-31 周一 09:30","开盘: A当日买4000转可卖",0,0,WITHDRAWABLE+OLD_SELL_NET,0,"证券A可卖变18000","CAL-03"],
 ["2026-08-31 周一 盘中","计划卖出(若成交)",0,"条件成交",0,0,"净回笼计入下一可划转时点","COND"],
 ["2026-09-01 周二 09:00","8/31卖出款可划转",0,0,0,0,"仅8/31实际成交净额","CAL-04"],
 ["2026-09-01 周二 15:00","最终交易检查点",0,0,0,0,"9/2 10:00前须完成可成交卖出","CAL-05"],
 ["2026-09-02 周三 09:00","9/1卖出款可划转",0,0,0,0,"晚于此成交不能满足10:00","CAL-06"],
 ["2026-09-02 周三 10:00","用款截止",-CASH_TARGET,0,0,0,"可划转须>=260000(本任务不划款)","CAL-07"],
 ["2026-09-03 周四 09:00","证券B股息到账",DIV_B,0,0,0,"晚于截止，不计入260000","CAL-08"],
]
wcsv("cash_availability_schedule.csv",
  ["日期/时点","事件","已知流入","计划卖出净收入(条件)","期末可划转(已知)","未结算/交易可用","说明","source_id"],cash_rows)

# 3 portfolio_diagnosis.csv
wd=weights(nav_detail); vol,fr=risk(wd)
diag=[
 ["股票+ETF仓位",0.75,wd and mv_sum/nav_detail,mv_sum/NAV_BROKER,"<=0.75","CON-02"],
 ["单票A权重",0.27,wd["A"],pos["A"]["mv"]/NAV_BROKER,"<=0.27","CON-03"],
 ["单票B权重",0.27,wd["B"],pos["B"]["mv"]/NAV_BROKER,"<=0.27","CON-03"],
 ["单票C权重",0.27,wd["C"],pos["C"]["mv"]/NAV_BROKER,"<=0.27","CON-03"],
 ["单票D权重",0.27,wd["D"],pos["D"]["mv"]/NAV_BROKER,"<=0.27","CON-03"],
 ["单票ETF权重",0.27,wd["E"],pos["E"]["mv"]/NAV_BROKER,"<=0.27","CON-03"],
 ["高波动A+D权重",0.30,(pos["A"]["mv"]+pos["D"]["mv"])/nav_detail,(pos["A"]["mv"]+pos["D"]["mv"])/NAV_BROKER,"<=0.30","CON-04"],
 ["电子A+D权重",0.32,(pos["A"]["mv"]+pos["D"]["mv"])/nav_detail,(pos["A"]["mv"]+pos["D"]["mv"])/NAV_BROKER,"<=0.32","CON-05"],
 ["现金权重","-",cash_all/nav_detail,cash_all/NAV_BROKER,">=0.03(CON-09 3万)","CON-09"],
 ["年化组合波动率","-",vol,"-","参考","RISK"],
 ["A风险贡献",0.55,fr["A"],"-","<=0.55","CON-07"],
 ["D风险贡献",0.55,fr["D"],"-","<=0.55","CON-07"],
 ["ETF风险贡献",0.55,fr["E"],"-","<=0.55","CON-07"],
 ["最不利压力损失(S1)",0.09,sum(pos[k]["mv"]*stress["S1"][k] for k in ids)/nav_detail,"-","<=0.09","CON-06"],
]
diag_rows=[[r[0],r[1],round(r[2],6) if isinstance(r[2],float) else r[2],
            round(r[3],6) if isinstance(r[3],float) else r[3],r[4],r[5]] for r in diag]
wcsv("portfolio_diagnosis.csv",
  ["指标","阈值","明细NAV口径值","券商NAV口径值","关系","约束ID"],diag_rows)

# 4 action_priority.csv
pri=[
 ["A","技术偏强但单票38.77%、高波动、风险贡献58.2%、最不利压力主损源；流动性好、可卖14000(8/31后18000)","正常可交易",
  "高(可快速成交且减A直接压单票/风险贡献/压力)","卖出A同时实现盈利(均价21.19<现价22.40)","先卖A底仓TRD-A-01/A-02，当日新买4000不可卖","PREF-01要求留>=12000，硬约束要求更激进，冲突见memo"],
 ["D","风险最高(波动0.62,与A相关0.78,高波动电子)，但跌停零买盘,决策点不可执行","不可执行(跌停零买盘)",
  "条件性(仅复牌有买盘后)","卖出D直接压高波动/电子,但实现亏损(成本16.80>现价13.50)且滑点0.3%","若复牌按有效报价,单日ADV上限9000股","SLIP-04/RISK-04"],
 ["ETF","流动性最好(ADV1500万,价差2bps),快速筹现,但非电子/非高波动,卖它不修高波动集中","正常可交易",
  "高(筹现速度最快)","不改善高波动与行业集中，仅降仓位与现金","卖ETF只能补现金缺口，不能替代减A/D","MKT-E-01/POLICY-03"],
 ["B","低波动银行,除息后总回报已转正(+723.75),PREF-04倾向股息前不卖","正常可交易",
  "中(流动性好但soft偏好不卖)","卖出B减少的是现金与仓位，对高波动/行业无改善","股息3750元9/3到账,晚于截止","CA-B-01/PREF-04"],
 ["C","停牌,价格陈旧(8/24),账面121600≠可变现;历史波动低估复牌跳空","停牌不可执行",
  "无(窗口内0可执行)","不得计入可变现资金,不得按陈旧价成交","复牌首日不得假设按15.20成交","EXCH-CA-02"],
]
wcsv("action_priority.csv",
  ["证券","证据/诊断","可执行状态","现金与约束改善","费用/盈亏特征","执行备注","来源"],pri)

# 5 scenario_comparison.csv
sc_rows=[]
for r in [rA,rB,rC]:
    trades="; ".join(f"{pos[k]['name']}{q}股" for k,q,rr in r["lines"])
    sc_rows.append([r["label"],trades,round(r["nav"],2),round(r["fee"],2),round(r["net"],2),
        round(r["transferable"],2),round(r["cash_need_gap"],2),round(r["posn"],4),round(r["wA"],4),
        round(r["hvol"],4),round(r["elec"],4),round(r["vol"],4),round(r["fr"]["A"],4),
        round(r["stress"],4),round(r["real"],2),
        "条件:D须复牌有买盘" if "D" in r["sales"] else "不依赖D"])
# 成交失败情景
sc_rows.append(["D持续跌停无买盘(=方案B基准)","A5500+ETF14900,D0",round(rB["nav"],2),round(rB["fee"],2),
    round(rB["net"],2),round(rB["transferable"],2),round(rB["cash_need_gap"],2),round(rB["posn"],4),
    round(rB["wA"],4),round(rB["hvol"],4),round(rB["elec"],4),round(rB["vol"],4),
    round(rB["fr"]["A"],4),round(rB["stress"],4),round(rB["real"],2),"高波动39.9%/压力10.6%仍超限"])
sc_rows.append(["计划价格较收盘-5%","同方案B股数按-5%重算",0,0,175187.80,251173.62,8826.38,
    "","","","","","","","按附件价-5%重算净回笼","现金缺口8826元,需加卖约1900股ETF"],)
sc_rows.append(["计划价格较收盘-10%","同方案B股数按-10%重算",0,0,165967.39,241953.21,18046.79,
    "","","","","","","","按附件价-10%重算净回笼","现金缺口18047元,需加卖约3900股ETF"],)
wcsv("scenario_comparison.csv",
  ["方案/情景","逐笔卖出","交易后NAV","总费用","净回笼","预计可划转(若成交)","对260k缺口",
   "股票仓位","单票A权重","高波动权重","电子权重","组合波动率","A风险贡献","最不利压力损失率","实现盈亏(扣费)","执行前提/备注"],
  sc_rows)

# 6 if_then_rules.csv
rules=[
 ["R01","2026-08-31 09:25前","证券A","8/28当日买入4000股已过T+1且券商可卖显示=18000",
  "确认后A可卖=18000;若仍显示14000则当日只可卖14000,不卖出该4000","可卖上限(本规则不新增卖出)","官方价22.40","09:30",
  "费用另计","0","9/1起","A 18000可卖","CON-10=0(未卖当日买)","可卖!=18000则维持14000口径","09:30复核"],
 ["R02","2026-08-31 09:30起","证券D","D恢复有效买盘且最佳买价存在、买量>0、未再封跌停",
  "按方案A/C以有效买价限价卖D:方案A卖7700股,方案C卖7000股;单日不超过ADV5%=9000股","累计卖出7700/7000(基准持仓10000)",
  "D当时有效报价(非账面13.50)","09:30后","佣金0.025%+印花0.05%+过户0.001%+滑点0.3%","按成交","成交后下一交易日09:00",
  "D余2300/3000;高波动<=30%","若买盘撤回/封板则未成交,转入R03","成交后复核高波动"],
 ["R03","8/31-9/1","证券D","D仍跌停/零买盘/无有效报价",
  "不对D下任何卖出委托,不计入可变现;高波动/电子约束保持超限并在memo标注;改用压A(见R06)或接受不达标",
  "D卖出0","-","不可执行","-","0","-","D持仓不变","CON-04/05仍FAIL","D出现买盘则切R02","9/1 15:00复核"],
 ["R04","8/31-9/1","证券C","继续停牌;或复牌但无连续竞价有效价",
  "不挂单、不计入现金;若复牌跳空,仅在出现可成交连续报价后按R06同款整手规则另行评估,不得按15.20陈旧价成交",
  "C卖出0","复牌后有效报价","复牌且连续竞价后","-","0","-","C持仓8000","价格陈旧不可分配","复牌首日不按理论价成交","复牌当日复核"],
 ["R05","8/31开盘","宽基ETF","流动性正常且仅卖ETF无法修复高波动/电子",
  "ETF仅用于补现金缺口:方案B卖14900股;不把ETF当作风控修复手段","累计14900(基准50000)",
  "4.12附近","09:30","佣金0.02%无印花无过户滑点0.02%","净61363.44","9/1 09:00","ETF余35100","不修高波动","买盘不足则减量","成交后现金复核"],
 ["R06","8/31-9/1","证券A","A技术偏强但单票/高波动/风险贡献超限",
  "方案A/B卖5500股留12500;方案C卖10000股留8000(破PREF-01,需客户确认);只卖可卖底仓,不碰当日新买4000",
  "累计5500或10000(基准18000)","22.40附近","09:30","佣金0.025%+印花0.05%+过户0.001%+滑点0.05%","方案A净123044.77","9/1或9/2 09:00",
  "A余12500/8000;单票<=27%","价格下跌则按R08重算数量","成交后复核权重"],
 ["R07","8/31","证券B","除息机械下跌0.30元,股息3750元9/3到账",
  "不因价格机械下跌视为亏损而恐慌卖出;PREF-04倾向保留;仅当其他方案无法满足现金时才用B补充,卖出按整手",
  "默认0;备用12500以内","除息后有效价","09:30","佣金+印花+过户+滑点0.06%","按成交","成交下一交易日","B持仓","股息9/3到账不计入截止","现金不足时启用","备用规则"],
 ["R08","8/31-9/1","全部","计划价较收盘下跌5%或10%,或成交量触及ADV5%上限,或单笔佣金受最低5元影响",
  "按下跌后价重算整手股数:-5%时方案B净回笼175187.80,缺口8826.38,加卖ETF约1900股;-10%净165967.39,缺口18046.79,加卖ETF约3900股",
  "在R05基础上追加1900/3900股ETF","当时市价","09:30-14:30","重算","补足至>=260000","对应T+1","ETF相应减少","现金达标","仍不足则转R09","逐日收盘复核"],
 ["R09","2026-09-01 14:30-15:00","现金","届时预计可划转(含在途)仍<260000",
  "立即以可成交流动性标的(A/ETF)补足整手缺口;9/1卖出款9/2 09:00可划转,赶10:00截止;9/1 15:00后不再下单",
  "整手补足差额","当时市价","9/1 14:30-15:00","重算","补足","9/2 09:00","现金>=260000",
  "9/1后成交来不及","仍不足则为不可行,报告缺口","15:00截止下单"],
 ["R10","全程","全部","任何委托未成交/部分成交/资金未可划转",
  "条件未成立或仅部分成立,该笔净回笼记0,不计入260000;按R08/R09后手补足,禁止写成已成交/已到账",
  "0或部分","-","-","-","0(未成交部分)","-","持仓不变","不得记为已完成","成交回报确认后才更新","实时"],
 ["R11","全程","做T","仅在硬约束全部回限内后,且价差覆盖双边佣金+过户+卖出印花+双边滑点+0.10%缓冲",
  "先卖已有可卖底仓再当日回补不超过已卖量;日终不增仓、不影响现金目标;硬约束未回限内禁止做T",
  "回补<=已卖","-","-","双边成本+0.10%缓冲","0净","-","日终持仓不增","不用于解决刚性缺口","价差不覆盖则不做","日终复核"],
]
wcsv("if_then_rules.csv",
  ["规则ID","适用日期/时段","对象","触发条件","动作","数量及口径","价格口径","最早执行时间",
   "费用与滑点","预计净现金","资金可划转日","交易后持仓/约束","失效条件/后手","复核时点"],rules)

# 7 assumptions_manifest.json
manifest=dict(
 decision_point="2026-08-28 15:20 Asia/Shanghai 周五收盘后",
 plan_window="2026-08-31~2026-09-02 10:00",
 base_currency="CNY",
 cash_target=CASH_TARGET,
 nav=dict(statement_total_assets=NAV_BROKER, detail_reconciled=round(nav_detail,2),
          residual_detail_minus_statement=round(residual,2),
          note="明细=5只市值964050+现金76000(58000已结算+18000未结算毛额);残差1250元未用其他资产补平"),
 fees=dict(A_stock="佣金max(5,0.025%)+印花0.05%+过户0.001%+滑点0.05%",
           ETF="佣金max(5,0.02%)+无印花+无过户+滑点0.02%",
           D="恢复买盘后滑点0.3%", old_sale_net=round(OLD_SELL_NET,2)),
 settlement="A股卖出当日可用,下一交易日09:00可划转;T+1当日买入不可卖",
 constraints=dict(CON01_cash=">=260000",CON02_position="<=0.75",CON03_single="<=0.27",
   CON04_highvol="<=0.30",CON05_electronics="<=0.32",CON06_worst_stress="<=0.09",
   CON07_risk_contrib="<=0.55",CON08_ADV="<=0.05",CON09_min_cash=">=30000",CON10_T1="当日买当日卖=0"),
 adopted=[
   ["官方持仓","BRK-POS-01..05 数量/可卖为正式起点"],
   ["交易批次","BRK-TRD-01..07 重建成本;TRD-A-03当日买4000不计当日可卖"],
   ["行情","MKT-A/B/D/E-01为正式收盘;MKT-A-FUT(8/31 09:35)晚于决策点,已排除"],
   ["旧卖出TRD-X-01","净额17985.82于8/31 09:00可划转"]],
 excluded=[
   ["MKT-A-FUT 2026-08-31 09:35 未来记录","晚于决策点,前视控制,排除"],
   ["OCR-001 证券A总量13000? 置信0.62","<0.80,仅列异常,不覆盖官方18000"],
   ["OCR-005 证券D名称错识 置信0.74","以security_id匹配"],
   ["证券B股息3750元9/3到账","晚于截止,不计入260000"],
   ["证券C停牌价15.20(8/24)","陈旧价,不计可变现"]],
 open_items=[
   "证券D何时恢复买盘不可知:方案A/C依赖此,方案B不依赖",
   "证券C复牌时间不可知,复牌跳空未计入",
   "客户是否愿为硬约束接受A<12000股(PREF-01与CON-04/05/07冲突)",
   "残差1250元未在附件内消除,影响权重约0.12%",
   "客户个人税务特殊情形未提供"],
 scheme_summary=dict(
   A_preference=dict(keep_A=12500, depends_on_D=True, cash=round(rA["transferable"],2),
     highvol=round(rA["hvol"],4), A_risk=round(rA["fr"]["A"],4),
     note="留A12500但需卖D7700;A风险贡献61.03%仍>55%,完全达标须破PREF-01"),
   B_liquidity=dict(keep_A=12500, depends_on_D=False, cash=round(rB["transferable"],2),
     highvol=round(rB["hvol"],4), A_risk=round(rB["fr"]["A"],4),
     note="现金确定;高波动39.91%与压力10.56%仍超限(D冻结所致)"),
   C_risk=dict(keep_A=8000, depends_on_D=True, cash=round(rC["transferable"],2),
     highvol=round(rC["hvol"],4), A_risk=round(rC["fr"]["A"],4),
     note="风险约束全部回限内,但破PREF-01(A<12000)且需D复牌")),
 files={}
)
# SHA256
for fn in ["position_reconciliation.csv","cash_availability_schedule.csv","portfolio_diagnosis.csv",
           "action_priority.csv","scenario_comparison.csv","if_then_rules.csv",
           "liquidity_decision_memo.md"]:
    with open(os.path.join(HERE,fn),"rb") as f:
        manifest["files"][fn]=hashlib.sha256(f.read()).hexdigest()
with open(os.path.join(HERE,"assumptions_manifest.json"),"w",encoding="utf-8") as f:
    json.dump(manifest,f,ensure_ascii=False,indent=2)

print("\nFILES WRITTED")
