"""
兵棋推演 Agent：使用当前 Skills 回答规则问题，内置 Nudge 计数机制。

设计要点：
  1. Agent 的唯一知识来源是 Skill 文件，不直接访问 policy.md
  2. Nudge 计数器：每 N 次调用后触发后台回顾
  3. 每次回答前动态加载最新 Skills，保证进化后立即生效
  4. 内置 token 追踪：记录每次调用的 prompt/completion/total tokens 及延迟

使用方式：
  from agent import WargameAgent
  from skill_manager import SkillManager
  sm = SkillManager("skills/")
  agent = WargameAgent(sm, nudge_interval=20)
  answer = agent.answer("坦克机动中能射击吗？")
  usage = agent.get_token_usage()  # 获取累计 token 消耗
"""

import os
import time
from openai import OpenAI
from skill_manager import SkillManager


SYSTEM_TEMPLATE = """你是兵棋推演系统的智能规则助手。

你的所有知识来源于以下技能文档，严格基于文档内容回答，不要自行推断或编造规则。

## 回答规则（严格遵守）
- 【能回答】如果技能文档覆盖了用户问题：直接给出完整具体的答案（含具体秒数、
  速度比、射程等规则细节）。**不要在答案中加"建议查询规则手册"之类的推脱话**。
- 【不能回答】如果技能文档确实不覆盖：**仅回答一句** "需要查询规则手册"，
  不要编造答案，也不要列举可能的情况。

{skills_section}
"""

SKILLS_SECTION_TEMPLATE = """## 当前知识库（共{count}个技能）

{skills_content}
"""


class WargameAgent:
    def __init__(
        self,
        skill_manager: SkillManager,
        nudge_interval: int = 20,
        model: str = "qwen-plus",
    ):
        self.skill_manager = skill_manager
        self.nudge_interval = nudge_interval
        self.model = model
        self._iters_since_nudge = 0
        self.conversation_history: list[dict] = []
        self._token_log: list[dict] = []   # 累计 token 使用记录

        self.client = OpenAI(
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    def answer(self, question: str) -> str:
        """
        回答单个问题。每次调用都会重新加载最新 Skills（保证 Nudge 后立即生效），
        且 messages 里只含系统提示 + 当前问题（不携带 conversation_history），
        这样每次评估互不干扰。

        同时记录本次调用的 token 使用量和延迟，累计到 _token_log 中。
        """
        system_prompt = self._build_system_prompt()
        t0 = time.time()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            temperature=0,
            max_tokens=400,
        )
        latency_ms = round((time.time() - t0) * 1000, 1)
        answer_text = response.choices[0].message.content.strip()

        # 记录 token 使用情况
        usage = response.usage
        skill_count = len(self.skill_manager.load_all())
        self._token_log.append({
            "question": question[:60],
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "total_tokens": usage.total_tokens if usage else 0,
            "latency_ms": latency_ms,
            "skill_count": skill_count,
            "system_prompt_length": len(system_prompt),
        })

        self.conversation_history.append({
            "question": question,
            "answer": answer_text,
            "skills_used": list(self.skill_manager.load_all().keys()),
        })
        if len(self.conversation_history) > 50:
            self.conversation_history = self.conversation_history[-50:]

        self._iters_since_nudge += 1
        return answer_text

    def should_trigger_nudge(self) -> bool:
        """判断是否应该触发后台回顾。触发后计数器归零。"""
        if self.nudge_interval > 0 and self._iters_since_nudge >= self.nudge_interval:
            self._iters_since_nudge = 0
            return True
        return False

    def reset_nudge_counter(self):
        """Agent 主动调用 skill_manage 时手动归零，避免重复触发。"""
        self._iters_since_nudge = 0

    def get_token_usage(self) -> dict:
        """
        获取自上次调用以来的累计 token 使用统计，并清空内部日志。
        返回格式：
        {
            "calls": 10,
            "prompt_tokens": 5000,
            "completion_tokens": 1200,
            "total_tokens": 6200,
            "avg_prompt_tokens": 500.0,
            "avg_completion_tokens": 120.0,
            "avg_total_tokens": 620.0,
            "avg_latency_ms": 850.5,
            "max_latency_ms": 1200.0,
            "skill_count_range": "2-2",
            "details": [...]
        }
        """
        log = self._token_log
        self._token_log = []

        if not log:
            return {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "avg_prompt_tokens": 0.0,
                "avg_completion_tokens": 0.0,
                "avg_total_tokens": 0.0,
                "avg_latency_ms": 0.0,
                "max_latency_ms": 0.0,
                "skill_count_range": "-",
                "details": [],
            }

        calls = len(log)
        total_prompt = sum(e["prompt_tokens"] for e in log)
        total_completion = sum(e["completion_tokens"] for e in log)
        total_all = sum(e["total_tokens"] for e in log)
        latencies = [e["latency_ms"] for e in log]
        skill_counts = set(e["skill_count"] for e in log)

        return {
            "calls": calls,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_all,
            "avg_prompt_tokens": round(total_prompt / calls, 1),
            "avg_completion_tokens": round(total_completion / calls, 1),
            "avg_total_tokens": round(total_all / calls, 1),
            "avg_latency_ms": round(sum(latencies) / calls, 1),
            "max_latency_ms": round(max(latencies), 1),
            "skill_count_range": f"{min(skill_counts)}-{max(skill_counts)}" if len(skill_counts) > 1 else str(min(skill_counts)),
            "details": log,
        }

    def peek_token_usage(self) -> dict:
        """查看当前累计 token 使用统计但不清空（只读）。"""
        log = self._token_log
        if not log:
            return {"calls": 0, "total_tokens": 0}
        calls = len(log)
        return {
            "calls": calls,
            "prompt_tokens": sum(e["prompt_tokens"] for e in log),
            "completion_tokens": sum(e["completion_tokens"] for e in log),
            "total_tokens": sum(e["total_tokens"] for e in log),
        }

    def _build_system_prompt(self) -> str:
        skills = self.skill_manager.load_all()
        if not skills:
            skills_section = "（暂无技能文档，请依据通用兵棋原则回答）"
        else:
            parts = []
            for name, content in sorted(skills.items()):
                parts.append(f"### 技能：{name}\n{content}")
            skills_content = "\n\n---\n\n".join(parts)
            skills_section = SKILLS_SECTION_TEMPLATE.format(
                count=len(skills),
                skills_content=skills_content,
            )
        return SYSTEM_TEMPLATE.format(skills_section=skills_section)
