"""Any OpenAI-compatible Chat Completions API, over plain HTTP (no extra dependency).

Examples (.env):
  Ollama (local, free):  LLM_BASE_URL=http://localhost:11434/v1   LLM_MODEL=qwen3:14b
  OpenAI:                LLM_BASE_URL=https://api.openai.com/v1   LLM_MODEL=<model>  LLM_API_KEY=sk-...
  Mistral:               LLM_BASE_URL=https://api.mistral.ai/v1   LLM_MODEL=<model>  LLM_API_KEY=...
  OpenRouter:            LLM_BASE_URL=https://openrouter.ai/api/v1 LLM_MODEL=<vendor/model> LLM_API_KEY=...
  Gemini:                LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai LLM_MODEL=<model>
"""

from __future__ import annotations

import requests

from ..config import Settings
from .base import LLMError


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings):
        if not settings.llm_base_url or not settings.llm_model:
            raise LLMError("LLM_PROVIDER=openai-compatible needs LLM_BASE_URL and LLM_MODEL")
        self.url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.timeout = settings.llm_timeout
        self.native_json = settings.llm_json_mode

    def complete(self, prompt: str, system: str, json_mode: bool) -> str:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if json_mode and self.native_json:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = requests.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise LLMError(f"cannot reach {self.url}: {e}") from e
        if resp.status_code >= 400:
            raise LLMError(f"LLM API error {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError(f"unexpected LLM API response: {resp.text[:300]}") from e
