"""LLM boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class LLM(ABC):
    @abstractmethod
    async def complete(
        self,
        prompt: str,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float = 0.1,
    ) -> str:
        """Single-shot completion. With `json_mode`, the result parses as JSON."""
        raise NotImplementedError

    @abstractmethod
    async def stream(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.1,
    ) -> AsyncIterator[str]:
        """Yield completion text incrementally. Used by the chat API in M2."""
        raise NotImplementedError
