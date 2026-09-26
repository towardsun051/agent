"""Exercise the real API, agent loop and file storage with mocked model frames.

No Ollama or external service is called. Python tool execution remains real.
"""

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import chat, compress, sessions
from graph.agent import AgentManager
from graph.session_manager import SessionManager


class ChatIntegrationTests(unittest.TestCase):
    def test_tool_canvas_history_titles_and_compression(self):
        responses = iter([
            '{"action":"python_repl","arguments":{"code":"print(6 * 7)"}}',
            '{"action":"final","content":"结果42。<openclaw-canvas><h1>42</h1></openclaw-canvas>"}',
            "乘法计算",
            '{"action":"final","content":"刚才的结果是42。"}',
            "计算结果回顾",
            "用户调用 Python 计算 6×7，实际结果为42。",
        ])
        model_requests = []

        async def fake_stream(model, messages, stop=None, **kwargs):
            model_requests.append(messages)
            content = next(responses)
            for piece in [content[:7], content[7:]]:
                yield {"message": {"content": piece}, "done": False}
            yield {"message": {"content": ""}, "done": True, "done_reason": "stop"}

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            storage = SessionManager()
            manager = AgentManager()
            stack.enter_context(patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_TOOL_MODE": "json"}))
            for module in ["api.chat", "api.sessions", "api.compress", "graph.agent"]:
                stack.enter_context(patch(f"{module}.session_manager", storage))
            stack.enter_context(patch("api.chat.agent_manager", manager))
            stack.enter_context(patch("graph.agent.get_rag_mode", return_value=False))
            stack.enter_context(patch("langchain_ollama.ChatOllama._acreate_chat_stream", fake_stream))
            manager.initialize(root)
            app = FastAPI()
            for router in [chat.router, sessions.router, compress.router]:
                app.include_router(router, prefix="/api")

            with TestClient(app) as client:
                sid = client.post("/api/sessions").json()["id"]
                response = client.post("/api/chat", json={"session_id": sid, "message": "计算6×7并展示Canvas"})
                self.assertEqual(response.status_code, 200)
                events = []
                name = None
                for line in response.text.splitlines():
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        events.append((name, json.loads(line[5:].strip())))
                event_names = [name for name, _ in events]
                for name in ["tool_start", "tool_end", "token", "done", "canvas", "title"]:
                    self.assertIn(name, event_names)
                output = next(data for name, data in events if name == "tool_end")
                self.assertEqual(output["tool"], "python_repl")
                self.assertEqual(output["output"].strip(), "42")
                self.assertEqual(next(data for name, data in events if name == "canvas")["html"], "<h1>42</h1>")
                self.assertIn("真实执行结果", model_requests[1][-1].content)

                reply = client.post("/api/chat", json={"session_id": sid, "message": "刚才的结果？", "stream": False})
                self.assertEqual(reply.json()["reply"], "刚才的结果是42。")
                title = client.post(f"/api/sessions/{sid}/generate-title")
                self.assertEqual(title.json()["title"], "计算结果回顾")
                result = client.post(f"/api/sessions/{sid}/compress")
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["archived_count"], 4)
                saved = json.loads((root / "sessions" / f"{sid}.json").read_text(encoding="utf-8"))
                self.assertIn("42", saved["compressed_context"])
                self.assertEqual(len(list((root / "sessions" / "archive").glob("*.json"))), 1)
                self.assertEqual(client.delete(f"/api/sessions/{sid}").status_code, 200)
                self.assertFalse((root / "sessions" / f"{sid}.json").exists())

