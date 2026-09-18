# 持仓交易审批复核 - 运行说明

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
