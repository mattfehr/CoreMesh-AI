"""Public hybrid-retrieval API.

System role:
    Re-exports chunk/result contracts and the dense/sparse retrieval facade for
    trusted Python callers and the RAG agent specialist.
Dependencies:
    Importing loads retrieval definitions and settings; OpenAI, Qdrant, BM25,
    and cross-encoder clients/models remain lazy.
Side effects:
    Importing has no I/O. Index/search calls can write Qdrant, call OpenAI, and
    load or download reranker weights.
"""

from src.rag.retrieval import HybridRetriever, RetrievalResult, TextChunk

__all__ = ["HybridRetriever", "RetrievalResult", "TextChunk"]
