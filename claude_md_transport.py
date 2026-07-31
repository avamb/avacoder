"""
CLAUDE.md chat transport (claude engine only)
=============================================

On the claude engine, chat sessions (assistant, spec) deliver their system
prompts by writing them into the project's CLAUDE.md and loading it via
setting_sources - the workaround for the ~8191 char Windows command-line
limit. That file is ALSO what coding agents load as project instructions, so
a chat prompt left behind ("You must NEVER implement code yourself") poisons
every subsequent coding session - observed to burn entire subscription
windows on agents refusing to write code.

This module makes the transport safe:
- write_chat_claude_md() backs up the original CLAUDE.md and prefixes the
  chat prompt with a sentinel line;
- restore_chat_claude_md() puts the original back (chat close, and
  self-healing at coding-agent startup when a chat session crashed without
  restoring).

The codex engine never touches CLAUDE.md (native system-prompt channel).
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CLAUDE_MD_SENTINEL = "<!-- autoforge-chat-transport: temporary chat prompt; original is backed up -->"

# Safe default when no backup exists: point Claude Code at the AGENTS.md
# conventions file so coding agents still get real project instructions.
_FALLBACK_CONTENT = "@AGENTS.md\n"


def _backup_path(project_dir: Path) -> Path:
    from autoforge_paths import ensure_autoforge_dir

    return ensure_autoforge_dir(project_dir) / "claude_md.orig"


def write_chat_claude_md(project_dir: Path, system_prompt: str) -> Path:
    """Write a chat system prompt into CLAUDE.md, preserving the original."""
    claude_md = project_dir / "CLAUDE.md"
    try:
        if claude_md.exists():
            original = claude_md.read_text(encoding="utf-8", errors="replace")
            if not original.startswith(CLAUDE_MD_SENTINEL):
                _backup_path(project_dir).write_text(original, encoding="utf-8")
    except OSError:
        logger.warning("Could not back up CLAUDE.md", exc_info=True)
    claude_md.write_text(
        CLAUDE_MD_SENTINEL + "\n\n" + system_prompt, encoding="utf-8"
    )
    return claude_md


def restore_chat_claude_md(project_dir: Path, *, context: str = "") -> bool:
    """Restore the original CLAUDE.md if a chat prompt is (still) in it.

    Returns True when a restore happened. Never touches a CLAUDE.md that
    doesn't carry the sentinel (user-authored content stays intact).
    """
    claude_md = project_dir / "CLAUDE.md"
    try:
        if not claude_md.exists():
            return False
        current = claude_md.read_text(encoding="utf-8", errors="replace")
        is_marked = current.startswith(CLAUDE_MD_SENTINEL)
        # Legacy poison from before the sentinel existed
        is_legacy_chat_prompt = current.startswith(
            "You are a helpful project assistant and backlog manager"
        )
        if not (is_marked or is_legacy_chat_prompt):
            return False
        backup = _backup_path(project_dir)
        if backup.exists():
            claude_md.write_text(
                backup.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
            )
        else:
            claude_md.write_text(_FALLBACK_CONTENT, encoding="utf-8")
        if context:
            logger.info("Restored project CLAUDE.md (%s)", context)
        return True
    except OSError:
        logger.warning("Could not restore CLAUDE.md", exc_info=True)
        return False
