"""AI provider registry. Each backend is implemented in its own module."""
from __future__ import annotations

from .claude import ClaudeProvider
from .codex import CodexProvider

PROVIDERS = {"claude": ClaudeProvider, "codex": CodexProvider}


def create_providers(claude_command=None, codex_command=None):
    return {"claude": ClaudeProvider(claude_command), "codex": CodexProvider(codex_command)}