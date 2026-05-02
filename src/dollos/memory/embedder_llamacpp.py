"""llama.cpp /embedding raw endpoint adapter."""

import logging

import httpx

from dollos.memory.embedder import Embedder

logger = logging.getLogger(__name__)


class LlamaCppEmbedder(Embedder):
    """Adapter for self-hosted llama.cpp `/embedding` endpoint.

    The model_id is configured statically (used as identity in memory_meta).
    The server-reported dimension is discovered via a probe call in initialize().
    Per spec, batches are issued sequentially as one HTTP request per input.
    """

    def __init__(self, base_url: str, model_id: str, timeout_s: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._timeout_s = timeout_s
        self._dimensions: int | None = None

    async def initialize(self) -> None:
        # Probe to discover dimensions
        v = await self._post_embedding("_dim_probe_")
        self._dimensions = len(v)
        logger.info(
            "LlamaCppEmbedder initialized: model_id=%s dim=%d",
            self._model_id,
            self._dimensions,
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            raise RuntimeError("LlamaCppEmbedder not initialized")
        return self._dimensions

    async def embed(self, text: str) -> list[float]:
        if self._dimensions is None:
            raise RuntimeError("LlamaCppEmbedder not initialized")
        return await self._post_embedding(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self._dimensions is None:
            raise RuntimeError("LlamaCppEmbedder not initialized")
        # llama.cpp /embedding batch behavior varies by version;
        # we issue one request per input for predictable behavior.
        return [await self._post_embedding(t) for t in texts]

    async def _post_embedding(self, text: str) -> list[float]:
        url = f"{self._base_url}/embedding"
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(url, json={"content": text})
            resp.raise_for_status()
            data = resp.json()
        return list(data["embedding"])
