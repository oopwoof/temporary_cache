# G3.5｜doubao-skill-check 准出前置

位置：**G3 回归核验之后、G4 准出核算之前**。它抓的是 G1 抓不到的东西（结构、引用链、脚本孤儿、description 触发质量、安全扫描），两者互补，都要跑。

## 怎么跑

```bash
python3 <skill-check>/scripts/local_skill_scan.py skills/current --json > runs/<n>/skillcheck_scan.json
python3 scripts/run_skill_check.py --run runs/<n> --scan runs/<n>/skillcheck_scan.json
```

第二条把机械扫描结果填进 14 维报告骨架，本地判不了的预置为 `SKIP` 并写明缺什么证据。

## 14 维里哪些本地判不了

**本地可判**：1 命名、3 description、4 描述清晰、6 无冲突无冗余、7 格式规范、10 代码层面。

**要人判或要平台证据，本地一律不许判 PASS**：

| 维度 | 缺什么 |
|---|---|
| 2 粒度治理 | 业务方对边界的确认 |
| 5 逻辑一致 | 要人读完全文判断，脚本判不了 |
| 8 安全合规·命名 | 品牌词/商标检索 |
| 9 安全合规·内容 | 外部模板、素材、License 来源核验 |
| 11 安全合规·数据 | 数据源授权与权限边界 |
| 12 功能可用性 | 实际 `--help`／dry-run／平台元信息 |
| 13 Skill 间关系 | 同场景 Skill 列表或 ActionHub app_id |
| 14 Skill/Tool 关系 | Tool 注册与可用状态 |

没有证据就标 `SKIP` 并写明补什么，**不要为了报告好看判 PASS**。

## 已知误报

- `scripts.orphan`：脚本不解析 import，被间接引用的脚本会被报成孤儿。逐个核实后标「误报放行」，不要据此删脚本。
- `scripts.many_args_few_examples`：维护者专用脚本（不在回答路径上）会命中。如实标注用途后放行。
- `required_sections` 全 false 不等于 FAIL：维度 4 明确写了不强制固定章节名，可从「何时调用／功能与边界／路由规则／分支决策」等语义结构判断。用语义结构说明理由，别为了过检查硬加「概述/使用场景/核心流程」三个空标题——那会平白增加体量。

## 准出口径

skill-check 输出**双层结论**：

- **本地准出**：任一安全／格式／逻辑／可用性 `FAIL` → 不建议准出；只有 `PASS/WARN/SKIP` → 有条件准出，列出待修与待补证项
- **完整准出**：关键外验维度 `SKIP` → 需平台补证后再完整准出

在本流水线里：**本地准出 FAIL 即阻断 G4**；`SKIP` 不阻断 G4，但要进卡 6 并写进结论边界。

## 一条实证教训

固收那轮重写 SKILL.md 时删掉了两个脚本的入口链接，脚本从「被引用」变成孤儿——G1 完全没抓到，是 skill-check 抓到的。**任何重写或大幅压缩之后必须重跑这一步。**
