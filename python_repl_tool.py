"""Python REPL Tool — wraps LangChain experimental PythonREPLTool with UTF-8 default."""

import io
import sys
import threading
from typing import Type

# 串行化 stdout 重定向临界区，防止并行子 agent 并发执行时输出互相污染
_REPL_LOCK = threading.Lock()

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class PythonReplInput(BaseModel):
    code: str = Field(description="Python code to execute")


class Utf8PythonReplTool(BaseTool):
    """Python REPL that forces UTF-8 for all I/O operations.

    注意（教学版安全边界）：本工具用 exec() 直接执行代码，未做沙箱隔离，
    可访问完整 __builtins__（能 import os / 联网 / 读写任意文件）。仅适用于可信教学环境，
    切勿在生产或面向不可信输入的场景下暴露此工具。
    """

    name: str = "python_repl"
    description: str = (
        "Execute Python code in an interactive REPL environment. "
        "Use this for calculations, data processing, running scripts, "
        "and any task that benefits from programmatic execution. "
        "Input should be valid Python code. Use print() to see output. "
        "IMPORTANT: When writing files, always specify encoding='utf-8' in open()."
    )
    args_schema: Type[BaseModel] = PythonReplInput

    def _run(self, code: str) -> str:
        with _REPL_LOCK:
            # 串行化此临界区：PythonREPL 用全局 sys.stdout 重定向，并发不安全
            # Capture stdout
            old_stdout = sys.stdout
            sys.stdout = buf = io.StringIO()
            try:
                exec(code, {"__builtins__": __builtins__})
                output = buf.getvalue()
            except Exception as e:
                output = f"Error: {type(e).__name__}: {e}"
            finally:
                sys.stdout = old_stdout

        if not output.strip():
            output = "(code executed with no output)"
        if len(output) > 5000:
            output = output[:5000] + "\n...[truncated]"
        return output


def create_python_repl_tool() -> BaseTool:
    return Utf8PythonReplTool()
