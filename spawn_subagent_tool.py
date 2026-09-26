"""SpawnSubagentTool — 运行时派生子智能体完成自包含子任务。

主智能体把一个明确、自包含的子任务交给本工具，工具内部用 LangChain create_agent
即时 build 一个"子智能体"（复用同一个 LLM + 工具子集），跑完后把子智能体的最终结论
作为字符串返回给主智能体。

递归防护（硬约束）：子智能体拿到的工具子集里**绝不能再包含 spawn 工具本身**，否则会
无限派生。本文件不直接 import get_all_tools 的 spawn 分支——由 get_all_tools 用
include_spawn=False 保证子工具集干净。
"""

from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from langchain.agents import create_agent

# 延迟 import 避免与 tools/__init__ 形成循环依赖
from tools import get_all_tools

SUBAGENT_SYSTEM_PROMPT = """你是一个子智能体（subagent），由主智能体派生，专注独立完成一个明确的子任务。
你拥有终端、Python、网页抓取、文件读取、知识库检索、浏览器、联网搜索等工具，可自主调用。
请围绕交给你的任务自主完成，然后给出一份清晰、完整、可被主智能体直接使用的最终结论。
不要反问、不要寒暄，直接交付结果。注意：你看不到主智能体的对话历史，只能依据传入的任务描述工作。"""


class SpawnSubagentInput(BaseModel):
    task: str = Field(
        description=(
            "交给子智能体独立完成的、自包含的子任务描述。子智能体看不到当前对话历史，"
            "因此必须把背景、目标、期望产出写清楚。"
        )
    )


def create_spawn_subagent_tool(llm: Any, base_dir: Path) -> StructuredTool:
    """构造 spawn_subagent 工具。

    Args:
        llm: 已初始化的 LLM（与主智能体共用，避免重复构造）。
        base_dir: 项目根目录，用于给子智能体构造受沙箱约束的文件类工具。
    """

    async def _spawn(task: str) -> str:
        
        # 递归防护：include_spawn=False 确保子智能体工具集不含 spawn 工具
        sub_tools = get_all_tools(base_dir, include_spawn=False)

        sub_agent = create_agent(
            model=llm,
            tools=sub_tools,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
        )

        try:
            result = await sub_agent.ainvoke(
                {"messages": [HumanMessage(content=task)]},
                config={"recursion_limit": 24},
            )
        except Exception as e:  # 工具不应向父智能体抛异常，转成可读错误文本
            return f"❌ 子智能体执行失败：{type(e).__name__}: {e}"

        # 提取子智能体最终一条 AI 文本消息作为结论
        final_messages = result.get("messages", []) if isinstance(result, dict) else []
        for msg in reversed(final_messages):
            if getattr(msg, "type", None) in ("ai", "AIMessageChunk") and getattr(msg, "content", ""):
                return str(msg.content)
        return "子智能体未产生有效输出。"

    return StructuredTool.from_function(
        coroutine=_spawn,
        name="spawn_subagent",
        description=(
            "派生一个独立的子智能体来完成一个明确、自包含的子任务，并返回它的最终结论。"
            "适用于需要多步工具调用的独立研究/检索/计算任务——把它隔离出去交给一个专注的"
            "子智能体处理，避免占用当前对话上下文。传入 task 时务必写清楚背景与目标"
            "（子智能体看不到当前对话历史）。"
        ),
        args_schema=SpawnSubagentInput,
    )
