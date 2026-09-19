你是某资产管理机构投顾风控组的持仓决策分析师。2025年12月31日14:45，稳健账户A触发股票仓位、集中度、现金和尾部风险复核。投资负责人必须在14:50前决定是否批准交易台提交的方案A、方案B或方案C，并指定一个主方案；如果没有方案能够在规则内执行，应明确拒绝全部方案并列出恢复审批所需条件。本任务只形成内部审批意见和条件化动作，不连接券商、不发送订单，也不构成收益承诺。

一、输入附件与时点

必须读取以下5个附件：

1.“持仓标的日线_20240102_20251230.csv”。
2.“持仓批次_20251231T1445.csv”。
3.“持仓标的报价_20251231T1445.csv”。
4.“账户与交易约束_20251231T1445.csv”。
5.“候选减仓方案_20251231.csv”。

决策时点固定为2025-12-31T14:45:00+08:00，审批截止时点取账户约束表中的approval_deadline。只能使用附件中不晚于相应as_of的记录。retrieved_at只表示公开历史数据的抓取时间，不是可用行情时点。不得联网补入事后行情、公司事件、实时盘口或成交结果。

日线为公开历史快照；持仓、账户、报价和候选方案为题方脱敏或仿真输入。不得把脱敏账户识别为真实客户，也不得把当前价锚定公开日线解释成真实可成交报价。

二、数据审计与账户勾稽

1.检查每个CSV的字段、主键、日期、单位、币种、as_of、重复值、空值和记录状态。按ts_code连接持仓、报价和日线；任何方案中出现未知代码时，该方案直接判为不可评估。
2.按持仓批次汇总quantity和available_to_sell。buy_date为2025-12-31且available_to_sell为0的批次视为T+1不可卖，不得用总持仓替代可卖数量。
3.每个批次市值=quantity×current_price_cny×fx_to_cny；股票总市值为全部批次市值之和；净现金=cash_balance_cny-accrued_fees_payable_cny；复算净资产=股票总市值+净现金。复算值与reported_net_asset_cny绝对差超过1元时停止审批，输出“账户快照无法勾稽”、差额和所需更正文件。
4.逐批次计算未实现盈亏=(current_price_cny-cost_price_cny)×quantity，但不得把未计入cost_basis_note的历史现金分红自行补回。当前权重一律以复算净资产为分母。
5.日线amount单位为千元。按每只股票截至2025-12-30最近20条“有行情”记录计算ADV20=mean(amount×1000)。风险收益使用pct_chg÷100，不得用不复权close首尾相除替代。

三、当前风险画像

1.计算股票仓位、净现金权重、每只股票权重、前3大权重和未实现盈亏，并逐项对照账户约束表。
2.取6只股票共同存在且状态有效的最近250个交易日；若共同有效日少于risk_history_common_days，则停止CVaR审批并说明缺口。现金日收益使用cash_daily_return。
3.给定某组交易后权重w_i，组合日收益r_p,t=sum(w_i×r_i,t)，损失L_t=-r_p,t。设N为共同日数，m=ceil((1-risk_confidence)×N)，historical_CVaR95为按损失从大到小排列后前m个损失的算术平均值。不得把VaR分位点或单日最大损失当作CVaR。
4.在CVaR尾部日期上计算每只股票的平均损失贡献=mean(-w_i×r_i,t)，并核验各项之和等于组合CVaR。相关矩阵使用同一250日样本、Pearson相关系数和ddof=1；相关性只用于风险解释，不得写成因果。

四、候选方案执行测算

每个方案必须独立从原始账户快照开始测算，不能把多个方案串联。只把execution_window为“当日”的订单计入14:50前的即时修复；“下一交易日”订单只能列为后续条件动作，不能提前计入当日合规结果。

1.按plan_id和order_sequence检查订单。每只股票当日累计sell_quantity不得超过available_to_sell，且必须是board_lot_shares的整数倍。
2.只有trading_status为“连续竞价”、quote_age_seconds不超过quote_max_age_seconds、current_price_cny高于limit_down_cny且不高于limit_up_cny时，附件报价才可用于当日批准。报价陈旧、停牌、跌停不可成交或字段缺失时，不得凭空更新价格；应把该订单及受影响方案列为“待条件恢复”。
3.订单参考名义金额=sell_quantity×current_price_cny。参与率=订单参考名义金额÷ADV20，超过max_adv20_participation时该订单不符合硬约束。
4.滑点基点=min(slippage_cap_bps,slippage_base_bps+slippage_sqrt_coefficient_bps×sqrt(参与率))。卖出执行估价=current_price_cny×(1-滑点基点÷10000)。
5.执行金额=sell_quantity×卖出执行估价；佣金=max(minimum_commission_cny,执行金额×commission_rate)；印花税=执行金额×stamp_duty_sell_rate；过户费=执行金额×transfer_fee_rate；净卖出回款=执行金额-佣金-印花税-过户费。
6.同一股票按buy_date升序、lot_id升序从可卖批次分配卖出数量。已实现盈亏=sum((卖出执行估价-cost_price_cny)×分配数量)-该订单全部费用。不得分配到available_to_sell为0的批次。
7.交易后股票市值按附件current_price_cny对剩余数量盯市；交易后净现金=原净现金+全部净卖出回款；交易后净资产=交易后股票市值+交易后净现金。该净资产也应等于reported_net_asset_cny减滑点损失及全部交易费用，允许绝对误差1元。
8.按交易后净资产重算股票仓位、净现金权重、单票权重、前3大权重、250日historical_CVaR95、尾部贡献和参与率。每项分别输出“通过”“不通过”或“无法评估”，不能只给综合分数。
9.另做两项敏感性检查：全部订单按slippage_cap_bps执行；CVaR窗口缩短为最近200个共同有效日。敏感性结果用于风险提示，不替换硬约束的主口径。

五、审批规则与条件化动作

只有同时满足以下条件的方案才能标为“可批准”：账户可勾稽；全部当日订单可卖且符合T+1、整手、交易状态、报价时效、涨跌停和ADV20参与率；交易后equity_exposure_max、single_name_weight_max、top3_weight_max、historical_cvar95_max和minimum_cash_weight全部通过。

对方案A、方案B和方案C逐一给出“可批准”“待条件恢复”或“拒绝”，并列明每个未通过项目的实际值、阈值和缺口。若只有一个方案满足全部硬约束，将其作为唯一主方案；若多个方案满足，则依次按交易后CVaR较低、总交易成本较低、总卖出金额较低排序，仍相同则按plan_id升序决定；若没有方案满足，不得挑选“最接近”的方案冒充合规方案。

所有建议必须写成“如果→那么”结构。例如，如果指定报价在审批截止前刷新且其余约束复算仍通过，那么提交投资负责人重新审批；如果任一硬约束仍不通过，那么拒绝执行并要求交易台改案。不得使用“立即下单”“已经卖出”或任何代表用户完成交易的措辞。

六、交付物

1.“持仓交易审批备忘录.md”：包括账户勾稽、当前风险、3个方案比较、主方案或拒绝结论、反向证据、敏感性、条件化动作和数据限制。
2.“当前持仓风险快照.csv”：至少包含ts_code、quantity、available_to_sell、market_value_cny、weight、unrealized_pnl_cny、adv20_cny、current_cvar_contribution和breach_flag。
3.“候选方案约束矩阵.csv”：每个plan_id逐项列出metric_name、actual_value、threshold、status、gap、calculation_basis和evidence_file。
4.“批次卖出分配.csv”：至少包含plan_id、order_sequence、ts_code、lot_id、allocated_quantity、estimated_execution_price、estimated_fee、realized_pnl_cny和eligibility_status。
5.“审批监控条件.csv”：至少包含condition_id、if_condition、threshold、then_action、owner、check_deadline、evidence_needed和stop_condition。
6.“[run_portfolio_review.py](http://run_portfolio_review.py)”、依赖清单和运行说明：从5个原始附件一键重建全部客观CSV和报告数字；脚本不得联网，也不得硬编码附件中的价格、数量或推荐方案。

七、验收原则

原始快照、逐批次汇总、订单级费用、交易后现金、净资产、权重、CVaR和约束矩阵必须逐级勾稽；计算过程保留全精度，展示可舍入但需说明位数。报告、CSV和脚本中的主方案状态必须一致。不得忽略T+1、报价陈旧、下一交易日订单或交易费用，不得把研究测算写成真实成交，不得给出保证收益或绕过审批的指令。
