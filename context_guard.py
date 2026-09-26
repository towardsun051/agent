"""Context guard — preflight token estimation + overflow retry.

对齐 OpenClaw 的廉价防御策略（preemptive compaction 子集）：
  ① Tool-result truncation：已移交 LangChain 官方 ContextEditingMiddleware
     （见 agent.py:_build_agent），在 agent 执行循环内拿到真实 ToolMessage 后
     按 token 阈值清除旧 tool result。本模块不再实现此层。
  ② Preflight token estimation：每轮 LLM 调用前用 len(text)/2 粗估（OpenClaw 系数
     2 chars/token；tool output 偏密故用 2 而非通用 4）。超过模型窗口的 90% 抛
     ContextOverflowError（OpenClaw transformContext 同款）。
  ③ Overflow retry：捕获 ContextOverflowError → 自动调 compress_session 压缩历史 →
     再试 1 次（OpenClaw 在 overflow 时也走"压缩后重试"策略）。

教学注释：本模块有意保持最小骨架——未实现的更高级策略（按 tool-use 边界切分、
分块摘要合并、preemptive 阈值自动压缩）留给课后扩展，避免一次塞进 5 层让学生过载。
"""
from __future__ import annotations

from typing import Any, List

from langchain_core.messages import BaseMessage
from model_config import get_context_window

# 模型上下文窗口（DeepSeek-V3.2 deepseek-chat = 128K）。可从 env 覆盖。
DEFAULT_CONTEXT_WINDOW = 128_000

# 预校验阈值：超过 90% 上下文窗口即拒绝继续（OpenClaw transformContext 同款）。
PREFLIGHT_SAFETY_RATIO = 0.9

# 粗估系数：tool output / code / JSON 偏密，OpenClaw 工具结果守卫用 2 chars/token。
CHARS_PER_TOKEN_ESTIMATE = 2


def _content_chars(content) -> int:
    """从消息 content 字段提取可估算 token 的纯文本字符数。

    处理三种形态：
    - str：直接 len
    - list：多模态/结构化 content（[{"type":"text","text":"hi"}, ...]），
      只统计 dict["text"] 字段，忽略 image_url 等二进制描述。
      修 M-1：str(list) 含 Python 表示法字符会高估。
      注：tool_calls（list[{"name":...,"args":...}]）也走这里——无 "text" 键
      时会落到 str(part) 分支一并估上（避免漏算 tool_calls）。
    - 其他（dict / None）：str() 化后粗估（保守但可用）。
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if "text" in part:
                    # 多模态 text 块
                    total += len(str(part["text"]))
                else:
                    # tool_calls 等其他 dict（如 {"name":..., "args":...}）
                    total += len(str(part))
            elif isinstance(part, str):
                total += len(part)
        return total
    return len(str(content)) if content is not None else 0


def estimate_tokens(messages: List[BaseMessage] | list) -> int:
    """粗估消息列表的 token 数（OpenClaw 同款 chars/2 系数）。

    教学简化：用 len(content)/2 估算（不调 tokenizer），与 OpenClaw 的
    fast character heuristic 一致。生产应换 tiktoken/cl100k_base。

    修 M-1 + M-2：
    - content 用 _content_chars 处理多模态 list 形态
    - AIMessage.tool_calls 也计入估算（之前系统性低估，tool_calls JSON 占大量 token）
    """
    total = 0
    for m in messages:
        content = getattr(m, "content", "") or (m.get("content", "") if isinstance(m, dict) else "")
        total += _content_chars(content) // CHARS_PER_TOKEN_ESTIMATE

        # M-2 修：AIMessage 的 tool_calls 字段（list[dict]）也占 token，必须纳入估算
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            total += _content_chars(tool_calls) // CHARS_PER_TOKEN_ESTIMATE
    return total


def preflight_check(messages: List[BaseMessage], context_window: int | None = None) -> int:
    """Preflight token 估算。超过 90% 窗口即抛 ContextOverflowError。

    返回估算的 token 数（即使不抛错也返回，方便调用方记录指标）。
    """
    window = context_window or get_context_window()
    estimated = estimate_tokens(messages)
    threshold = int(window * PREFLIGHT_SAFETY_RATIO)
    if estimated > threshold:
        raise ContextOverflowError(
            f"Preflight: estimated {estimated} tokens exceeds "
            f"{int(PREFLIGHT_SAFETY_RATIO*100)}% of context window ({window})"
        )
    return estimated


def is_context_overflow_error(error) -> bool:
    """判断一个异常/错误对象是否为上下文溢出（模型层兜底用）。

    用鸭子类型探测，不 import openai（避免硬依赖具体包路径）：
    - 优先结构化：status_code/status == 400 且 message 指向 context length，
      或 code 命中 context_length_exceeded 类标识。
    - fallback：英文关键词匹配（措辞变了仍能粗略命中，作为最后一道）。

    修 M-7：之前 agent.py 仅靠英文关键词匹配，DeepSeek 改报错措辞即漏判。
    """
    # 澄清（C-A2）：error 可能是真实异常对象（status_code/code/message 属性齐全），
    # 也可能是事件流里的 dict/str（getattr 全部落空，message 退化为 str(error) 整体）。
    # 故结构化判定（status/code 分支）只对真实异常对象生效；dict/str 输入只能由
    # 下方 fallback 关键词兜底——agent.py 第一处调用(data["error"])即属后者。
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    code = getattr(error, "code", None)
    message = str(getattr(error, "message", "") or error).lower()

    # 结构化优先：400 + context 且伴随长度信号。
    # 修 C-A3：必须 "context" AND 长度词（与 fallback 同口径），否则含 "context"
    # 字样的普通 400 参数/格式错误会被误判为溢出，触发无意义压缩重试并掩盖真实错误。
    if status == 400 and "context" in message and (
        "too long" in message or "length" in message or "overflow" in message
    ):
        return True
    # 结构化优先：错误码命中
    if code in ("context_length_exceeded", "string_above_max_length"):
        return True
    # fallback：英文关键词（兜底；也是 dict/str 输入的唯一判定路径）
    return "context" in message and (
        "overflow" in message or "too long" in message or "length" in message
    )


class ContextOverflowError(Exception):
    """自定义溢出异常。OpenClaw 抛 'prompt is too long'，我们用独立类型便于重试识别。"""

    pass
