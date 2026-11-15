"""Anthropic API via the official SDK (pay per use). `pip install anthropic`, set ANTHROPIC_API_KEY."""

from __future__ import annotations

from ..config import Settings
from .base import LLMError

# Models that accept server-side refusal fallbacks (re-run on another model if one declines).
_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class AnthropicProvider:
    def __init__(self, settings: Settings):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("LLM_PROVIDER=anthropic needs the SDK: pip install anthropic") from e
        self._anthropic = anthropic
        # Credentials resolve from the environment (ANTHROPIC_API_KEY, ...); LLM_API_KEY overrides.
        self.client = anthropic.Anthropic(api_key=settings.llm_api_key or None, timeout=settings.llm_timeout)
        self.model = settings.llm_model or "claude-opus-5-5"
        self.effort = settings.llm_effort or "medium"

    def complete(self, prompt: str, system: str, json_mode: bool) -> str:
        params = dict(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.effort},
        )
        try:
            if self.model in _FALLBACK_MODELS:
                response = self.client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                response = self.client.messages.create(**params)
        except self._anthropic.APIConnectionError as e:
            raise LLMError(f"cannot reach the Anthropic API: {e}") from e
        except self._anthropic.RateLimitError as e:
            raise LLMError(f"Anthropic rate limit: {e}") from e
        except self._anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e

        if response.stop_reason == "refusal":
            raise LLMError("the model declined to answer")
        text = "".join(block.text for block in response.content if block.type == "text")
        if response.stop_reason == "max_tokens" and not text:
            raise LLMError("answer truncated (max_tokens)")
        return text
