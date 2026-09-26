"""Global configuration management — JSON-based persistence."""

import json
from pathlib import Path
from typing import Any

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "rag_mode": False,
    # 记忆检索策略：'vector' = 纯向量语义检索；'hybrid' = BM25 + 向量 RRF 融合。
    # 仅在 rag_mode=True 时生效（rag_mode=False 时 MEMORY.md 整文注入 prompt，不检索）。
    "retrieval_mode": "vector",
}


def load_config() -> dict[str, Any]:
    """Load configuration from disk, returning defaults if missing."""
    if not CONFIG_FILE.exists():
        return dict(_DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return {**_DEFAULT_CONFIG, **data}
    except Exception:
        return dict(_DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    """Persist configuration to disk."""
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_rag_mode() -> bool:
    """Get current RAG mode setting."""
    return bool(load_config().get("rag_mode", False))


def set_rag_mode(enabled: bool) -> None:
    """Set RAG mode on/off.

    关闭 RAG 时同步重置 retrieval_mode='vector'：前端 setMemoryMode('off') 路径只
    调 apiSetRagMode，不下发 retrieval_mode；后端兜底保证下次开启 RAG 时检索策略
    必然回到默认 vector（避免上次 hybrid 残留导致 UI 与后端实际行为不一致）。
    """
    config = load_config()
    config["rag_mode"] = enabled
    if not enabled:
        config["retrieval_mode"] = "vector"
    save_config(config)


def get_retrieval_mode() -> str:
    """Get current memory retrieval strategy: 'vector' or 'hybrid'."""
    mode = str(load_config().get("retrieval_mode", "vector"))
    return mode if mode in ("vector", "hybrid") else "vector"


def set_retrieval_mode(mode: str) -> None:
    """Set memory retrieval strategy ('vector' | 'hybrid')."""
    if mode not in ("vector", "hybrid"):
        raise ValueError(f"Invalid retrieval_mode: {mode}")
    config = load_config()
    config["retrieval_mode"] = mode
    save_config(config)
