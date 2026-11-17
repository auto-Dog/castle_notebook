"""AI provider registry. Each backend is implemented in its own module."""
from __future__ import annotations

from .claude import ClaudeProvider
from .codex import CodexProvider
from .custom import CustomProvider

PROVIDERS = {"claude": ClaudeProvider, "codex": CodexProvider, "custom": CustomProvider}


def create_providers(claude_command=None, codex_command=None):
    return {"claude": ClaudeProvider(claude_command), "codex": CodexProvider(codex_command),
            "custom": CustomProvider()}