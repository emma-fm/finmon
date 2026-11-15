"""Claude Code CLI in headless mode (`claude -p`), billed to the Claude subscription it is logged in with.

On a server, authenticate once with `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN.
All tools are disabled, so the model can only read the prompt and answer.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

from ..config import Settings
from .base import LLMError


class ClaudeCodeProvider:
    def __init__(self, settings: Settings):
        self.bin = settings.claude_bin
        self.model = settings.llm_model or "opus"
        self.timeout = settings.llm_timeout

    def complete(self, prompt: str, system: str, json_mode: bool) -> str:
        cmd = [self.bin, "-p", "--output-format", "json", "--tools", "", "--model", self.model,
               "--system-prompt", system]
        # Run from an empty directory so no project CLAUDE.md / settings leak in.
        with tempfile.TemporaryDirectory(prefix="finmon-llm-") as cwd:
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                      timeout=self.timeout, cwd=cwd)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise LLMError(f"claude CLI failed to run: {e}") from e
        if proc.returncode != 0:
            raise LLMError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"unexpected claude output: {proc.stdout[:500]}") from e
        if data.get("is_error"):
            raise LLMError(f"claude returned an error: {data.get('result')}")
        return data.get("result", "")
