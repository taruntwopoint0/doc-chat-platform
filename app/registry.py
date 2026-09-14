"""Interface -> implementation wiring, selected by config at startup.

The single place concrete provider classes are named. Pipeline and API code
imports from here, never from app.implementations.*, so swapping a provider is
one env var plus one entry in the maps below.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.implementations.parsers import ParserRegistry, build_registry
from app.interfaces.embedder import Embedder
from app.interfaces.file_store import FileStore
from app.interfaces.llm import LLM
from app.interfaces.vector_store import VectorStore
from app.models.constants import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)


def _gemini_embedder() -> Embedder:
    from app.implementations.gemini_embedder import GeminiEmbedder

    return GeminiEmbedder()


def _gemini_llm() -> LLM:
    from app.implementations.gemini_llm import GeminiLLM

    return GeminiLLM()


EMBEDDERS: dict[str, Callable[[], Embedder]] = {"gemini": _gemini_embedder}
LLMS: dict[str, Callable[[], LLM]] = {"gemini": _gemini_llm}


def _select(maps: dict, name: str, kind: str):
    factory = maps.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown {kind} implementation {name!r}. Available: {sorted(maps)}"
        )
    return factory()


@lru_cache(maxsize=1)
def get_parser_registry() -> ParserRegistry:
    return build_registry(get_settings().parser_impl)


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    return _select(EMBEDDERS, get_settings().embedder_impl, "embedder")


@lru_cache(maxsize=1)
def get_llm() -> LLM:
    return _select(LLMS, get_settings().llm_impl, "LLM")


def get_vector_store(session: AsyncSession | None = None) -> VectorStore:
    name = get_settings().vector_store_impl
    if name != "pgvector":
        raise ValueError(f"Unknown vector store implementation {name!r}")
    from app.implementations.pgvector_store import PgVectorStore

    return PgVectorStore(session)


def get_file_store(session: AsyncSession | None = None) -> FileStore:
    name = get_settings().file_store_impl
    if name != "postgres":
        raise ValueError(f"Unknown file store implementation {name!r}")
    from app.implementations.postgres_file_store import PostgresFileStore

    return PostgresFileStore(session)


def validate_wiring() -> None:
    """Fail loudly at startup rather than silently at the first insert.

    A mismatch between the embedder's width and the `chunks.embedding` column
    would otherwise surface as a Postgres error deep inside a background job.
    """
    settings = get_settings()
    embedder = get_embedder()
    if embedder.dimensions != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"Embedder produces {embedder.dimensions}-dimensional vectors but "
            f"the chunks.embedding column is vector({EMBEDDING_DIMENSIONS}). "
            "Set EMBEDDING_DIMENSIONS to match and run a migration."
        )
    if not settings.gemini_api_key and "gemini" in {
        settings.embedder_impl,
        settings.llm_impl,
    }:
        logger.warning(
            "GEMINI_API_KEY is empty; profiling and embedding will fail at "
            "the first provider call."
        )
    logger.info(
        "Wiring: parser=%s embedder=%s llm=%s vector_store=%s file_store=%s",
        settings.parser_impl,
        settings.embedder_impl,
        settings.llm_impl,
        settings.vector_store_impl,
        settings.file_store_impl,
    )


def reset_caches() -> None:
    """Drop memoised implementations. Tests use this after changing settings."""
    get_parser_registry.cache_clear()
    get_embedder.cache_clear()
    get_llm.cache_clear()
