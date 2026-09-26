"""BrowserUseTool — Use a browser to complete web tasks via browser-use library."""

import traceback
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from model_config import browser_model_settings


class BrowserUseInput(BaseModel):
    task: str = Field(description="浏览器任务描述，如'打开百度搜索Python教程'")


class BrowserUseTool(BaseTool):
    name: str = "browser_use"
    description: str = (
        "Use a browser to complete web tasks. Describe the task in natural language, "
        "e.g., 'Go to google.com and search for Python tutorial'. "
        "The browser will execute the steps and return the result. "
        "Use this for tasks that require browser interaction like filling forms, "
        "clicking buttons, or navigating multi-step web workflows."
    )
    args_schema: Type[BaseModel] = BrowserUseInput

    def _run(self, task: str) -> str:
        """Sync wrapper for async browser-use Agent."""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(asyncio.run, self._arun(task)).result()
                return result
            else:
                return asyncio.run(self._arun(task))
        except Exception as e:
            traceback.print_exc()
            return f"❌ Browser use error: {type(e).__name__}: {str(e)}"

    async def _arun(self, task: str) -> str:
        """Execute browser task using browser-use Agent.

        browser-use >= 0.3.3 ships its own LLM wrappers (not LangChain).
        Must import ChatOpenAI from browser_use, not from langchain_openai.
        DeepSeek is OpenAI-compatible, so we use browser_use.ChatOpenAI
        with a custom base_url.

        use_vision=False is set because DeepSeek is a text-only model and
        does not support image/screenshot inputs.
        """
        try:
            from browser_use import Agent as BrowserAgent
            # browser-use has its own ChatOpenAI that exposes the .provider
            # attribute it requires internally — LangChain classes don't work.
            from browser_use import ChatOpenAI as BrowserChatOpenAI

            llm = BrowserChatOpenAI(**browser_model_settings())

            agent = BrowserAgent(
                task=task,
                llm=llm,
                use_vision=False,  # DeepSeek doesn't support vision/screenshots
            )
            result = await agent.run()

            if result:
                return str(result)
            return "Browser task completed but returned no text result."

        except ImportError as e:
            traceback.print_exc()
            return (
                "❌ browser-use is not installed. "
                "Run: pip install browser-use && playwright install chromium"
                f"\nDetail: {e}"
            )
        except Exception as e:
            traceback.print_exc()
            return f"❌ Browser use error: {type(e).__name__}: {str(e)}"


def create_browser_use_tool() -> BrowserUseTool:
    return BrowserUseTool()
