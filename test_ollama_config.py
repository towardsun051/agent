"""Offline regression checks for local model routing and index isolation."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from model_config import create_chat_model, browser_model_settings, embedding_signature
from graph.memory_indexer import MemoryIndexer
from tools.skills_scanner import scan_skills
from api.compress import _generate_summary


LOCAL_ENV = {
    "LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": "http://localhost:11434",
    "OLLAMA_MODEL": "deepseek-r1:latest", "OLLAMA_REASONING": "true",
    "MODEL_CONTEXT_WINDOW": "16384", "EMBEDDING_PROVIDER": "ollama",
    "EMBEDDING_MODEL": "qwen3-embedding:latest",
    "OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "",
}


class OllamaConfigurationTests(unittest.TestCase):
    def test_model_timeout_produces_a_useful_sse_error(self):
        import json
        import httpx
        from api.chat import event_generator

        async def failed_stream(*args, **kwargs):
            raise httpx.ReadTimeout("")
            yield  # async generator interface

        async def collect():
            return [event async for event in event_generator("test", "timeout-test")]

        with patch("api.chat.session_manager.load_session_for_agent", return_value=[]), \
             patch("api.chat.agent_manager.astream", failed_stream), \
             patch("api.chat.traceback.print_exc"):
            events = asyncio.run(collect())
        self.assertEqual(events[0]["event"], "error")
        self.assertIn("OLLAMA_TIMEOUT", json.loads(events[0]["data"])["error"])

    def test_model_uses_native_ollama_without_cloud_keys(self):
        with patch.dict(os.environ, LOCAL_ENV):
            model = create_chat_model()
            self.assertEqual(model.model, "deepseek-r1:latest")
            self.assertEqual(model.base_url, "http://localhost:11434")
            self.assertEqual(model.num_ctx, 16384)
            self.assertTrue(model.reasoning)
            self.assertEqual(browser_model_settings()["base_url"], "http://localhost:11434/v1")

    def test_embedding_model_change_cannot_reuse_old_vectors(self):
        with patch.dict(os.environ, LOCAL_ENV):
            old = embedding_signature()
            with patch.dict(os.environ, {"EMBEDDING_MODEL": "another-embedding-model"}):
                self.assertNotEqual(old, embedding_signature())
                indexer = MemoryIndexer(Path("temporary-backend"))
                self.assertNotIn(old, str(indexer._storage_dir))

    def test_skill_location_resolves_from_backend_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "sample" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: sample\ndescription: example\n---\nTest", encoding="utf-8")
            self.assertIn("<location>skills/sample/SKILL.md</location>", scan_skills(root))

    def test_summary_uses_shared_factory_and_final_content(self):
        fake = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content="测试摘要")))
        with patch("api.compress.create_chat_model", return_value=fake) as factory:
            result = asyncio.run(_generate_summary([{"role": "user", "content": "测试"}]))
        self.assertEqual(result, "测试摘要")
        factory.assert_called_once_with(temperature=0.3)

    def test_memory_index_rebuild_and_hybrid_are_offline_with_mock_embeddings(self):
        from llama_index.core.embeddings import MockEmbedding
        from llama_index.core import Settings
        from utils.tokenization import local_tokenizer

        Settings.tokenizer = local_tokenizer
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, LOCAL_ENV):
            root = Path(directory)
            (root / "memory").mkdir()
            memory = root / "memory" / "MEMORY.md"
            memory.write_text("项目代号为蓝鲸。用户喜欢简洁中文回答。", encoding="utf-8")
            with patch("graph.memory_indexer.create_embedding_model", return_value=MockEmbedding(embed_dim=8)):
                indexer = MemoryIndexer(root)
                results = indexer.retrieve("蓝鲸", mode="hybrid")
                self.assertTrue(results, "Hybrid retrieval must not silently return empty on API incompatibility")
                self.assertEqual(results[0]["source"], "MEMORY.md")
                reloaded = MemoryIndexer(root)
                self.assertTrue(reloaded.retrieve("蓝鲸"))
                memory.write_text("", encoding="utf-8")
                self.assertEqual(indexer.retrieve("蓝鲸"), [])

    def test_empty_summary_cannot_archive_history(self):
        fake = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content="")))
        with patch("api.compress.create_chat_model", return_value=fake):
            with self.assertRaises(RuntimeError):
                asyncio.run(_generate_summary([{"role": "user", "content": "keep this"}]))

    def test_reasoning_only_stream_is_an_error_not_a_successful_blank_answer(self):
        from langchain_core.messages import AIMessageChunk
        from graph.agent import AgentManager

        class FakeAgent:
            async def astream(self, *args, **kwargs):
                yield "messages", (AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking"}), {})

        async def collect(manager):
            return [event async for event in manager._astream_inner("hello", [])]

        manager = AgentManager()
        with patch("graph.agent.get_rag_mode", return_value=False), patch.object(manager, "_build_agent", return_value=FakeAgent()):
            with self.assertRaises(RuntimeError):
                asyncio.run(collect(manager))


if __name__ == "__main__":
    unittest.main()
