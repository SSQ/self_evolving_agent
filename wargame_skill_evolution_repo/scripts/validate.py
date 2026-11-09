"""Validation script for wargame_skill_evolution_repo project."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

errors = []
warnings = []

# 1. Check directory structure
required_dirs = [
    "data",
    "skills",
    "src",
    "skills/mobility",
    "skills/direct_fire",
]
for d in required_dirs:
    p = ROOT / d
    if not p.exists():
        errors.append(f"Missing directory: {d}")
    else:
        print(f"[OK] Directory exists: {d}/")

# 2. Check required files
required_files = [
    "data/policy.md",
    "data/eval_set.json",
    "data/experiment_script.json",
    "skills/mobility/SKILL.md",
    "skills/direct_fire/SKILL.md",
    "src/agent.py",
    "src/skill_manager.py",
    "src/evaluator.py",
    "src/background_reviewer.py",
    "src/run_experiment.py",
    "src/rule_eval_with_review.py",
    "requirements.txt",
]
for f in required_files:
    p = ROOT / f
    if not p.exists():
        errors.append(f"Missing file: {f}")
    else:
        size = p.stat().st_size
        print(f"[OK] File exists: {f} ({size} bytes)")

# 3. Validate eval_set.json
print("\n" + "="*50)
print("Validating eval_set.json...")
eval_path = ROOT / "data/eval_set.json"
try:
    with open(eval_path, 'r', encoding='utf-8') as f:
        eval_data = json.load(f)
    
    questions = eval_data.get("questions", [])
    print(f"  Total questions: {len(questions)}")
    
    # Check question IDs
    ids = sorted([q["id"] for q in questions])
    expected_ids = list(range(1, len(questions) + 1))
    if ids != expected_ids:
        errors.append(f"eval_set.json: Question IDs are not sequential (got {ids[:5]}...{ids[-3:]})")
    
    # Count initial_skill_handles
    handles_true = sum(1 for q in questions if q.get("initial_skill_handles"))
    handles_false = sum(1 for q in questions if not q.get("initial_skill_handles"))
    print(f"  initial_skill_handles=true: {handles_true}")
    print(f"  initial_skill_handles=false: {handles_false}")
    print(f"  Static initial skill coverage: {handles_true}/{len(questions)} = {handles_true/len(questions):.1%}")
    
    # Check required fields
    for q in questions:
        for field in ["id", "category", "question", "ground_truth", "initial_skill_handles"]:
            if field not in q:
                errors.append(f"eval_set.json: Q{q.get('id','?')} missing field '{field}'")
        gt = q.get("ground_truth", {})
        if "required" not in gt:
            errors.append(f"eval_set.json: Q{q.get('id','?')} ground_truth missing 'required'")
    
    # Check categories
    categories = {}
    for q in questions:
        cat = q.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1
    print(f"  Categories: {categories}")
    
    print("  [OK] eval_set.json is valid JSON")
    
except json.JSONDecodeError as e:
    errors.append(f"eval_set.json: JSON parse error: {e}")
except Exception as e:
    errors.append(f"eval_set.json: {e}")

# 4. Validate experiment_script.json
print("\n" + "="*50)
print("Validating experiment_script.json...")
script_path = ROOT / "data/experiment_script.json"
try:
    with open(script_path, 'r', encoding='utf-8') as f:
        script_data = json.load(f)
    
    script_questions = script_data.get("questions", [])
    print(f"  Total questions: {len(script_questions)}")
    
    nudge_interval = script_data.get("nudge_interval", "not set")
    print(f"  nudge_interval: {nudge_interval}")
    
    probe_ids = script_data.get("probe_question_ids", [])
    print(f"  probe_question_ids: {len(probe_ids)} questions")
    
    expected_evol = script_data.get("expected_evolutions", [])
    print(f"  expected_evolutions: {len(expected_evol)} stages")
    
    # Verify all script eval_ids exist in eval_set
    eval_ids = set(q["id"] for q in questions)
    for dq in script_questions:
        eid = dq.get("eval_id")
        if eid not in eval_ids:
            errors.append(f"experiment_script.json: seq={dq.get('seq')} references eval_id={eid} which doesn't exist in eval_set")
    
    # Verify all probe IDs exist
    for pid in probe_ids:
        if pid not in eval_ids:
            errors.append(f"experiment_script.json: probe_question_ids references {pid} which doesn't exist in eval_set")
    
    # Check sequential
    seq_nums = [dq["seq"] for dq in script_questions]
    expected_seqs = list(range(1, len(script_questions) + 1))
    if seq_nums != expected_seqs:
        errors.append(f"experiment_script.json: seq numbers not sequential")
    
    print("  [OK] experiment_script.json is valid JSON")
    
except json.JSONDecodeError as e:
    errors.append(f"experiment_script.json: JSON parse error: {e}")
except Exception as e:
    errors.append(f"experiment_script.json: {e}")

# 5. Validate SKILL.md files
print("\n" + "="*50)
print("Validating SKILL.md files...")
for skill_dir in (ROOT / "skills").iterdir():
    if skill_dir.is_dir():
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            content = skill_file.read_text(encoding="utf-8")
            print(f"  [OK] skills/{skill_dir.name}/SKILL.md ({len(content)} chars)")
            if "---" not in content[:10]:
                warnings.append(f"skills/{skill_dir.name}/SKILL.md: missing frontmatter")
            if "version:" not in content:
                warnings.append(f"skills/{skill_dir.name}/SKILL.md: missing version field")
            if "description:" not in content:
                warnings.append(f"skills/{skill_dir.name}/SKILL.md: missing description field")

# 6. Check Python syntax of src files
print("\n" + "="*50)
print("Validating Python source files...")
src_dir = ROOT / "src"
for py_file in src_dir.glob("*.py"):
    try:
        with open(py_file, 'r', encoding='utf-8') as f:
            compile(f.read(), str(py_file), 'exec')
        print(f"  [OK] src/{py_file.name} - syntax valid")
    except SyntaxError as e:
        errors.append(f"src/{py_file.name}: SyntaxError: {e}")

# 7. Cross-check: skills coverage of eval questions
print("\n" + "="*50)
print("Cross-checking skill coverage...")
skill_ids_handled = [q["id"] for q in questions if q.get("initial_skill_handles")]
skill_ids_unhandled = [q["id"] for q in questions if not q.get("initial_skill_handles")]
print(f"  Questions handled by initial skills: {len(skill_ids_handled)} ({sorted(skill_ids_handled)[:10]}...)")
print(f"  Questions NOT handled: {len(skill_ids_unhandled)} ({sorted(skill_ids_unhandled)[:10]}...)")
print(f"  Static initial skill coverage: {len(skill_ids_handled)/len(questions):.1%}")

# 8. Summary
print("\n" + "="*50)
print("VALIDATION SUMMARY")
print("="*50)
if errors:
    print(f"\n❌ {len(errors)} ERROR(S):")
    for e in errors:
        print(f"  - {e}")
else:
    print("\n✅ No errors found!")

if warnings:
    print(f"\n⚠️ {len(warnings)} WARNING(S):")
    for w in warnings:
        print(f"  - {w}")

if not errors:
    print("\n✅ Project structure is valid and ready for use!")
    print("   Run experiment with: python src/run_experiment.py (requires DASHSCOPE_API_KEY)")

sys.exit(1 if errors else 0)
