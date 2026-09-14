"""Gemini-backed embedder.

Translates the pipeline's neutral task types onto Gemini's vocabulary: document
and query embeddings are asymmetric, and using the wrong one quietly costs
retrieval quality rather than failing outright.
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types

from app.config import get_settings
from app.implementations.retry import with_backoff
from app.interfaces.embedder import TASK_DOCUMENT, TASK_QUERY, Embedder

logger = logging.getLogger(__name__)

_TASK_TYPES = {
    TASK_DOCUMENT: "RETRIEVAL_DOCUMENT",
    TASK_QUERY: "RETRIEVAL_QUERY",
}


class GeminiEmbedder(Embedder):
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        settings = get_settings()
        self._model = model or settings.embedding_model
        self._dimensions = dimensions or settings.embedding_dimensions
        self._max_attempts = settings.embed_max_retries
        self._client = genai.Client(api_key=api_key or settings.gemini_api_key)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_batch(
        self, texts: list[str], task_type: str = TASK_DOCUMENT
    ) -> list[list[float]]:
        if not texts:
            return []
        gemini_task = _TASK_TYPES.get(task_type)
        if gemini_task is None:
            raise ValueError(
                f"Unknown task type {task_type!r}; expected one of {sorted(_TASK_TYPES)}"
            )

        async def call() -> list[list[float]]:
            # Each text must be wrapped in its own Content. A bare list[str]
            # matches the SDK's "one Content, many Parts" overload, which
            # embeds the concatenation of the batch as a single vector instead
            # of one vector per chunk.
            response = await self._client.aio.models.embed_content(
                model=self._model,
                contents=[
                    types.Content(parts=[types.Part(text=t)]) for t in texts
                ],
                # No auto_truncate here: it exists only in Vertex / Enterprise
                # Agent Platform mode and the Developer API rejects the whole
                # request if it is set. Staying under the input limit is the
                # chunker's job -- see ChunkingConfig.max_tokens.
                config=types.EmbedContentConfig(
                    task_type=gemini_task,
                    output_dimensionality=self._dimensions,
                ),
            )
            embeddings = response.embeddings or []
            if len(embeddings) != len(texts):
                raise RuntimeError(
                    f"Embedder returned {len(embeddings)} vectors for "
                    f"{len(texts)} inputs"
                )
            vectors: list[list[float]] = []
            for embedding in embeddings:
                values = list(embedding.values or [])
                if len(values) != self._dimensions:
                    raise RuntimeError(
                        f"Expected {self._dimensions}-dimensional vectors, "
                        f"got {len(values)}"
                    )
                vectors.append(values)
            return vectors

        return await with_backoff(
            call,
            max_attempts=self._max_attempts,
            description=f"gemini embed ({self._model}, {len(texts)} texts)",
        )
