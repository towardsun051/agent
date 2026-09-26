"""AgentManager — LangChain Agent with a configurable Ollama/DeepSeek model."""

import asyncio
from pathlib import Path
from typing import Any, AsyncGenerator

from langchain_core.messages import HumanMessage, AIMessage
from langchain.agents import create_agent
from langchain.agents.middleware import ClearToolUsesEdit, ContextEditingMiddleware

from config import get_rag_mode, get_retrieval_mode
from model_config import create_chat_model, get_model_name, get_provider
from graph.context_guard import (
    ContextOverflowError,
    get_context_window,
    is_context_overflow_error,
    preflight_check,
)
from graph.prompt_builder import build_system_prompt
from graph.session_manager import session_manager
from tools import get_all_tools


class AgentManager:
    """Manages the Agent lifecycle: initialization, streaming, invocation."""

    def __init__(self) -> None:
        self._base_dir: Path | None = None
        self._tools: list = []
        self._llm = None

    def initialize(self, base_dir: Path) -> None:
        """Initialize the configured model and tools."""
        self._base_dir = base_dir

        self._llm = create_chat_model(streaming=True)

        # 主智能体工具集：挂载 spawn_subagent（运行时派生子智能体），需在 llm 之后构造。
        # 子智能体的工具集由 spawn 工具内部用 include_spawn=False 构造，杜绝无限递归。
        self._tools = get_all_tools(base_dir, llm=self._llm, include_spawn=True)

        session_manager.initialize(base_dir)
        print(f"Agent initialized with {len(self._tools)} tools ({get_provider()}: {get_model_name()})")

    def _build_agent(self):
        """Build a fresh agent with current system prompt (re-reads files each time)."""
        assert self._base_dir is not None
        assert self._llm is not None

        rag_mode = get_rag_mode()
        system_prompt = build_system_prompt(self._base_dir, rag_mode=rag_mode)

        # 工具结果截取：用官方 ContextEditingMiddleware（对齐 Anthropic context editing）。
        # 它在 (a)wrap_model_call hook 里拿到 LangGraph 内部产生的 ToolMessage，累积 token
        # 超过 trigger 就把较旧的 tool result 清成 [cleared]（保留最近 keep=3 条）。
        # 注：trigger 单位是 middleware 内部 count_tokens_approximately 口径（4 chars/token），
        # 与本项目 preflight 的 CHARS_PER_TOKEN_ESTIMATE=2 不是同一把尺子（约差 2 倍）。
        # 取窗口一半只为便于演示——调小 MODEL_CONTEXT_WINDOW 即可让小对话触发。
        context_editing = ContextEditingMiddleware(
            edits=[ClearToolUsesEdit(trigger=int(get_context_window() * 0.5))],
        )

        agent = create_agent(
            model=self._llm,
            tools=self._tools,
            system_prompt=system_prompt,
            middleware=[context_editing],
        )
        return agent

    def _build_messages(self, user_message: str, history: list[dict[str, Any]]) -> list:
        """Convert session history + new message into LangChain messages.

        在转换后做一道 preflight 估算（OpenClaw transformContext 同款）：超过 90%
        模型上下文窗口就抛 ContextOverflowError，由 astream 的 overflow retry 捕获。
        """
        messages = []
        for msg in history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
        messages.append(HumanMessage(content=user_message))

        # Preflight：估算 token 超过 90% 窗口即抛错（OpenClaw transformContext 同款）。
        # 工具结果截取已移交 ContextEditingMiddleware（见 _build_agent）；历史消息里本就
        # 没有 ToolMessage，旧的注入前拦截点对真实工具输出无效，故移除。
        preflight_check(messages)
        return messages

    async def astream(
        self, message: str, history: list[dict[str, Any]], session_id: str | None = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream agent response with token-level and node-level events.

        Yields events:
          {"type": "retrieval", "query": "...", "results": [...]}  (RAG mode only)
          {"type": "token", "content": "..."}
          {"type": "tool_start", "tool": "...", "input": "..."}
          {"type": "tool_end", "tool": "...", "output": "..."}
          {"type": "compression", "archived": N, "remaining": M}  (overflow retry 时)
          {"type": "done", "content": "..."}

        Overflow retry：捕获 ContextOverflowError（preflight 或模型抛的 context
        overflow）→ 自动调一次 compress_session（基于已落盘历史）→ 重试 1 次。
        与 OpenClaw "API 报 overflow 时压缩重试" 策略对齐。

        session_id：可选；不传则 overflow 时不压缩、直接 yield error 让 chat.py
        处理（保持向后兼容）。传了就走"压缩-重试"闭环。
        """
        try:
            async for event in self._astream_inner(message, history):
                yield event
        except ContextOverflowError:
            if not session_id:
                # 无 session_id 无法压缩，向上抛给 chat.py 兜底
                raise
            # 有 session_id：调一次压缩，再带新 history 跑一次
            async for event in self._overflow_retry(message, session_id):
                yield event

    async def _astream_inner(
        self, message: str, history: list[dict[str, Any]]
    ) -> AsyncGenerator[dict[str, Any], None]:
        """单次 astream：RAG 召回 + 构造消息 + 事件流。"""
        # RAG retrieval: inject memory context if enabled
        rag_mode = get_rag_mode()
        rag_context = ""
        if rag_mode and self._base_dir:
            from graph.memory_indexer import get_memory_indexer

            retrieval_mode = get_retrieval_mode()
            indexer = get_memory_indexer(self._base_dir)
            results = await asyncio.to_thread(indexer.retrieve, message, mode=retrieval_mode)
            if results:
                yield {
                    "type": "retrieval",
                    "query": message,
                    "mode": retrieval_mode,
                    "results": results,
                }
                snippets = "\n\n".join(
                    f"[片段 {i+1}] (score: {r['score']})\n{r['text']}"
                    for i, r in enumerate(results)
                )
                rag_context = f"[记忆检索结果]\n{snippets}"

        agent = self._build_agent()

        # Build messages with optional RAG context appended to history
        augmented_history = list(history)
        if rag_context:
            augmented_history.append(
                {"role": "assistant", "content": rag_context}
            )
        messages = self._build_messages(message, augmented_history)

        full_response = ""
        tools_just_finished = False

        try:
            async for event in agent.astream(
                {"messages": messages},
                config={"recursion_limit": 24},
                stream_mode=["messages", "updates"],
            ):
                # event is a tuple of (stream_mode, data) when using multiple modes
                if isinstance(event, tuple):
                    mode, data = event
                else:
                    mode = "messages"
                    data = event

                # 模型的 context_overflow 错误信号（preflight 已守一道，模型层兜一道）
                # M-7 修：用 is_context_overflow_error 结构化判断替代脆弱的英文关键词匹配
                # 路径A：error 被塞进 LangGraph 事件流（data["error"] 多为 dict/str，
                # 走 is_context_overflow_error 的 fallback 关键词判定）。与下方 except
                # 的路径B（模型直接抛异常，走结构化判定）互补，两条都需保留。
                if isinstance(data, dict) and data.get("error"):
                    if is_context_overflow_error(data["error"]):
                        raise ContextOverflowError(f"Model reported context overflow: {data['error']}")

                if mode == "messages":
                    # Token-level streaming from LLM
                    msg, metadata = data
                    if hasattr(msg, "content") and msg.content:
                        if msg.type == "AIMessageChunk" or msg.type == "ai":
                            if msg.content and not getattr(msg, "tool_calls", None):
                                # If tools just finished, signal a new response segment
                                if tools_just_finished:
                                    yield {"type": "new_response"}
                                    tools_just_finished = False
                                full_response += msg.content
                                yield {"type": "token", "content": msg.content}

                elif mode == "updates":
                    if isinstance(data, dict):
                        for node_name, node_data in data.items():
                            if node_name == "tools" and "messages" in node_data:
                                for tool_msg in node_data["messages"]:
                                    if hasattr(tool_msg, "name"):
                                        yield {
                                            "type": "tool_end",
                                            "tool": tool_msg.name,
                                            "output": str(tool_msg.content)[:2000],
                                            "id": getattr(tool_msg, "tool_call_id", None),
                                        }
                                # After all tool results, mark that tools finished
                                tools_just_finished = True
                            elif node_name == "model" and "messages" in node_data:
                                for agent_msg in node_data["messages"]:
                                    if hasattr(agent_msg, "tool_calls") and agent_msg.tool_calls:
                                        for tc in agent_msg.tool_calls:
                                            yield {
                                                "type": "tool_start",
                                                "tool": tc["name"],
                                                "input": str(tc.get("args", ""))[:1000],
                                                "id": tc.get("id"),
                                            }
        except ContextOverflowError:
            raise
        except Exception as e:
            # M-7 修（路径B）：模型层多半是直接抛 openai.BadRequestError（status 400），
            # 而非把 error 塞进事件流（路径A 见上方 data["error"] 检测）。这里 e 是真实
            # 异常对象，is_context_overflow_error 走结构化判定；是溢出则转成
            # ContextOverflowError 交给 astream 的 overflow retry，否则原样抛出。
            if is_context_overflow_error(e):
                raise ContextOverflowError(f"Model reported context overflow: {e}")
            raise

        if not full_response.strip():
            raise RuntimeError(
                "模型未返回回答正文。若使用 R1，请检查 Ollama 日志或提高 OLLAMA_NUM_PREDICT；"
                "推理耗尽输出预算时可能只生成思考内容。"
            )
        yield {"type": "done", "content": full_response}

    async def _overflow_retry(
        self, message: str, session_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Overflow 重试：调一次 compress_session 压缩历史（基于已落盘消息）→ 拉新
        history → 跑 _astream_inner。仍失败就把异常向上抛。

        边界诚实标注：chat.py 在 done 后才落盘 user message，所以本轮 user message
        不在压缩历史里——压缩的是上一轮及之前的内容。这是教学骨架的简化，真实
        实现应把 user message 落盘后再调压缩（避免丢失当前轮上下文）。
        """
        from api.compress import _generate_summary

        yield {"type": "compression_start", "reason": "context_overflow"}
        messages_to_compress = session_manager.load_session(session_id)
        if len(messages_to_compress) >= 4:
            num_to_remove = max(4, len(messages_to_compress) // 2)
            try:
                summary = await _generate_summary(messages_to_compress[:num_to_remove])
                # M-5 修：compress_history 是同步函数（内含文件 IO + JSON 序列化），
                # 在 async generator 内直接 await 会阻塞 asyncio 事件循环。
                # 用 asyncio.to_thread 放到默认线程池跑，保持事件循环通畅。
                await asyncio.to_thread(
                    session_manager.compress_history,
                    session_id,
                    summary,
                    num_to_remove,
                )
                yield {
                    "type": "compression",
                    "archived": num_to_remove,
                    "remaining": len(messages_to_compress) - num_to_remove,
                }
            except Exception as e:
                yield {"type": "compression", "error": str(e)}

        # 拉新 history（已包含压缩后的 compressed_context）再跑
        new_history = session_manager.load_session_for_agent(session_id)
        async for event in self._astream_inner(message, new_history):
            yield event

    async def ainvoke(self, message: str, session_id: str) -> str:
        """Non-streaming invocation (fallback)."""
        history = session_manager.load_session_for_agent(session_id)
        agent = self._build_agent()
        messages = self._build_messages(message, history)
        result = await agent.ainvoke({"messages": messages}, config={"recursion_limit": 24})

        final_messages = result.get("messages", [])
        for msg in reversed(final_messages):
            if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                response = msg.content
                session_manager.save_message(session_id, "user", message)
                session_manager.save_message(session_id, "assistant", response)
                return response
        return "No response generated."


agent_manager = AgentManager()
