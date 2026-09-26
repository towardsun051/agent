"""SafeTerminalTool — 在项目目录内执行 shell 命令，附带危险命令黑名单。

注意（教学版安全边界）：_is_safe 用子串匹配拦截 12 条高危命令，属"黑名单"机制，
并不完备——`rm -rf ~` / `rm -rf ../..` / `curl ... | sh` 等不在表内即可绕过，
且 shell=True 直接执行。仅限可信教学环境，生产需改用白名单或真正的沙箱隔离。
"""

import subprocess
import sys
from pathlib import Path
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


BLACKLISTED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "chmod -R 777 /",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "format c:",
    "del /f /s /q c:",
]


class TerminalInput(BaseModel):
    command: str = Field(description="The shell command to execute")


class SafeTerminalTool(BaseTool):
    name: str = "terminal"
    description: str = (
        "Execute shell commands within the project directory. "
        "The working directory is restricted to the project root. "
        "Use this for file operations, installing packages, running scripts, etc. "
        "On Windows, commands run in cmd.exe. Use chcp 65001 for UTF-8 output if needed."
    )
    args_schema: Type[BaseModel] = TerminalInput
    root_dir: str = ""

    def _is_safe(self, command: str) -> bool:
        cmd_lower = command.lower().strip()
        for blocked in BLACKLISTED_COMMANDS:
            if blocked in cmd_lower:
                return False
        return True

    def _run(self, command: str) -> str:
        if not self._is_safe(command):
            return f"❌ Command blocked for safety: {command}"
        try:
            # On Windows, cmd.exe outputs in the system codepage (GBK/cp936).
            # We capture raw bytes and decode with a fallback chain.
            is_win = sys.platform == "win32"

            if is_win:
                # Prepend chcp 65001 to force UTF-8 output from cmd.exe
                wrapped = f"chcp 65001 >nul 2>&1 && {command}"
                result = subprocess.run(
                    wrapped,
                    shell=True,
                    cwd=self.root_dir,
                    capture_output=True,
                    timeout=30,
                )
                # Decode: try UTF-8 first → GBK fallback → replace
                stdout = self._decode(result.stdout)
                stderr = self._decode(result.stderr)
            else:
                result = subprocess.run(
                    command,
                    shell=True,
                    cwd=self.root_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    encoding="utf-8",
                    errors="replace",
                )
                stdout = result.stdout
                stderr = result.stderr

            output = stdout
            if stderr:
                output += f"\n[stderr]: {stderr}"
            if not output.strip():
                output = "(command completed with no output)"
            # Truncate very long output
            if len(output) > 5000:
                output = output[:5000] + "\n...[truncated]"
            return output
        except subprocess.TimeoutExpired:
            return "❌ Command timed out (30s limit)"
        except Exception as e:
            return f"❌ Error: {str(e)}"

    @staticmethod
    def _decode(raw: bytes) -> str:
        """Decode bytes with UTF-8 → GBK → latin-1 fallback chain."""
        if not raw:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            pass
        return raw.decode("latin-1")


def create_terminal_tool(base_dir: Path) -> SafeTerminalTool:
    return SafeTerminalTool(root_dir=str(base_dir))
