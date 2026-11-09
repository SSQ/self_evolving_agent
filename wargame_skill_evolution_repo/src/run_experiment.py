"""
实验主程序：运行兵棋 Skill 自进化实验，输出多版本对比数据。

设计要点：
  完整展示"无进化基线 → 运行时 Nudge 多轮进化"的量化效果对比。
  每次 Nudge 后跑 probe eval，记录各版本对同一问题的回答差异。
  同时追踪 token 消耗，对比优化前后的成本差异。

使用方式：
  cd wargame_skill_evolution_repo
  python src/run_experiment.py

依赖：
  pip install openai
  set DASHSCOPE_API_KEY=your_key
"""

import os
import sys
import json
import shutil
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from skill_manager import SkillManager
from evaluator import Evaluator
from agent import WargameAgent
from background_reviewer import BackgroundReviewer

SKILLS_DIR    = ROOT / "skills"
SKILLS_ORIG   = ROOT / "outputs" / "skills_original"
EVAL_SET      = ROOT / "data" / "eval_set.json"
EXPERIMENT_SCRIPT = ROOT / "data" / "experiment_script.json"
POLICIES      = ROOT / "data" / "policy.md"
VERSIONS_DIR  = ROOT / "outputs" / "skill_versions"
EVAL_RUNS_DIR = ROOT / "outputs" / "eval_runs"
EVOL_LOG      = ROOT / "outputs" / "evolution_log.json"


# ── Token 统计辅助 ────────────────────────────────────────────────────────────

# DashScope qwen-plus-chat 参考价格: 0.004元/千prompt-token, 0.015元/千completion-token
PROMPT_PRICE_PER_1K = 0.004
COMPLETION_PRICE_PER_1K = 0.015


def estimate_cost(token_data: dict) -> float:
    """根据 token 使用量估算费用（元）。"""
    prompt_cost = (token_data.get("prompt_tokens", 0) / 1000) * PROMPT_PRICE_PER_1K
    completion_cost = (token_data.get("completion_tokens", 0) / 1000) * COMPLETION_PRICE_PER_1K
    return round(prompt_cost + completion_cost, 4)


# ── 备份 / 还原 ──────────────────────────────────────────────────────────────

def ensure_original(sm: SkillManager):
    """首次运行时创建原始备份（并保存快照 v1），之后只读不写。"""
    if not SKILLS_ORIG.exists():
        shutil.copytree(SKILLS_DIR, SKILLS_ORIG)
        print(f"✓ 首次运行：原始 Skills 备份至 {SKILLS_ORIG.name}/")
        for skill_name, content in sm.load_all().items():
            sm._save_version(skill_name, content, action="initial", reason="初始版本")
    else:
        print("✓ 检测到原始备份，已跳过覆盖")

def restore_from_original():
    """每次实验前从原始备份还原，保证可重复运行。"""
    if not SKILLS_ORIG.exists():
        raise RuntimeError("原始备份不存在，请删除 outputs/ 并重新运行")
    if SKILLS_DIR.exists():
        shutil.rmtree(SKILLS_DIR)
    shutil.copytree(SKILLS_ORIG, SKILLS_DIR)
    if VERSIONS_DIR.exists():
        shutil.rmtree(VERSIONS_DIR)
    snapshots = ROOT / "outputs" / "skill_snapshots"
    if snapshots.exists():
        shutil.rmtree(snapshots)
    if EVAL_RUNS_DIR.exists():
        shutil.rmtree(EVAL_RUNS_DIR)
    print("✓ 已还原初始 Skills，清空上次版本历史")


# ── Probe / Full Eval ─────────────────────────────────────────────────────────

def run_probe_eval(agent: WargameAgent, evaluator: Evaluator,
                   probe_ids: list[int], run_id: str, label: str,
                   sm: SkillManager) -> dict:
    """
    对 probe_ids 指定的题目运行评估，保存到 eval_runs/{run_id}.json。
    返回包含 per-question 答案和 token 使用数据的完整结果字典。
    """
    EVAL_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    total, correct = 0, 0
    by_category: dict = {}
    answers: dict = {}

    for qid in probe_ids:
        q = evaluator.questions[qid]
        answer = agent.answer(q["question"])
        ok, reason = evaluator.evaluate_answer(answer, qid)
        total += 1
        cat = q["category"]
        by_category.setdefault(cat, {"total": 0, "correct": 0})
        by_category[cat]["total"] += 1
        if ok:
            correct += 1
            by_category[cat]["correct"] += 1
        answers[str(qid)] = {"answer": answer, "correct": ok, "fail_reason": reason if not ok else ""}

    # 收集本次评估的 token 使用情况
    token_usage = agent.get_token_usage()

    for cat in by_category.values():
        cat["accuracy"] = round(cat["correct"] / cat["total"], 3)

    result = {
        "run_id": run_id,
        "label": label,
        "timestamp": datetime.now().isoformat(),
        "skill_versions_active": sm.get_active_versions(),
        "summary": {"total": total, "correct": correct, "accuracy": round(correct / total, 3)},
        "by_category": by_category,
        "answers": answers,
        "token_usage": {k: v for k, v in token_usage.items() if k != "details"},
    }
    (EVAL_RUNS_DIR / f"{run_id}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result

def run_full_eval(agent: WargameAgent, evaluator: Evaluator,
                  run_id: str, label: str, sm: SkillManager) -> dict:
    all_ids = list(evaluator.questions.keys())
    return run_probe_eval(agent, evaluator, all_ids, run_id, label, sm)


# ── Evolution Log ─────────────────────────────────────────────────────────────

class EvolutionLog:
    """收集实验过程中所有结构化数据，实验结束后写入 evolution_log.json。"""

    def __init__(self):
        self.eval_runs: list[dict] = []
        self.nudge_events: list[dict] = []
        self.question_history: dict[str, list] = {}
        self.token_phases: list[dict] = []   # 每个阶段的 token 汇总

    def add_eval_run(self, result: dict):
        token_info = result.get("token_usage", {})
        self.eval_runs.append({
            "run_id": result["run_id"],
            "label": result["label"],
            "timestamp": result["timestamp"],
            "skill_versions_active": result["skill_versions_active"],
            "accuracy": result["summary"]["accuracy"],
            "correct": result["summary"]["correct"],
            "total": result["summary"]["total"],
            "by_category": {k: v["accuracy"] for k, v in result["by_category"].items()},
            "token_usage": token_info,
        })
        # 记录阶段 token 汇总
        if token_info.get("calls", 0) > 0:
            self.token_phases.append({
                "phase": result["label"],
                "run_id": result["run_id"],
                "calls": token_info["calls"],
                "total_tokens": token_info["total_tokens"],
                "avg_total_tokens": token_info["avg_total_tokens"],
                "avg_prompt_tokens": token_info["avg_prompt_tokens"],
                "avg_completion_tokens": token_info["avg_completion_tokens"],
                "avg_latency_ms": token_info["avg_latency_ms"],
                "max_latency_ms": token_info["max_latency_ms"],
                "skill_count_range": token_info["skill_count_range"],
                "cost_estimate": estimate_cost(token_info),
                "accuracy": result["summary"]["accuracy"],
                "correct_per_total": f"{result['summary']['correct']}/{result['summary']['total']}",
            })
        for qid_str, ans_data in result["answers"].items():
            self.question_history.setdefault(qid_str, []).append({
                "run_id": result["run_id"],
                "label": result["label"],
                "skill_versions": result["skill_versions_active"],
                "answer": ans_data["answer"],
                "correct": ans_data["correct"],
                "fail_reason": ans_data.get("fail_reason", ""),
            })

    def add_nudge_event(self, seq: int, block: str, actions_taken: list[dict],
                        accuracy_before: float, skill_versions_after: dict):
        self.nudge_events.append({
            "after_seq": seq,
            "block": block,
            "timestamp": datetime.now().isoformat(),
            "accuracy_before_this_block": round(accuracy_before, 3),
            "actions_taken": actions_taken,
            "skill_versions_after": skill_versions_after,
        })

    def save(self, sm: SkillManager, evaluator: Evaluator):
        """写入 evolution_log.json，包含 Skill 版本历史、token 使用和题目对比数据。"""
        skill_snapshots = {}
        for skill_dir in SKILLS_DIR.iterdir():
            if skill_dir.is_dir():
                name = skill_dir.name
                history = sm.get_version_history(name)
                skill_snapshots[name] = [
                    {
                        "version": h["version"],
                        "time": h["time"],
                        "action": h["action"],
                        "reason": h["reason"][:120],
                        "snapshot_file": h.get("snapshot_file", ""),
                    }
                    for h in history
                ]

        question_comparison = {}
        for qid_str, history in self.question_history.items():
            qid = int(qid_str)
            q = evaluator.questions.get(qid)
            if q:
                question_comparison[qid_str] = {
                    "question": q["question"],
                    "category": q["category"],
                    "difficulty": q["difficulty"],
                    "ground_truth": q["ground_truth"],
                    "history": history,
                }

        log = {
            "generated_at": datetime.now().isoformat(),
            "skill_snapshots": skill_snapshots,
            "eval_runs": self.eval_runs,
            "nudge_events": self.nudge_events,
            "question_comparison": question_comparison,
            "token_comparison": self.token_phases,
        }
        EVOL_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✓ evolution_log.json 已保存 ({EVOL_LOG})")


# ── 主实验流程 ────────────────────────────────────────────────────────────────

def run_experiment():
    print("=" * 60)
    print("  兵棋推演 Agent 自进化实验 v2")
    print("=" * 60)

    (ROOT / "outputs").mkdir(exist_ok=True)
    sm = SkillManager(str(SKILLS_DIR), str(VERSIONS_DIR))

    ensure_original(sm)
    restore_from_original()

    sm = SkillManager(str(SKILLS_DIR), str(VERSIONS_DIR))
    agent = WargameAgent(sm, nudge_interval=0)
    reviewer = BackgroundReviewer(str(POLICIES), sm)
    evaluator = Evaluator(str(EVAL_SET))
    elog = EvolutionLog()

    for skill_name, content in sm.load_all().items():
        sm._save_version(skill_name, content, action="initial", reason="初始版本")

    script_data = json.loads(EXPERIMENT_SCRIPT.read_text(encoding="utf-8"))
    script_questions = script_data["questions"]
    nudge_interval = script_data["nudge_interval"]
    probe_ids = script_data.get("probe_question_ids", list(range(1, 31)))

    # ── 基线评估 ─────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("基线评估（初始 Skills，无进化）")
    print("─" * 60)
    baseline = run_full_eval(agent, evaluator, "baseline", "基线（初始Skills）", sm)
    elog.add_eval_run(baseline)
    _print_summary(baseline)
    _print_token_summary(baseline, "基线")
    baseline_tokens = baseline.get("token_usage", {})
    agent.conversation_history.clear()

    # ── 实验脚本 ─────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"实验脚本运行（{len(script_questions)} 题，Nudge 间隔={nudge_interval}）")
    print("─" * 60)

    iters = 0
    block_correct = 0
    block_total = 0
    block_failed_turns: list[dict] = []
    block_tokens: list[dict] = []  # 记录每个块的 token 使用

    for item in script_questions:
        seq = item["seq"]
        eval_id = item["eval_id"]
        question = item["question"]
        block = item.get("block", "")

        answer = agent.answer(question)
        ok, reason = evaluator.evaluate_answer(answer, eval_id)
        block_correct += int(ok)
        block_total += 1
        iters += 1
        if not ok:
            block_failed_turns.append({"question": question, "answer": answer, "fail_reason": reason})

        status = "✓" if ok else "✗"
        note = f"  [{item['note']}]" if item.get("note") else ""
        print(f"  Q{seq:02d} {status}  {question[:50]:<50}{note}")

        if iters >= nudge_interval:
            block_acc = block_correct / block_total if block_total else 0
            block_token = agent.get_token_usage()
            block_tokens.append({
                "block": block,
                "seq_range": f"{seq - nudge_interval + 1}-{seq}",
                "correct": block_correct,
                "total": block_total,
                "accuracy": round(block_acc, 3),
                "total_tokens": block_token["total_tokens"],
                "avg_total_tokens": block_token["avg_total_tokens"],
                "avg_prompt_tokens": block_token["avg_prompt_tokens"],
                "avg_completion_tokens": block_token["avg_completion_tokens"],
                "avg_latency_ms": block_token["avg_latency_ms"],
                "cost_estimate": estimate_cost(block_token),
                "skill_count_range": block_token["skill_count_range"],
            })

            print(f"\n{'━'*60}")
            print(f"  本块 [{block}] 完成: {block_correct}/{block_total} = {block_acc:.1%}")
            print(f"  Token: {block_token['total_tokens']} 总计 "
                  f"({block_token['avg_total_tokens']}/题, "
                  f"prompt={block_token['avg_prompt_tokens']}, "
                  f"completion={block_token['avg_completion_tokens']}) "
                  f"延迟={block_token['avg_latency_ms']}ms")

            if not block_failed_turns:
                print(f"  ✓ 本块全对，跳过 Nudge 和 Probe eval")
                elog.add_nudge_event(seq, block, [], block_acc, sm.get_active_versions())
            else:
                print(f"  🔔 Nudge 触发（{len(block_failed_turns)} 条失败样本注入 Reviewer）")
                actions = reviewer.review(block_failed_turns)
                executed_actions = []
                for act in (actions or []):
                    try:
                        if act["action"] == "create":
                            ok_act = sm.create(act["skill_name"], act["content"], reason=act.get("reason",""))
                        elif act["action"] == "patch":
                            ok_act = sm.patch(act["skill_name"], act["old_text"], act["new_text"], reason=act.get("reason",""))
                        else:
                            ok_act = False
                        if ok_act:
                            executed_actions.append({"action": act["action"], "skill": act["skill_name"], "reason": act.get("reason","")[:80]})
                    except Exception as e:
                        print(f"  [Reviewer] 执行失败: {e}")
                print(f"  ✓ 执行了 {len(executed_actions)} 个 Skill 操作")

                probe_run_id = f"after_nudge_seq{seq}"
                probe_label = f"Nudge后（seq={seq}, block={block}）"
                probe_result = run_probe_eval(agent, evaluator, probe_ids, probe_run_id, probe_label, sm)
                elog.add_eval_run(probe_result)
                probe_tok = probe_result.get("token_usage", {})
                print(f"  Probe eval: {probe_result['summary']['correct']}/{probe_result['summary']['total']} = {probe_result['summary']['accuracy']:.1%}")
                if probe_tok:
                    print(f"  Probe Token: {probe_tok.get('total_tokens', 0)} 总计 "
                          f"({probe_tok.get('avg_total_tokens', 0)}/题)")
                elog.add_nudge_event(seq, block, executed_actions, block_acc, sm.get_active_versions())

            iters = 0
            block_correct = 0
            block_total = 0
            block_failed_turns = []
            agent.conversation_history = agent.conversation_history[-5:]
            print(f"{'━'*60}\n")

    # ── 最终评估 ─────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("最终评估（进化后 Skills）")
    print("─" * 60)
    final = run_full_eval(agent, evaluator, "final", "最终（进化后）", sm)
    elog.add_eval_run(final)
    _print_summary(final)
    _print_token_summary(final, "最终")
    final_tokens = final.get("token_usage", {})

    # ── 汇总 ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  实验结果汇总")
    print("=" * 60)
    base_acc = baseline["summary"]["accuracy"]
    final_acc = final["summary"]["accuracy"]
    print(f"  基线准确率:   {base_acc:.1%}  ({baseline['summary']['correct']}/{baseline['summary']['total']})")
    print(f"  进化后准确率: {final_acc:.1%}  ({final['summary']['correct']}/{final['summary']['total']})")
    print(f"  准确率提升:   +{(final_acc - base_acc):.1%}")

    # ── Token 消耗对比 ──────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  Token 消耗对比（基线 vs 最终，均为 80 题全量评估）")
    print("─" * 60)
    _print_token_comparison(baseline_tokens, final_tokens, base_acc, final_acc)

    # ── 实验脚本各块 Token 轨迹 ────────────────────────────────────────────
    if block_tokens:
        print(f"\n{'─'*60}")
        print("  实验脚本各块 Token 轨迹（10 题/块）")
        print("─" * 60)
        print(f"  {'块':<30} {'准确率':>8} {'Token/题':>10} {'总计':>8} {'Skill数':>8} {'费用(元)':>10}")
        print(f"  {'─'*74}")
        for bt in block_tokens:
            print(f"  {bt['block']:<30} {bt['accuracy']:>7.1%} "
                  f"{bt['avg_total_tokens']:>9.0f} "
                  f"{bt['total_tokens']:>7} "
                  f"{bt['skill_count_range']:>7} "
                  f"{bt['cost_estimate']:>10.4f}")

    print(f"\n  进化轨迹（probe eval 准确率 + Token）:")
    for run in elog.eval_runs:
        if run["run_id"].startswith("after_nudge"):
            tok = run.get("token_usage", {})
            tok_str = f"Token={tok.get('total_tokens', 0)}" if tok else ""
            print(f"    {run['label']:<35}  {run['correct']:>2}/{run['total']:>2} = {run['accuracy']:.1%}  {tok_str}")

    print(f"\n  Skill 版本历史:")
    for name, versions in sorted(sm.get_all_version_summaries().items()):
        print(f"    {name}: {len(versions)} 个版本")
        for v in versions:
            print(f"      {v.get('time','')[:19]} [{v['action']}] {v['reason'][:60]}")

    elog.save(sm, evaluator)
    print(f"\n✓ 评估详情见 outputs/eval_runs/")
    print(f"✓ 各版本 Skill 见 outputs/skill_snapshots/")
    print(f"✓ Token 对比数据见 outputs/evolution_log.json → token_comparison")


def _print_summary(result: dict):
    r = result["summary"]
    print(f"总体准确率: {r['correct']}/{r['total']} = {r['accuracy']:.1%}")
    print("分类准确率:")
    for cat, stats in sorted(result["by_category"].items()):
        bar = "█" * int(stats["accuracy"] * 20)
        print(f"  {cat:<22} {stats['correct']:>2}/{stats['total']:>2}  {bar} {stats['accuracy']:.0%}")


def _print_token_summary(result: dict, label: str = ""):
    """打印单次评估的 Token 使用摘要。"""
    tok = result.get("token_usage", {})
    if not tok or tok.get("calls", 0) == 0:
        return
    prefix = f"[{label}] " if label else ""
    print(f"  {prefix}Token: {tok['total_tokens']} 总计 | "
          f"{tok['avg_total_tokens']}/题 | "
          f"prompt={tok['avg_prompt_tokens']} completion={tok['avg_completion_tokens']} | "
          f"延迟={tok['avg_latency_ms']}ms (max={tok['max_latency_ms']}ms) | "
          f"Skill数={tok['skill_count_range']} | "
          f"预估费用={estimate_cost(tok):.4f}元")


def _print_token_comparison(baseline: dict, final: dict,
                            base_acc: float, final_acc: float):
    """打印基线 vs 最终的 Token 消耗对比表。"""
    if not baseline or not final:
        print("  (无 Token 数据)")
        return

    print(f"  {'指标':<24} {'基线':>12} {'最终':>12} {'变化':>12}")
    print(f"  {'─'*60}")

    def row(name: str, base_val, final_val, is_int: bool = False, unit: str = ""):
        if is_int:
            b_str = f"{base_val}"
            f_str = f"{final_val}"
        else:
            b_str = f"{base_val:.1f}"
            f_str = f"{final_val:.1f}"
        if base_val and base_val != 0:
            pct = (final_val - base_val) / base_val * 100
            sign = "+" if pct >= 0 else ""
            delta = f"{sign}{pct:.1f}%"
        else:
            delta = "N/A"
        print(f"  {name:<24} {b_str:>12} {f_str:>12} {delta:>12}")

    base_total = baseline.get("total_tokens", 0)
    final_total = final.get("total_tokens", 0)
    base_avg = baseline.get("avg_total_tokens", 0)
    final_avg = final.get("avg_total_tokens", 0)
    base_prompt = baseline.get("avg_prompt_tokens", 0)
    final_prompt = final.get("avg_prompt_tokens", 0)
    base_completion = baseline.get("avg_completion_tokens", 0)
    final_completion = final.get("avg_completion_tokens", 0)
    base_lat = baseline.get("avg_latency_ms", 0)
    final_lat = final.get("avg_latency_ms", 0)
    base_max_lat = baseline.get("max_latency_ms", 0)
    final_max_lat = final.get("max_latency_ms", 0)
    base_cost = estimate_cost(baseline)
    final_cost = estimate_cost(final)

    row("总 Token 消耗", base_total, final_total, is_int=True)
    row("平均 Token/题", base_avg, final_avg)
    row("  ├ Prompt/题", base_prompt, final_prompt)
    row("  └ Completion/题", base_completion, final_completion)
    row("平均延迟(ms)", base_lat, final_lat)
    row("最大延迟(ms)", base_max_lat, final_max_lat)
    row("预估费用(元)", base_cost, final_cost, is_int=False)

    # 额外指标：每题成本效率
    if final_acc > 0 and base_acc > 0:
        base_cost_per_correct = base_cost / (baseline.get("calls", 1) * base_acc) if base_acc > 0 else float('inf')
        final_cost_per_correct = final_cost / (final.get("calls", 1) * final_acc) if final_acc > 0 else float('inf')
        print(f"\n  效率指标（每题成本 / 准确率）:")
        print(f"    基线: {base_cost_per_correct:.4f}元/题per准确率点")
        print(f"    最终: {final_cost_per_correct:.4f}元/题per准确率点")

        # Token 效率
        base_tok_per_correct = base_total / (baseline.get("calls", 1) * base_acc) if base_acc > 0 else float('inf')
        final_tok_per_correct = final_total / (final.get("calls", 1) * final_acc) if final_acc > 0 else float('inf')
        print(f"\n  Token 效率（总Token / 准确率）:")
        print(f"    基线: {base_tok_per_correct:.1f} token/准确率点")
        print(f"    最终: {final_tok_per_correct:.1f} token/准确率点")


if __name__ == "__main__":
    if not os.getenv("DASHSCOPE_API_KEY"):
        print("错误: 请先设置 DASHSCOPE_API_KEY 环境变量")
        sys.exit(1)
    run_experiment()
