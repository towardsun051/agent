"""子智能体管理 API + 工具池查询。"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from graph.subagents import (
    create_subagent,
    delete_subagent,
    load_subagents,
    update_subagent,
)
from tools.registry import list_available_tools

router = APIRouter(prefix="/api", tags=["subagents"])

# 必须与 agent 初始化用的 base_dir 一致（main.py 用 backend 目录）
BASE_DIR = Path(__file__).parent.parent


class SubagentPayload(BaseModel):
    id: str | None = None
    name: str
    system_prompt: str
    tool_ids: list[str] = []
    model: str | None = None


@router.get("/subagents")
async def list_all():
    return load_subagents()


@router.post("/subagents")
async def create(payload: SubagentPayload):
    return create_subagent(payload.model_dump())


@router.put("/subagents/{agent_id}")
async def update(agent_id: str, payload: SubagentPayload):
    result = update_subagent(agent_id, payload.model_dump())
    if not result:
        raise HTTPException(404, "Subagent not found")
    return result


@router.delete("/subagents/{agent_id}")
async def delete(agent_id: str):
    if not delete_subagent(agent_id):
        raise HTTPException(404, "Subagent not found")
    return {"status": "ok"}


@router.get("/tools/available")
async def available_tools():
    """工具池：只返回允许暴露给子智能体的工具。"""
    return [t for t in list_available_tools(BASE_DIR) if t["subagent_allowed"]]
