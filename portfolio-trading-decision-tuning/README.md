# 持仓交易决策 portfolio-trading-decision · 第三轮离线调优

在搭档调过的 v2 基础上继续优化，产出 v3。**本轮全部是离线工作：没有跑题、没有判分，效果是预期不是结果。**

## 目录

```
skills/v1.0/      被评测的原始包（8 题四方盲评的 B 方），冻结
skills/v2.0/      搭档调过的版本，冻结
skills/current/   v3 = 本轮产出
portfolio-trading-decision.zip   v3 打包（顶层目录 portfolio-trading-decision/，*.zip 被仓库忽略，按下方命令重建）
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
G3.5 `doubao-skill-check` v1.0.9：**0 findings**（引用链完整、无孤儿 reference、无安全项、目录名与 name 一致）。
**Y6 档位余量仍 SKIPPED**（无领域纪律包与骨架模板），skill-check 14 维里 8 维要人判或平台证据，本地记 SKIP。

## 重新打包（上平台用）

```bash
mkdir -p /tmp/pkg/portfolio-trading-decision && cp -r skills/current/. /tmp/pkg/portfolio-trading-decision/
(cd /tmp/pkg && zip -r <目标路径>/portfolio-trading-decision.zip portfolio-trading-decision)
```

跑题方会对 zip 做 sha256sum，注意 **zip 字节校验值 ≠ 目录内容摘要**，两者分开报。

## 跑题包（阶段 3，已生成）

```
runs/inputs/prompts/          8 题题面（已剥掉迭代包加的元信息标题行）
runs/inputs/attachments/      15 件原始附件（不含红项扫描用的 .txt 旁挂件）
manifest.yaml                 本轮唯一声明；run_config 的 platform/model/route 待需求方补
gates/thresholds.yaml         8 题的阈值覆盖（G0 6/8、G2 8/8），已注明理由
runs/1/run_pack/              ★ 跑题包本体
runs/1/需求方待补清单.md      ★ 跑题前要补什么、采集前要定死什么
跑题包_run1.zip               跑题包打包（*.zip 被仓库忽略，用下方命令重建）
```

```bash
python3 <autotune>/scripts/make_run_pack.py --run runs/1 --skill skills/current \
    --prompts runs/inputs/prompts --attachments runs/inputs/attachments --rounds 2
```

目录内容摘要 `b5082caece480718bfb59266ede22514aac9cf41fa24bc9554dae03ec331079b`（校验以此为准）。
仓库忽略 `*.zip`，所以 `run_pack/` 里的 skill zip 不在版本库里——上面那条命令会连它一起重建，
重建后跑 `python3 校验.py portfolio-trading-decision` 应输出与上面一致的摘要。
模板脚本**不进本仓库**，避免实例副本随模板升版变旧（经验沉淀 24）；用 autotune-template v1.6.0 的 `scripts/`。

## 下一步

1. 按 `runs/1/需求方待补清单.md` 补 platform/model/route，定死平局口径，**并要求判分方同时给强制无平排序**
   （上一轮 8 题的 verdict 里本来就有严格无平序，却只用了会打平的 1–5 分）。
2. 跑题（8 题 + 噪声底复跑，建议并发 4）。
3. 起草领域纪律包 `packs/portfolio-trading-decision.yaml`——判分包需要它，跑题包不需要。
4. 判分回来先跑 `check_judge_quality.py`，再按 `改动台账.md` 的失败判据逐条核对哪条落地了。
