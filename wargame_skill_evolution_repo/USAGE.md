# USAGE — 运行、配置与模块调用

## 一、环境准备

### 1.1 依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 内容：

- `openai>=1.0.0`（LLM 调用，使用 OpenAI 兼容接口访问 DashScope）

### 1.2 API Key

默认使用 DashScope（`qwen-plus`）。设置环境变量：

```bash
# Linux / macOS
export DASHSCOPE_API_KEY="sk-xxxxxx"

# Windows PowerShell
$env:DASHSCOPE_API_KEY = "sk-xxxxxx"

# Windows cmd
set DASHSCOPE_API_KEY=sk-xxxxxx
```

### 1.3 工作目录

所有命令默认在仓库根目录 `wargame_skill_evolution_repo/` 下执行：

```bash
cd wargame_skill_evolution_repo
```

---

## 二、三种运行方式

### 方式 A：完整自进化实验（主要方式）

```bash
python src/run_experiment.py
```

执行流程：基线 80 题评估 → 8 块 ×（10 题 + Nudge 或跳过）→ 最终 80 题评估。
结果落盘至 `outputs/evolution_log.json` 与 `outputs/eval_runs/`。

典型一次完整运行时长：5~12 分钟（取决于 LLM 响应速度和跳过块数）。

费用参考（qwen-plus，按 0.004 元/千 prompt token、0.015 元/千 completion token 估算）：

- 基线 80 题：约 0.34 元
- 最终 80 题：约 1.10 元（Skill 数量增加导致 prompt 变长）

### 方式 B：独立全量评估（验证当前 Skill 库）

```bash
python src/rule_eval_with_review.py
```

用当前 `skills/` 目录的 Skill 跑一次全量 80 题评估，**不触发 Nudge、不修改 Skill**。
控制台打印每题对错和分类准确率，所有答案保存到 `outputs/rule_eval_full.json`，便于手工复核。

### 方式 C：项目结构校验

```bash
python scripts/validate.py
```

验证目录结构、JSON 语法、Python 语法、Skill 覆盖度等。该命令不需要 API Key。

---

## 三、核心契约

理解以下契约，才能正确解读准确率数字：

**Agent 行为契约**（`src/agent.py` 的系统提示）

- 能从 Skill 答出 → 给出具体完整答案（含秒数、速度比、射程等规则细节），不加推脱尾巴。
- 不能从 Skill 答出 → **仅回答一句**“需要查询规则手册”。

**Evaluator 判定规则**（`src/evaluator.py`）

- 答案含 `查询规则` → 判定“Agent 推脱”（硬性失败）。
- 否则检查 required 关键词是否全部出现、forbidden 关键词是否未被否定前置命中。

因此三种失败原因互斥：

- **Agent 推脱**：Skill 库没有覆盖这类问题。
- **缺少关键词**：Skill 有条款但信息不完整。
- **出现禁止词**：Skill 有条款但 Agent 错误引用。

---

## 四、脚本模块化调用

### 4.1 作为模块使用各组件

```python
import sys
sys.path.insert(0, "src")

from skill_manager import SkillManager
from evaluator import Evaluator
from agent import WargameAgent
from background_reviewer import BackgroundReviewer

sm = SkillManager("skills", "outputs/skill_versions")
ev = Evaluator("data/eval_set.json")
agent = WargameAgent(sm, nudge_interval=0)
reviewer = BackgroundReviewer("data/policy.md", sm)

# 单题应答
answer = agent.answer("地面棋子从机动状态转换为停止状态需要多长时间？")
ok, reason = ev.evaluate_answer(answer, question_id=1)
print(f"对错: {ok} | 原因: {reason}")

# 收集一批失败样本，手动触发 Reviewer
failed_turns = [
    {"question": "车辆对无人机的观察距离是多少？", "answer": "需要查询规则手册",
     "fail_reason": "Agent 推脱（含 '查询规则'）"},
]
actions = reviewer.review(failed_turns)
for act in actions:
    if act["action"] == "create":
        sm.create(act["skill_name"], act["content"], reason=act["reason"])
    elif act["action"] == "patch":
        sm.patch(act["skill_name"], act["old_text"], act["new_text"],
                 reason=act["reason"])
```

### 4.2 查询 Skill 版本历史

```python
sm = SkillManager("skills", "outputs/skill_versions")

# 所有 Skill 的版本摘要
for name, versions in sm.get_all_version_summaries().items():
    print(f"{name}: {len(versions)} 个版本")
    for v in versions:
        print(f"  [{v['action']}] {v['reason'][:60]}")

# 指定 Skill 的完整历史
mobility_history = sm.get_version_history("mobility")
for v in mobility_history:
    print(f"v{v['version']} [{v['action']}] {v['reason']}")
```

### 4.3 全量评估

```python
sm = SkillManager("skills", "outputs/skill_versions")
ev = Evaluator("data/eval_set.json")
agent = WargameAgent(sm, nudge_interval=0)

result = ev.run_full_eval(agent.answer, verbose=True)
ev.print_report(result, label="当前Skill评估")
```

---

## 五、关键配置项

### 5.1 修改 Nudge 间隔

编辑 `data/experiment_script.json`：

```json
{
  "nudge_interval": 10,
  "probe_question_ids": [1, 3, 5, 9, 15, 19, 25, 29, 35, 41, 45, 49, 52, 56, 60, 64, 68, 72, 76, 80]
}
```

### 5.2 修改评估关键词

编辑 `data/eval_set.json`。给某题加 `note` 字段记录修改意图：

```json
{
  "id": 16,
  "ground_truth": {
    "required": ["不受", "影响"],
    "forbidden": ["受地形影响", "受高差影响"]
  },
  "note": "道路行驶不受地形影响，forbidden 改为具体场景避免误判"
}
```

### 5.3 切换 LLM 提供商

默认使用 DashScope（`qwen-plus`）。切换到其他 OpenAI 兼容接口（如 DeepSeek）：

```python
# src/agent.py 和 src/background_reviewer.py
self.client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1",
)
self.model = "deepseek-chat"
```

### 5.4 调整初始 Skill

编辑 `skills/` 下的 `SKILL.md` 文件。Skill 文件格式：

```markdown
---
name: skill_name
description: 技能描述（一句话）
type: knowledge
version: 1
---

## 规则标题
- 规则条目 1
- 规则条目 2
...

<!-- 版本注释 -->
```

修改初始 Skill 后，若 `outputs/skills_original/` 已存在，实验会从该备份还原。需要同步更新 `outputs/skills_original/`，或删除 `outputs/` 后重新运行以重建备份。

---

## 六、FAQ

### Q1：基线准确率为什么是 27.5%，而校验脚本显示 38.8%？

`scripts/validate.py` 输出的 31/80 = 38.8% 是**静态覆盖上限**，表示初始 Skill 中存在对应条款的题目比例。真实运行时，部分题目虽有关键条款，但回答仍缺关键词或命中禁止词，因此实际基线为 22/80 = 27.5%。

初始 `mobility` 与 `direct_fire` 两个 Skill 覆盖基础规则；观察、间瞄、特殊单位、同格交战、复杂规则没有对应 Skill，按契约回答“需要查询规则手册”并被判为推脱失败。

### Q2：为什么初始 Skill 要故意不完整？

如果初始 Skill 已覆盖全部规则，基线会很高，无法观察“从无到有”的 Skill 创建过程。刻意移除堆叠、夺控、掩蔽、观察等规则后：

- 基线 27.5% → 最终 70.0%，提升幅度清晰。
- 每次 Nudge 都有明确的 Skill 创建或 patch 行为。
- Skill 数量从 2 个扩展到 7 个，版本记录 22 条。

### Q3：为什么每个块都有 Nudge？有全对跳过的块吗？

本次真实运行中 8 个块均有失败样本，因此 8 次 Nudge 全部触发。代码支持“块内全对 → 跳过 Reviewer 与 Probe”，用于节省成本并保持“没失败就没进化”的因果。若某块全对，会在控制台打印“本块全对，跳过 Nudge 和 Probe eval”。

### Q4：Reviewer 为什么有时只 patch 一条就返回？

这是刻意设计。Reviewer 系统提示明确“按失败频次从高到低只修复 1~2 类，留出进化梯度”。一次修完所有失败会让后续块失去进化空间，曲线变成“一次跃升”而不是逐块改进。

### Q5：LLM 输出有随机性，同一实验跑两次准确率不一样？

即使 `temperature=0`，LLM 也不完全确定，±5% 波动正常。关注**趋势**（基线 → 最终的提升幅度）而非绝对值。若波动 >10%，检查：

- 是否切换了不同 LLM。
- 是否修改过 `eval_set.json` 的 ground truth。
- 初始 Skill 是否被改过（删除 `outputs/` 后重新运行可还原）。

### Q6：怎么看某题用的是哪个版本的 Skill？

`outputs/eval_runs/{run_id}.json` 有 `skill_versions_active` 字段：

```json
{
  "skill_versions_active": {
    "mobility": 2,
    "direct_fire": 1,
    "observation": 1
  }
}
```

### Q7：`evolution_log.json` 里的 `question_comparison` 怎么用？

`question_comparison[qid]` 包含这道题在所有评估中的答案历史：

```python
import json
log = json.load(open("outputs/evolution_log.json", encoding="utf-8"))
for h in log["question_comparison"]["43"]["history"]:
    versions = h["skill_versions"]
    mark = "✓" if h["correct"] else "✗"
    print(f"[{h['label']:<32}] {mark} {h['answer'][:60]}")
```

### Q8：怎么新增评估题？

编辑 `data/eval_set.json`：

```json
{
  "id": 81,
  "category": "mobility_basic",
  "difficulty": "medium",
  "question": "...",
  "ground_truth": {
    "required": ["75"],
    "forbidden": ["150", "300"]
  },
  "initial_skill_handles": false,
  "note": "为什么这道题值得测"
}
```

然后把它加入 `data/experiment_script.json` 的 `questions` 数组，或加入 `probe_question_ids`。

### Q9：forbidden 关键词被否定前置误伤怎么办？

当前算法检查 forbidden 前 4 字是否含 `不/无/非/未/没`：

- 合法答案：“不受影响” → “影响”前有“不”→ 跳过，不算命中 ✓
- 错误答案：“受地形影响” → “影响”前无否定 → 命中，判为 fail ✓

若遇到跨句否定（如“虽然不受影响。但建议……”），4 字窗口抓不到。可以：

- 把 forbidden 改成更长的正向错误词（如用“受地形影响”代替“影响”）。
- 在 `note` 里记录这个 trade-off。
- 接受偶尔误判。

### Q10：如何重置实验？

每次运行 `run_experiment.py` 时会自动从 `outputs/skills_original/` 还原初始 Skill。如需手动重置：

```python
import shutil
from pathlib import Path

ROOT = Path(".")

# 还原 Skill
if (ROOT / "skills").exists():
    shutil.rmtree(ROOT / "skills")
shutil.copytree(ROOT / "outputs" / "skills_original", ROOT / "skills")

# 清空版本历史与评估记录
for d in ["skill_versions", "skill_snapshots", "eval_runs"]:
    p = ROOT / "outputs" / d
    if p.exists():
        shutil.rmtree(p)
```

---

## 七、真实输出片段

`python src/run_experiment.py` 的关键输出（2026-08-29 真实运行）：

```text
============================================================
  兵棋推演 Agent 自进化实验 v2
============================================================

────────────────────────────────────────────────────────────
基线评估（初始 Skills，无进化）
────────────────────────────────────────────────────────────
总体准确率: 22/80 = 27.5%
分类准确率:
  complex_rules           0/ 8   0%
  direct_fire_basic       4/10  ████████ 40%
  indirect_fire           0/ 8   0%
  mobility_basic          4/ 8  ██████████ 50%
  observation_basic       0/ 8   0%
  personnel_mobility      5/ 8  ████████████ 62%
  same_hex_combat         0/ 6   0%
  special_units           0/ 8   0%
  suppression             1/ 8  ██ 12%
  vehicle_mobility        8/ 8  ████████████████████ 100%
  [基线] Token: 79604 总计 | 995.0/题 | prompt=970.1 completion=24.9 | 延迟=956.5ms | Skill数=2 | 预估费用=0.3403元

...

────────────────────────────────────────────────────────────
最终评估（进化后 Skills）
────────────────────────────────────────────────────────────
总体准确率: 56/80 = 70.0%
  [最终] Token: 265616 总计 | 3320.2/题 | prompt=3276.1 completion=44.1 | 延迟=1317.6ms | Skill数=7 | 预估费用=1.1012元

============================================================
  实验结果汇总
============================================================
  基线准确率:   27.5%  (22/80)
  进化后准确率: 70.0%  (56/80)
  准确率提升:   +42.5%
```

完整日志与逐题记录见 `outputs/evolution_log.json`。

---

## 八、Skill 文件模板

创建新 Skill 时使用以下模板：

```markdown
---
name: skill_name (如 indirect_fire)
description: 一句话描述，用于 Skill 列表展示
type: knowledge
version: 1
---

## 标题1
- 规则条目，包含具体数字/条件
- 注意区分适用范围和例外

## 标题2
- 规则条目

<!-- v1: 创建原因说明 -->
```

编写 Skill 的最佳实践：

1. **关键数字用粗体**：`**75秒**` 而非 `75秒`，Agent 更容易捕获。
2. **正面规则 + 排除项**：先讲“什么情况下可以”，再讲“什么情况下不可以”。
3. **交叉点用 ⚠️ 标注**：`⚠️ 此规则优先于 XXX`。
4. **版本注释**：在文件末尾添加 `<!-- v1: 创建原因 -->`。

---

## 九、输出文件说明

### 9.1 `outputs/eval_runs/`

每次评估生成一个 JSON 文件，包含：

- `run_id`：评估标识（如 `baseline`、`after_nudge_seq10`）
- `label`：评估标签
- `skill_versions_active`：当时各 Skill 的版本号
- `summary`：总体准确率
- `by_category`：分类准确率
- `answers`：每题答案详情
- `token_usage`：本次评估的 token 与延迟统计

### 9.2 `outputs/skill_versions/`

每个 Skill 一个 `{name}_history.json`，包含所有历史版本的完整内容、时间、操作类型与修改原因。

### 9.3 `outputs/skill_snapshots/`

每个版本一个独立 `.md` 文件，便于 diff 对比和人工查看。

### 9.4 `outputs/evolution_log.json`

总日志，包含：

- `skill_snapshots`：所有 Skill 的版本历史摘要
- `eval_runs`：所有评估结果
- `nudge_events`：所有 Nudge 事件（触发块、执行操作、操作后版本）
- `question_comparison`：每题在所有评估中的答案对比
- `token_comparison`：各阶段 token、延迟、费用与准确率
