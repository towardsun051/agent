"""MemoryIndexer — Vector / Hybrid index for MEMORY.md with auto-rebuild on change."""

import hashlib
import re
from pathlib import Path
from typing import Any

from utils.encoding import safe_read_text
from model_config import create_embedding_model, embedding_signature


def _zh_en_tokenizer(text: str) -> list[str]:
    """中英混合分词器（教学版，零依赖），供 BM25 关键词检索使用。

    英文/数字按词切分（统一小写），中文按单字切分。
    局限：中文用单字而非词级（未接 jieba），词级召回精度不如生产实现；
    但 MEMORY.md 含大量英文术语（skill 名 / API 名 / 文件名），BM25 对这些
    精确关键词的召回正是向量语义检索的弱项——两路融合形成互补，这正是
    引入混合检索的核心价值。生产环境可替换为 jieba.lcut 提升中文词级召回。
    """
    text = text.lower()
    en_tokens = re.findall(r"[a-z0-9]+", text)
    zh_tokens = re.findall(r"[一-鿿]", text)
    return en_tokens + zh_tokens


class MemoryIndexer:
    """Indexes memory/MEMORY.md for RAG retrieval.

    Uses MD5 hash to detect changes and auto-rebuild the vector index.
    Storage is kept separate from the knowledge base index.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._memory_path = base_dir / "memory" / "MEMORY.md"
        self._embedding_signature = embedding_signature()
        # Never reuse vectors produced by a different embedding model.
        self._storage_dir = base_dir / "storage" / "memory_index" / self._embedding_signature
        self._hash_path = self._storage_dir / ".memory_hash"
        self._index: Any = None
        # 缓存 rebuild 时切出的 nodes，供 hybrid 模式构建 BM25 检索器；
        # 重启后若未 rebuild，则从 index.docstore 还原（见 _get_nodes）。
        self._nodes: Any = None

    def _get_file_hash(self) -> str:
        """Get MD5 hash of MEMORY.md."""
        if not self._memory_path.exists():
            return ""
        content = self._memory_path.read_bytes()
        return hashlib.md5(content).hexdigest()

    def _get_stored_hash(self) -> str:
        """Get the stored hash from the last build."""
        if not self._hash_path.exists():
            return ""
        return self._hash_path.read_text(encoding="utf-8").strip()

    def _save_hash(self, hash_value: str) -> None:
        """Save the current hash."""
        self._hash_path.parent.mkdir(parents=True, exist_ok=True)
        self._hash_path.write_text(hash_value, encoding="utf-8")

    def _maybe_rebuild(self) -> None:
        """Rebuild index if MEMORY.md has changed."""
        current_hash = self._get_file_hash()
        stored_hash = self._get_stored_hash()
        if current_hash and current_hash != stored_hash:
            self.rebuild_index()

    def rebuild_index(self) -> None:
        """Read MEMORY.md, split into chunks, build vector index, persist."""
        if not self._memory_path.exists():
            print("⚠️ memory/MEMORY.md not found, skipping index build")
            self._index = None
            return

        try:
            from llama_index.core import (
                Document,
                StorageContext,
                VectorStoreIndex,
            )
            from llama_index.core.node_parser import SentenceSplitter
            embed_model = create_embedding_model()

            content = safe_read_text(self._memory_path)
            if not content.strip():
                self._index = None
                return

            doc = Document(text=content, metadata={"source": "MEMORY.md"})

            splitter = SentenceSplitter(chunk_size=256, chunk_overlap=32)
            nodes = splitter.get_nodes_from_documents([doc])

            self._storage_dir.mkdir(parents=True, exist_ok=True)
            index = VectorStoreIndex(nodes, embed_model=embed_model)
            index.storage_context.persist(persist_dir=str(self._storage_dir))
            self._index = index
            self._nodes = nodes  # 缓存供 hybrid 模式的 BM25 使用

            # Save hash
            self._save_hash(self._get_file_hash())
            print(f"🔄 Memory index rebuilt ({len(nodes)} chunks)")

        except ImportError as e:
            print(f"⚠️ LlamaIndex not fully installed: {e}")
            self._index = None
        except Exception as e:
            print(f"⚠️ Memory index build error: {e}")
            self._index = None

    def _load_index(self) -> Any:
        """Load persisted index from storage."""
        if self._index is not None:
            return self._index

        if not self._storage_dir.exists() or not any(self._storage_dir.iterdir()):
            return None

        try:
            from llama_index.core import StorageContext, load_index_from_storage
            embed_model = create_embedding_model()

            storage_context = StorageContext.from_defaults(
                persist_dir=str(self._storage_dir)
            )
            self._index = load_index_from_storage(storage_context, embed_model=embed_model)
            return self._index
        except Exception as e:
            print(f"⚠️ Failed to load memory index: {e}")
            return None

    def _get_nodes(self, index: Any) -> list[Any]:
        """获取用于 BM25 的 nodes：优先用 rebuild 缓存，否则从持久化 docstore 还原。"""
        if self._nodes:
            return list(self._nodes)
        try:
            return list(index.docstore.docs.values())
        except Exception:
            return []

    def _hybrid_retrieve(self, index: Any, query: str, top_k: int) -> list:
        """BM25 关键词检索 + 向量语义检索两路并行，再用 RRF 融合。

        为什么自己实现 RRF 而不用 LlamaIndex 的 QueryFusionRetriever：后者构造时会
        无条件 resolve 一个 LLM（即便 num_queries=1 不做查询扩展），给教学项目徒增
        OpenAI LLM 依赖与隐藏调用。自实现 RRF 既零额外依赖，也让融合算法对学员完全
        透明。若 nodes 取不到（索引为空）则回退纯向量。
        """
        from llama_index.retrievers.bm25 import BM25Retriever

        vector_nodes = index.as_retriever(similarity_top_k=top_k).retrieve(query)

        nodes = self._get_nodes(index)
        if not nodes:
            return vector_nodes

        # BM25 关键词召回：中英混合 tokenizer + skip_stemming（中文不做英文词干还原）
        bm25 = BM25Retriever.from_defaults(
            nodes=nodes,
            similarity_top_k=top_k,
            # Current BM25Retriever ignores the deprecated tokenizer argument.
            token_pattern=r"[a-zA-Z0-9]+|[一-鿿]",
            language="en",
            skip_stemming=True,
        )
        bm25_nodes = bm25.retrieve(query)

        return self._reciprocal_rank_fusion([vector_nodes, bm25_nodes], top_k)

    @staticmethod
    def _reciprocal_rank_fusion(ranked_lists: list, top_k: int, k: int = 60) -> list:
        """RRF 倒数排名融合：score(d) = Σ_i 1 / (k + rank_i(d))，k=60 为业界经验常数。

        对每一路检索结果按名次取倒数累加，名次越靠前（rank 越小）贡献越大；同时被
        多路命中的文档得分叠加，因此关键词、语义都认可的片段排最前。融合分写回
        NodeWithScore.score 以便前端展示。
        """
        scores: dict[str, float] = {}
        node_map: dict[str, Any] = {}
        for nodes in ranked_lists:
            for rank, nws in enumerate(nodes):
                nid = nws.node.node_id
                node_map[nid] = nws
                scores[nid] = scores.get(nid, 0.0) + 1.0 / (k + rank + 1)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        fused: list = []
        for nid, score in ranked:
            nws = node_map[nid]
            nws.score = score
            fused.append(nws)
        return fused

    def retrieve(
        self, query: str, top_k: int = 3, mode: str = "vector"
    ) -> list[dict[str, Any]]:
        """Retrieve relevant memory chunks for a query.

        mode='vector'：纯向量语义检索（默认）。
        mode='hybrid'：BM25 关键词 + 向量语义的 RRF 融合检索。
        """
        if not self._memory_path.exists() or not safe_read_text(self._memory_path).strip():
            self._index = None
            self._nodes = None
            return []
        self._maybe_rebuild()

        index = self._load_index()
        if index is None:
            return []

        try:
            if mode == "hybrid":
                nodes = self._hybrid_retrieve(index, query, top_k)
            else:
                nodes = index.as_retriever(similarity_top_k=top_k).retrieve(query)

            results: list[dict[str, Any]] = []
            for node in nodes:
                results.append({
                    "text": node.get_text(),
                    "score": f"{node.get_score():.4f}" if node.get_score() else "N/A",
                    "source": node.metadata.get("source", "MEMORY.md"),
                })
            return results
        except Exception as e:
            print(f"⚠️ Memory retrieval error ({mode}): {e}")
            return []


# Singleton
_instance: MemoryIndexer | None = None


def get_memory_indexer(base_dir: Path) -> MemoryIndexer:
    """Get or create the singleton MemoryIndexer."""
    global _instance
    if (_instance is None or _instance._base_dir != base_dir
            or _instance._embedding_signature != embedding_signature()):
        _instance = MemoryIndexer(base_dir)
    return _instance
