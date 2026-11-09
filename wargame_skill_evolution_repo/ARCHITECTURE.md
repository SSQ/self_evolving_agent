# ARCHITECTURE — 兵棋推演自进化 Agent 技术方案

## 一、项目定位

本项目是一个可复现、低成本的 Agent 自进化实验系统，参考 Anthropic Hermes Agent 的 Skill Nudge 机制与 DSPy GEPA 的离线优化思路，在**兵棋推演规则问答**场景下验证三件事：

- Skill 能否从对话失败中自动演化（运行时 Nudge）
- Skill 版本差异对准确率的影响能否被量化（固定 Probe + 版本快照）
- Agent / Evaluator / Reviewer 的契约式协作能否让评估清晰、可解释

### 场景选择

| 场景 | 基础 LLM 表现 | 进化空间 | Ground truth 可控性 |
|------|--------------|---------|-------------------|
| 通用数学题 | 已较强（约 90%） | 小 | 低 |
| 代码生成 | 已较强 | 中 | 中（需沙箱执行） |
| 真实客服数据 | 有先验 | 中 | 低（存在泄漏风险） |
| **兵棋推演规则** | **约 27.5%（实测基线）** | **大** | **高（规则完全自定）** |

兵棋规则天然适合验证 Skill 自进化：

1. **规则专有数字密集**：75 秒状态转换、25 格观察距离、4 班堆叠上限等，LLM 无法凭预训练猜中。
2. **规则交叉点清晰**：机动停止 × 射击、同格交战 × 武器冷却、掩蔽 × 压制，构成天然陷阱题。
3. **规则层级分明**：机动、直瞄、观察、间瞄、特殊单位、复杂规则，可自然划分为 Skill 边界。
4. **可渐进覆盖**：从基础机动 → 直瞄 → 观察 → 间瞄 → 同格交战 → 复杂规则，形成清晰的进化梯度。

---

## 二、整体流水线

```text
                    ┌─────────────────────────────────────┐
                    │  experiment_script.json              │
                    │  80 题按类别分 8 块，每块 10 题      │
                    └─────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│   单块 10 题循环                                                     │
│                                                                     │
│   ┌────────────┐    ┌─────────────┐    ┌────────────────────────┐   │
│   │ 主 Agent   │ ─► │ 关键词评估  │ ─► │ 答对/答错分流           │   │
│   │ answer()   │    │ evaluator   │    │ 失败样本入 failed_turns │   │
│   └────────────┘    └─────────────┘    └────────────────────────┘   │
│         ▲                                        │                  │
│         │                                        ▼                  │
│         │           ┌───────────────────────────────────────┐       │
│         │           │   本块结束：检查 failed_turns          │       │
│         │           │   空 → 跳过 Nudge / Probe，Skill 不变  │       │
│         │           │   非空 → 仅失败样本送入 Reviewer       │       │
│         │           └───────────────────────────────────────┘       │
│         │                                        │                  │
│         │                                        ▼                  │
│         │           ┌───────────────────────────────────────┐       │
│         │           │   后台回顾 Agent                       │       │
│         │           │   输入：失败样本 + 完整规则 + 当前Skill│       │
│         │           │   输出：最小必要 create/patch JSON     │       │
│         │           └───────────────────────────────────────┘       │
│         │                                        │                  │
│         │                                        ▼                  │
│         │           ┌───────────────────────────────────────┐       │
│         │           │   SkillManager 执行                    │       │
│         └───────────┤   create/patch + 版本快照 + history    │       │
│                     └───────────────────────────────────────┘       │
│                                         │                           │
│                                         ▼                           │
│                     ┌───────────────────────────────────────┐       │
│                     │   Probe Eval（固定 20 题）             │       │
│                     │   只在 Nudge 触发后执行                │       │
│                     └───────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                     ┌────────────────────────────┐
                     │   最终 80 题评估 + 对比表   │
                     │   evolution_log.json 落盘   │
                     └────────────────────────────┘
```

核心脚本与职责：

| 文件 | 职责 | 关键设计 |
|------|------|---------|
| `src/agent.py` | 主 Agent 单题应答 | 无状态（不传历史），每次加载最新 Skill |
| `src/skill_manager.py` | Skill CRUD + 版本追踪 | 三层持久化（活动版 + 历史 JSON + 独立快照） |
| `src/background_reviewer.py` | 回顾 Agent，输出操作 JSON | 只接受失败样本，最小改动原则 |
| `src/evaluator.py` | 关键词匹配评估 | 数字归一化 + 否定前置检测 + 推脱一票否决 |
| `src/run_experiment.py` | 全流程实验 | 无 UI 依赖，纯脚本，内置 token / 成本追踪 |
| `src/rule_eval_with_review.py` | 独立全量评估 | 不触发 Nudge，一次性评估当前 Skill 库 |

---

## 三、Skill 进化机制

### 3.1 Skill 与 RAG 文档的区别

```text
❌ RAG 式知识文档（规则原文扁平堆砌）
机动停止：地面棋子转换为停止状态需要75秒...

✅ Skill（带决策流程的操作文档）
## 机动停止
- 地面棋子转换为停止状态需要 75秒 转换时间
- 空中棋子转为悬停状态需要 75秒
- 必须等待转换完成后才能执行射击、上下车等操作
- 停止机动命令会导致 75秒 惩罚性转换时间
```

同样的规则，RAG 把信息扁平化存储；Skill 把它结构化为**带边界条件的可执行 SOP**。LLM 读到后者时不需要“推理”，只需要“按步骤照做”，更不容易幻觉。

### 3.2 初始 Skill 的刻意不完整

初始只有两个 Skill，且刻意只覆盖基础规则：

**`mobility/SKILL.md`（初始 v1）**
- 覆盖：基本机动、机动状态、机动停止、车辆地形/坡度速度、人员冲锋与疲劳
- 不含：堆叠规则、夺控规则、车辆行军、掩蔽转换

**`direct_fire/SKILL.md`（初始 v1）**
- 覆盖：武器展开、武器冷却、射击条件、行进间射击基础、压制影响基础
- 不含：战果表示与修正、掩蔽交叉、上下车交叉、工事与射击交叉

这使基线真实可测：Agent 对未覆盖问题按契约回答“需要查询规则手册”，实测基线 22/80 = 27.5%。

### 3.3 三种进化操作

**操作 1：patch 补分支（添加缺失条件）**

最常见。Skill 已覆盖主流程，某个边界情况反复失败 → 追加一条分支。

```markdown
## 压制影响（v1）
- 车辆被压制后人员不能上下车
- 人员被压制后不能机动和射击
- 压制状态持续 150秒 后自动移除

## 压制影响（v2，补掩蔽交叉）
- 车辆被压制后人员不能上下车
- 人员被压制后不能机动和射击
- 压制状态持续 150秒 后自动移除
- ⚠️ 压制状态的棋子不能执行掩蔽命令
```

v2 不替换 v1，而是**扩充判定空间的覆盖范围**。

**操作 2：create 建新 Skill（发现新领域）**

主 Skill 完全不覆盖某类问题，且与现有 Skill 差异较大，Reviewer 选择新建文件。例如间瞄射击涉及飞行阶段、校射、爆炸持续伤害，与直瞄射击有根本差异。

```text
触发信号：连续 10 题间瞄/巡飞弹/无人机问题全部失败
Reviewer 决策：create indirect_fire、aerial_mobility
```

**操作 3：patch 重写（强化提示优先级）**

规则存在但 Agent 引用时忽略优先级。Reviewer 不改信息量，而是**改写表达强度**。

```markdown
## v1（Agent 忽略适用范围）
坦克在机动转停止过程中可射击。

## v2（重写后正确使用）
## ⚠️ 行进间射击（关键规则）
- 坦克主炮可在机动中射击
- 其他地面武器在机动中不能射击
```

这对应 GEPA 思路中的“突变”：改的是提示强度，不是新增事实。

### 3.4 Reviewer 的输入设计

Hermes 原设计让 Reviewer 接收整个对话快照，自主判断哪些是失败。本项目改为**只接收已被评估器判定为失败的样本**。

原因：LLM 作为 Reviewer 时，会把对全量对话的“美学感知”带进来——觉得答对的对话还不够周到、现有 Skill 还不够全面，于是对完全答对的块也产生改动建议。隔离后：

1. Reviewer 只针对客观失败改进，不做主观美学优化。
2. 块内全对时直接跳过 Reviewer 调用，节省成本。
3. 触发条件可解释：能精准看到“因为这 N 条失败，所以做这 M 个改动”。

```python
# src/run_experiment.py
if not block_failed_turns:
    # 本块全对 → 不调 Reviewer、不跑 Probe、Skill 不变
    pass
else:
    actions = reviewer.review(block_failed_turns)
```

### 3.5 Reviewer 提示词的最小改动三原则

```text
1. 仅修复观察到的失败 —— 不扩展到“规则里还有但样本里没出现”的场景
2. 最小改动优先 —— patch 优于 create；old_text 精确到要改的那几行
3. 按频次只修 1~2 类 —— 保留进化梯度，不一次改完所有问题
```

第三条用于避免“一次跃升”：如果 Reviewer 一次修完所有失败，后续块就没有进化空间，也无法观察逐块改进。限制为 1~2 类后，每次 Nudge 的修改对应清晰的失败归因。

---

## 四、Agent–Evaluator 契约式设计

规则评估的根本难题是：LLM 答得“含糊”时到底算对算错？不断堆同义词表会越来越乱。本项目用“契约”把语义判断转成简单子串检查。

### 4.1 契约双方约定

**Agent 侧（系统提示）**
- 能从 Skill 答出 → 给出具体完整答案（含秒数、速度比、射程等规则细节），**不允许**附加“需要查询规则手册”的推脱尾巴。
- 不能从 Skill 答出 → **仅回答一句**“需要查询规则手册”，不编造、不列举可能情况。

**Evaluator 侧（实现）**
- 答案含 `查询规则` → 判定“Agent 推脱”（一票否决）。
- 否则检查 required 关键词是否全部出现、forbidden 关键词是否未被否定。

### 4.2 三种互斥失败原因

```python
# src/evaluator.py
DEFERRAL_SIGNAL = "查询规则"

def evaluate_answer(self, answer, question_id):
    if DEFERRAL_SIGNAL in normalized_answer:
        return False, "Agent 推脱"          # 契约违约 / Skill 未覆盖
    for kw in required:
        if kw not in normalized_answer:
            return False, "缺少关键词"        # 答案没引用规则
    for kw in forbidden:
        if unnegated_hit(answer, kw):
            return False, "出现禁止词"        # 答案有规则冲突内容
    return True, "correct"
```

辅助规则：

- **数字归一化**：`4,000 → 4000`、`75秒 / 75 秒 / 75s → 75`，消除千位分隔符与单位写法差异。
- **否定前置检测**：forbidden 关键词前 4 字出现 `不/无/非/未/没` 时视为被否定，不算命中。例如“不受影响”不会误伤 forbidden“受地形影响”。

### 4.3 契约带来的好处

| 问题 | 无契约做法 | 本项目做法 |
|------|-----------|-----------|
| Agent 不确定时说什么 | 开放式，LLM 自行决定 | 强制“需要查询规则手册”，无二义 |
| 答对带推脱尾巴 | Nudge 会想改 Skill 让其更自信 | 契约禁止尾巴，评估器不罚也无需改 |
| 评估器面对多样表达 | 大同义词表 / LLM-as-judge | 核心走关键词，“不确定”走契约 |
| 失败原因分布 | 复杂多层 | **三类互斥**（推脱 / 缺关键词 / 禁止词） |

实测中三种失败原因呈清晰分布：

- **推脱**集中在没有 Skill 的类别（观察、间瞄、特殊单位、复杂规则初始为 0%）。
- **缺关键词**集中在 Skill 有但表达不完整的题。
- **禁止词**集中在 Skill 有但 Agent 错误引用的题。

看到分类准确率与失败原因分布，就能判断下一步该做什么：推脱多 → `create` 新 Skill；缺关键词多 → `patch` 补细节；禁止词多 → Skill 已有但需改提示强度。

---

## 五、评估系统

### 5.1 三层评估集

| 集合 | 题数 | 用途 | 频率 |
|------|------|------|------|
| 基线全量评估 | 80 | 进化前能力基线 | 1 次 |
| Probe 固定子集 | 20 | 跨 Nudge 轨迹对比 | 每次 Nudge 触发后 1 次 |
| 最终全量评估 | 80 | 进化后整体能力 | 1 次 |

Probe 固定 20 题（ID：1, 3, 5, 9, 15, 19, 25, 29, 35, 41, 45, 49, 52, 56, 60, 64, 68, 72, 76, 80），覆盖全部 10 个类别。固定而非随机抽样，保证多次 Probe 的数值可直接比较——难度不变，变的只有 Skill。

### 5.2 十个评估类别

| 类别 | 题数 | 描述 | 静态初始覆盖 |
|------|------|------|-------------|
| `mobility_basic` | 8 | 基本机动、停止、状态转换 | 6/8 |
| `vehicle_mobility` | 8 | 车辆地形/坡度速度 | 8/8 |
| `personnel_mobility` | 8 | 人员冲锋、疲劳 | 8/8 |
| `direct_fire_basic` | 10 | 武器展开、冷却、射击条件 | 9/10 |
| `suppression` | 8 | 压制影响、掩蔽交叉 | 0/8 |
| `observation_basic` | 8 | 观察距离、通视判断 | 0/8 |
| `indirect_fire` | 8 | 间瞄计划、校射、裁决 | 0/8 |
| `special_units` | 8 | 巡飞弹、无人机、直升机 | 0/8 |
| `same_hex_combat` | 6 | 同格交战触发、裁决 | 0/6 |
| `complex_rules` | 8 | 聚合解聚、工事、雷场、防空 | 0/8 |
| **合计** | **80** | | **31/80（38.8%，静态上限）** |

注意：静态覆盖 31/80 是“初始 Skill 有对应条款”的理论上限；真实基线为 22/80 = 27.5%，因为部分题虽有条款，回答仍缺关键词或命中禁止词。

### 5.3 真实准确率曲线

```text
类别               │ 基线   │ 最终   │ 进化说明
───────────────────┼────────┼────────┼──────────────────────────
complex_rules      │   0%   │ 100%   │ create action_timing + patch close_combat/direct_fire
indirect_fire      │   0%   │  88%   │ create indirect_fire
same_hex_combat    │   0%   │  83%   │ create close_combat
direct_fire_basic  │  40%   │  70%   │ patch 战果/掩蔽/工事交叉规则
mobility_basic     │  50%   │  62%   │ patch 堆叠、夺控、掩蔽转换
personnel_mobility │  62%   │  62%   │ 初始已覆盖，保持不变
suppression        │  12%   │  62%   │ patch 压制影响细节
observation_basic  │   0%   │  50%   │ create observation
special_units      │   0%   │  25%   │ create aerial_mobility
vehicle_mobility   │ 100%   │ 100%   │ 初始已覆盖，保持不变
───────────────────┼────────┼────────┼──────────────────────────
整体               │ 27.5%  │ 70.0%  │ +42.5pp
```

### 5.4 八个 Nudge 阶段（真实运行）

| 阶段 | 题号 | 块 | 本块准确率 | 主要动作 |
|------|------|----|-----------|---------|
| #1 | 1-10 | mobility_vehicle | 6/10 | patch mobility、direct_fire |
| #2 | 11-20 | personnel_mobility | 8/10 | patch mobility（冲锋速度表述） |
| #3 | 21-30 | direct_fire_basic | 7/10 | 3 次 patch direct_fire |
| #4 | 31-40 | suppression_observation | 1/10 | patch direct_fire、mobility |
| #5 | 41-50 | observation_advanced | 1/10 | create observation、patch mobility |
| #6 | 51-60 | indirect_fire_special | 0/10 | create indirect_fire、aerial_mobility |
| #7 | 61-70 | special_units_hex_combat | 3/10 | 5 次操作：aerial_mobility ×3、observation patch、create close_combat |
| #8 | 71-80 | complex_rules | 0/10 | create action_timing、patch close_combat、direct_fire |

8 轮 Nudge 共执行 20 次 Skill 操作；加上 2 个初始版本，`outputs/evolution_log.json` 记录 22 条版本历史。

---

## 六、典型 Bad Case 与修复

### Case 1：同格交战的“首次不受冷却限制”陷阱

- **失败模式**：Skill 写了“武器冷却 75 秒”，Agent 回答同格交战时仍说“需要等 75 秒冷却”。
- **根因**：把通用武器冷却规则套用到同格交战的特殊情况。
- **解法**：`close_combat` Skill 显式标注“进入同格交战第一瞬间立刻射击，不受武器冷却、机动状态和压制状态限制”。

### Case 2：行进间射击的范围泛化

- **失败模式**：Agent 说“所有地面武器都能在机动中射击”，实际只有坦克主炮可以。
- **根因**：Skill 只写“坦克主炮可在机动中射击”，Agent 泛化到所有武器。
- **解法**：重写为“**仅限坦克主炮**，其他地面武器在机动中不能射击”。

### Case 3：观察距离的“2 格”陷阱

- **失败模式**：Agent 对“车辆对无人机”的观察距离回答 25 格（车辆对车辆的距离）。
- **根因**：Skill 中只有车辆观察距离 25 格，Agent 套用到无人机。
- **解法**：`observation` Skill 显式区分不同观察组合的距离表（步兵/车辆/直升机 10、25；无人机/巡飞弹 2 或不可观察）。

### Case 4：块内全对跳过 Nudge

代码支持“块内全对 → 完全跳过 Reviewer 和 Probe”。本次真实运行中 8 个块均有失败，因此未触发跳过分支；该机制用于保证“没失败就没进化”的因果清晰，并节省 Reviewer 调用。

---

## 七、规则评估的已知局限

契约式设计大幅简化评估器，但规则层仍有天花板，本项目主动接受：

| 局限 | 典型场景 | 缓解方式 |
|------|---------|---------|
| 同义词多样性 | “75秒 / 75 秒 / 75s” | 选规则专有数字（“75”）而非描述性词语 |
| 跨句否定 | “虽然不受影响，但……” | 否定窗口 4 字只捕捉近距；远距放过 |
| 冗余信息误导 | 答“25格射程”但附加错误参数 | forbidden 包含错误数字可捕获 |
| GT 需要迭代 | 初版关键词过严 | 在 `note` 字段记录每次修改原因 |

选择规则评估而非 LLM-as-judge 的理由：

- **成本**：单次 80 题全量评估的模型费用为 0.34~1.10 元；LLM-as-judge 会成倍增加。
- **可复现**：规则判定是确定性的。
- **可解释**：规则简单，失败原因可逐题归因。

天花板约 90%。真实生产可叠加三层：规则快速筛选 → LLM-as-judge 复核 → 人工抽查；本项目聚焦 Skill 进化机制，不跑三层栈。

---

## 八、关键工程决策与踩坑

| 问题 | 根因 | 解法 |
|------|------|------|
| Skill 进化后再读取延迟 | 每次 LLM 调用前都 `load_all` 全部文件 | 规模 <10 Skill 时可忽略；更大时加 mtime 缓存 |
| patch 失败：“找不到 old_text” | Reviewer 给的 old_text 与 Skill 有空格差异 | 不做模糊匹配，强制 Reviewer 精确引用 |
| 数字千位分隔符导致误判 | 简单 substring 匹配 | `re.sub(r"(?<=\d)[,，](?=\d)", "", text)` |
| “不受影响”被判为命中 forbidden“受影响” | 裸子串匹配不看否定 | 前 4 字窗口含否定词即视为被否定，跳过 |
| Reviewer 主动补将来的 Skill | 传入整个对话历史 | 改为仅传 `failed_turns`，全对直接跳过 |
| Agent 答对还加推脱尾巴 | 原提示措辞模糊 | 改为严格契约：能答就不说查询规则 |
| 初始 Skill 准确率过高 | 覆盖了太多规则 | 刻意精简：删除 observation、堆叠、夺控、掩蔽规则 |
| 评估关键词冲突 | required 与 forbidden 语义矛盾 | forbidden 改为具体场景，如“受地形影响” |

---

## 九、优化方向

### 数据层
- `policy.md` 补充更多交叉点（间瞄 × 防空、引导 × 同格交战）
- 评估集扩到 100 题，提高统计显著性
- 给每题增加 `difficulty_level`，细分 easy / medium / hard

### 模型层
- 主 Agent 对比不同模型（qwen-plus vs glm-4）的基线能力
- Reviewer 使用更保守的模型，倾向于 patch 而非 create

### 训练策略（迈向真 GEPA）
- 每个 Skill 生成 5 个变体（突变：改写分支；交叉：融合两个 Skill 的好段落）
- 在 holdout 子集（不在实验脚本中的题）上评估各变体
- Pareto 前沿：准确率 vs token 消耗双目标

### 工程部署
- `evolution_log.json` 改 SQLite，便于按时间/类别查询
- 支持导出 evolution_log 为 Markdown 报告
- 增加 Web UI 版本（FastAPI + SSE）

---

## 十、目录结构

```text
wargame_skill_evolution_repo/
├── src/
│   ├── agent.py                   # 主 Agent，单题应答（契约：能答就答、不能只说查询规则）
│   ├── skill_manager.py           # Skill CRUD + 三层版本持久化
│   ├── background_reviewer.py     # 回顾 Agent，仅接受失败样本
│   ├── evaluator.py               # 规则评估 + 推脱一票否决
│   ├── run_experiment.py          # 全流程实验脚本
│   └── rule_eval_with_review.py   # 一次性规则评估脚本（不触发 Nudge）
│
├── data/
│   ├── policy.md                  # 兵棋推演规则手册（仅 Reviewer 可读）
│   ├── eval_set.json              # 80 题评估集（10 类规则）
│   └── experiment_script.json     # 80 题实验脚本（8 块 × 10）
│
├── skills/                        # 活动版本（会被 create/patch 修改）
│   ├── mobility/SKILL.md          # 初始：基本机动 + 车辆/人员速度
│   └── direct_fire/SKILL.md       # 初始：武器展开/冷却 + 射击条件
│
├── outputs/                       # 运行产物
│   ├── skills_original/           # 永不覆盖的初始备份（重置从这里还原）
│   ├── skill_versions/            # {name}_history.json（含全量内容）
│   ├── skill_snapshots/           # {name}_v{N}.md（每版独立文件）
│   ├── eval_runs/                 # {run_id}.json（每次评估详细数据）
│   └── evolution_log.json         # 总日志（含 question_comparison、token_comparison）
│
├── scripts/
│   └── validate.py                # 项目验证脚本
│
├── ARCHITECTURE.md                # 本文档
├── USAGE.md                       # 运行指南
├── README.md                      # 项目说明与真实结果
└── requirements.txt
```

`outputs/evolution_log.json` 是真实运行日志（2026-08-29），包含 8 次 Nudge 事件、22 条 Skill 版本记录、逐题跨版本答案历史与阶段 token 对比。仓库中的 `skills/` 默认停留在初始 v1 状态，便于复现实验。
