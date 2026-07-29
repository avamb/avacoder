"""
Engine Abstraction Types
========================

Engine-neutral option types shared by all engine implementations.

AutoForge can drive different coding-agent backends ("engines"): the Claude
Code CLI via claude-agent-sdk today, others (e.g. Codex CLI) in the future.
`EngineOptions` captures the subset of client options AutoForge actually
uses, decoupled from any particular SDK's options class.
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class EngineOptions:
    """Engine-neutral client options.

    Fields left as ``None`` are omitted when translating to a concrete
    engine's native options object, preserving that engine's defaults.
    (An explicit empty value such as ``betas=[]`` is passed through.)
    """

    model: Optional[str] = None
    effort: Optional[str] = None
    system_prompt: Optional[str] = None
    setting_sources: Optional[list[str]] = None
    allowed_tools: Optional[list[str]] = None
    disallowed_tools: Optional[list[str]] = None
    mcp_servers: Optional[dict[str, Any]] = None
    hooks: Optional[dict[str, Any]] = None
    permission_mode: Optional[str] = None
    max_turns: Optional[int] = None
    cwd: Optional[str] = None
    settings: Optional[str] = None
    env: Optional[dict[str, str]] = None
    max_buffer_size: Optional[int] = None
    betas: Optional[list[str]] = None


@dataclass
class EngineConfig:
    """Resolved engine selection for the current provider settings.

    Produced by ``registry.get_effective_engine_config()`` and consumed by
    the client factories; carries the provider identity that the flat env
    dict of ``get_effective_sdk_env()`` erases.
    """

    engine: str
    provider_id: str
    model: str
    effort: str
    env: dict[str, str]
