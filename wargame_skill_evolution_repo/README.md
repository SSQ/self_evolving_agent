# 兵棋推演规则问答的可自进化 Agent

基于 **Skill 运行时自进化** 的兵棋推演规则问答 Agent。主 Agent 只依赖可写的 Skill 文件回答规则问题；每完成一块题目，把失败样本送入后台回顾 Agent（Reviewer），由它按最小改动原则生成 `create` / `patch` 操作，SkillManager 落盘并保留完整版本历史，后续答题立即使用新 Skill。

不引入向量库，不做 RAG 检索，核心验证的是：**Agent 的知识库能否从对话失败中自动演化**。

---

## 核心设计

| 机制 | 说明 |
|------|------|
| 失败驱动闭环 | 每 10 题统计一次失败样本 → 注入 Reviewer → 输出最小必要 Skill 操作 → 下一题立即生效 |
| 契约式 Agent–Evaluator | Agent 能答就给出具体规则细节；不能答只回答“需要查询规则手册”。Evaluator 以 `查询规则` 一票否决推脱，失败原因三分类互斥 |
| Reviewer 只接收失败样本 | 不传完整对话历史，避免对已答对内容做“美学式补全”；块内全对时完全跳过 Reviewer |
| 最小改动原则 | 能 patch 不 create；按失败条数只修 1~2 类，保留多轮进化梯度 |
| 三层版本持久化 | 活动版 `skills/` + 历史 `outputs/skill_versions/*.json` + 每版独立 `outputs/skill_snapshots/*.md` |
| 零 LLM 成本评估 | 关键词匹配 + 数字千位归一化 + 否定前置检测，单次全量评估不产生额外模型调用 |
| 可复现实验 | 固定 20 题 Probe 子集 + 固定 80 题评估集；基线 / 每次 Nudge 后 / 最终三次评估口径一致 |

---

## 真实运行结果

以下数据来自仓库内 `outputs/evolution_log.json` 对应的真实运行（2026-08-29，qwen-plus，温度 0）：

| 指标 | 基线（2 个 Skill） | 进化后（7 个 Skill） | 变化 |
|------|------------------|-------------------|------|
| 总体准确率 | 22/80 = **27.5%** | 56/80 = **70.0%** | **+42.5pp** |
| 固定 Probe（20 题） | 6/20 = 30.0% | 14/20 = 70.0% | +40.0pp |
| 总 Token | 79,604 | 265,616 | +233.7% |
| 平均 Token/题 | 995.0 | 3,320.2 | +233.7% |
| 平均延迟 | 956.5 ms | 1,317.6 ms | +37.8% |
| 单次 80 题评估费用 | 0.3403 元 | 1.1012 元 | +223.6% |

分类准确率（同一 80 题评估集）：

| 类别 | 题数 | 基线 | 进化后 |
|------|------|------|--------|
| complex_rules | 8 | 0% | **100%** |
| indirect_fire | 8 | 0% | **88%** |
| same_hex_combat | 6 | 0% | **83%** |
| direct_fire_basic | 10 | 40% | **70%** |
| mobility_basic | 8 | 50% | 62% |
| personnel_mobility | 8 | 62% | 62% |
| suppression | 8 | 12% | 62% |
| observation_basic | 8 | 0% | 50% |
| special_units | 8 | 0% | 25% |
| vehicle_mobility | 8 | 100% | 100% |

Skill 演化规模：初始 **2** 个（mobility、direct_fire）→ 最终 **7** 个（新增 observation、indirect_fire、aerial_mobility、close_combat、action_timing），8 轮 Nudge 共执行 **20** 次 Skill 操作，累计 **22** 条版本记录。

> 说明：`scripts/validate.py` 输出的静态初始覆盖率为 31/80 = 38.8%，表示“初始 Skill 有对应条款”的题目上限；真实基线为 22/80 = 27.5%，因为部分题目虽被 Skill 覆盖，但回答仍缺关键词或命中禁止词。两者口径不同，真实基线以实际评估为准。

---

## 快速开始

```bash
pip install -r requirements.txt

# Windows PowerShell
$env:DASHSCOPE_API_KEY = "sk-xxxxxx"
# Linux / macOS
export DASHSCOPE_API_KEY="sk-xxxxxx"

# 1) 结构与数据校验（不需要 API Key）
python scripts/validate.py

# 2) 完整自进化实验：基线 80 题 → 8 块 × 10 题 → 最终 80 题
python src/run_experiment.py

# 3) 只用当前 Skill 跑一次全量评估（不触发 Nudge）
python src/rule_eval_with_review.py
```

完整实验通常耗时 5~12 分钟，费用随 Skill 数量增加而上升：基线 80 题约 0.34 元，最终 80 题约 1.10 元。

---

## 目录结构

```text
wargame_skill_evolution_repo/
├── src/
│   ├── agent.py                   # 主 Agent：单题无状态应答，动态加载最新 Skill
│   ├── skill_manager.py           # Skill CRUD + 三层版本持久化
│   ├── background_reviewer.py     # 后台回顾 Agent：仅接收失败样本，输出 create/patch JSON
│   ├── evaluator.py               # 关键词评估：数字归一化 + 否定前置 + 推脱一票否决
│   ├── run_experiment.py          # 全流程实验：基线 → 分块 → Nudge → Probe → 最终评估
│   └── rule_eval_with_review.py   # 一次性全量规则评估（不修改 Skill）
├── data/
│   ├── policy.md                  # 完整兵棋推演规则（仅 Reviewer 可读）
│   ├── eval_set.json              # 80 题评估集（10 类，含 required/forbidden 关键词）
│   └── experiment_script.json     # 80 题实验脚本（8 块 × 10 题，Nudge 间隔 10）
├── skills/                        # 初始 2 个 Skill（实验时会自动还原到该状态）
├── outputs/
│   ├── skills_original/           # 初始 Skill 备份，实验前自动还原
│   └── evolution_log.json         # 真实运行日志：8 次 Nudge、22 条版本、逐题历史
├── scripts/
│   └── validate.py                # 项目结构 / JSON / 语法 / 覆盖度校验
├── ARCHITECTURE.md                # 架构与设计决策
├── USAGE.md                       # 运行、配置与模块调用
├── requirements.txt
└── README.md
```

模型侧默认使用 DashScope 的 `qwen-plus`（OpenAI 兼容接口），可在 `src/agent.py` 与 `src/background_reviewer.py` 中切换为其他兼容服务。

---

## 仓库定位说明

本仓库是**兵棋推演规则场景的命令行实验版本**，包含完整的数据、Skill、评估与真实运行日志。另有一套面向通用政策问答的 Web UI 版本属于独立项目，两者场景与交互形态不同，未合并到本仓库。

---

## 已知边界

- 规则评估存在约 10% 的同义词 / 跨句否定误判上限，因此准确率不宣称 90%+。
- LLM 即使 `temperature=0` 仍有随机性，重复实验可能出现数个百分点的波动，应关注基线 → 最终的提升趋势。
- 当前为单机 CLI 原型，未覆盖并发、多用户、权限与审计等生产要求。

更完整的设计推导见 [ARCHITECTURE.md](ARCHITECTURE.md)，运行与配置见 [USAGE.md](USAGE.md)。
