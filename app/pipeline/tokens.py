"""Token counting for chunk sizing.

Gemini has no public offline tokeniser, and calling its count-tokens endpoint
once per candidate chunk would make ingestion network-bound for no real gain --
chunk sizing only needs to be roughly right. So this is a deterministic local
estimate, deliberately biased to overestimate slightly so chunks land under the
target rather than over it.

`set_token_counter` exists so a deployment that does have an exact tokeniser
(a local HF tokeniser, say) can install it at startup without touching the
chunker.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

_WORD = re.compile(r"\S+")

#: Characters per token. English prose averages ~4, but this is deliberately
#: set lower so the estimate runs high and chunks land under the embedder's
#: input limit rather than over it. Markdown tables and code are denser still,
#: which the word-based term below catches.
_CHARS_PER_TOKEN = 3.5
#: A word is about 1.3 tokens once sub-word splits and punctuation are counted.
_TOKENS_PER_WORD = 1.3


def estimate_tokens(text: str) -> int:
    """Estimate the token count of `text`. Never returns less than 1 for
    non-empty input."""
    if not text:
        return 0
    by_chars = len(text) / _CHARS_PER_TOKEN
    by_words = len(_WORD.findall(text)) * _TOKENS_PER_WORD
    return max(1, math.ceil(max(by_chars, by_words)))


_counter: Callable[[str], int] = estimate_tokens


def set_token_counter(counter: Callable[[str], int]) -> None:
    """Install an exact tokeniser. Called at startup, never mid-ingest."""
    global _counter
    _counter = counter


def count_tokens(text: str) -> int:
    return _counter(text)
