# 持仓交易决策 portfolio-trading-decision · 第三轮离线调优

在搭档调过的 v2 基础上继续优化，产出 v3。**本轮全部是离线工作：没有跑题、没有判分，效果是预期不是结果。**

## 目录

```
skills/v1.0/      被评测的原始包（8 题四方盲评的 B 方），冻结
skills/v2.0/      搭档调过的版本，冻结
skills/current/   v3 = 本轮产出
portfolio-trading-decision.zip   v3 打包（顶层目录 portfolio-trading-decision/）
SKILL.diff        v2 → v3 全量 diff
改动台账.md       ★ 逐条改动 → 证据（caseNN + 专家原话）→ 失败判据
人审卡.md         ★ 4 张卡，含「我没把握的地方」
gates/            G1 静态核验 json：v2 基线与 v3
```

## 一句话结论

v2 已经覆盖了交接包点出的 4 个卡点里的 3 个（日历、过度自信、提现口径），本轮补的是**落地载体与可自检判据**：
分解型占比加总检验、交易日历表、硬约束状态表、监控不算动作、触发方向自检、数量口径标注、压力取最不利，
外加两条 v2 未保护的既有优势（脚本一键重建、敏感性数值化）。

## 本轮最重要的一件事：case05 归因是错的

交接包建议「先做 case05，硬门 if→then 必须有正文直接失效」。逐条回查原文后，这个前提不成立：

- 专家给的 4 条失分理由（缺盈亏 / 未分析仓位 / 无三情景 / 无 if→then）**全部与原文不符**，这些内容都写了；
- 独立复算 B 的 6 条均线、60 日高低、波动率、最大回撤、占比全部与材料一致；
- B 的基准价取自材料末行，而**拿 4 分的裸模用了材料里不存在的价格与日期**。

所以本轮不为 case05 重构包，只做三条轻量改动，主攻改为 case07 → case02/case04 → case06。
详见 `改动台账.md` D 节与 `人审卡.md` 卡 1。

## 静态门禁

```bash
python3 <autotune>/scripts/lint_skill.py skills/current/SKILL.md \
    --cases-dir <prompts> <attachments> --responses-dir <rival responses>
```

v3 **全绿**（v2 为「无红项、黄项 1 类」）；4 份 reference 单独扫描均无红项。
**Y6 档位余量 SKIPPED**（无领域纪律包与骨架模板），**G3.5 `doubao-skill-check` 未测**（环境无此工具）——
本轮新增了一份 reference，正是它专门抓的那类改动，上平台前务必补跑。

## 下一步

1. 人审卡卡 1 的确认（评审到底看到了什么）会影响 B-01 的修法方向，建议先问。
2. 补跑 `doubao-skill-check`，确认 4 份 reference 都在引用链上。
3. 跑题 + 三轮独立判分，按 `改动台账.md` 的失败判据逐条核对是否落地。
