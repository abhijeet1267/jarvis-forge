"""Provider-neutral model gateway (spec §10, §11, §12) — the contract is shared, the authority is not.

Measured before this file existed: `jarvis/gateway/router.py` spoke exactly one wire format —
`{llm_base_url}/chat/completions` with a `Bearer` header — and reported
`provider="openai-compatible"` whatever the configuration said, with memory's embedder, the vision
provider and the voice transcriber each building their own copy of that shape. There was no
selection at all: no `JARVIS_AI_PROVIDER`, nothing that could ask a vendor in its own native form.
A second vendor was reachable only by pointing the OpenAI-shaped URL at its compatibility endpoint,
which is what `READ ME FIRST.txt` has always told Gemini users to do.

So the accurate complaint is narrower than "the other vendor would have failed", and worth stating
exactly: `/health` could say a model was configured without ever naming which door it would use,
the router's own `provider` field was a constant, and asking for the native shapes by name was not
possible. The first draft of this file then got the native path wrong in a way only the shipped
readme could expose — see `GeminiProvider.root`.

What is decided here:

* **One selection point.** `select_provider(settings)` reads `JARVIS_AI_PROVIDER`. An unknown name
  is refused with the list of known ones — a typo that silently meant "we used the other vendor
  anyway" is a claim the status page would then repeat back as false.
* **Model names come from configuration** (spec §10's rule), so they reach the wire only through
  the settings that hold them.
* **A provider's failure is its own words.** An HTTP error carries the vendor's message into
  `ProviderReply.error`, because "the model was unavailable" tells a user nothing while "model not
  found" tells them what to change. A timeout says *timeout* and carries its number — the discipline
  Phase 34 established for a server that never goes silent.
* **A capability a provider does not have is refused, not returned empty.** `[]` from a failed
  embedding or `""` from a failed transcript is the offline-as-empty bug this project has already
  fixed twice, for mail and for calendar.
* **No provider plans or executes.** This module answers questions and turns text into vectors.
  The permission gate stays the only door to an action, exactly as Phase 42 left it.

Every read goes through `jarvis.net.http_read`, so the wait is a *total* budget rather than an
inactivity gap: a vendor that dribbles bytes cannot hold a Vantage turn open forever. The call runs
in a thread so async callers keep their shape.

What this file deliberately does **not** do: claim any of it has been spoken to a real vendor.
There is no API key on this machine, so the proof is a local endpoint that records the bytes —
see the phase report, `PROVIDER_LIVE_VENDOR_CALL: NOT MEASURED`.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from ..net import DeadlineExceeded, http_read

log = get_logger("gateway.providers")

#: How long one model read may take in total. An answer that needs longer is not worth holding a
#: command pipeline open for; the rules answer the same sentence in milliseconds.
DEFAULT_TIMEOUT_S = 20.0


class ProviderUnknown(ValueError):
    """The configured provider name is not one this build can talk to."""


class ProviderCapabilityMissing(ValueError):
    """This provider has no shape for the task asked of it."""


class ProviderFailed(RuntimeError):
    """A synchronous caller asked for a result and did not get one.

    It carries the provider's own reason, and it exists so that the paths which *must* have a
    vector -- indexing a memory -- fail the way they already fail today (an exception the caller
    records) instead of learning to check a return code in three places.
    """


@dataclass
class ProviderReply:
    """One answer, from one named vendor, with its own failure if it had one.

    `ok` is the only authority on an answer having arrived. `error` is always populated when it is
    false, and the key is never part of it.
    """

    ok: bool
    provider: str
    model: str
    text: str = ""
    vectors: Optional[List[List[float]]] = None
    error: str = ""
    status: int = 0
    notes: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ok and not self.error:
            raise ValueError("a failed provider reply has to say why")


def _redact(text: str, secret: str) -> str:
    """A vendor error can echo the request it rejected, and the key rides in the headers."""
    if secret and secret in text:
        return text.replace(secret, "[redacted-key]")
    return text


def _vendor_message(body: bytes, status: int, secret: str) -> str:
    """The vendor's own reason, or the status code when it did not give one."""
    try:
        parsed = json.loads(body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    detail = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("status") or "")
    else:
        message = str(detail or "")
    message = message or (str(parsed)[:200] if parsed else "no response body")
    return _redact(f"provider returned {status}: {message[:300]}", secret)


class AIProvider:
    """The shape every vendor adapter fills, and the one place a POST is built."""

    name = ""

    def __init__(self, settings=None) -> None:
        from ..config import get_settings

        self.settings = settings or get_settings()

    # -- what the settings say ----------------------------------------------

    @property
    def base_url(self) -> str:
        return str(getattr(self.settings, "llm_base_url", "") or "")

    @property
    def api_key(self) -> str:
        return str(getattr(self.settings, "llm_api_key", "") or "")

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def root(self) -> str:
        return self.base_url.rstrip("/")

    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    # -- the one POST -------------------------------------------------------

    def _post(self, url: str, payload: Dict[str, Any], *, timeout: float,
              model: str) -> ProviderReply:
        """Send, read inside a total budget, and turn whatever came back into a reply.

        `parse` is the only part a vendor is allowed to differ in, which is the whole argument for
        putting this here instead of in each adapter.
        """
        if not self.configured():
            return ProviderReply(False, self.name, model, error="no model endpoint is configured")
        request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json",
                                                  **self.headers()},
                                         method="POST")
        try:
            raw = http_read(request, total=timeout, what=f"the {self.name} model endpoint")
        except DeadlineExceeded as exc:
            return ProviderReply(False, self.name, model,
                                 error=_redact(f"timeout: {exc}", self.api_key))
        except urllib.error.HTTPError as exc:
            detail = exc.read() if hasattr(exc, "read") else b""
            return ProviderReply(False, self.name, model,
                                 error=_vendor_message(detail, exc.code, self.api_key),
                                 status=exc.code)
        except urllib.error.URLError as exc:
            return ProviderReply(False, self.name, model,
                                 error=_redact(f"could not be reached: {exc.reason}", self.api_key))
        except Exception as exc:  # a transport surprise is a failure, never an empty answer
            return ProviderReply(False, self.name, model,
                                 error=_redact(f"call failed: {exc}", self.api_key))
        return self.parse(raw, model)

    def parse(self, raw: bytes, model: str) -> ProviderReply:
        raise NotImplementedError

    # -- the tasks ----------------------------------------------------------

    def _chat_request(self, system: str, user: str, model: str,
                      json_mode: bool) -> tuple:
        """`(url, payload)` for one chat turn, in this vendor's language."""
        raise NotImplementedError

    def _embed_request(self, text: str, model: str) -> tuple:
        """`(url, payload)` for one text, in this vendor's language."""
        raise NotImplementedError

    async def chat(self, system: str, user: str, *, model: str, json_mode: bool = False,
                   timeout: float = DEFAULT_TIMEOUT_S) -> ProviderReply:
        url, payload = self._chat_request(system, user, model, json_mode)
        return await asyncio.to_thread(self._post, url, payload, timeout=timeout, model=model)

    async def embed(self, texts: List[str], *, model: str,
                    timeout: float = DEFAULT_TIMEOUT_S) -> ProviderReply:
        """Vectors for each text, one request per text.

        Batching is a vendor-specific extra (`input: [...]` here, `:batchEmbedContents` there),
        and a contract that quietly differs per vendor is the thing this module exists to stop.
        """
        return await asyncio.to_thread(self.embed_many_sync, texts, model=model, timeout=timeout)

    def embed_sync(self, text: str, *, model: str,
                   timeout: float = DEFAULT_TIMEOUT_S) -> List[float]:
        """One vector, or an exception that says why there is not one."""
        reply = self._embed([text], model=model, timeout=timeout)
        if not reply.ok or not reply.vectors or not reply.vectors[0]:
            raise ProviderFailed(f"{self.name} embedding: "
                                 f"{reply.error or 'no vector came back'}")
        return reply.vectors[0]

    def embed_many_sync(self, texts: List[str], *, model: str,
                        timeout: float = DEFAULT_TIMEOUT_S) -> ProviderReply:
        return self._embed(texts, model=model, timeout=timeout)

    def _embed(self, texts: List[str], *, model: str,
               timeout: float) -> ProviderReply:
        rows: List[List[float]] = []
        for text in texts:
            url, payload = self._embed_request(text, model)
            reply = self._post(url, payload, timeout=timeout, model=model)
            if not reply.ok:
                return reply
            rows.extend(reply.vectors or [])
        if not rows:
            return ProviderReply(False, self.name, model, error="provider returned no vectors")
        return ProviderReply(True, self.name, model, vectors=rows)

    # -- the shape, for a caller that reads with its own deadline ------------

    def embedding_request(self, text: str, model: str) -> Dict[str, Any]:
        """`(url, headers, payload)` for one embedding read.

        Returned rather than sent because the memory path reads through
        `jarvis.net.http_read` at its own call site, inside a budget Phase 34 measured and
        pinned. The vendor's shape belongs to the provider; the deadline belongs to the sentence.
        Splitting it any other way would have moved one honest rule to make the other convenient.
        """
        url, payload = self._embed_request(text, model)
        return {"url": url, "headers": {"Content-Type": "application/json", **self.headers()},
                "payload": payload}

    def parse_vector(self, raw: bytes) -> List[float]:
        """One vector out of a response body, or a raised error saying why there is not one."""
        reply = self.parse(raw, "")
        if not reply.ok or not reply.vectors or not reply.vectors[0]:
            raise ProviderFailed(f"{self.name} embedding: "
                                 f"{reply.error or 'no vector came back'}")
        return reply.vectors[0]

    def transcribe(self, audio: bytes, *, model: str = "") -> ProviderReply:
        """Not a provider task yet, and said so instead of quietly answering nothing.

        Voice keeps its own OpenAI-shaped POST in `jarvis/voice/stt.py`; a caller that came here
        for a transcript while that stayed true would get an empty string dressed as a result.
        """
        raise ProviderCapabilityMissing(
            "transcription is not routed through a provider yet -- voice answers still come from "
            "jarvis/voice/stt.py, which is OpenAI-shaped (open item 60-A)")


class OpenAIProvider(AIProvider):
    """The shape this build already used: `/chat/completions`, `Bearer`, the model in the body."""

    name = "openai"

    def _chat_request(self, system: str, user: str, model: str,
                      json_mode: bool) -> tuple:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.1,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return f"{self.root()}/chat/completions", payload

    def _embed_request(self, text: str, model: str) -> tuple:
        return f"{self.root()}/embeddings", {"model": model, "input": text}

    def parse(self, raw: bytes, model: str) -> ProviderReply:
        try:
            data = json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            return ProviderReply(False, self.name, model, error=f"unreadable response: {exc}")
        if isinstance(data.get("error"), dict) or "error" in data:
            return ProviderReply(False, self.name, model,
                                 error=_vendor_message(raw, 200, self.api_key))
        if "data" in data:
            rows = data.get("data") or []
            if not rows:
                return ProviderReply(False, self.name, model,
                                     error="provider returned no vectors")
            return ProviderReply(True, self.name, model,
                                 vectors=[list(map(float, row.get("embedding") or []))
                                          for row in rows])
        choices = data.get("choices") or []
        if not choices:
            return ProviderReply(False, self.name, model, error="provider returned no choices")
        text = str(((choices[0].get("message") or {}).get("content")) or "")
        if not text:
            return ProviderReply(False, self.name, model, error="provider returned an empty answer")
        return ProviderReply(True, self.name, model, text=text,
                             notes={"usage": data.get("usage") or {}})


class GeminiProvider(AIProvider):
    """Google's REST surface: the model in the path, the key in `x-goog-api-key`, parts in the body.

    The spec named the official `google-genai` SDK. It is not installed on this machine, and adding
    a dependency to a stdlib-first bundle is a decision rather than a detail, so this speaks the
    REST API that SDK wraps. The deviation is recorded in the phase report, not left implicit.
    """

    name = "gemini"
    API_VERSION = "v1beta"

    def root(self) -> str:
        """One version segment, whichever spelling of the base URL the user typed.

        `JARVIS_LLM_BASE_URL` is written for the OpenAI shape, so it usually ends in `/v1` -- and
        `READ ME FIRST.txt` in the delivery folder has always told Gemini users the compatibility
        address, `…/v1beta/openai/`. Both arrive here. What neither can be is a second version
        segment appended to them: `…/v1beta/openai/v1beta/models/…` is how a documented setup ends
        up answering nothing, and it is the case this method was wrong about until the shipped
        readme was read instead of assumed.
        """
        base = self.base_url.rstrip("/")
        for tail in ("/v1", "/openai"):
            if base.endswith(tail):
                base = base[: -len(tail)]
        if base.endswith(f"/{self.API_VERSION}"):
            return base
        return f"{base}/{self.API_VERSION}"

    def headers(self) -> Dict[str, str]:
        return {"x-goog-api-key": self.api_key}

    def _chat_request(self, system: str, user: str, model: str,
                      json_mode: bool) -> tuple:
        body: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.1},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        return f"{self.root()}/models/{model}:generateContent", body

    def _embed_request(self, text: str, model: str) -> tuple:
        return (f"{self.root()}/models/{model}:embedContent",
                {"model": f"models/{model}", "content": {"parts": [{"text": text}]}})

    def parse(self, raw: bytes, model: str) -> ProviderReply:
        try:
            data = json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            return ProviderReply(False, self.name, model, error=f"unreadable response: {exc}")
        if data.get("error"):
            return ProviderReply(False, self.name, model,
                                 error=_vendor_message(raw, 200, self.api_key))
        if "embedding" in data:
            values = list(map(float, (data.get("embedding") or {}).get("values") or []))
            if not values:
                return ProviderReply(False, self.name, model,
                                     error="provider returned no vectors")
            return ProviderReply(True, self.name, model, vectors=[values])
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            reason = f"prompt blocked: {blocked}" if blocked else "provider returned no candidates"
            return ProviderReply(False, self.name, model, error=reason)
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = " ".join(str(part.get("text", "")) for part in parts).strip()
        if not text:
            finish = str(candidates[0].get("finishReason") or "no text").lower()
            return ProviderReply(False, self.name, model,
                                 error=f"provider returned an empty answer ({finish})")
        return ProviderReply(True, self.name, model, text=text,
                             notes={"finish": candidates[0].get("finishReason") or ""})


#: The names `JARVIS_AI_PROVIDER` accepts.
PROVIDERS: Dict[str, type] = {"openai": OpenAIProvider, "gemini": GeminiProvider}


def select_provider(settings=None) -> AIProvider:
    """Build the provider the configuration names, or refuse and say what was available.

    A name that is not on the list is an error rather than a default: using another vendor quietly
    would make every status page, log line and reply that names a model false.
    """
    from ..config import get_settings

    settings = settings or get_settings()
    name = str(getattr(settings, "ai_provider", "openai") or "openai").strip().lower()
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ProviderUnknown(
            f"JARVIS_AI_PROVIDER={name!r} is not a provider this build can talk to. "
            f"Known: {', '.join(sorted(PROVIDERS))}")
    return cls(settings)


def provider_capabilities(settings=None) -> Dict[str, str]:
    """Which task goes through which vendor, stated as a fact rather than implied.

    The two shapes that are *not* provider-routed are named as themselves instead of being left
    out, so a reader of `/health` sees the boundary instead of assuming past it.
    """
    from ..config import get_settings

    settings = settings or get_settings()
    try:
        active = select_provider(settings).name
    except ProviderUnknown as exc:
        # Four keys, same as the healthy branch. Dropping vision and stt here made the refusal
        # path answer a different *shape* of question than the working one, so a reader that asked
        # "and screen text?" got silence -- which reads as "nothing to report", not as
        # "nobody has looked". A blank is not a boundary.
        return {"chat": f"refused: {exc}", "embed": f"refused: {exc}",
                "vision": "not asked -- the vendor could not be chosen",
                "stt": "not asked -- the vendor could not be chosen"}
    return {"chat": active, "embed": active,
            "vision": "openai-shaped, not provider-routed",
            "stt": "openai-shaped, not provider-routed"}
