"""Embedder-free memory backend for DollOS.

Phase 1: episodic lexical retrieval over markdown daily files, backed by
SQLite FTS5 (BM25) + jieba CJK tokenization. Markdown stays the source of
truth; the SQLite DB is a derived, disposable index. No embeddings, no
Milvus, no onnxruntime.

See docs/superpowers/specs/2026-06-26-embedder-free-memory-design.md.
"""
from dollos.memory.fts_store import FtsMemory

__all__ = ["FtsMemory"]
