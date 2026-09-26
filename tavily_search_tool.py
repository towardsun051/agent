"""Tavily Web Search Tool — 网络实时搜索。"""

import os
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class TavilySearchInput(BaseModel):
    query: str = Field(description="搜索关键词或问题")


class TavilySearchTool(BaseTool):
    name: str = "tavily_search"
    description: str = (
        "Search the web using Tavily API. Use when you need up-to-date information, "
        "current events, or real-time data that you don't have in your training data. "
        "Input a search query and get relevant web results."
    )
    args_schema: Type[BaseModel] = TavilySearchInput

    def _run(self, query: str) -> str:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            return "❌ TAVILY_API_KEY not configured in .env"
        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=api_key)
            result = client.search(query, max_results=5)

            lines: list[str] = [f"🔍 Web search results for: {query}\n"]
            for i, item in enumerate(result.get("results", []), 1):
                title = item.get("title", "No title")
                url = item.get("url", "")
                content = item.get("content", "")
                lines.append(f"**{i}. {title}**")
                lines.append(f"   URL: {url}")
                lines.append(f"   {content[:300]}")
                lines.append("")

            return "\n".join(lines) if len(lines) > 1 else "No results found."
        except ImportError:
            return "❌ tavily-python package not installed. Run: pip install tavily-python"
        except Exception as e:
            return f"❌ Tavily search error: {e}"


def create_tavily_search_tool() -> TavilySearchTool:
    return TavilySearchTool()
