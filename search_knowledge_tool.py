"""SearchKnowledgeBaseTool — LlamaIndex 纯向量语义检索（VectorStoreIndex KNN）。

注意：本工具当前仅实现向量语义召回，未实现 BM25 关键词召回或混合检索。
requirements.txt 虽装了 llama-index-retrievers-bm25，但代码未 import 使用。
如需对齐 openclaw 的向量 + BM25 双路混合，可改用 QueryFusionRetriever。
"""

from pathlib import Path
from typing import Type, Optional

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from model_config import create_embedding_model, embedding_signature


class SearchKnowledgeInput(BaseModel):
    query: str = Field(description="The search query to find relevant knowledge")


class SearchKnowledgeBaseTool(BaseTool):
    name: str = "search_knowledge_base"
    description: str = (
        "Search the local knowledge base using semantic vector retrieval. "
        "Use this when the user asks about specific knowledge or documents. "
        "Returns the most relevant passages from the knowledge base."
    )
    args_schema: Type[BaseModel] = SearchKnowledgeInput
    base_dir: str = ""
    _index: Optional[object] = None

    class Config:
        arbitrary_types_allowed = True

    def _build_index(self):
        """Build or load LlamaIndex index from knowledge/ directory."""
        knowledge_dir = Path(self.base_dir) / "knowledge"
        storage_dir = Path(self.base_dir) / "storage" / "knowledge_index" / embedding_signature()

        if not knowledge_dir.exists() or not any(knowledge_dir.iterdir()):
            return None

        try:
            from llama_index.core import (
                SimpleDirectoryReader,
                StorageContext,
                VectorStoreIndex,
                load_index_from_storage,
            )
            embed_model = create_embedding_model()

            # Try loading persisted index
            if storage_dir.exists() and any(storage_dir.iterdir()):
                try:
                    storage_context = StorageContext.from_defaults(
                        persist_dir=str(storage_dir)
                    )
                    return load_index_from_storage(storage_context, embed_model=embed_model)
                except Exception:
                    pass

            # Build fresh index
            documents = SimpleDirectoryReader(
                str(knowledge_dir), recursive=True
            ).load_data()

            if not documents:
                return None

            index = VectorStoreIndex.from_documents(documents, embed_model=embed_model)
            storage_dir.mkdir(parents=True, exist_ok=True)
            index.storage_context.persist(persist_dir=str(storage_dir))
            return index

        except ImportError as e:
            print(f"⚠️ LlamaIndex not fully installed: {e}")
            return None
        except Exception as e:
            print(f"⚠️ Index build error: {e}")
            return None

    def _run(self, query: str) -> str:
        if self._index is None:
            self._index = self._build_index()

        if self._index is None:
            return "📭 Knowledge base is empty. Add documents to backend/knowledge/ to enable search."

        try:
            # Return passages to the main Agent. as_query_engine() would silently
            # resolve a second, default OpenAI LLM and require a cloud API key.
            nodes = self._index.as_retriever(similarity_top_k=3).retrieve(query)
            result = "\n\n".join(
                f"[{node.metadata.get('file_name', 'knowledge')}]\n{node.get_text()}"
                for node in nodes
            ) or "No relevant knowledge found."
            if len(result) > 5000:
                result = result[:5000] + "\n...[truncated]"
            return result
        except Exception as e:
            return f"❌ Search error: {str(e)}"


def create_search_knowledge_tool(base_dir: Path) -> SearchKnowledgeBaseTool:
    return SearchKnowledgeBaseTool(base_dir=str(base_dir))
