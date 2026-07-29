"""
Claude Engine
=============

Client factory for the Claude Code CLI backend (claude-agent-sdk).

This is a pure translation layer: it maps engine-neutral ``EngineOptions``
onto ``ClaudeAgentOptions`` and returns a ``ClaudeSDKClient``. Behavior is
identical to constructing the SDK client directly.
"""

import shutil

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from .types import EngineOptions

# ClaudeAgentOptions fields we may forward; None means "not set, keep SDK default"
_OPTION_FIELDS = (
    "model",
    "effort",
    "system_prompt",
    "setting_sources",
    "allowed_tools",
    "disallowed_tools",
    "mcp_servers",
    "hooks",
    "permission_mode",
    "max_turns",
    "cwd",
    "settings",
    "env",
    "max_buffer_size",
    "betas",
)


def find_cli() -> str | None:
    """Locate the system Claude CLI (preferred over the SDK's bundled one,
    which crashes with the Bun runtime on Windows, exit code 3)."""
    return shutil.which("claude")


def create_client(options: EngineOptions) -> ClaudeSDKClient:
    """Build a ClaudeSDKClient from engine-neutral options."""
    kwargs = {}
    for name in _OPTION_FIELDS:
        value = getattr(options, name)
        if value is not None:
            kwargs[name] = value

    system_cli = find_cli()
    if system_cli:
        kwargs["cli_path"] = system_cli

    # Note: the SDK 0.1.x `effort` Literal omits "xhigh" but the CLI accepts
    # it; passing via **kwargs forwards the string unchanged (same behavior
    # as the previous `# type: ignore` call sites).
    return ClaudeSDKClient(options=ClaudeAgentOptions(**kwargs))
