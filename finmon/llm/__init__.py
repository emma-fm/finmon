"""Pluggable LLM backend.

The rest of finmon only calls `ask()` / `ask_json()` with a prompt and a system prompt and gets
text back. Which model answers is decided by LLM_PROVIDER in .env:

  claude-code        Claude Code CLI headless (`claude -p`), uses a Claude subscription (default)
  anthropic          Anthropic API with the official SDK (pay per use, needs ANTHROPIC_API_KEY)
  openai-compatible  Any OpenAI-compatible chat API: OpenAI, Ollama (local, free), Mistral, Groq,
                     OpenRouter, Gemini, LM Studio... (LLM_BASE_URL + LLM_API_KEY + LLM_MODEL)
  command            Any CLI that reads the prompt on stdin and prints the answer (LLM_COMMAND)

To add a provider: write a class with `complete(prompt, system, json_mode) -> str` in this
package and add it to _build() below.
"""

from __future__ import annotations

import json
import logging
import re
import time

from ..config import Settings
from .base import LLMError, Provider

log = logging.getLogger(__name__)

__all__ = ["LLMError", "ask", "ask_json", "get_provider"]


def _build(settings: Settings) -> Provider:
    name = settings.llm_provider
    if name == "claude-code":
        from .claude_code import ClaudeCodeProvider
        return ClaudeCodeProvider(settings)
    if name == "anthropic":
        from .anthropic_api import AnthropicProvider
        return AnthropicProvider(settings)
    if name == "openai-compatible":
        from .openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(settings)
    if name == "command":
        from .command import CommandProvider
        return CommandProvider(settings)
    raise LLMError(f"Unknown LLM_PROVIDER {name!r} (use claude-code, anthropic, openai-compatible or command)")


_providers: dict[tuple, Provider] = {}

# Per-process counters, reported by debug mode.
usage = {"calls": 0, "errors": 0, "seconds": 0.0}


def _complete(settings: Settings, prompt: str, system: str, json_mode: bool) -> str:
    start = time.monotonic()
    usage["calls"] += 1
    try:
        return get_provider(settings).complete(prompt, system, json_mode=json_mode)
    except LLMError:
        usage["errors"] += 1
        raise
    finally:
        usage["seconds"] += time.monotonic() - start


def get_provider(settings: Settings) -> Provider:
    """One provider instance per configuration."""
    key = (settings.llm_provider, settings.llm_model, settings.llm_base_url, settings.llm_command)
    if key not in _providers:
        _providers[key] = _build(settings)
    return _providers[key]


def ask(settings: Settings, prompt: str, system: str) -> str:
    """Free-text answer."""
    text = _complete(settings, prompt, system, json_mode=False)
    if not text.strip():
        raise LLMError("empty answer from the LLM")
    return text


def ask_json(settings: Settings, prompt: str, system: str) -> dict:
    """Answer parsed as a JSON object. Tolerates code fences / text around the object."""
    text = _complete(settings, prompt, system, json_mode=True)
    return parse_json_object(text)


def parse_json_object(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise LLMError(f"no JSON object in the answer: {text[:300]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON in the answer: {e}") from e
