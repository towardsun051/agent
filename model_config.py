"""Shared model configuration for chat, summaries, titles, tools and embeddings."""

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# Resolve .env from the backend directory, even when launched from another cwd.
load_dotenv(BASE_DIR / ".env")


def get_provider() -> str:
    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    if provider not in {"ollama", "deepseek"}:
        raise ValueError("LLM_PROVIDER must be ollama or deepseek")
    return provider


def get_ollama_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


def get_model_name() -> str:
    if get_provider() == "ollama":
        return os.getenv("OLLAMA_MODEL", "deepseek-r1:latest")
    return os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def get_context_window() -> int:
    default = 16384 if get_provider() == "ollama" else 128000
    value = int(os.getenv("MODEL_CONTEXT_WINDOW", str(default)))
    if value < 1024:
        raise ValueError("MODEL_CONTEXT_WINDOW must be at least 1024")
    return value


def create_chat_model(temperature: float = 0.6, *, streaming: bool = False):
    """Use native Ollama so num_ctx and reasoning are explicitly controlled."""
    if get_provider() == "ollama":
        from langchain_ollama import ChatOllama

        model_class = ChatOllama
        if streaming:
            mode = os.getenv("OLLAMA_TOOL_MODE", "json").lower()
            if mode == "json":
                from graph.ollama_tools import OllamaJsonToolChat
                model_class = OllamaJsonToolChat
            elif mode != "native":
                raise ValueError("OLLAMA_TOOL_MODE must be json or native")
        return model_class(
            model=get_model_name(),
            base_url=get_ollama_url(),
            temperature=temperature,
            num_ctx=get_context_window(),
            num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "4096")),
            # Separate reasoning_content from final text (titles/HTML stay clean).
            reasoning=os.getenv("OLLAMA_REASONING", "true").lower() == "true",
            client_kwargs={"timeout": float(os.getenv("OLLAMA_TIMEOUT", "600"))},
        )

    from langchain_deepseek import ChatDeepSeek

    return ChatDeepSeek(
        model=get_model_name(),
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        api_base=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        temperature=temperature,
        streaming=streaming,
    )


def get_embedding_provider() -> str:
    provider = os.getenv("EMBEDDING_PROVIDER", "ollama").strip().lower()
    if provider not in {"ollama", "openai"}:
        raise ValueError("EMBEDDING_PROVIDER must be ollama or openai")
    return provider


def get_embedding_model_name() -> str:
    default = "qwen3-embedding:latest" if get_embedding_provider() == "ollama" else "text-embedding-3-small"
    return os.getenv("EMBEDDING_MODEL", default)


def embedding_signature() -> str:
    """Keep indexes from different providers/models/endpoints separate."""
    url = get_ollama_url() if get_embedding_provider() == "ollama" else os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    identity = f"{get_embedding_provider()}|{url}|{get_embedding_model_name()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def create_embedding_model():
    from llama_index.core import Settings
    from utils.tokenization import local_tokenizer

    # LlamaIndex otherwise lazily downloads the OpenAI tokenizer vocabulary.
    Settings.tokenizer = local_tokenizer
    if get_embedding_provider() == "ollama":
        from llama_index.embeddings.ollama import OllamaEmbedding

        return OllamaEmbedding(
            model_name=get_embedding_model_name(),
            base_url=get_ollama_url(),
            client_kwargs={"timeout": float(os.getenv("OLLAMA_TIMEOUT", "600"))},
        )

    from llama_index.embeddings.openai import OpenAIEmbedding

    return OpenAIEmbedding(
        model=get_embedding_model_name(),
        api_key=os.getenv("OPENAI_API_KEY"),
        api_base=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )


def browser_model_settings() -> dict:
    """browser-use has its own model wrapper; use Ollama's OpenAI endpoint."""
    if get_provider() == "ollama":
        return {"model": get_model_name(), "base_url": f"{get_ollama_url()}/v1", "api_key": "ollama"}
    return {
        "model": get_model_name(),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "api_key": os.getenv("DEEPSEEK_API_KEY"),
    }
