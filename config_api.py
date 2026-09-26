"""GET/PUT /api/config/* — RAG mode toggle + API Key management."""

import os
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import (
    get_rag_mode,
    set_rag_mode,
    get_retrieval_mode,
    set_retrieval_mode,
)

router = APIRouter()

# ── RAG mode ──────────────────────────────────────────

class RagModeRequest(BaseModel):
    enabled: bool


@router.get("/config/rag-mode")
async def get_rag_mode_endpoint():
    return {"rag_mode": get_rag_mode()}


@router.put("/config/rag-mode")
async def set_rag_mode_endpoint(request: RagModeRequest):
    set_rag_mode(request.enabled)
    return {"rag_mode": request.enabled}


# ── Retrieval mode（记忆检索策略：vector / hybrid）────────

class RetrievalModeRequest(BaseModel):
    mode: str  # 'vector' | 'hybrid'


@router.get("/config/retrieval-mode")
async def get_retrieval_mode_endpoint():
    return {"retrieval_mode": get_retrieval_mode()}


@router.put("/config/retrieval-mode")
async def set_retrieval_mode_endpoint(request: RetrievalModeRequest):
    set_retrieval_mode(request.mode)
    return {"retrieval_mode": request.mode}


# ── API Key management ────────────────────────────────

MODEL_DEFAULTS = {
    "LLM_PROVIDER": "ollama",
    "OLLAMA_BASE_URL": "http://localhost:11434",
    "OLLAMA_MODEL": "deepseek-r1:latest",
    "OLLAMA_TOOL_MODE": "json",
    "OLLAMA_REASONING": "true",
    "EMBEDDING_PROVIDER": "ollama",
    "EMBEDDING_MODEL": "qwen3-embedding:latest",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "DEEPSEEK_MODEL": "deepseek-chat",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
}
MANAGED_KEYS = [*MODEL_DEFAULTS, "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY"]
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _mask_value(key_name: str, value: str) -> str:
    """Mask API key values, leave URLs unmasked."""
    if not value:
        return ""
    if not key_name.endswith("_KEY"):
        return value
    if len(value) <= 8:
        return "****"
    return value[:3] + "****" + value[-4:]


@router.get("/config/api-keys")
async def get_api_keys():
    """Return masked API keys from environment."""
    keys: Dict[str, str] = {}
    for key_name in MANAGED_KEYS:
        val = os.getenv(key_name, MODEL_DEFAULTS.get(key_name, ""))
        keys[key_name] = _mask_value(key_name, val)
    return keys


class ApiKeysRequest(BaseModel):
    keys: Dict[str, str]


@router.put("/config/api-keys")
async def set_api_keys(request: ApiKeysRequest):
    """Update .env file with new keys (only non-empty values)."""
    for key_name, value in request.keys.items():
        if key_name not in MANAGED_KEYS or not value or "****" in value:
            continue
        if any(char in value for char in ("\n", "\r", '"', "'", "#")):
            raise HTTPException(400, "Configuration values must be single-line values without quotes or #")
        if key_name == "LLM_PROVIDER" and value not in {"ollama", "deepseek"}:
            raise HTTPException(400, "LLM_PROVIDER must be ollama or deepseek")
        if key_name == "EMBEDDING_PROVIDER" and value not in {"ollama", "openai"}:
            raise HTTPException(400, "EMBEDDING_PROVIDER must be ollama or openai")
        if key_name == "OLLAMA_TOOL_MODE" and value not in {"json", "native"}:
            raise HTTPException(400, "OLLAMA_TOOL_MODE must be json or native")
        if key_name == "OLLAMA_REASONING" and value not in {"true", "false"}:
            raise HTTPException(400, "OLLAMA_REASONING must be true or false")
        if key_name.endswith("_URL"):
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise HTTPException(400, "Base URL must be a valid http(s) URL")
            if key_name == "OLLAMA_BASE_URL" and parsed.path.rstrip("/"):
                raise HTTPException(400, "OLLAMA_BASE_URL must not include /v1 or /api")

    # Read existing .env content
    existing_lines: list[str] = []
    if ENV_PATH.exists():
        existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    # Build a map of key -> line index for existing entries
    key_line_map: Dict[str, int] = {}
    for i, line in enumerate(existing_lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in MANAGED_KEYS:
                key_line_map[k] = i

    # Update or append
    for key_name, new_value in request.keys.items():
        if key_name not in MANAGED_KEYS:
            continue
        if not new_value or "****" in new_value:
            # Skip empty or masked (unchanged) values
            continue

        env_line = f"{key_name}={new_value}"
        if key_name in key_line_map:
            existing_lines[key_line_map[key_name]] = env_line
        else:
            existing_lines.append(env_line)

        # Also update current process env
        os.environ[key_name] = new_value

    # Write back
    ENV_PATH.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")

    # Existing requests keep their model; subsequent requests use the new config.
    from graph.agent import agent_manager
    agent_manager.initialize(ENV_PATH.parent)

    # Return masked keys
    result: Dict[str, str] = {}
    for key_name in MANAGED_KEYS:
        val = os.getenv(key_name, MODEL_DEFAULTS.get(key_name, ""))
        result[key_name] = _mask_value(key_name, val)
    return result
