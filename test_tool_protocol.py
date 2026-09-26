import unittest
import asyncio
from unittest.mock import patch

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from graph.ollama_tools import OllamaJsonToolChat, action_schema, decode_action, prepare_request

TOOLS = [{"type": "function", "function": {
    "name": "python_repl", "description": "Run Python",
    "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
}}]


class ToolProtocolTests(unittest.TestCase):
    def test_agent_executes_tool_and_feeds_result_back(self):
        from langchain.agents import create_agent
        from langchain_core.tools import tool

        executed = []
        requests = []

        @tool
        def calculate(code: str) -> str:
            """Calculate a test expression."""
            executed.append(code)
            return "42"

        async def fake_stream(model, messages, stop=None, **kwargs):
            requests.append(messages)
            self.assertNotIn("tools", kwargs)
            self.assertIn("oneOf", kwargs["format"])
            self.assertIn('"parameters"', messages[0].content)
            content = ('{"action":"calculate","arguments":{"code":"print(6 * 7)"}}'
                       if len(requests) == 1 else '{"action":"final","content":"结果是42"}')
            # Splitting JSON exercises buffering across provider stream frames.
            for piece in [content[:13], content[13:]]:
                yield {"message": {"content": piece}, "done": False}
            yield {"message": {"content": ""}, "done": True, "done_reason": "stop"}

        async def run():
            model = OllamaJsonToolChat(model="deepseek-r1:latest")
            agent = create_agent(model=model, tools=[calculate])
            return await agent.ainvoke({"messages": [("user", "Calculate 6*7 using the tool")]})

        with patch("langchain_ollama.ChatOllama._acreate_chat_stream", fake_stream):
            result = asyncio.run(run())
        self.assertEqual(executed, ["print(6 * 7)"])
        self.assertIn("42", requests[1][-1].content)
        self.assertEqual(result["messages"][-1].content, "结果是42")

    def test_valid_tool_action_is_an_actual_langchain_tool_call(self):
        msg = decode_action('{"action":"python_repl","arguments":{"code":"print(6 * 7)"}}', action_schema(TOOLS))
        self.assertEqual(msg.tool_calls[0]["name"], "python_repl")
        self.assertEqual(msg.tool_calls[0]["args"], {"code": "print(6 * 7)"})
        self.assertEqual(msg.content, "")

    def test_prose_unknown_tools_and_wrong_arguments_are_not_executed(self):
        for content in ['```python_repl\nprint(42)\n```', '{"action":"shell","arguments":{}}', '{"action":"python_repl","arguments":{"code":42}}']:
            with self.subTest(content=content), self.assertRaises(ValueError):
                decode_action(content, action_schema(TOOLS))

    def test_final_answer_preserves_canvas_and_creates_no_tools(self):
        msg = decode_action('{"action":"final","content":"<openclaw-canvas><h1>OK</h1></openclaw-canvas>"}', action_schema(TOOLS))
        self.assertTrue(msg.content.startswith("<openclaw-canvas>"))
        self.assertFalse(msg.tool_calls)

    def test_exact_json_fence_is_validated_but_surrounding_prose_is_rejected(self):
        payload = '```json\n{"action":"final","content":"OK"}\n```'
        self.assertEqual(decode_action(payload, action_schema(TOOLS)).content, "OK")
        with self.assertRaises(ValueError):
            decode_action("Example: " + payload, action_schema(TOOLS))

    def test_tool_result_is_included_in_next_model_request(self):
        messages, _ = prepare_request([
            SystemMessage(content="test"),
            AIMessage(content="", tool_calls=[{"name": "python_repl", "args": {"code": "print(42)"}, "id": "test"}]),
            ToolMessage(content="42", name="python_repl", tool_call_id="test"),
        ], TOOLS)
        self.assertIn('"action": "python_repl"', messages[1].content)
        self.assertIn("42", messages[2].content)
