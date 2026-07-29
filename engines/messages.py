"""
Engine-Neutral Message Classes
==============================

Plain duck-typed message classes emitted by non-Claude engine adapters
(currently the Codex engine).

AutoForge consumers (``agent.py`` ``run_agent_session`` and the three chat
sessions in ``server/services/``) never use ``isinstance`` against the
claude-agent-sdk types -- they compare ``type(msg).__name__`` and read
duck-typed attributes.  These classes therefore only need to match the SDK
classes by *name* and attribute surface:

    AssistantMessage.content -> list of blocks
    UserMessage.content      -> list of blocks
    TextBlock.text           -> str
    ToolUseBlock.name/.input/.id
    ToolResultBlock.content/.is_error/.tool_use_id

The Claude engine keeps yielding real claude-agent-sdk objects; these are
only used by adapters that translate a foreign event stream.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TextBlock:
    """A block of plain assistant text."""

    text: str


@dataclass
class ToolUseBlock:
    """An assistant tool invocation (Bash command, MCP tool call, edit...)."""

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultBlock:
    """The result of a tool invocation, carried in a UserMessage."""

    tool_use_id: str
    content: Any = None
    is_error: Optional[bool] = None


@dataclass
class AssistantMessage:
    """A message from the assistant: text and/or tool-use blocks."""

    content: list[Any]
    model: str = ""


@dataclass
class UserMessage:
    """A user-role message; used by engines to carry tool results."""

    content: list[Any]


__all__ = [
    "AssistantMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
]
