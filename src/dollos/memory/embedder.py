"""Embedder abstract interface and a deterministic stub for tests."""

import hashlib
import struct
from abc import ABC, abstractmethod


class Embedder(ABC):
    """Abstract embedder.

    Initialization is two-stage:
      1. __init__: sync, stores config; model_id is available immediately
      2. await initialize(): async, discovers dimensions from the backend

    Callers MUST await initialize() before reading dimensions or calling embed().
    """

    @abstractmethod
    async def initialize(self) -> None: ...

    @property
    @abstractmethod
    def model_id(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class StubEmbedder(Embedder):
    """Deterministic embedder for tests.

    Hashes input text via SHA-256 and unpacks bytes as 8 IEEE-754 floats,
    repeated to fill 32 dimensions. Same text → same vector.
    """

    _DIM = 32

    def __init__(self) -> None:
        self._initialized = False

    async def initialize(self) -> None:
        self._initialized = True

    @property
    def model_id(self) -> str:
        return "stub"

    @property
    def dimensions(self) -> int:
        if not self._initialized:
            raise RuntimeError("StubEmbedder not initialized")
        return self._DIM

    async def embed(self, text: str) -> list[float]:
        if not self._initialized:
            raise RuntimeError("StubEmbedder not initialized")
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 32 bytes → 8 little-endian floats; tile to 32 floats
        floats_8 = list(struct.unpack("<8f", digest))
        return (floats_8 * 4)[: self._DIM]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]
