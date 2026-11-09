"""
用当前 skills/ 目录下的 Skill 跑一次全量规则评估（80题），
保存答案到 outputs/rule_eval_full.json，便于后续复核。
同时输出 token 消耗统计。
"""
import os, sys, json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from skill_manager import SkillManager
from evaluator import Evaluator
from agent import WargameAgent

sm = SkillManager(str(ROOT / "skills"), str(ROOT / "outputs" / "skill_versions"))
ev = Evaluator(str(ROOT / "data" / "eval_set.json"))
agent = WargameAgent(sm, nudge_interval=0)

print(f"当前 Skills: {list(sm.load_all().keys())}")
print(f"共 {len(ev.questions)} 题\n")

results = []
correct = 0
by_cat = {}

for qid in sorted(ev.questions.keys()):
    q = ev.questions[qid]
    ans = agent.answer(q["question"])
    ok, reason = ev.evaluate_answer(ans, qid)
    if ok: correct += 1
    cat = q["category"]
    by_cat.setdefault(cat, {"total": 0, "correct": 0})
    by_cat[cat]["total"] += 1
    if ok: by_cat[cat]["correct"] += 1

    mark = "✓" if ok else "✗"
    print(f"Q{qid:02d} {mark} [{cat}] {q['question'][:50]}")
    if not ok:
        print(f"      → {reason}")

    results.append({
        "id": qid,
        "category": cat,
        "question": q["question"],
        "ground_truth": q["ground_truth"],
        "answer": ans,
        "rule_correct": ok,
        "rule_reason": reason,
    })

# 收集 token 使用统计
tok = agent.get_token_usage()

print(f"\n总体准确率: {correct}/{len(ev.questions)} = {correct/len(ev.questions):.1%}")
print("分类准确率:")
for cat in sorted(by_cat):
    s = by_cat[cat]
    s["accuracy"] = round(s["correct"]/s["total"], 3)
    print(f"  {cat:<22} {s['correct']:>2}/{s['total']:>2}  {s['accuracy']:.0%}")

print(f"\nToken 消耗:")
print(f"  总调用: {tok['calls']} 次")
print(f"  总 Token: {tok['total_tokens']}")
print(f"  平均 Token/题: {tok['avg_total_tokens']}")
print(f"  Prompt/题: {tok['avg_prompt_tokens']}")
print(f"  Completion/题: {tok['avg_completion_tokens']}")
print(f"  平均延迟: {tok['avg_latency_ms']}ms (max={tok['max_latency_ms']}ms)")
print(f"  Skill 数范围: {tok['skill_count_range']}")

out = ROOT / "outputs" / "rule_eval_full.json"
out.write_text(json.dumps({
    "timestamp": datetime.now().isoformat(),
    "skill_versions_active": sm.get_active_versions(),
    "summary": {"total": len(ev.questions), "correct": correct,
                "accuracy": round(correct/len(ev.questions), 3)},
    "by_category": by_cat,
    "token_usage": {k: v for k, v in tok.items() if k != "details"},
    "results": results,
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n✓ 完整结果已保存到 {out}")
