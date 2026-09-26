"""Fufan-OpenClaw Backend — FastAPI Entry Point"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from model_config import get_model_name, get_provider

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize services; build embeddings lazily on the first RAG request."""
    from tools.skills_scanner import scan_skills
    from graph.agent import agent_manager
    from graph.memory_indexer import get_memory_indexer

    scan_skills(BASE_DIR)
    agent_manager.initialize(BASE_DIR)

    # Initialize memory indexer for RAG mode
    get_memory_indexer(BASE_DIR)

    print("fufan OpenClaw backend ready")
    yield


app = FastAPI(title="fufan OpenClaw", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from api.chat import router as chat_router
from api.files import router as files_router
from api.sessions import router as sessions_router
from api.tokens import router as tokens_router
from api.compress import router as compress_router
from api.config_api import router as config_router

app.include_router(chat_router, prefix="/api")
app.include_router(files_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(tokens_router, prefix="/api")
app.include_router(compress_router, prefix="/api")
app.include_router(config_router, prefix="/api")


@app.get("/")
async def root():
    return {"name": "fufan OpenClaw", "version": "0.1.0", "status": "running",
            "provider": get_provider(), "model": get_model_name()}
