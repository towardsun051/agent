"""Opt-in live integration check. Creates and deletes only its own test session.

Run after starting the server: .venv/Scripts/python.exe tests/smoke_live.py
This invokes the configured local model and RAG embedding model.
"""

import json
import time
from pathlib import Path

import requests

API = "http://localhost:8002/api"
RESULT_FILE = Path(__file__).resolve().parents[2] / ".run" / "smoke_results.json"


def save_results(results: dict) -> None:
    """Checkpoint completed stages so interruption cannot erase useful evidence."""
    RESULT_FILE.parent.mkdir(exist_ok=True)
    RESULT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def chat(session_id: str, message: str) -> list[dict]:
    events = []
    event_name = "message"
    first_token = True
    started = time.monotonic()
    with requests.post(f"{API}/chat", json={"message": message, "session_id": session_id},
                       stream=True, timeout=(10, 900)) as response:
        response.raise_for_status()
        response.encoding = "utf-8"
        for line in response.iter_lines(decode_unicode=True):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
                events.append({"event": event_name, "data": data})
                if event_name == "token" and first_token:
                    print(f"First answer token after {time.monotonic() - started:.1f}s", flush=True)
                    first_token = False
                elif event_name in {"retrieval", "tool_start", "tool_end", "done", "title"}:
                    print(f"Event: {event_name} (+{time.monotonic() - started:.1f}s)", flush=True)
                if event_name == "error":
                    raise RuntimeError(data.get("error"))
            elif not line:
                event_name = "message"
    assert any(e["event"] == "done" for e in events), "No done event"
    print(f"Chat complete in {time.monotonic() - started:.1f}s; event types: {sorted(set(e['event'] for e in events))}", flush=True)
    return events


def main():
    results = {}
    response = requests.post(f"{API}/sessions", timeout=10)
    response.raise_for_status()
    session_id = response.json()["id"]
    try:
        print("Testing SSE chat, RAG and automatic title...", flush=True)
        events = chat(session_id, "这是本地部署连通性测试。不要调用工具，只回复：Ollama连接成功。")
        results["streaming_chat"] = any(e["event"] == "token" and e["data"].get("content", "").strip() for e in events)
        results["automatic_title"] = any(e["event"] == "title" for e in events)
        rag = requests.get(f"{API}/config/rag-mode", timeout=10).json()["rag_mode"]
        results["rag_retrieval"] = any(e["event"] == "retrieval" for e in events) if rag else "disabled"
        save_results(results)

        print("Testing real python_repl tool invocation...", flush=True)
        events = chat(session_id, "请务必调用 python_repl 工具执行 print(6 * 7)，再用一句中文报告结果。")
        results["tool_call"] = any(e["event"] == "tool_end" and e["data"]["tool"] == "python_repl" and "42" in e["data"].get("output", "") for e in events)
        save_results(results)

        print("Testing manual title and compression...", flush=True)
        title = requests.post(f"{API}/sessions/{session_id}/generate-title", timeout=900)
        title.raise_for_status()
        results["manual_title"] = bool(title.json()["title"])
        save_results(results)
        compressed = requests.post(f"{API}/sessions/{session_id}/compress", timeout=900)
        compressed.raise_for_status()
        results["compression"] = compressed.json()["archived_count"] >= 4
        save_results(results)

        print("Testing non-streaming chat...", flush=True)
        response = requests.post(f"{API}/chat", json={"session_id": session_id, "message": "刚才的乘法结果是多少？不要调用工具，简短回答。", "stream": False}, timeout=900)
        response.raise_for_status()
        results["non_streaming_chat"] = "42" in response.json()["reply"]
        save_results(results)

        print("Testing Canvas output...", flush=True)
        events = chat(session_id, "不要调用任何工具。只输出下面这个完整内容，不加说明：<openclaw-canvas><h1>Ollama OK</h1></openclaw-canvas>")
        results["canvas"] = any(e["event"] == "canvas" and "Ollama OK" in e["data"]["html"] for e in events)
        save_results(results)
        assert all(value is True or value == "disabled" for value in results.values()), results
    except BaseException as exc:
        results["error"] = str(exc) or type(exc).__name__
        save_results(results)
        raise
    finally:
        requests.delete(f"{API}/sessions/{session_id}", timeout=10).raise_for_status()
        # Remove only archives created for this new test session, never user history.
        archive_dir = Path(__file__).resolve().parents[1] / "sessions" / "archive"
        for archive in archive_dir.glob(f"{session_id}_*.json"):
            archive.unlink()
        save_results(results)
        print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
