"""
Settings Router
===============

API endpoints for global settings management.
Settings are stored in the registry database and shared across all projects.
"""

import json
import mimetypes
import sys

from fastapi import APIRouter

from ..schemas import ModelInfo, ModelsResponse, ProviderInfo, ProvidersResponse, SettingsResponse, SettingsUpdate
from ..services.chat_constants import ROOT_DIR

# Mimetype fix for Windows - must run before StaticFiles is mounted
mimetypes.add_type("text/javascript", ".js", True)

# Ensure root is on sys.path for registry import
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from registry import (
    API_PROVIDERS,
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
    get_all_settings,
    get_effort_setting,
    get_setting,
    set_setting,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _parse_yolo_mode(value: str | None) -> bool:
    """Parse YOLO mode string to boolean."""
    return (value or "false").lower() == "true"


@router.get("/providers", response_model=ProvidersResponse)
async def get_available_providers():
    """Get list of available API providers."""
    current = get_setting("api_provider", "claude") or "claude"
    providers = []
    for pid, pdata in API_PROVIDERS.items():
        providers.append(ProviderInfo(
            id=pid,
            name=pdata["name"],
            base_url=pdata.get("base_url"),
            models=[ModelInfo(id=m["id"], name=m["name"]) for m in pdata.get("models", [])],
            default_model=pdata.get("default_model", ""),
            requires_auth=pdata.get("requires_auth", False),
            engine=pdata.get("engine", "claude"),
            auth_mode=pdata.get("auth_mode", "token" if pdata.get("requires_auth") else "none"),
        ))
    return ProvidersResponse(providers=providers, current=current)


@router.get("/models", response_model=ModelsResponse)
async def get_available_models():
    """Get list of available models.

    Returns models for the currently selected API provider.
    """
    current_provider = get_setting("api_provider", "claude") or "claude"
    provider = API_PROVIDERS.get(current_provider)

    if provider and current_provider != "claude":
        provider_models = provider.get("models", [])
        return ModelsResponse(
            models=[ModelInfo(id=m["id"], name=m["name"]) for m in provider_models],
            default=provider.get("default_model", ""),
        )

    # Default: return Claude models
    return ModelsResponse(
        models=[ModelInfo(id=m["id"], name=m["name"]) for m in AVAILABLE_MODELS],
        default=DEFAULT_MODEL,
    )


def _parse_int(value: str | None, default: int) -> int:
    """Parse integer setting with default fallback."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_bool(value: str | None, default: bool = False) -> bool:
    """Parse boolean setting with default fallback."""
    if value is None:
        return default
    return value.lower() == "true"




def _parse_model_routing(raw: str | None, provider_id: str) -> dict[str, str]:
    """Current provider's slice of the (provider-scoped) routing setting."""
    from registry import resolve_provider_scoped_setting

    parsed = resolve_provider_scoped_setting(raw, provider_id)
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items() if k in ("1", "2", "3") and isinstance(v, str)}


def _parse_model_planning(raw: str | None, provider_id: str) -> str | None:
    """Current provider's slice of the (provider-scoped) planning setting."""
    from registry import resolve_provider_scoped_setting

    value = resolve_provider_scoped_setting(raw, provider_id)
    return value if isinstance(value, str) and value else None


def _has_auth_token(raw: str | None, provider_id: str) -> bool:
    """Whether the current provider has a stored auth token (scoped store)."""
    from registry import resolve_provider_scoped_setting

    value = resolve_provider_scoped_setting(raw, provider_id)
    return bool(value) and isinstance(value, str)


def _parse_provider_fallback(raw: str | None) -> list[str]:
    """Parse the stored provider_fallback JSON list."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [p for p in parsed if isinstance(p, str) and p in API_PROVIDERS]


def _load_scoped_store(raw: str | None) -> dict:
    """Load the full provider-scoped store for merging on writes.

    Legacy unscoped values are dropped here; they were attributed to the
    current provider on reads, and the first write migrates the store to the
    scoped format.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    if parsed and all(k in ("1", "2", "3") for k in parsed):
        return {}  # legacy flat routing - superseded by this write
    return parsed


@router.get("", response_model=SettingsResponse)
async def get_settings():
    """Get current global settings."""
    all_settings = get_all_settings()

    api_provider = all_settings.get("api_provider", "claude")

    glm_mode = api_provider == "glm"
    ollama_mode = api_provider == "ollama"

    return SettingsResponse(
        yolo_mode=_parse_yolo_mode(all_settings.get("yolo_mode")),
        model=all_settings.get("model", DEFAULT_MODEL),
        glm_mode=glm_mode,
        ollama_mode=ollama_mode,
        testing_agent_ratio=_parse_int(all_settings.get("testing_agent_ratio"), 1),
        playwright_headless=True,  # Always headless - embedded browser view replaces desktop windows
        batch_size=_parse_int(all_settings.get("batch_size"), 3),
        testing_batch_size=_parse_int(all_settings.get("testing_batch_size"), 3),
        effort=get_effort_setting(),
        api_provider=api_provider,
        api_base_url=all_settings.get("api_base_url"),
        api_has_auth_token=_has_auth_token(all_settings.get("api_auth_token"), api_provider),
        api_model=all_settings.get("api_model"),
        model_routing=_parse_model_routing(all_settings.get("model_routing"), api_provider),
        model_planning=_parse_model_planning(all_settings.get("model_planning"), api_provider),
        provider_fallback=[
            p for p in _parse_provider_fallback(all_settings.get("provider_fallback"))
            if p != api_provider
        ],
        integrator_interval=_parse_int(all_settings.get("integrator_interval"), 5),
        auto_push=_parse_bool(all_settings.get("auto_push"), False),
    )


@router.patch("", response_model=SettingsResponse)
async def update_settings(update: SettingsUpdate):
    """Update global settings."""
    if update.yolo_mode is not None:
        set_setting("yolo_mode", "true" if update.yolo_mode else "false")

    if update.model is not None:
        set_setting("model", update.model)

    if update.testing_agent_ratio is not None:
        set_setting("testing_agent_ratio", str(update.testing_agent_ratio))

    # playwright_headless is no longer user-configurable; always headless
    # with embedded browser view panel in the UI

    if update.batch_size is not None:
        set_setting("batch_size", str(update.batch_size))

    if update.testing_batch_size is not None:
        set_setting("testing_batch_size", str(update.testing_batch_size))

    if update.effort is not None:
        set_setting("effort", update.effort)

    # API provider settings
    if update.api_provider is not None:
        old_provider = get_setting("api_provider", "claude")
        set_setting("api_provider", update.api_provider)

        # When provider changes, auto-set defaults for the new provider
        if update.api_provider != old_provider:
            provider = API_PROVIDERS.get(update.api_provider)
            if provider:
                # Auto-set base URL from provider definition; clear it for
                # providers without one (codex, claude) so a stale URL from
                # the previous provider doesn't leak into the engine env
                if provider.get("base_url"):
                    set_setting("api_base_url", provider["base_url"])
                else:
                    set_setting("api_base_url", "")
                # Auto-set model to provider's default
                if provider.get("default_model") and update.api_model is None:
                    set_setting("api_model", provider["default_model"])
                # model_routing/model_planning are stored per provider and
                # survive switches - no reset needed

    if update.api_base_url is not None:
        set_setting("api_base_url", update.api_base_url)

    if update.api_auth_token is not None:
        # Stored per provider so a failover chain can hold keys for several
        # token-based providers at once
        provider_id = get_setting("api_provider", "claude") or "claude"
        store = _load_scoped_store(get_setting("api_auth_token", None))
        if update.api_auth_token:
            store[provider_id] = update.api_auth_token
        else:
            store.pop(provider_id, None)
        set_setting("api_auth_token", json.dumps(store))

    if update.api_model is not None:
        set_setting("api_model", update.api_model)

    if update.model_routing is not None:
        provider_id = get_setting("api_provider", "claude") or "claude"
        store = _load_scoped_store(get_setting("model_routing", None))
        # Drop empty values so the stored JSON stays compact
        routing = {k: v for k, v in update.model_routing.items() if v}
        if routing:
            store[provider_id] = routing
        else:
            store.pop(provider_id, None)
        set_setting("model_routing", json.dumps(store))

    if update.model_planning is not None:
        provider_id = get_setting("api_provider", "claude") or "claude"
        store = _load_scoped_store(get_setting("model_planning", None))
        if update.model_planning:
            store[provider_id] = update.model_planning
        else:
            store.pop(provider_id, None)
        set_setting("model_planning", json.dumps(store))

    if update.provider_fallback is not None:
        set_setting("provider_fallback", json.dumps(update.provider_fallback))

    if update.integrator_interval is not None:
        set_setting("integrator_interval", str(update.integrator_interval))

    if update.auto_push is not None:
        set_setting("auto_push", "true" if update.auto_push else "false")

    # Return updated settings
    all_settings = get_all_settings()
    api_provider = all_settings.get("api_provider", "claude")
    glm_mode = api_provider == "glm"
    ollama_mode = api_provider == "ollama"

    return SettingsResponse(
        yolo_mode=_parse_yolo_mode(all_settings.get("yolo_mode")),
        model=all_settings.get("model", DEFAULT_MODEL),
        glm_mode=glm_mode,
        ollama_mode=ollama_mode,
        testing_agent_ratio=_parse_int(all_settings.get("testing_agent_ratio"), 1),
        playwright_headless=True,  # Always headless - embedded browser view replaces desktop windows
        batch_size=_parse_int(all_settings.get("batch_size"), 3),
        testing_batch_size=_parse_int(all_settings.get("testing_batch_size"), 3),
        effort=get_effort_setting(),
        api_provider=api_provider,
        api_base_url=all_settings.get("api_base_url"),
        api_has_auth_token=_has_auth_token(all_settings.get("api_auth_token"), api_provider),
        api_model=all_settings.get("api_model"),
        model_routing=_parse_model_routing(all_settings.get("model_routing"), api_provider),
        model_planning=_parse_model_planning(all_settings.get("model_planning"), api_provider),
        provider_fallback=[
            p for p in _parse_provider_fallback(all_settings.get("provider_fallback"))
            if p != api_provider
        ],
        integrator_interval=_parse_int(all_settings.get("integrator_interval"), 5),
        auto_push=_parse_bool(all_settings.get("auto_push"), False),
    )
