"""Per-category provider settings: seven jobs, seven independent configurations.

This is the store behind the tabbed provider screen. Every category — text,
documents, speech, image, vision, video, custom — has its **own** block:

    {"text":    {"provider": "openai", "model": "gpt-4o", "base_url": "...",
                 "enabled": "true", "timeout": "30", "key_ref": "text"},
     "image":   {"provider": "pollinations", "model": "flux.1-schnell", ...},
     ...}

Why a separate store rather than reusing the single-provider selection: the
previous implementation *did* have one selection for chat and a routing map for
the media tasks, and a person changing the image model still saw the chat model
change in the UI, because the screen had one form. Seven categories means seven
blocks, and `set_category` only ever writes the block it was given — there is a
test for exactly that.

**Keys never live here.** Each block stores a `key_ref` (a name), and the secret
is in the OS keychain under `vantage.ai.<key_ref>`, through the same
`KeychainStorage` the rest of the app uses. `public()` cannot emit a key because
it never holds one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

#: The seven visible tabs, in the order the UI shows them.
CATEGORIES: tuple = (
    "text", "documents", "speech", "image", "vision", "video", "custom",
)

#: The capabilities each tab may assign, and the job each one performs. `speech`
#: holds two because the tab shows two sections; the others hold one or two.
CATEGORY_CAPABILITIES: Dict[str, tuple] = {
    "text": ("text_chat",),
    "documents": ("document_reading", "document_vision"),
    "speech": ("text_to_speech", "speech_to_text"),
    "image": ("image_generation", "image_editing"),
    "vision": ("vision",),
    "video": ("video_generation", "video_understanding"),
    "custom": ("text_chat",),
}

#: A tab's single-line purpose, for the empty state and the test button.
CATEGORY_PURPOSE: Dict[str, str] = {
    "text": "Command Center answers, conversation and reasoning.",
    "documents": "Reading PDFs and documents, and reading them as images when the "
                 "provider supports it.",
    "speech": "Reading answers aloud, and turning recordings into text.",
    "image": "Creating images, and editing an existing one.",
    "vision": "Answering questions about a picture.",
    "video": "Submitting a video job, and understanding a video where supported.",
    "custom": "Any OpenAI-compatible server: a hosted API, a proxy, Ollama or LM Studio.",
}

#: Provider types the UI offers per tab. A provider that cannot do the job is
#: not offered -- the list is a promise the backend also enforces.
#:
#: 9Router is offered for every tab its live catalog can actually serve. Its
#: `/api/v1/models` reports capabilities per model, and the counts are what
#: decide this: 1003 routes with `tools`, 668 with `vision`, 186 with `pdf`,
#: 176 with `videoInput`, 103 with `audioInput` -- but **zero** with
#: `imageOutput` or `audioOutput`.
#:
#: Note that `videoInput` is *reading* a video, which is not what the `video`
#: tab does: that tab submits a generation job to `POST {base}/videos`, and
#: 9router has no such route (it answers with its HTML shell, a 404). So 9router
#: is added to text, documents and vision only -- not to `image` or `video`
#: (nothing there generates media) and not to `speech` (nothing there speaks).
CATEGORY_PROVIDERS: Dict[str, tuple] = {
    "text": ("openai", "gemini", "groq", "9router", "chat2api", "openai_compatible",
             "pollinations", "custom"),
    "documents": ("openai", "gemini", "9router", "openai_compatible", "pollinations",
                  "custom"),
    "speech": ("openai", "gemini", "openrouter", "openai_compatible", "custom"),
    "image": ("pollinations", "openai", "gemini", "openai_compatible", "custom"),
    "vision": ("openai", "gemini", "9router", "openai_compatible", "pollinations",
               "custom"),
    "video": ("openai", "openai_compatible", "custom"),
    "custom": ("custom", "openai_compatible"),
}

#: Human labels for the provider ids.
PROVIDER_LABELS: Dict[str, str] = {
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "groq": "Groq",
    "9router": "9Router (local gateway)",
    "chat2api": "chat2api (local)",
    "openai_compatible": "OpenAI-compatible",
    "openrouter": "OpenRouter",
    "pollinations": "Pollinations",
    "custom": "Custom (OpenAI-compatible)",
}

#: Base URLs the picker fills in, when the provider has a known one. A blank
#: value means "the person supplies it" — never a guess.
DEFAULT_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "pollinations": "https://gen.pollinations.ai/v1",
}

@dataclass
class CategorySettings:
    """One tab's configuration. Every field optional; absent means unset."""

    category: str
    provider: str = ""
    model: str = ""
    base_url: str = ""
    enabled: bool = True
    timeout: float = 60.0
    #: A *name*, not a secret: the keychain entry that holds this category's key.
    key_ref: str = ""
    #: Which capability inside this tab the settings apply to (speech has two).
    capability: str = ""
    #: Free-form extras the provider needs (voice, format, size), bounded.
    options: Dict[str, str] = field(default_factory=dict)
    #: When this category's last real test ran and what it did.
    tested_at: float = 0.0
    test_ok: bool = False
    test_note: str = ""

    @property
    def configured(self) -> bool:
        """A provider and a base URL are both set."""
        return bool(self.provider and self.base_url)

    def public(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "provider": self.provider,
            "provider_label": PROVIDER_LABELS.get(self.provider, self.provider),
            "model": self.model,
            "base_url": self.base_url,
            "enabled": self.enabled,
            "timeout": self.timeout,
            "capability": self.capability,
            "options": dict(self.options),
            "has_key": bool(self.key_ref),
            "configured": bool(self.provider and self.base_url),
            "tested_at": self.tested_at or None,
            "test_ok": self.test_ok if self.tested_at else None,
            "test_note": self.test_note,
        }

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

BLOCK_KEY = "provider_categories"
MAX_OPTION_VALUE = 500
MAX_OPTIONS = 16

def _path(home: Optional[Path] = None) -> Path:
    from .selection import selection_path

    return selection_path(home)

def _read(home: Optional[Path] = None) -> Dict[str, Any]:
    path = _path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def _write(data: Dict[str, Any], home: Optional[Path] = None) -> None:
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass

def load_all(home: Optional[Path] = None) -> Dict[str, CategorySettings]:
    """Every category's settings. An unconfigured one is an empty block, not absent."""
    data = _read(home)
    block = data.get(BLOCK_KEY)
    out: Dict[str, CategorySettings] = {c: CategorySettings(category=c) for c in CATEGORIES}
    if not isinstance(block, dict):
        return out
    for category in CATEGORIES:
        raw = block.get(category)
        if not isinstance(raw, dict):
            continue
        out[category] = CategorySettings(
            category=category,
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            base_url=str(raw.get("base_url") or ""),
            enabled=bool(raw.get("enabled", True)),
            timeout=_float(raw.get("timeout"), 60.0),
            key_ref=str(raw.get("key_ref") or ""),
            capability=str(raw.get("capability") or ""),
            options={str(k): str(v)[:MAX_OPTION_VALUE]
                     for k, v in (raw.get("options") or {}).items()
                     if isinstance(v, (str, int, float, bool))},
            tested_at=_float(raw.get("tested_at"), 0.0),
            test_ok=bool(raw.get("test_ok", False)),
            test_note=str(raw.get("test_note") or "")[:300],
        )
    return out

def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def load(category: str, home: Optional[Path] = None) -> CategorySettings:
    return load_all(home).get(category) or CategorySettings(category=category)

def set_category(category: str, *, provider: Optional[str] = None,
                 model: Optional[str] = None, base_url: Optional[str] = None,
                 enabled: Optional[bool] = None, timeout: Optional[float] = None,
                 options: Optional[Dict[str, str]] = None,
                 capability: Optional[str] = None,
                 home: Optional[Path] = None) -> CategorySettings:
    """Write **one** category's block. Nothing else in the file is touched.

    This is the isolation guarantee the screen depends on: saving the Image tab
    cannot change the Text tab, because the only key written is
    ``provider_categories[category]``.
    """
    if category not in CATEGORIES:
        raise ValueError(f"{category!r} is not a provider category. "
                         f"Known: {', '.join(CATEGORIES)}.")
    data = _read(home)
    block = data.get(BLOCK_KEY)
    if not isinstance(block, dict):
        block = {}
    entry = block.get(category)
    if not isinstance(entry, dict):
        entry = {}

    if provider is not None:
        entry["provider"] = provider.strip()
        # The provider decides what this category can do; re-derive it rather
        # than letting a stale capability survive a provider change.
        allowed = CATEGORY_CAPABILITIES.get(category, ())
        entry["capability"] = allowed[0] if allowed else ""
    if model is not None:
        entry["model"] = model.strip()
    if base_url is not None:
        entry["base_url"] = base_url.strip()
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if timeout is not None:
        entry["timeout"] = str(max(1.0, min(float(timeout), 600.0)))
    if capability is not None:
        allowed = CATEGORY_CAPABILITIES.get(category, ())
        if capability and capability not in allowed:
            raise ValueError(
                f"{capability!r} is not a capability of {category!r}. "
                f"It has: {', '.join(allowed) or 'none'}.")
        entry["capability"] = capability
    if options is not None:
        cleaned = {str(k)[:64]: str(v)[:MAX_OPTION_VALUE]
                   for k, v in list(options.items())[:MAX_OPTIONS]
                   if isinstance(v, (str, int, float, bool))}
        entry["options"] = cleaned

    block[category] = entry
    data[BLOCK_KEY] = block
    _write(data, home)
    return load(category, home)

def record_test(category: str, *, ok: bool, note: str = "",
                home: Optional[Path] = None) -> CategorySettings:
    """Record a real test's outcome for one category."""
    if category not in CATEGORIES:
        raise ValueError(f"{category!r} is not a provider category.")
    data = _read(home)
    block = data.get(BLOCK_KEY)
    if not isinstance(block, dict):
        block = {}
    entry = block.get(category)
    if not isinstance(entry, dict):
        entry = {}
    entry["tested_at"] = str(time.time())
    entry["test_ok"] = bool(ok)
    entry["test_note"] = (note or "")[:300]
    block[category] = entry
    data[BLOCK_KEY] = block
    _write(data, home)
    return load(category, home)

# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def key_ref_for(category: str) -> str:
    """The keychain entry name for a category's key.

    One entry per category, so changing the Image key cannot alter the Speech
    key even though both are stored through the same keychain helper.
    """
    return f"category.{category}"

def store_key(category: str, secret: str, home: Optional[Path] = None) -> bool:
    """Store this category's key in the keychain and remember that it exists."""
    if category not in CATEGORIES:
        raise ValueError(f"{category!r} is not a provider category.")
    if not secret:
        return False
    try:
        from ..security.providers import KeychainStorage

        stored = KeychainStorage.store(key_ref_for(category), secret)
    except Exception:
        return False
    if stored:
        data = _read(home)
        block = data.get(BLOCK_KEY)
        if not isinstance(block, dict):
            block = {}
        entry = block.get(category) if isinstance(block.get(category), dict) else {}
        entry["key_ref"] = key_ref_for(category)
        block[category] = entry
        data[BLOCK_KEY] = block
        _write(data, home)
    return stored

def retrieve_key(category: str) -> str:
    """This category's key, or "" — never raises for a missing backend."""
    try:
        from ..security.providers import KeychainStorage

        return KeychainStorage.retrieve(key_ref_for(category)) or ""
    except Exception:
        return ""

def clear_key(category: str, home: Optional[Path] = None) -> None:
    try:
        from ..security.providers import KeychainStorage

        KeychainStorage.delete(key_ref_for(category))
    except Exception:
        pass
    data = _read(home)
    block = data.get(BLOCK_KEY)
    if isinstance(block, dict) and isinstance(block.get(category), dict):
        block[category].pop("key_ref", None)
        data[BLOCK_KEY] = block
        _write(data, home)

# ---------------------------------------------------------------------------
# Provider catalogue for the UI
# ---------------------------------------------------------------------------

def catalogue(home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Everything the seven tabs need, in one payload.

    The screen is driven from this rather than from hard-coded Swift lists, so
    adding a provider or a capability is a backend change only.
    """
    settings = load_all(home)
    out: List[Dict[str, Any]] = []
    for category in CATEGORIES:
        out.append({
            "category": category,
            "purpose": CATEGORY_PURPOSE[category],
            "capabilities": list(CATEGORY_CAPABILITIES[category]),
            "providers": [
                {"id": pid, "label": PROVIDER_LABELS.get(pid, pid),
                 "default_base_url": DEFAULT_BASE_URLS.get(pid, "")}
                for pid in CATEGORY_PROVIDERS[category]
            ],
            "settings": settings[category].public(),
        })
    return out