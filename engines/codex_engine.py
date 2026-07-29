"""
Codex Engine
============

Client adapter for the OpenAI Codex backend (Phase 3 of
docs/ENGINE_ADAPTER_PLAN.md).

Transport: the official ``openai-codex`` Python SDK (PyPI, beta).  It spawns
its own pinned ``codex app-server`` binary (bundled by the
``openai-codex-cli-bin`` wheel -- no PATH lookup or .cmd-shim problems on
Windows) and speaks JSON-RPC over stdio.  Auth is shared with the local
Codex CLI via ``~/.codex/auth.json`` (ChatGPT subscription login,
auto-refreshing).  The previously considered fallback -- spawning
``codex exec --json`` and parsing JSONL -- is NOT implemented; the SDK
transport proved reliable and ships its own binary, which removes the main
Windows risk the fallback existed for.

``CodexClient`` mirrors the runtime protocol of ``ClaudeSDKClient`` that
AutoForge actually uses:

    async with client:            (or manual __aenter__/__aexit__)
        await client.query(prompt)               # str or AsyncIterable[dict]
        async for msg in client.receive_response():
            ...                                  # duck-typed messages
        await client.query(next_prompt)          # re-entrant: same thread

Event mapping (plan section 3.3):

    item/started   commandExecution -> AssistantMessage([ToolUseBlock("Bash")])
    item/completed commandExecution -> UserMessage([ToolResultBlock(...)])
    item/started   mcpToolCall      -> AssistantMessage([ToolUseBlock("mcp__<srv>__<tool>")])
    item/completed mcpToolCall      -> UserMessage([ToolResultBlock(...)])
    item/completed agentMessage     -> AssistantMessage([TextBlock(text)])
    item/completed fileChange       -> AssistantMessage([ToolUseBlock("Edit")]) (display only)
    item/*         reasoning        -> skipped
    turn/completed                  -> generator ends (raises on failed turns)
"""

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, AsyncIterable, AsyncIterator, Optional

from .messages import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from .types import EngineOptions

logger = logging.getLogger(__name__)

# Reasoning effort mapping: AutoForge effort values -> Codex model_reasoning_effort.
# low/medium/high/xhigh map 1:1.  AutoForge "max" maps to "xhigh": the models
# cache lists "max"/"ultra" for gpt-5.6-sol only, so "xhigh" is the safe value
# accepted by every subscription model (plan section 3.4).
_EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}

# Built-in Claude tools whose presence in disallowed_tools signals a
# read-only chat session (assistant / expand).  Codex has no per-tool
# disable for its built-in shell, so this becomes the read-only OS sandbox.
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}

_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class CodexEngineError(RuntimeError):
    """Raised for Codex turn failures.

    For rate limits, ``str(exc)`` deliberately contains the phrases
    ``rate limit`` and (when known) ``retry after N seconds`` so that the
    free-text parsers in ``rate_limit_utils.py`` (``is_rate_limit_error`` /
    ``parse_retry_after``) classify it correctly.
    """


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _codex_auth_file() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home) if codex_home else Path.home() / ".codex"
    return base / "auth.json"


def find_system_codex() -> Optional[str]:
    """Resolve the system Codex CLI executable (informational / fallback).

    On Windows ``shutil.which("codex")`` may return a ``.ps1``/``.cmd`` npm
    shim that ``subprocess`` cannot exec directly; prefer a real ``.exe``.
    """
    found = shutil.which("codex")
    if not found:
        return None
    p = Path(found)
    if os.name == "nt" and p.suffix.lower() in (".ps1", ".cmd", ".bat"):
        exe = shutil.which("codex.exe")
        if exe:
            return exe
        sibling = p.with_suffix(".exe")
        if sibling.exists():
            return str(sibling)
        # No .exe available -- the shim is unusable for direct spawning,
        # but the bundled SDK binary makes this a non-issue.
        return None
    return found


def preflight_check() -> None:
    """Verify a usable Codex runtime + subscription credentials exist.

    Raises RuntimeError with actionable guidance otherwise.
    """
    try:
        from codex_cli_bin import bundled_codex_path  # bundled with openai-codex

        has_runtime = Path(bundled_codex_path()).exists()
    except Exception:
        has_runtime = False

    if not has_runtime and not find_system_codex():
        raise RuntimeError(
            "Codex runtime not found. Install the Python SDK into the AutoForge "
            "environment (pip install openai-codex) or install the Codex CLI "
            "(npm install -g @openai/codex)."
        )

    if not _codex_auth_file().exists():
        raise RuntimeError(
            "Codex is not authenticated: no auth.json found at "
            f"{_codex_auth_file()}. Run: codex login"
        )


# ---------------------------------------------------------------------------
# TOML encoding for `codex --config key=value` overrides
# ---------------------------------------------------------------------------

def _toml_value(value: Any) -> str:
    """Encode a Python value as a TOML literal for a -c/--config override."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # JSON string escaping is a valid TOML basic string
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        parts = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()]
        return "{" + ", ".join(parts) + "}"
    raise TypeError(f"Cannot encode {type(value).__name__} as TOML")


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY.match(key) else json.dumps(key)


def build_mcp_config_overrides(mcp_servers: Optional[dict[str, Any]]) -> list[str]:
    """Translate AutoForge's mcp_servers dict into codex config overrides.

    AutoForge configures stdio MCP servers as
    ``{"features": {"command": ..., "args": [...], "env": {...}}}`` -- the
    same fields Codex uses under ``mcp_servers.<name>.*`` in config.toml.
    Codex surfaces the tools of server ``features`` as items with
    ``server="features"``; the adapter re-emits them under the Claude-style
    name ``mcp__features__<tool>`` so consumers (``allowed_tools`` lists,
    the ask_user interception in assistant_chat_session.py) keep working.
    """
    overrides: list[str] = []
    for name, cfg in (mcp_servers or {}).items():
        if not isinstance(cfg, dict) or "command" not in cfg:
            logger.warning(
                "Codex engine: skipping MCP server '%s' (unsupported config shape "
                "-- only command/args/env stdio servers are translated)", name
            )
            continue
        prefix = f"mcp_servers.{_toml_key(name)}"
        overrides.append(f"{prefix}.command={_toml_value(cfg['command'])}")
        if cfg.get("args"):
            overrides.append(f"{prefix}.args={_toml_value(list(cfg['args']))}")
        if cfg.get("env"):
            overrides.append(f"{prefix}.env={_toml_value(dict(cfg['env']))}")
    return overrides


# ---------------------------------------------------------------------------
# Rate-limit message shaping
# ---------------------------------------------------------------------------

_RETRY_IN_RE = re.compile(
    r"try\s+again\s+in\s+"
    r"(?:(\d+)\s*(?:hours?|hrs?|h)\s*)?"
    r"(?:(\d+)\s*(?:minutes?|mins?|m)\s*)?"
    r"(?:(\d+)\s*(?:seconds?|secs?|s))?",
    re.IGNORECASE,
)


def _extract_retry_seconds(message: str) -> Optional[int]:
    """Best-effort extraction of a retry delay from Codex error text."""
    match = _RETRY_IN_RE.search(message)
    if match and any(match.groups()):
        hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
        total = hours * 3600 + minutes * 60 + seconds
        return total or None
    return None


def _codex_error_kind(codex_error_info: Any) -> Optional[str]:
    """Extract the CodexErrorInfoValue string ('usageLimitExceeded', ...)."""
    if codex_error_info is None:
        return None
    root = getattr(codex_error_info, "root", codex_error_info)
    value = getattr(root, "value", None)
    if isinstance(value, str):
        return value
    return None


def _turn_error_to_exception(error: Any) -> CodexEngineError:
    """Map a Codex TurnError to an exception with parseable text.

    Rate-limit shapes get "rate limit" plus "retry after N seconds" (when
    derivable) in str(exc) so rate_limit_utils regexes match (plan sec. 5.6).
    """
    message = getattr(error, "message", None) or "Codex turn failed"
    details = getattr(error, "additional_details", None)
    if details:
        message = f"{message} ({details})"
    kind = _codex_error_kind(getattr(error, "codex_error_info", None))

    if kind == "usageLimitExceeded":
        retry = _extract_retry_seconds(message)
        text = f"Codex rate limit exceeded (usage limit): {message}"
        if retry:
            text += f". retry after {retry} seconds"
        return CodexEngineError(text)
    if kind == "serverOverloaded":
        return CodexEngineError(f"Codex server overloaded (rate limit): {message}")
    if kind == "unauthorized":
        return CodexEngineError(
            f"Codex authentication failed: {message}. Run: codex login"
        )
    if kind == "contextWindowExceeded":
        return CodexEngineError(f"Codex context window exceeded: {message}")
    return CodexEngineError(message)


def _translate_sdk_error(exc: Exception) -> Exception:
    """Reshape SDK transport errors so downstream text parsing works."""
    try:
        from openai_codex.errors import ServerBusyError
    except ImportError:  # pragma: no cover
        return exc
    if isinstance(exc, ServerBusyError):
        return CodexEngineError(f"Codex server overloaded (rate limit): {exc}")
    return exc


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CodexClient:
    """Codex-backed engine client with the ClaudeSDKClient runtime protocol.

    EngineOptions mapping:
        model            -> thread model (Codex slug, e.g. "gpt-5.6-sol")
        effort           -> per-turn reasoning effort (see _EFFORT_MAP)
        system_prompt    -> thread developerInstructions (native support --
                            no prompt prepending needed)
        cwd              -> app-server process cwd + thread cwd
        mcp_servers      -> `--config mcp_servers.*` overrides on the
                            app-server process (build_mcp_config_overrides)
        permission_mode /
        disallowed_tools -> sandbox: read-only when write tools are
                            disallowed or permission_mode=="plan",
                            workspace-write otherwise; approvals are always
                            "never" (headless; ApprovalMode.deny_all)
        max_turns        -> adapter-enforced turn cap (Codex has no native
                            max-turns): each Bash/MCP tool call counts as a
                            turn; on overflow the turn is interrupted
        env              -> merged into the app-server subprocess env
        allowed_tools    -> IGNORED: Codex built-in tools cannot be
                            allowlisted per-tool; containment relies on the
                            OS sandbox instead (plan section 3.4)
        setting_sources  -> IGNORED: replaced by codex
                            project_doc_fallback_filenames config so the
                            project CLAUDE.md is still auto-loaded
        settings         -> IGNORED: Claude settings JSON (sandbox flags,
                            statusline...) has no Codex equivalent; sandbox
                            is mapped explicitly above
        hooks            -> IGNORED: no PreToolUse/PreCompact hook system in
                            Codex; bash allowlisting is replaced by the OS
                            sandbox, compaction is automatic (plan sec. 5.2)
        max_buffer_size  -> IGNORED: the JSON-RPC transport has no
                            configurable stdout buffer limit
        betas            -> IGNORED: Anthropic-specific beta flags
    """

    def __init__(self, options: EngineOptions):
        self.options = options
        self._codex: Any = None  # AsyncCodex
        self._thread: Any = None  # AsyncThread
        self._turn_handle: Any = None  # AsyncTurnHandle
        self._turn_stream: Optional[AsyncIterator[Any]] = None
        self._entered = False

    # -- context management -------------------------------------------------

    async def __aenter__(self) -> "CodexClient":
        from openai_codex import AsyncCodex, CodexConfig

        opts = self.options
        overrides = build_mcp_config_overrides(opts.mcp_servers)
        # Honor the project CLAUDE.md that the chat sessions write their
        # system prompts into: codex auto-loads AGENTS.md and falls back to
        # CLAUDE.md via this documented config key (plan section 3.4).
        overrides.append(
            'project_doc_fallback_filenames=["AGENTS.md", "CLAUDE.md"]'
        )

        # Coding agents (workspace-write) need what the Claude-path sandbox
        # allows: network (package installs, docker) and git commits. Codex's
        # workspace-write keeps <cwd>/.git read-only by default, which made
        # agents skip features with "cannot create .git/index.lock"; listing
        # the .git dir itself as a writable root lifts that protection.
        if self._resolve_sandbox().value == "workspace-write":
            overrides.append("sandbox_workspace_write.network_access=true")
            if opts.cwd:
                git_dir = str(Path(opts.cwd).resolve() / ".git")
                overrides.append(
                    f"sandbox_workspace_write.writable_roots=[{_toml_value(git_dir)}]"
                )

        env: Optional[dict[str, str]] = None
        if opts.env:
            env = {k: v for k, v in opts.env.items()}

        config = CodexConfig(
            config_overrides=tuple(overrides),
            cwd=opts.cwd,
            env=env,
        )
        self._codex = AsyncCodex(config=config)
        self._install_approval_handler()
        try:
            await self._codex.__aenter__()
            self._thread = await self._start_thread()
        except Exception as exc:
            await self._codex.close()
            self._codex = None
            raise _translate_sdk_error(exc) from exc
        self._entered = True
        return self

    def _install_approval_handler(self) -> None:
        """Accept every escalated approval request (headless operation).

        The SDK's default handler accepts only commandExecution/fileChange
        approvals and silently REJECTS everything else - notably MCP tool
        calls, which surface to the model as "user rejected MCP tool call".
        AutoForge intends its MCP feature tools to run unattended (the Claude
        path pre-approves them in the permissions allow-list), and shell/file
        containment comes from the OS sandbox, so accepting here is safe.

        Reaches through private attributes (AsyncCodex._client._sync); the
        openai-codex dependency is pinned <0.145.0 precisely because of this.
        """

        def _accept_all(method: str, params: Any) -> dict:
            if method.startswith("item/") and method.endswith("/requestApproval"):
                return {"decision": "accept"}
            if method == "mcpServer/elicitation/request":
                # Codex wraps MCP tool-call consent in an MCP elicitation:
                # message "Allow the <name> MCP server to run tool ...?" with
                # _meta.codex_approval_kind == "mcp_tool_call" and an empty
                # form schema. Accept those; decline genuine elicitations
                # (a server asking the user for data) -- headless, nobody to ask.
                meta = (params or {}).get("_meta") or {}
                if meta.get("codex_approval_kind") == "mcp_tool_call":
                    return {"action": "accept", "content": {}}
                logger.warning(
                    "Codex engine: declining non-approval MCP elicitation from "
                    "server %r (headless)", (params or {}).get("serverName")
                )
                return {"action": "decline"}
            logger.warning(
                "Codex engine: unhandled app-server request %s (empty reply)", method
            )
            return {}

        try:
            self._codex._client._sync._approval_handler = _accept_all  # noqa: SLF001
        except AttributeError:
            logger.warning(
                "Codex engine: could not install approval handler (SDK layout "
                "changed?); MCP tool approvals may be auto-rejected"
            )

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._turn_stream = None
        self._turn_handle = None
        self._thread = None
        self._entered = False
        if self._codex is not None:
            codex, self._codex = self._codex, None
            await codex.close()

    async def _start_thread(self) -> Any:
        from openai_codex import ApprovalMode

        opts = self.options
        return await self._codex.thread_start(
            model=opts.model or None,
            cwd=opts.cwd,
            sandbox=self._resolve_sandbox(),
            approval_mode=ApprovalMode.deny_all,  # headless: never ask
            developer_instructions=opts.system_prompt or None,
            # Ephemeral: AutoForge sessions are fresh-per-iteration with
            # SQLite as shared state; don't pollute ~/.codex thread storage.
            ephemeral=True,
        )

    def _resolve_sandbox(self) -> Any:
        from openai_codex import Sandbox

        opts = self.options
        disallowed = set(opts.disallowed_tools or [])
        if disallowed & _WRITE_TOOLS or opts.permission_mode == "plan":
            return Sandbox.read_only

        # Coding-agent sessions. On Windows the Claude-path sandbox is not
        # enforced either (Claude Code sandboxing is a macOS/Linux feature),
        # and Codex's workspace-write blocks the Docker named pipe with no
        # config knob - which breaks backend verification (PostgreSQL via
        # docker compose) and made agents skip features. Default to full
        # access on Windows for engine parity; POSIX keeps workspace-write.
        # Override with AUTOFORGE_CODEX_SANDBOX=workspace-write|full-access.
        override = os.environ.get("AUTOFORGE_CODEX_SANDBOX", "").strip().lower()
        if override == "workspace-write":
            return Sandbox.workspace_write
        if override in ("full-access", "danger-full-access"):
            return Sandbox.full_access
        if os.name == "nt":
            return Sandbox.full_access
        return Sandbox.workspace_write

    def _resolve_effort(self) -> Optional[Any]:
        effort = self.options.effort
        if not effort:
            return None
        from openai_codex.generated.v2_all import ReasoningEffort

        return ReasoningEffort(_EFFORT_MAP.get(effort, effort))

    # -- query --------------------------------------------------------------

    async def query(self, prompt: Any) -> None:
        """Start a turn. Accepts a plain string or the multimodal
        AsyncIterable[dict] envelope produced by make_multimodal_message()."""
        if not self._entered:
            raise RuntimeError("CodexClient used before __aenter__")

        await self._abort_active_turn()

        if isinstance(prompt, str):
            input_items: Any = prompt
        else:
            input_items = await self._envelope_to_input(prompt)

        try:
            self._turn_handle = await self._thread.turn(
                input_items,
                effort=self._resolve_effort(),
            )
        except Exception as exc:
            raise _translate_sdk_error(exc) from exc
        self._turn_stream = self._turn_handle.stream()

    async def _envelope_to_input(self, prompt: AsyncIterable[dict]) -> list[Any]:
        """Translate the Claude SDK multimodal message envelope to Codex input.

        Text blocks become TextInput; base64 image blocks become data-URL
        ImageInput (Codex supports image turn input).  Unsupported image
        shapes degrade to a "[image attached]" note instead of crashing.
        """
        from openai_codex import ImageInput, TextInput

        items: list[Any] = []
        async for message in prompt:
            content = (message or {}).get("message", {}).get("content", [])
            if isinstance(content, str):
                items.append(TextInput(text=content))
                continue
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    items.append(TextInput(text=block.get("text", "")))
                elif btype == "image":
                    source = block.get("source") or {}
                    if source.get("type") == "base64" and source.get("data"):
                        media = source.get("media_type", "image/png")
                        items.append(
                            ImageInput(url=f"data:{media};base64,{source['data']}")
                        )
                    else:
                        logger.warning(
                            "Codex engine: unsupported image block shape %r; "
                            "substituting a text note", source.get("type")
                        )
                        items.append(TextInput(text="[image attached]"))
                else:
                    logger.warning(
                        "Codex engine: ignoring unsupported content block type %r",
                        btype,
                    )
        if not items:
            items.append(TextInput(text=""))
        return items

    async def _abort_active_turn(self) -> None:
        """Interrupt a still-active turn before starting a new one.

        Consumers normally drain receive_response() fully, but error paths
        may abandon the stream; without this a second query() would fail
        with an active-turn error from the app-server.
        """
        stream, self._turn_stream = self._turn_stream, None
        handle, self._turn_handle = self._turn_handle, None
        if stream is not None:
            try:
                await stream.aclose()
            except Exception:  # noqa: BLE001
                pass
        if handle is not None:
            try:
                await handle.interrupt()
            except Exception:  # noqa: BLE001
                # Turn already finished -- nothing to interrupt.
                pass

    async def interrupt(self) -> None:
        """Interrupt the active turn (parity with ClaudeSDKClient)."""
        if self._turn_handle is not None:
            try:
                await self._turn_handle.interrupt()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Codex interrupt failed: %s", exc)

    # -- receive ------------------------------------------------------------

    async def receive_response(self) -> AsyncIterator[Any]:
        """Yield duck-typed messages for the active turn, ending at
        end-of-turn.  Re-entrant: after the generator finishes, query()
        may be called again on the same client/thread."""
        stream = self._turn_stream
        if stream is None:
            raise RuntimeError("receive_response() called with no active query()")

        max_turns = self.options.max_turns
        turn_count = 0
        interrupt_sent = False
        turn_error: Any = None

        try:
            while True:
                try:
                    event = await stream.__anext__()
                except StopAsyncIteration:
                    break
                except Exception as exc:  # transport / JSON-RPC failure
                    raise _translate_sdk_error(exc) from exc

                method = event.method
                payload = event.payload

                if method == "error":
                    # ErrorNotification: remember it; a failed turn/completed
                    # follows unless willRetry is set.
                    turn_error = getattr(payload, "error", None)
                    if getattr(payload, "will_retry", False):
                        logger.info(
                            "Codex transient error (will retry): %s",
                            getattr(turn_error, "message", turn_error),
                        )
                        turn_error = None
                    continue

                if method == "turn/completed":
                    turn = payload.turn
                    status = getattr(turn.status, "value", str(turn.status))
                    if status == "failed":
                        raise _turn_error_to_exception(turn.error or turn_error)
                    # completed / interrupted -> normal end of stream
                    return

                message = self._map_item_event(method, payload)
                if message is None:
                    continue

                if isinstance(message, AssistantMessage) and any(
                    isinstance(b, ToolUseBlock) for b in message.content
                ):
                    turn_count += 1

                yield message

                # Adapter-enforced turn cap (Codex has no native max_turns).
                if (
                    max_turns is not None
                    and turn_count >= max_turns
                    and not interrupt_sent
                ):
                    interrupt_sent = True
                    logger.warning(
                        "Codex engine: max_turns=%s reached; interrupting turn",
                        max_turns,
                    )
                    try:
                        await self._turn_handle.interrupt()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Codex max-turns interrupt failed: %s", exc)
        finally:
            # The turn is over (or the consumer abandoned us): drop stream
            # state so the next query() starts clean.
            self._turn_stream = None
            self._turn_handle = None
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:  # noqa: BLE001
                    pass

    # -- event mapping ------------------------------------------------------

    def _map_item_event(self, method: str, payload: Any) -> Optional[Any]:
        """Translate an item/* notification into a duck-typed message."""
        if method not in ("item/started", "item/completed"):
            return None
        item = getattr(payload, "item", None)
        item = getattr(item, "root", item)  # unwrap pydantic RootModel
        if item is None:
            return None
        item_type = getattr(item, "type", None)
        started = method == "item/started"

        if item_type == "commandExecution":
            if started:
                return AssistantMessage(
                    content=[
                        ToolUseBlock(
                            id=item.id,
                            name="Bash",
                            input={"command": item.command},
                        )
                    ]
                )
            exit_code = getattr(item, "exit_code", None)
            return UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id=item.id,
                        content=getattr(item, "aggregated_output", None) or "",
                        is_error=exit_code is not None and exit_code != 0,
                    )
                ]
            )

        if item_type == "mcpToolCall":
            name = f"mcp__{item.server}__{item.tool}"
            if started:
                arguments = item.arguments
                if not isinstance(arguments, dict):
                    arguments = {"arguments": arguments} if arguments else {}
                return AssistantMessage(
                    content=[ToolUseBlock(id=item.id, name=name, input=arguments)]
                )
            status = getattr(item.status, "value", str(item.status))
            is_error = status == "failed" or item.error is not None
            if item.error is not None:
                content: Any = getattr(item.error, "message", str(item.error))
            else:
                content = _mcp_result_text(item.result)
            return UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id=item.id, content=content, is_error=is_error
                    )
                ]
            )

        if item_type == "agentMessage":
            if started:
                return None  # only the completed item carries full text
            return AssistantMessage(content=[TextBlock(text=item.text)])

        if item_type == "fileChange":
            if started:
                return None
            # Display-only mapping: Codex applies patches itself; surface a
            # summary under the familiar "Edit" tool name (plan sec. 3.3).
            changes = []
            for change in getattr(item, "changes", []) or []:
                changes.append(
                    {
                        "file_path": getattr(change, "path", ""),
                        "kind": _patch_kind_name(getattr(change, "kind", None)),
                    }
                )
            return AssistantMessage(
                content=[
                    ToolUseBlock(id=item.id, name="Edit", input={"changes": changes})
                ]
            )

        if item_type == "webSearch":
            if started:
                return AssistantMessage(
                    content=[
                        ToolUseBlock(
                            id=item.id,
                            name="WebSearch",
                            input={"query": getattr(item, "query", "")},
                        )
                    ]
                )
            return None

        # reasoning, plan, imageView, contextCompaction, ... -> skipped
        return None


def _mcp_result_text(result: Any) -> str:
    """Flatten an MCP tool-call result into text (mirrors what the Claude
    CLI puts into ToolResultBlock.content for MCP tools)."""
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if not content:
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            try:
                return json.dumps(structured)
            except (TypeError, ValueError):
                return str(structured)
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(json.dumps(block, default=str))
        else:
            text = getattr(block, "text", None)
            parts.append(str(text) if text is not None else str(block))
    return "\n".join(parts)


def _patch_kind_name(kind: Any) -> str:
    """Best-effort readable name for a Codex PatchChangeKind."""
    kind = getattr(kind, "root", kind)
    if kind is None:
        return "update"
    for attr in ("add", "update", "delete"):
        if getattr(kind, attr, None) is not None:
            return attr
    value = getattr(kind, "type", None) or getattr(kind, "value", None)
    return str(value) if value else "update"


def create_client(options: EngineOptions) -> CodexClient:
    """Build a CodexClient from engine-neutral options (auth preflight
    included -- raises RuntimeError with actionable guidance)."""
    preflight_check()
    return CodexClient(options)
