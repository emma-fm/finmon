"""Any command-line LLM: the system prompt and prompt are sent on stdin, the answer read from stdout.

Examples (.env):
  LLM_COMMAND=ollama run qwen3:14b
  LLM_COMMAND=llm -m <model>            (simonw/llm, supports many vendors)
"""

from __future__ import annotations

import shlex
import subprocess

from ..config import Settings
from .base import LLMError


class CommandProvider:
    def __init__(self, settings: Settings):
        if not settings.llm_command:
            raise LLMError("LLM_PROVIDER=command needs LLM_COMMAND")
        self.cmd = shlex.split(settings.llm_command)
        self.timeout = settings.llm_timeout

    def complete(self, prompt: str, system: str, json_mode: bool) -> str:
        stdin = f"{system}\n\n---\n\n{prompt}"
        try:
            proc = subprocess.run(self.cmd, input=stdin, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LLMError(f"LLM command failed to run: {e}") from e
        if proc.returncode != 0:
            raise LLMError(f"LLM command exited {proc.returncode}: {proc.stderr[:500]}")
        return proc.stdout
