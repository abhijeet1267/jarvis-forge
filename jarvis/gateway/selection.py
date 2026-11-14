"""Provider *selection* persistence + secret resolution for the live provider path.

The provider adapters (``gateway/providers.py``) read their configuration from
``Settings``. A person configures the assistant in two ways:

* **Secretly** -- an API key, stored in the macOS Keychain (``KeychainStorage``). This
  is what the AI Settings screen writes, and it is the secure store the phase requires.
* **By selection** -- which provider, which model, and (optionally) which base URL.
  These are not secrets, so they persist in a small JSON file beside the data
  (``ai_selection.json`` under ``JARVIS_HOME``).

This module is the single bridge between those two stores and the ``Settings``/
adapter path, so a key saved in the UI actually reaches the main assistant instead of
sitting unread in the Keychain while the router keeps asking ``llm.env``.

Import discipline: this module is imported by ``config.py`` (so ``get_settings`` can
overlay the selection), which means it must not import ``security.providers`` at module
load time -- that would be a ``config -> selection -> security.providers -> config``
cycle. ``KeychainStorage`` is therefore imported lazily inside the function that needs
it, where ``config`` is already fully loaded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

#: File that holds the non-secret selection. It never holds the key itself.
SELECTION_FILE_NAME = "ai_selection.json"

#: The selection keys, in the order they map onto ``Settings`` fields.
SELECTION_FIELDS = ("ai_provider", "llm_model", "llm_base_url")

#: Per-provider non-secret state. Each provider keeps its own base URL, model and
#: last test result under ``providers.<id>``, so configuring one endpoint never
#: overwrites another's -- the top-level ``llm_base_url``/``llm_model`` stay a single
#: *active* selection (what chat uses), while the per-provider map is what a given
#: provider card's Test button resolves. Never holds a secret.
PROVIDER_CONFIG_KEY = "providers"

#: The AI on/off switch, stored separately because it is a *boolean* choice, not a
#: string field, and its default (on) must not be overridden by the absence of a value
#: the way an empty string is. A person turns the assistant off to keep every sentence
#: on the deterministic rule planner -- a key that is present must not defeat that.
SELECTION_ENABLED_KEY = "ai_enabled"


def ai_enabled(home: Optional[Path] = None) -> bool:
    """Whether the assistant may call a model at all.

    Defaults to True when never set (the historical behaviour: a configured key is
    used). Only an explicit ``false`` turns it off, so a missing/blank file leaves the
    assistant on, and "off" is a deliberate, persisted choice.
    """
    path = selection_path(home)
    if not path.is_file():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    value = data.get(SELECTION_ENABLED_KEY)
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    # Tolerate a stringified boolean (e.g. someone edited the file by hand).
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def selection_path(home: Optional[Path] = None) -> Path:
    """Where the selection file lives: beside the data, never inside the app bundle.

    A key that lands in the bundle would break its code signature (Phase 40's finding),
    and the selection is kept with the data for the same reason.
    """
    root = home or Path(os.environ.get("JARVIS_HOME") or Path.home() / ".jarvis-forge")
    # Imported here, not at module scope: ``config`` imports this module, so a
    # top-level import would be a cycle.
    from ..config import data_home

    return data_home(Path(root)) / SELECTION_FILE_NAME


def load_selection(home: Optional[Path] = None) -> Dict[str, str]:
    """The persisted selection, or an empty dict when there is none / it is unreadable.

    Only the known selection fields are returned; anything else in the file is ignored
    so a future field cannot silently become configuration, and a corrupt file degrades
    to "unconfigured" rather than crashing the model path.
    """
    path = selection_path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(data[k]) for k in SELECTION_FIELDS if isinstance(data.get(k), str) and data.get(k)}


def save_selection(
    home: Optional[Path],
    *,
    provider: str = "",
    model: str = "",
    base_url: str = "",
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Persist the selection. Only non-empty string values are written; an empty value
    clears that field so a "remove the custom base URL" actually stops overriding the
    default. ``enabled`` is a boolean and only written when explicitly given, so a
    caller that is not touching the on/off switch does not silently flip it.
    """
    path = selection_path(home)
    # Read the raw file (not load_selection, which drops non-string fields) so the
    # boolean switch survives an edit to the provider/model.
    raw: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            raw = {}
    current: Dict[str, Any] = {k: str(raw[k]) for k in SELECTION_FIELDS
                               if isinstance(raw.get(k), str) and raw.get(k)}
    if SELECTION_ENABLED_KEY in raw and isinstance(raw[SELECTION_ENABLED_KEY], bool):
        current[SELECTION_ENABLED_KEY] = raw[SELECTION_ENABLED_KEY]
    # Carry the per-provider block through an active-selection edit, so selecting a
    # provider never discards another provider's saved base URL/model.
    if isinstance(raw.get(PROVIDER_CONFIG_KEY), dict):
        current[PROVIDER_CONFIG_KEY] = raw[PROVIDER_CONFIG_KEY]

    if provider:
        current["ai_provider"] = provider
    else:
        current.pop("ai_provider", None)
    if model:
        current["llm_model"] = model
    else:
        current.pop("llm_model", None)
    if base_url:
        current["llm_base_url"] = base_url
    else:
        current.pop("llm_base_url", None)
    if enabled is not None:
        current[SELECTION_ENABLED_KEY] = bool(enabled)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # not a secret, but owner-only is the cheap safe default
    except OSError:
        pass
    return current


#: Non-secret MCP settings that belong in the selection file rather than a shell
#: export. The *token* is deliberately absent: a credential goes to the Keychain
#: through ``KeychainStorage``, exactly like a provider key, so it can never end up
#: in this 0600 file, a log line, or a diagnostic payload.
MCP_CONFIG_KEY = "mcp"

def mcp_config(home: Optional[Path] = None) -> Dict[str, str]:
    """Saved MCP settings (endpoint, allowlist, label). Never a token."""
    data = _read_selection_file(home)
    block = data.get(MCP_CONFIG_KEY)
    if not isinstance(block, dict):
        return {}
    return {str(k): str(v) for k, v in block.items() if isinstance(v, str) and v}

def save_mcp_config(
    *,
    home: Optional[Path] = None,
    url: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    name: Optional[str] = None,
) -> Dict[str, str]:
    """Persist the MCP endpoint and allowlist. ``None`` leaves a field alone.

    Nothing here is a secret, so it is safe in this file; the bearer token is
    stored separately (``KeychainStorage`` under the ``mcp`` provider id), because
    a URL carrying a credential must not be written to disk in the clear.
    """
    path = selection_path(home)
    data = _read_selection_file(home)
    block = data.get(MCP_CONFIG_KEY) if isinstance(data.get(MCP_CONFIG_KEY), dict) else {}
    block = {str(k): str(v) for k, v in block.items() if isinstance(v, str)}
    if url is not None:
        if url.strip():
            block["url"] = url.strip()
        else:
            block.pop("url", None)
    if allowed_tools is not None:
        if allowed_tools.strip():
            block["allowed_tools"] = allowed_tools.strip()
        else:
            block.pop("allowed_tools", None)
    if name is not None:
        if name.strip():
            block["name"] = name.strip()
        else:
            block.pop("name", None)
    if block:
        data[MCP_CONFIG_KEY] = block
    else:
        data.pop(MCP_CONFIG_KEY, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return dict(block)

#: Non-secret n8n settings. The API key is deliberately absent for the same
#: reason MCP's token is: it goes to the Keychain under the ``n8n`` id.
N8N_CONFIG_KEY = "n8n"

def n8n_config(home: Optional[Path] = None) -> Dict[str, str]:
    """Saved n8n settings (URL, enable flag, allowlist, timeouts). Never a key."""
    data = _read_selection_file(home)
    block = data.get(N8N_CONFIG_KEY)
    if not isinstance(block, dict):
        return {}
    return {str(k): str(v) for k, v in block.items() if isinstance(v, str) and v}

def save_n8n_config(
    *,
    home: Optional[Path] = None,
    enabled: Optional[bool] = None,
    base_url: Optional[str] = None,
    allowed_keys: Optional[str] = None,
    timeout: Optional[float] = None,
    allow_remote: Optional[bool] = None,
) -> Dict[str, str]:
    """Persist the n8n connection settings. ``None`` leaves a field alone.

    Nothing written here is a secret. The API key is stored separately by the
    route, through ``KeychainStorage``, so it cannot reach this 0600 file, a log
    line, or a status payload.
    """
    path = selection_path(home)
    data = _read_selection_file(home)
    block = data.get(N8N_CONFIG_KEY) if isinstance(data.get(N8N_CONFIG_KEY), dict) else {}
    block = {str(k): str(v) for k, v in block.items() if isinstance(v, str)}
    if enabled is not None:
        block["enabled"] = "true" if enabled else "false"
    if base_url is not None:
        if base_url.strip():
            block["base_url"] = base_url.strip()
        else:
            block.pop("base_url", None)
    if allowed_keys is not None:
        if allowed_keys.strip():
            block["allowed_keys"] = allowed_keys.strip()
        else:
            block.pop("allowed_keys", None)
    if timeout is not None:
        block["timeout"] = f"{float(timeout):g}"
    if allow_remote is not None:
        block["allow_remote"] = "true" if allow_remote else "false"
    if block:
        data[N8N_CONFIG_KEY] = block
    else:
        data.pop(N8N_CONFIG_KEY, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return dict(block)

def clear_selection(home: Optional[Path] = None) -> None:
    """Forget the selection. The key is a separate store and is removed separately."""
    path = selection_path(home)
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def _read_selection_file(home: Optional[Path] = None) -> Dict[str, Any]:
    """The raw selection file as a dict, or ``{}`` when absent/unreadable.

    One reader for every path here so a corrupt file degrades to "unconfigured"
    consistently instead of crashing one reader and not another.
    """
    path = selection_path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def provider_config(home: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """The per-provider non-secret config map, keyed by provider id.

    ``{ "<id>": {"base_url": ..., "model": ..., ...} }``. Derived from the
    ``providers`` block in the selection file; empty when there is none. Never
    returns a secret -- the key stays in the Keychain.
    """
    data = _read_selection_file(home)
    block = data.get(PROVIDER_CONFIG_KEY)
    if not isinstance(block, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for pid, cfg in block.items():
        if isinstance(cfg, dict):
            out[str(pid)] = {str(k): str(v) for k, v in cfg.items()
                             if isinstance(v, str) and v}
    return out


def get_provider_config(provider_name: str, home: Optional[Path] = None) -> Dict[str, str]:
    """One provider's saved non-secret config (base_url/model/last_test bits)."""
    return dict(provider_config(home).get(str(provider_name) or "", {}))


def save_provider_config(
    provider_name: str,
    *,
    home: Optional[Path] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    **extra: str,
) -> Dict[str, Any]:
    """Persist non-secret config for one provider without touching the active selection.

    Only the named provider's block changes; the top-level ``ai_provider`` /
    ``llm_model`` / ``llm_base_url`` (the active selection) and every other
    provider's block are left exactly as they were. ``None`` for ``base_url`` or
    ``model`` means "do not change this field"; an empty string clears it.
    """
    path = selection_path(home)
    data = _read_selection_file(home)
    block = data.get(PROVIDER_CONFIG_KEY) if isinstance(data.get(PROVIDER_CONFIG_KEY), dict) else {}
    block = {str(k): (v if isinstance(v, dict) else {}) for k, v in block.items()}

    pid = str(provider_name)
    entry: Dict[str, Any] = {str(k): str(v) for k, v in block.get(pid, {}).items()}
    if base_url is not None:
        entry["base_url"] = str(base_url).strip()
        if not entry["base_url"]:
            entry.pop("base_url", None)
    if model is not None:
        entry["model"] = str(model).strip()
        if not entry["model"]:
            entry.pop("model", None)
    for k, v in extra.items():
        v = str(v).strip()
        if v:
            entry[k] = v
        else:
            entry.pop(k, None)
    block[pid] = entry
    data[PROVIDER_CONFIG_KEY] = block

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return data


def stored_key(provider_name: str, home: Optional[Path] = None) -> Optional[str]:
    """The Keychain-stored key for a provider, or None.

    Imported lazily so this module stays importable from ``config`` without a cycle.
    An unavailable keychain backend reports ``None`` -- it never raises and never
    invents a key, so a missing backend reads as "not configured", not as a crash.
    """
    if not provider_name:
        return None
    try:
        from ..security.providers import KeychainStorage
    except Exception:
        return None
    try:
        return KeychainStorage.retrieve(provider_name)
    except Exception:
        return None


def effective_key(settings, provider_name: str) -> Optional[str]:
    """The key a provider should actually use.

    Precedence, highest first:
      1. a key the shell/``llm.env`` already set (``settings.llm_api_key``) -- the real
         environment always wins, matching ``apply_llm_file``'s contract;
      2. the key the settings screen stored in the Keychain for that provider.

    This is the bridge that makes a key saved in the UI reach the main assistant: the
    router and orchestrator build their provider from ``settings`` and read this, so a
    Keychain key no longer sits unread while the router only sees the (empty) env.
    """
    env_key = str(getattr(settings, "llm_api_key", "") or "") if settings is not None else ""
    if env_key:
        return env_key
    return stored_key(provider_name, getattr(settings, "home", None) if settings else None)


def effective_base_url(settings, provider_name: str) -> str:
    """The endpoint an adapter should call, or an empty string when none is known.

    Precedence, highest first:
      1. ``settings.llm_base_url`` — the env/``llm.env`` value baked in at
         ``Settings`` construction time (the "real environment always wins"
         contract). This is safe against ``apply_llm_file`` env leaks in tests.
      2. this provider's own saved base URL (the per-provider block);
      3. the adapter's own ``DEFAULT_BASE_URL``.
    """
    env_url = str(getattr(settings, "llm_base_url", "") or "").strip()
    if env_url:
        return env_url
    home = getattr(settings, "home", None) if settings is not None else None
    own = get_provider_config(provider_name, home).get("base_url")
    if own:
        return own
    try:
        from .providers import PROVIDERS
    except Exception:
        return ""
    return getattr(PROVIDERS.get(provider_name), "DEFAULT_BASE_URL", "") or ""


def provider_base_url(settings, provider_name: str) -> str:
    """The endpoint for *this* provider's card / Test button, scoped so a
    non-active provider never inherits the active provider's endpoint.

    Precedence:
      1. this provider's own saved base URL (the per-provider block);
      2. the active selection's base URL - only when the requested provider
         is the active one;
      3. the adapter's own ``DEFAULT_BASE_URL``.
    """
    home = getattr(settings, "home", None) if settings is not None else None
    own = get_provider_config(provider_name, home).get("base_url")
    if own:
        return own
    active = str(getattr(settings, "ai_provider", "") or "").strip().lower() \
        if settings is not None else ""
    if active and active == str(provider_name).strip().lower():
        url = str(getattr(settings, "llm_base_url", "") or "")
        if url:
            return url
    try:
        from .providers import PROVIDERS
    except Exception:
        return ""
    return getattr(PROVIDERS.get(provider_name), "DEFAULT_BASE_URL", "") or ""




def effective_model(settings, provider_name: str) -> str:
    """The model an adapter should use, or ``""`` when none is configured.

    Precedence, highest first:
      1. ``settings.llm_model`` — the env/``llm.env`` value baked in at
         ``Settings`` construction time;
      2. this provider's own saved model (the per-provider block);
      3. ``""`` (the caller falls back to the adapter's own default).
    """
    env_model = str(getattr(settings, "llm_model", "") or "").strip()
    if env_model:
        return env_model
    home = getattr(settings, "home", None) if settings is not None else None
    own = get_provider_config(provider_name, home).get("model")
    if own:
        return own
    return ""


def provider_model(settings, provider_name: str) -> str:
    """The model for *this* provider's card / Test button, scoped so a
    non-active provider never inherits the active provider's model.

    Precedence:
      1. this provider's own saved model (the per-provider block);
      2. the active selection's model - only when the requested provider
         is the active one;
      3. ``""``.
    """
    home = getattr(settings, "home", None) if settings is not None else None
    own = get_provider_config(provider_name, home).get("model")
    if own:
        return own
    active = str(getattr(settings, "ai_provider", "") or "").strip().lower() \
        if settings is not None else ""
    if active and active == str(provider_name).strip().lower():
        return str(getattr(settings, "llm_model", "") or "")
    return ""
