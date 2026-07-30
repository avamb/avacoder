# Multi-Engine Adapter Plan: Codex + Kimi Support

> **TL;DR (RU):** План добавления в AutoForge поддержки Codex (по подписке ChatGPT, через
> официальный Python SDK `openai-codex`) и обновления провайдера Kimi до актуальной
> подписки Kimi Code (модели K3). Весь существующий интерфейс сохраняется: провайдер и
> модель выбираются в Settings UI, список приходит с бэкенда. Kimi — быстрая правка
> реестра провайдеров (Anthropic-совместимый endpoint, движок не нужен). Codex — новый
> слой «engine» с адаптером, повторяющим интерфейс claude-agent-sdk.

**Status:** planned · **Date:** 2026-07-29 · **Base commit:** `427c228`
**Fork:** https://github.com/avamb/avacoder (origin; upstream = AutoForgeAI/autoforge)
**Verified against:** codex-cli 0.145.0 (installed, authenticated), models cache of 2026-07-29

---

## 1. Goal

Keep the entire AutoForge UI and workflow (projects, spec chat, assistant, expand chat,
parallel agents, scheduler) while allowing the user to select, in Settings → API Provider:

- **Claude (Anthropic)** — unchanged, default.
- **Codex (ChatGPT subscription)** — new engine driving the local `codex` CLI/app-server.
  Models: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`
  (from the local CLI models cache; refresh at implementation time).
- **Kimi Code (subscription)** — updated existing `kimi` provider. Models: `k3`, `k3-256k`,
  `kimi-for-coding`, `kimi-for-coding-highspeed`. Anthropic-compatible endpoint
  `https://api.kimi.com/coding/` → runs through the existing Claude engine path unchanged.
- Existing GLM / Ollama / Azure / Custom — untouched.

Additional goal: **complexity-based agent routing** — when the spec assistant / initializer
breaks a project into features, each feature gets a complexity rating, and the orchestrator
picks the model (and eventually engine) per feature from user-configurable routing rules
(e.g. simple → `gpt-5.4-mini` / `gpt-5.6-luna`, complex → `gpt-5.6-sol` / Opus). See §7a.

Non-goal (for now): fully independent engines per agent role in one run. The engine is a
global setting, same as today's provider; complexity routing first varies the **model**
within the active provider. (See §9 "Later" for the cross-engine extension point.)

## 2. Key facts the plan relies on

From codebase mapping (file:line refs are at base commit `427c228`):

1. The frontend hardcodes **no** provider/model lists — it renders `GET /api/settings/providers`
   (source of truth: `API_PROVIDERS` in `registry.py:777-838`). UI changes are cosmetic.
2. Only 4 files import claude-agent-sdk: `client.py`, `agent.py` (client type only), and the
   three chat sessions in `server/services/`. The runtime API surface actually used is tiny:
   `ClaudeSDKClient(options)`, `async with`, `query(str | AsyncIterable[dict])`,
   `receive_response()`. Message objects are consumed by **class name** (`type(x).__name__`)
   and duck-typed attributes — never `isinstance`. An adapter can emit its own classes named
   `AssistantMessage`, `TextBlock`, `ToolUseBlock`, `UserMessage`, `ToolResultBlock`.
3. Provider identity is currently erased into a flat env dict by
   `registry.get_effective_sdk_env()` (`registry.py:841-914`); downstream code re-infers the
   provider by substring-matching `ANTHROPIC_BASE_URL` (`client.py:351-354`). There is **no
   engine concept** — this plan introduces one.
4. The feature-registry MCP server (`mcp_server/feature_mcp.py`, FastMCP, stdio, 19 tools,
   `mcp__features__*`) is standard MCP and portable to Codex as-is.
5. Codex CLI 0.145.0 provides: `codex exec --json` (JSONL events), `exec resume`,
   `app-server` (JSON-RPC over stdio; `thread/start`, `turn/start`, `turn/interrupt`,
   streaming deltas), MCP config (`mcp_servers.*` in config.toml, overridable per-invocation
   via `-c`), sandbox modes (`read-only`/`workspace-write`/`danger-full-access`), approval
   policies, `AGENTS.md` + `project_doc_fallback_filenames`, `model_instructions_file`,
   `-C` cwd, `--output-schema`. **No built-in max-turns** — must be enforced by the adapter.
6. **Official Python SDK exists: `openai-codex` (PyPI, beta, Apache-2.0)** — wraps app-server,
   reuses `~/.codex/auth.json` (ChatGPT subscription auth, auto-refreshing, headless-safe).
   Primary integration point; `codex exec --json` subprocess is the fallback if the beta SDK
   misbehaves.
7. Kimi Code subscription: Anthropic-compatible `https://api.kimi.com/coding/`, API keys from
   the Kimi Code Console, model strings per their Claude Code guide (`k3`, optionally
   `k3[1m]` for the 1M window — verify exact syntax when implementing). A leftover
   `ANTHROPIC_AUTH_TOKEN` conflicts with `ANTHROPIC_API_KEY` — the existing cross-clearing
   in `registry.py:882-885` already handles this.

## 3. Architecture: the `engines/` package

New top-level package `engines/`:

```
engines/
  __init__.py        # get_engine(provider) -> EngineFactory
  types.py           # message classes + EngineOptions dataclass
  claude_engine.py   # wraps existing ClaudeSDKClient construction (moves from client.py)
  codex_engine.py    # CodexClient: same interface, backed by openai-codex SDK
```

### 3.1 `engines/types.py`

- `EngineOptions` dataclass mirroring the used subset of `ClaudeAgentOptions`:
  `model, effort, system_prompt, allowed_tools, disallowed_tools, mcp_servers, hooks,
  permission_mode, max_turns, cwd, settings, env, setting_sources, max_buffer_size, betas`.
- Message classes for the Codex adapter, named exactly `AssistantMessage`, `TextBlock`,
  `ToolUseBlock`, `UserMessage`, `ToolResultBlock` (duck-typed attribute parity:
  `.content`, `.text`, `.name`, `.input`, `.id`, `.is_error`, `.tool_use_id`).
  The Claude engine keeps yielding real SDK objects — consumers can't tell the difference
  because they compare `type(x).__name__`.
- `EngineRateLimitError(retry_after_seconds: int | None)` — structured rate-limit signal
  (see §5.6).

### 3.2 `engines/claude_engine.py`

Pure refactor: move the `ClaudeSDKClient(ClaudeAgentOptions(...))` construction out of
`client.py:454-501` and the three chat sessions into one factory that takes `EngineOptions`.
Behavior byte-for-byte identical (hooks, settings JSON, betas gate, `cli_path=which("claude")`).

### 3.3 `engines/codex_engine.py`

`CodexClient` implementing the same protocol:

- `__aenter__`: start `openai-codex` `Codex()` context + `thread_start(model=..., cwd=...,
  sandbox=..., approval_policy="never")`. Store `thread_id`.
- `query(prompt)`: begin a turn (`thread.run_streamed(prompt)`); the `AsyncIterable[dict]`
  multimodal form maps text blocks to prompt text and base64 image blocks to SDK image
  inputs (needed by spec/expand sessions).
- `receive_response()`: async generator translating Codex events → duck-typed messages:

  | Codex event | Emitted message |
  |---|---|
  | `item.completed: agent_message` | `AssistantMessage([TextBlock(text)])` |
  | `item.started: command_execution` | `AssistantMessage([ToolUseBlock(name="Bash", input={"command": ...})])` |
  | `item.completed: command_execution` | `UserMessage([ToolResultBlock(content=output, is_error=exit!=0)])` |
  | `item.*: mcp_tool_call` | `ToolUseBlock(name="mcp__features__<tool>") / ToolResultBlock` |
  | `item.completed: file_changes` | `ToolUseBlock(name="Edit", input={...})` (display only) |
  | `item.completed: reasoning` | skipped (or TextBlock behind a verbose flag) |
  | `turn.completed` | generator ends (mirrors `receive_response` end-of-turn) |
  | `turn.failed` / `error` | raise; rate-limit shapes → `EngineRateLimitError` |

- Turn cap: count model turns in the adapter; at `options.max_turns` call `turn/interrupt`
  and end the generator (compensates for Codex's missing max-turns).
- MCP: translate `options.mcp_servers` (command/args/env dicts) into thread config /
  `-c mcp_servers.features.*` overrides. Tool names must surface as `mcp__features__*`;
  if the SDK exposes different naming, add a name-mapping shim here so `allowed_tools`
  lists and the `ask_user` interception in `assistant_chat_session.py:443-450` keep working.
- Fallback transport (feature flag `CODEX_TRANSPORT=exec`): spawn
  `codex exec --json -C <cwd> -m <model> -s <sandbox> -c ...` and parse JSONL; resume via
  `codex exec resume <thread_id>`. Same event mapping.

### 3.4 Feature mapping (Claude Code concept → Codex)

| Claude Code mechanism | Codex replacement |
|---|---|
| settings JSON `sandbox.enabled` + permission modes | `sandbox_mode`: coding agent → `workspace-write`; assistant/read-only chats → `read-only`; `approval_policy="never"` everywhere (headless) |
| PreToolUse bash allowlist hook (`security.py`) | **Not portable as a hook.** Primary containment = Codex OS sandbox (`workspace-write`, `network_access` on, writable roots = project dir). Optionally generate execpolicy `.rules` from `allowed_commands.yaml` later. Document as a known behavioral difference. |
| PreCompact hook custom compaction guidance | Codex compacts automatically; no custom-instruction hook. Accept default compaction (the workflow state lives in SQLite + files, so losses are tolerable). |
| `setting_sources=["project"]` loading CLAUDE.md | `AGENTS.md` auto-load + `-c project_doc_fallback_filenames=["AGENTS.md","CLAUDE.md"]` so the existing "write system prompt to CLAUDE.md" transport (`assistant_chat_session.py:284-287`, `spec_chat_session.py:140-143`) works without changes. Alternatively write AGENTS.md alongside. |
| `.claude/skills/playwright-cli` skill | Inline the skill's instruction content into the coding/testing prompt templates for the Codex path (prompts.py already owns template selection), or ship as `~/.codex/skills` if the local Codex skills format proves compatible (verify — `~/.codex/skills` exists in 0.145.0). The `playwright-cli` binary itself is engine-neutral. |
| `system_prompt` option | `model_instructions_file` (full replacement) — but prefer AGENTS.md; the current `system_prompt` is one generic sentence. |
| `allowed_tools` / `disallowed_tools` | MCP: `enabled_tools`/`disabled_tools` per server in config. Built-in tools: Codex has shell/apply_patch/web_search — read-only chats rely on `sandbox=read-only` rather than tool removal. |
| `effort` (low/medium/high/xhigh/max) | `model_reasoning_effort`; map 1:1 for low/medium/high/xhigh; AutoForge `max` → Codex `xhigh` (CLI-safe) — models cache lists `max`/`ultra` for gpt-5.6-sol; verify acceptance and widen the map if so. |
| `betas=["context-1m-..."]` | N/A — omit. |
| auth error regexes (`auth.py`) | Add Codex patterns (`401`, `login`, `auth.json` refresh failures) + preflight check: `codex` on PATH and `~/.codex/auth.json` exists. |

## 4. Provider registry & settings plumbing

### 4.1 `registry.py`

- Extend `API_PROVIDERS` entries with two new fields (defaults keep old behavior):
  - `engine: "claude" | "codex"` (default `"claude"`)
  - `auth_mode: "none" | "token" | "subscription"` (default derived from `requires_auth`)
- Add provider:
  ```python
  "codex": {
      "name": "Codex (ChatGPT subscription)",
      "engine": "codex",
      "auth_mode": "subscription",       # no token field in UI; login via `codex login`
      "base_url": None,
      "requires_auth": False,
      "models": [gpt-5.6-sol / terra / luna, gpt-5.5, gpt-5.4, gpt-5.4-mini],
      "default_model": "gpt-5.6-sol",
  }
  ```
- Update `kimi`: name "Kimi Code (Moonshot)", models `k3` (default), `k3-256k`,
  `kimi-for-coding`, `kimi-for-coding-highspeed`; keep `base_url` and
  `auth_env_var="ANTHROPIC_API_KEY"`. Engine stays `"claude"` — no other changes needed.
- New function `get_effective_engine_config() -> EngineConfig` returning
  `{engine, provider_id, model, effort, env, base_url, auth}`; implement
  `get_effective_sdk_env()` as a thin wrapper over it for backward compatibility.
  For `engine="codex"` the env dict carries no `ANTHROPIC_*` fan-out; model/effort travel
  as explicit fields (and as `AUTOFORGE_ENGINE`, `AUTOFORGE_ENGINE_MODEL` env vars across
  the subprocess boundary — see 4.3).
- Make the unknown-provider fallback (`registry.py:864-873`) an explicit error instead of
  silently degrading to Claude.

### 4.2 `server/` changes

- `schemas.py`:
  - `validate_api_base_url` (`:514-521`): allow `None`/empty when the provider's engine
    is not URL-based (codex). Simplest: skip validation when value is empty.
  - `ProviderInfo` (`:461-468`): add `engine`, `auth_mode` fields; serialize in
    `settings.py:42-56` so the UI can render the right affordances.
  - Add an `api_provider` validator against `API_PROVIDERS.keys()` (currently any string
    is accepted).
  - Effort `Literal` stays as-is (values map cleanly).
- `server/routers/settings.py`: provider-change auto-defaults (`:158-166`) must also clear
  `api_base_url`/`api_auth_token` when switching to a subscription provider.
- `server/main.py` `GET /api/setup/status` (`:216-241`): make credential detection
  engine-aware — for codex: `shutil.which("codex")` + `~/.codex/auth.json` exists; keep
  claude checks for claude-engine providers; fix `glm_configured` to also read the DB, not
  only process env.
- `chat_constants.py:70-96` init-error guidance: branch per engine.

### 4.3 Engine config across the subprocess chain

`get_effective_engine_config()` is derived from the DB, and every process
(`process_manager` → `autonomous_agent_demo.py` → `agent.py`/`client.py`) can call it
directly — same pattern as today's triple call of `get_effective_sdk_env()`. No new IPC is
needed. The only addition: `process_manager.py:475-483` and `parallel_orchestrator.py`
spawn sites keep injecting env as today; for codex nothing Anthropic-shaped is injected.

### 4.4 `client.py` becomes engine-dispatching

`create_client(project_dir, model, yolo_mode, agent_type)` keeps its signature (so
`agent.py:270` is untouched) but:
1. builds `EngineOptions` from the existing per-agent-type config
   (tools/max_turns/mcp/hooks/settings-JSON for claude; sandbox/mcp for codex);
2. dispatches on `get_effective_engine_config().engine` to
   `claude_engine.create(...)` or `codex_engine.create(...)`.

Same dispatch in the three chat sessions (each currently hand-rolls its own
`ClaudeSDKClient` construction — route them through the factory too, which also
de-duplicates ~3 copies of `shutil.which("claude")` + model resolution from
`ANTHROPIC_DEFAULT_OPUS_MODEL`; the factory resolves model from `EngineConfig.model`).

## 5. Behavioral gaps to handle explicitly

1. **max_turns** — adapter-enforced turn counting + `turn/interrupt` (§3.3).
2. **Bash allowlist hook** — replaced by OS sandbox; note in README that yolo_mode /
   security semantics differ on Codex (§3.4). `security.py` remains active for Claude.
3. **Completion detection** (`agent.py:303`) matches English phrases
   ("all features are passing" / "no more work to do"). Keep it (engine-independent), but
   add the phrases to the Codex prompt templates' final-report instruction so GPT models
   reliably emit them; the authoritative signal remains the SQLite pass-count poll.
4. **MessageParseError retry loops** (`agent.py:78-136`, `chat_constants.py:99-124`) —
   Codex adapter never raises it; loops become dead code on that path. Fine.
5. **`ask_user` interception** in assistant chat keys on `ToolUseBlock.name ==
   "mcp__features__ask_user"` — preserved by the MCP name-mapping shim (§3.3).
6. **Rate limits**: `rate_limit_utils.py` regexes are Claude-phrased. Adapter raises
   `EngineRateLimitError` with `retry_after` from Codex's error payload (subscription
   limits are 5-hour rolling windows); teach `is_rate_limit_error()` /
   `parse_retry_after()` to recognize the structured error first, regexes second.
7. **Windows**: spawn `codex` via its real executable path (`shutil.which("codex")` may
   return a `.ps1`/`.cmd` shim — resolve to the `.exe`); `CREATE_NO_WINDOW` flags already
   used at spawn sites apply to the adapter's fallback transport too.
8. **Settings value cap** VARCHAR(500) (`registry.py:142`) — fine (no long tokens for
   codex; Kimi keys are short), but note for future OAuth blobs.

## 6. UI changes (all cosmetic; both files of the duplicated pair or after de-duplication)

1. **Delete dead `ui/src/components/SettingsModal.tsx`** (unreferenced duplicate of
   SettingsView) — or every change below must be made twice.
2. `SettingsView.tsx:21-27` `PROVIDER_INFO_TEXT`: add `codex` ("Uses your ChatGPT
   subscription via the local Codex CLI. Run `codex login` once."), update `kimi`
   (Kimi Code subscription, console.kimi.com API key), add missing `azure`.
3. Model selector (`SettingsView.tsx:360-402`): convert the single-row button strip to a
   dropdown or wrapped grid — 6 Codex models won't fit.
4. Auth affordance: when `provider.auth_mode === "subscription"`, hide the token input and
   show login status + hint (from new `setup/status` fields) instead.
5. Effort selector (`:430-452`): reword "How deeply Claude thinks" → provider-neutral;
   hide for providers whose engine ignores it (kimi passes through; codex maps; ollama —
   hide).
6. `SetupWizard.tsx` + `ProjectSetupRequired.tsx`: engine-aware checks/labels
   ("Codex CLI" / "ChatGPT login" when engine=codex).
7. Generic copy: `TypingIndicator.tsx:25` "Claude is thinking…" → "Assistant is thinking…"
   (or provider name); `SpecCreationChat.tsx:329` / `ExpandProjectChat.tsx:266`
   "Connecting to Claude…" → neutral; `NewProjectModal.tsx` "Create with Claude" →
   "Create with AI".
8. `ScheduleModal.tsx:374-383`: replace free-text model input + stale placeholder with a
   dropdown fed by `useAvailableModels()`.
9. `useProjects.ts:289-318` placeholder defaults: leave (pre-fetch flash only), optionally
   sync later.

## 7. Phases & deliverables

**Phase 0 — Fork infrastructure (½ day)**
Fork on GitHub, `git remote add upstream`, branch `feature/multi-engine`. Local dev run
from source (`npm link` + venv from requirements.txt), smoke-test baseline with Claude.

**Phase 1 — Kimi Code refresh (½–1 day, independent quick win)**
`API_PROVIDERS["kimi"]` model list + name; UI info text; model-selector dropdown
conversion (§6.3); verify against a real Kimi Code API key end-to-end (spec chat + one
feature build). No engine work.

**Phase 2 — Engine abstraction, no behavior change (1–2 days)**
Create `engines/` with `types.py` + `claude_engine.py`; route `client.py` and the three
chat sessions through the factory; add `get_effective_engine_config()`;
`ProviderInfo.engine/auth_mode`; schema/validator updates. All existing providers must
work exactly as before (regression: run a small project on Claude + one alt provider).

**Phase 3 — Codex engine for the autonomous agents (3–5 days, core)**
`codex_engine.py` on `openai-codex` SDK (exec-JSONL fallback behind a flag); MCP wiring +
name shim; sandbox/approval mapping; AGENTS.md/CLAUDE.md doc-fallback config; playwright
skill content inlined into Codex-path prompts; turn cap; rate-limit mapping; auth
preflight + `auth.py` patterns; setup-status engine awareness.
Acceptance: full autonomous run (initializer → coding → testing agents, parallel mode)
of an example project on `gpt-5.6-sol` with features marked passing in the registry.

**Phase 4 — Codex engine for chat sessions (1–2 days)**
Assistant (read-only sandbox), spec chat (multimodal images/docs), expand chat.
Acceptance: create a project spec via chat on Codex; assistant answers questions about a
project without writing; expand flow adds features.

**Phase 5 — UI polish + docs (1 day)**
§6 items; README/CLAUDE.md provider docs; note VISION.md divergence in the fork's README
(upstream explicitly refuses non-Claude engines — this fork intentionally diverges, so
upstream merges may conflict in `client.py`/`registry.py`; keep `engines/` additive to
minimize conflict surface).

**Phase 6 (optional, later)**
Per-agent-role engine choice (e.g. coding on Codex, assistant on Kimi): the
`EngineConfig` seam already carries `provider_id`; add per-role settings keys
(`api_provider_assistant`, …) and thread them through `_get_settings_defaults()` /
chat-session factories.

## 7a. Complexity-based agent routing (new requirement, after Phase 2)

When features are written out (spec chat / initializer via `feature_create_bulk`), rate
each feature's complexity and let the orchestrator dispatch it to a stronger or cheaper
agent accordingly. The plumbing is favorable: every per-feature agent spawn in
`parallel_orchestrator.py` already forwards an explicit `--model` flag
(`:842-843, :908-909, :1011-1012, :1072-1073`), so routing = choosing that value per
feature instead of globally.

Implementation steps:

1. **Feature schema**: add `complexity: int (1–3)` (1 = simple, 2 = standard, 3 = complex)
   to the feature model in `mcp_server/feature_mcp.py` (Pydantic inputs of
   `feature_create` / `feature_create_bulk` + SQLite column with `ALTER TABLE` migration,
   default 2 for legacy rows). Expose it in `feature_get_*` outputs and the UI feature list.
2. **Prompts**: update the spec/initializer templates (`prompts.py`, `.claude/commands/
   create-spec.md`, expand-project template) to require a complexity rating per feature
   with concrete criteria (touches one file / standard CRUD / cross-cutting architecture).
   The spec assistant already "расписывает фичи" — it just starts emitting one more field.
3. **Routing table in settings**: new setting `model_routing` (JSON in the settings table,
   e.g. `{"1": "gpt-5.6-luna", "2": "", "3": ""}`, empty = use the provider default
   model). Validated against the active provider's model list; falls back to the default
   model when a routed model is absent.
4. **Orchestrator**: when claiming a feature to spawn a coding agent, resolve
   `model_routing[feature.complexity] or default_model` and pass it as `--model`.
   Batch mode groups features by resolved model so one batch agent doesn't mix models.
   Testing agents keep the default model (they verify, not build) — revisit later.
5. **UI**: in Settings, under the model selector, an optional "Model routing" block with
   three dropdowns (Simple / Standard / Complex) fed by the provider's model list;
   feature cards show a complexity badge.
6. **Scope note**: routing varies the model within the current provider/engine. Cross-
   engine routing (simple → Kimi, complex → Codex) becomes possible once Phase 6 lands —
   the routing value would then be `provider:model`.

Effort: ~1–2 days, independent of the Codex engine — worth doing right after Phase 2
(works already with Claude and Kimi models).

## 8. Risks

| Risk | Mitigation |
|---|---|
| `openai-codex` SDK is beta | exec-JSONL fallback transport behind a flag; pin SDK version |
| Security regression (no bash allowlist on Codex) | rely on Codex OS sandbox; document; optional execpolicy `.rules` generation later |
| GPT models ignore completion phrases | SQLite pass-count remains the authoritative loop exit; strengthen prompt templates |
| MCP tool naming mismatch breaks `allowed_tools`/`ask_user` | dedicated name-mapping shim + integration test that lists tools at startup |
| Codex model list drifts (5.6 → 5.7…) | read `~/.codex/models_cache.json` at startup to populate/refresh the codex provider's models, falling back to the static list |
| Upstream merge conflicts (VISION.md policy) | keep changes additive (`engines/` package, dispatch shims); sync upstream regularly |
| Kimi model-string syntax (`k3` vs `k3[1m]`) | verify against kimi.com/code docs during Phase 1 e2e test |

## 8a. Post-E2E improvement plan (from the first real 18-feature wave, 2026-07-30)

The Codex engine itself performed well (18/18 features substantively implemented,
tests grew, none weakened). All defects found in external review were
orchestration-level and exist upstream too: agents verify per-feature/per-package
and never run repo-wide gates, and the loop ends at a local commit (no push, CI
never ran). Accumulated drift: OpenAPI spec missing 3 routes, codegen (Go/TS)
stale, full Go suite red on a guardrail, migration-head pin stale, lint nits.

Planned fixes, in priority order:

1. **Integrator agent ("wave gate")** - new agent type spawned by the
   orchestrator after every N passed features (setting, default ~5) and when the
   queue empties. Prompt = repo-wide gates: full test suite, lint, spec/codegen
   drift, migration pins. Fixes findings itself or files fix-features via
   feature_create. Reuses the testing-agent spawn mechanics + a new
   `.claude/templates/integrator_prompt.template.md`; trigger counter in
   `_on_agent_complete`.
2. **auto_push setting (default off)** - integrator pushes after green local
   gates; if `gh` is available, watches CI and files a fix-feature on red.
3. **Feature-completion contract in the coding prompt** - explicit final
   checklist: regenerate spec/codegen when API changed, update migration pins
   when migrations added. Cheap; would have prevented 3 of 5 review findings.
4. **claude-progress.txt hygiene** - integrator compacts/prunes stale wave notes
   (observed: an outdated "must not mark passing until Go available" note kept
   scaring later agents).
5. **Codex command timeout** - full `go test ./...` hit the ~4 min per-command
   limit; raise via config override for heavy repos (find the exact key in the
   codex config reference).
6. **Backlog complexity re-rating** - features created before routing shipped
   all default to complexity=2 (the whole AB wave ran on the standard-tier
   model); add a one-click assistant pass to re-rate pending features.

### Quota-constrained operation (user runs the $20 ChatGPT Plus tier)

Concurrency=1 is the CORRECT default on limited subscription tiers: with 3
parallel agents, quota exhaustion mid-window leaves all three features
half-done (confirmed by the user's experience). Do not recommend raising
concurrency; optimize quota instead:

7. **Prefer batching over parallelism** - batch_size 2-3 with concurrency 1:
   several small features share one session's context-reading overhead, which
   is cheaper per feature than separate sessions.
8. **Quota guard in the orchestrator** - before claiming a new feature, check
   the remaining 5-hour-window quota (usage arrives in every turn.completed;
   the SDK also has an account API). Below a threshold: finish the current
   feature, then pause until the window resets instead of starting work that
   will be cut off mid-way. Optionally surface a quota gauge in the UI.
9. **Complexity routing as cost control** - on limited tiers routing is
   primarily about quota, not speed: simple features on the cheap model
   (gpt-5.6-luna, low effort) stretch the window. Re-rating the backlog (item
   6) is a direct money saver.
10. **Integrator/testing agents on the cheap model** - gates are mostly
    command-running; route them to luna by default.

## 9. Explicitly out of scope

- Proxy/translation layers exposing subscriptions as generic APIs (ToS-fragile) — the
  Codex integration drives the official CLI/SDK under the user's own login.
- Usage/cost tracking (absent today for Claude too; Codex `turn.completed` usage data is
  captured by the adapter and logged, but no UI).
- Session resume across agent iterations (upstream design is fresh-context-per-iteration
  with SQLite as the shared state; keep it).
