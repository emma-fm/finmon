from __future__ import annotations

from typing import Protocol


class LLMError(RuntimeError):
    """Any failure talking to the LLM. Callers fall back to plain, non-LLM messages."""


class Provider(Protocol):
    def complete(self, prompt: str, system: str, json_mode: bool) -> str:
        """Return the model's text answer.

        json_mode=True means the prompt asks for a single JSON object; providers that support
        a native JSON mode should enable it, others can ignore the flag.
        """
        ...
