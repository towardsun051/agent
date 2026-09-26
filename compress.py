"""POST /api/sessions/{session_id}/compress — Compress conversation history."""

import traceback
from typing import Any

from fastapi import APIRouter, HTTPException
from langchain_core.messages import HumanMessage
from model_config import create_chat_model

from graph.session_manager import session_manager

router = APIRouter()


async def _generate_summary(messages: list[dict[str, Any]]) -> str:
    """Use the configured model to generate a compressed summary."""
    llm = create_chat_model(temperature=0.3)

    # Format messages for summary
    formatted = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if content:
            formatted.append(f"{role}: {content[:500]}")

    conversation_text = "\n".join(formatted)

    prompt = (
        "请将以下对话历史压缩为简洁的中文摘要，保留关键信息、决策和结论。"
        "摘要不超过500字。只输出摘要内容，不要添加额外说明。\n\n"
        f"{conversation_text}"
    )

    result = await llm.ainvoke([HumanMessage(content=prompt)])
    summary = result.content.strip()
    if not summary:
        raise RuntimeError("模型未生成摘要正文，保留原会话；请检查输出预算或推理模式。")
    return summary


@router.post("/sessions/{session_id}/compress")
async def compress_session(session_id: str) -> dict[str, Any]:
    """Compress the first 50% of conversation history into a summary."""
    messages = session_manager.load_session(session_id)
    if len(messages) < 4:
        raise HTTPException(
            status_code=400,
            detail="Not enough messages to compress (need at least 4)",
        )

    # Take the first 50% of messages
    num_to_remove = max(4, len(messages) // 2)

    messages_to_compress = messages[:num_to_remove]

    try:
        summary = await _generate_summary(messages_to_compress)
        session_manager.compress_history(session_id, summary, num_to_remove)
        remaining = len(messages) - num_to_remove
        return {
            "archived_count": num_to_remove,
            "remaining_count": remaining,
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Compression failed: {str(e)}")
