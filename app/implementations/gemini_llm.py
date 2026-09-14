"""Gemini-backed LLM. The only module in the app that knows Gemini's chat API."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator

from google import genai
from google.genai import types

from app.config import get_settings
from app.implementations.retry import with_backoff
from app.interfaces.llm import LLM

logger = logging.getLogger(__name__)

#: Models sometimes wrap JSON in a fenced code block despite being asked not to.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class GeminiLLM(LLM):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._model = model or settings.llm_model
        self._max_attempts = settings.embed_max_retries
        self._client = genai.Client(api_key=api_key or settings.gemini_api_key)

    def _config(
        self, system: str | None, json_mode: bool, temperature: float
    ) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json" if json_mode else None,
            # This app never declares tools. Left on, the SDK logs an automatic
            # function calling warning on every single call, which would be one
            # log line per section description across the whole corpus.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )

    async def complete(
        self,
        prompt: str,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float = 0.1,
    ) -> str:
        async def call() -> str:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=self._config(system, json_mode, temperature),
            )
            return response.text or ""

        text = await with_backoff(
            call,
            max_attempts=self._max_attempts,
            description=f"gemini complete ({self._model})",
        )
        return strip_code_fence(text) if json_mode else text

    async def stream(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.1,
    ) -> AsyncIterator[str]:
        # Retries are not applied mid-stream: a partial response has already
        # reached the caller, so replaying it would duplicate text.
        iterator = await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=prompt,
            config=self._config(system, False, temperature),
        )
        async for event in iterator:
            piece = getattr(event, "text", None)
            if piece:
                yield piece


def strip_code_fence(text: str) -> str:
    match = _FENCE.match(text or "")
    return match.group(1) if match else (text or "")


def parse_json_object(text: str) -> dict:
    """Parse an LLM JSON response, tolerating fences and trailing prose.

    Returns {} rather than raising: a document whose profile fails to parse
    should still be indexed, just without derived tags.
    """
    candidate = strip_code_fence(text).strip()
    if not candidate:
        return {}
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            logger.warning("LLM response contained no JSON object")
            return {}
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("LLM JSON response could not be parsed")
            return {}
    return parsed if isinstance(parsed, dict) else {}
