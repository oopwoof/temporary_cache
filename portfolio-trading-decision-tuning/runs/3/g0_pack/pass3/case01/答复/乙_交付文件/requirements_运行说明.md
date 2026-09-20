# 依赖清单

`run_portfolio_review.py` 仅依赖 Python 3.10+ 标准库：

| 模块 | 用途 |
|---|---|
| `csv` | 读写 CSV 附件 |
| `math` | CVaR95 取整、滑点 sqrt 计算 |
| `statistics` | （预留，本版本未直接调用；保留以兼容后续拓展） |
| `datetime` | 决策时点解析 |
| `pathlib` | 跨平台路径 |
| `collections` (`OrderedDict`, `defaultdict`) | 按 plan_id/lot_id 索引 |

无第三方依赖，无需 `pip install`。

# 运行说明

```bash
# 使用托管 Python 3.13
/Users/bytedance/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
    /Users/bytedance/WorkBuddy/2026-09-11-17-05-22/portfolio_review/run_portfolio_review.py
```

# 输入文件路径（脚本内硬编码为默认下载目录）

- `/Users/bytedance/Downloads/持仓标的报价_20251231T1445.csv`
- `/Users/bytedance/Downloads/持仓标的日线_20240102_20251230.csv`
- `/Users/bytedance/Downloads/持仓批次_20251231T1445.csv`
- `/Users/bytedance/Downloads/候选减仓方案_20251231.csv`
- `/Users/bytedance/Downloads/账户与交易约束_20251231T1445.csv`

如附件路径变更，请修改脚本顶部 `DOWNLOADS` 或对应 `*_FILE` 变量。

# 输出文件

| 文件 | 说明 |
|---|---|
| `当前持仓风险快照.csv` | 含 ts_code、quantity、available_to_sell、market_value_cny、weight、unrealized_pnl_cny、adv20_cny、current_cvar_contribution、breach_flag |
| `候选方案约束矩阵.csv` | 每个 plan_id × metric_name 一行，含 actual/threshold/status/gap |
| `批次卖出分配.csv` | 含 plan_id、order_sequence、ts_code、lot_id、allocated_quantity、estimated_execution_price、estimated_fee、realized_pnl_cny、eligibility_status |
| `审批监控条件.csv` | 6 条监控规则（M01–M06） |
| `持仓交易审批备忘录.md` | 完整审批意见：勾稽/风险/方案比较/主方案/反向证据/敏感性/条件化动作/免责 |
| `账户勾稽明细.csv` | 账户勾稽逐项明细（辅助验收） |

# 计算口径摘要

1. 报价/账户/持仓/方案 全部由 CSV 读入，脚本不硬编码任何价格、数量、推荐方案。
2. 市值 = quantity × current_price_cny × fx_to_cny（CNY 标的 fx=1）。
3. 净现金 = cash_balance_cny − accrued_fees_payable_cny；复算 NA = Σ 市值 + 净现金。
4. ADV20 = mean(amount × 1000)，amount 单位为千元，按每只股票最近 20 条 `有行情` 记录。
5. CVaR95：对齐 250 个共同有效日，r_i,t = pct_chg÷100；组合收益 r_p,t = Σ w_i r_i,t；损失 L_t = −r_p,t；m = ceil(0.05×N)；CVaR95 = mean(top-m L_t)。
6. 滑点：min(base_bps + sqrt_coef_bps × √参与率, cap_bps)；执行价 = current_price × (1 − 滑点bps/10000)。
7. 费用：佣金 = max(min_commission, 成交额 × commission_rate)；印花税 = 成交额 × stamp_duty_sell_rate；过户费 = 成交额 × transfer_fee_rate。
8. 批次分配：仅在 available_to_sell > 0 的批次上扣减，按 buy_date 升序、lot_id 升序；T+1 不可卖批次（P02、P04）不参与。
9. 交易后净资产：股票盯市 current_price_cny；净现金 += 当日 eligible 订单净回款。
10. 排序规则（多方案均通过时）：交易后 CVaR 低 → 总费用低 → 总卖出名义低 → plan_id 升序。

# 验收口径

- 原始快照 → 复算 NA → 报表 NA 三者勾稽（容差 ±1 元）。
- 逐批次市值 → Σ 股票市值 → 复算 NA。
- 订单级费用 → Σ 总费用 → after_net_cash 变动。
- 交易后权重 → after_eq / after_cash / single / top3。
- 交易后 CVaR（250 日） → 阈值 0.022。
- 矩阵每一行 status 与方案总状态一致。

# 数据限制

- 脚本离线运行，不联网、不调外部 API。
- 所有输入文件均视为题方脱敏仿真输入；当前价锚定公开日线仿真；不得解读为真实可成交报价或真实客户。