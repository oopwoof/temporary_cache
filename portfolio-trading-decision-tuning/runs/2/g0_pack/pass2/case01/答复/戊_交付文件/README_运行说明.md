# 运行说明 — run_portfolio_review.py

## 概述

`run_portfolio_review.py` 从5个原始附件一键重建稳健账户A持仓减仓方案审批的全部客观CSV和报告数字。脚本不联网，不硬编码价格、数量或推荐方案。

## 环境要求

- Python 3.8 或更高版本
- 仅使用标准库（csv, json, math, os, sys, argparse, collections, datetime），无需安装第三方包

## 输入文件

将以下5个CSV文件放在同一目录（默认与脚本同目录）：

1. `持仓标的日线_20240102_20251230.csv`
2. `持仓批次_20251231T1445.csv`
3. `持仓标的报价_20251231T1445.csv`
4. `账户与交易约束_20251231T1445.csv`
5. `候选减仓方案_20251231.csv`

## 运行命令

```bash
# 使用默认目录（脚本所在目录作为输入和输出目录）
python3 run_portfolio_review.py

# 指定输入和输出目录
python3 run_portfolio_review.py \
  --input-dir /path/to/attachments \
  --output-dir /path/to/output
```

## 输出文件

脚本运行后在输出目录生成以下文件：

| 文件 | 说明 |
|---|---|
| `当前持仓风险快照.csv` | 每只股票的持仓量、可卖量、市值、权重、未实现盈亏、ADV20、CVaR尾部贡献、超限标记 |
| `候选方案约束矩阵.csv` | 每个方案逐项列出metric_name、actual_value、threshold、status、gap、calculation_basis、evidence_file |
| `批次卖出分配.csv` | 每个订单的批次分配、执行价、费用、已实现盈亏、可卖状态 |
| `审批监控条件.csv` | 条件化监控规则（如果→那么结构） |
| `review_results.json` | 全部计算结果的结构化JSON，供备忘录引用 |

## 计算口径

- **决策时点**：2025-12-31T14:45:00+08:00（固定，取自账户约束表as_of）
- **ADV20**：每只股票截至2025-12-30最近20条"有行情"记录的mean(amount×1000)，amount单位千元
- **CVaR95**：6只股票共同存在的最近250个有效交易日，historical simulation，m=ceil(0.05×250)=13
- **风险收益**：pct_chg÷100，不使用不复权close首尾相除
- **滑点**：min(slippage_cap_bps, slippage_base_bps + slippage_sqrt_coefficient_bps × √参与率)
- **费用**：佣金=max(最低佣金, 执行金额×佣金率)；印花税=执行金额×印花税率；过户费=执行金额×过户费率
- **批次分配**：同一股票按buy_date升序、lot_id升序从可卖批次分配
- **T+1**：buy_date=2025-12-31且available_to_sell=0的批次不得分配
- **报价有效条件**：trading_status=连续竞价、quote_age≤quote_max_age_seconds、limit_down<price≤limit_up
- **勾稽容忍**：复算净资产与reported_net_asset_cny绝对差≤1元

## 注意事项

1. 脚本不会连接券商或发送订单，仅生成内部审批测算。
2. 输入文件编码应为UTF-8（支持BOM）。
3. 若输入文件缺失，脚本会报错退出。
4. 若账户勾稽失败（差额>1元），脚本会停止审批并输出错误信息。
5. 所有计算保留全精度，CSV展示值已舍入（金额2位、权重6位、CVaR 8位）。
