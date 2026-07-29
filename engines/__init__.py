"""
Engine Registry
===============

Dispatches engine-neutral client creation to the concrete backend selected
by the current provider settings (``API_PROVIDERS[provider]["engine"]``).

Engines:
    claude  - Claude Code CLI via claude-agent-sdk (default; also used by all
              Anthropic-compatible HTTP providers: GLM, Kimi, Ollama, Azure,
              Custom)
    codex   - OpenAI Codex app-server via the openai-codex SDK
              (ChatGPT subscription; see docs/ENGINE_ADAPTER_PLAN.md)
"""

from .types import EngineConfig, EngineOptions

KNOWN_ENGINES = ("claude", "codex")


def create_engine_client(engine: str, options: EngineOptions):
    """Create a client for the given engine.

    All engine clients provide the same runtime protocol:
    ``async with``-style context management, ``query()``, and
    ``receive_response()`` yielding duck-typed message objects.
    """
    if engine == "claude":
        from . import claude_engine

        return claude_engine.create_client(options)
    if engine == "codex":
        from . import codex_engine

        return codex_engine.create_client(options)
    raise ValueError(f"Unknown engine '{engine}'. Known engines: {KNOWN_ENGINES}")


__all__ = ["EngineConfig", "EngineOptions", "KNOWN_ENGINES", "create_engine_client"]
