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
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

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


def _error_category(status: int) -> str:
    """A stable, coarse label for a failed model call, so a UI/log can say *why*
    without parsing prose. Deliberately conservative: only the statuses whose meaning
    is unambiguous are named; everything else is ``upstream_failure``."""
    if status in (401, 403):
        return "auth_failed"
    if status == 404:
        return "model_not_found"
    if status == 410:
        # Gone: a withdrawn model or a dead route. Only the status says "gone" here;
        # the caller may sharpen it to `route_unavailable` when gateway metadata
        # confirms it was a route failure rather than a withdrawn model.
        return "upstream_gone"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "bad_request"
    if status >= 500:
        return "server_error"
    return "upstream_failure"


class AIProvider:
    """The shape every vendor adapter fills, and the one place a POST is built."""

    name = ""

    #: Whether this vendor refuses a request that carries no credential. True for
    #: every hosted vendor; a loopback connector that authenticates nobody (its
    #: gateway binds 127.0.0.1 and runs with auth disabled) sets it False so an
    #: empty key is a fact about the endpoint rather than an unconfigured state.
    REQUIRES_KEY = True

    def __init__(self, settings=None) -> None:
        from ..config import get_settings

        self.settings = settings or get_settings()

    # -- what the settings say ----------------------------------------------

    @property
    def base_url(self) -> str:
        """The effective endpoint for this provider.

        Env/``llm.env`` first, then the UI-persisted selection, then this adapter's own
        default. A provider always has a default, so a configured name is never left
        pointing nowhere; resolving lazily keeps a settings-screen save visible to a
        provider built earlier in the process.
        """
        from .selection import effective_base_url
        return effective_base_url(self.settings, self.name)

    @property
    def api_key(self) -> str:
        """The effective key for this provider.

        An explicitly configured key (env/``llm.env``) wins; otherwise the key the
        AI-settings screen stored in the Keychain is used. This is the bridge that makes
        a UI-saved key reach the adapter instead of sitting unread while the router
        only saw the (empty) environment. An absent key stays absent -- never invented.
        """
        from .selection import effective_key
        return effective_key(self.settings, self.name) or ""

    def configured(self) -> bool:
        if not self.base_url:
            return False
        return bool(self.api_key) or not self.REQUIRES_KEY

    def root(self) -> str:
        return self.base_url.rstrip("/")

    def headers(self) -> Dict[str, str]:
        if not self.api_key:
            # A keyless connector must not send a blank `Bearer `: that is a
            # malformed credential to a vendor, and needless noise to one that
            # authenticates nobody.
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    # -- the one POST -------------------------------------------------------

    def _post(self, url: str, payload: Dict[str, Any], *, timeout: float,
              model: str, source: str = "chat") -> ProviderReply:
        """Send, read inside a total budget, and turn whatever came back into a reply.

        `parse` is the only part a vendor is allowed to differ in, which is the whole argument for
        putting this here instead of in each adapter.

        ``source`` tags the health observation ("chat" for a real request, "test" for a
        ``verify_connection`` probe) so diagnostics can say what produced a status.
        Recording happens exactly once here; callers must not record again.

        **No retries.** A chat completion is a paid, non-idempotent operation with no
        idempotency key, so re-sending after an ambiguous timeout could bill twice or
        produce a second persisted assistant reply. This method therefore makes exactly
        one attempt and returns the failure verbatim. Callers that want a fallback (a
        different model/provider) must do so explicitly and only under a documented
        policy; nothing here silently re-sends or substitutes.
        """
        if not self.configured():
            return ProviderReply(False, self.name, model, error="no model endpoint is configured")
        request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json",
                                                  **self.headers()},
                                         method="POST")
        started = time.monotonic()
        try:
            # `gap` is the socket-inactivity timeout passed to urlopen; `total` is the
            # whole-call budget. A model that *reasons* (a combo router like 9Router's
            # `smooth` sends to reasoning upstreams) can stay silent for several seconds
            # before its first token, so a short gap (the default 5s) kills a call the
            # total budget was happy to allow. Set the gap to the total budget: the only
            # thing that may end a chat turn is its own deadline, never a fixed 5s of
            # silence between reasoning steps.
            raw = http_read(request, total=timeout, gap=timeout,
                            what=f"the {self.name} model endpoint")
            reply = self.parse(raw, model)
        except DeadlineExceeded as exc:
            reply = ProviderReply(False, self.name, model,
                                  error=_redact(f"timeout: {exc}", self.api_key),
                                  notes={"category": "timeout"})
        except urllib.error.HTTPError as exc:
            detail = exc.read() if hasattr(exc, "read") else b""
            reply = ProviderReply(False, self.name, model,
                                  error=_vendor_message(detail, exc.code, self.api_key),
                                  status=exc.code,
                                  notes={"category": _error_category(exc.code)})
        except urllib.error.URLError as exc:
            reply = ProviderReply(False, self.name, model,
                                  error=_redact(f"could not be reached: {exc.reason}", self.api_key),
                                  notes={"category": "network"})
        except (TimeoutError, OSError) as exc:
            # A socket inactivity timeout (the urlopen `gap`) is a distinct failure
            # from the whole-call deadline (DeadlineExceeded above): a slow-but-alive
            # route and a silent one must both read as "timeout". `socket.timeout` is
            # a subclass of `OSError`/`TimeoutError`, and previously fell into the
            # generic branch below, mislabelled "network".
            reply = ProviderReply(False, self.name, model,
                                  error=_redact(f"timeout: {exc}", self.api_key),
                                  notes={"category": "timeout"})
        except Exception as exc:  # a transport surprise is a failure, never an empty answer
            reply = ProviderReply(False, self.name, model,
                                  error=_redact(f"call failed: {exc}", self.api_key),
                                  notes={"category": "network"})

        self._record_health(reply, latency_ms=int((time.monotonic() - started) * 1000),
                            source=source)
        return reply

    def _record_health(self, reply: "ProviderReply", *, latency_ms: int, source: str) -> None:
        """Record one observation into the per-provider/model route-health ledger.

        ``parse`` places upstream metadata in ``reply.notes`` (``upstream_model`` /
        ``upstream_provider``) when the gateway exposes it; everything else is
        derived from the reply's own outcome. Never stores the request/response text.
        """
        try:
            from .route_health import get_route_health

            notes = reply.notes or {}
            get_route_health().record(
                reply.provider,
                reply.model,
                ok=reply.ok,
                source=source,
                status=int(reply.status or 0),
                category=notes.get("category", ""),
                latency_ms=latency_ms,
                upstream_model=notes.get("upstream_model", ""),
                upstream_id=notes.get("upstream_route_id", ""),
                upstream_identity_known=bool(
                    notes.get("upstream_model") or notes.get("upstream_route_id")
                ),
                error=reply.error,
            )
        except Exception:  # health recording is best-effort, never a chat failure
            pass

    def parse(self, raw: bytes, model: str) -> ProviderReply:
        raise NotImplementedError

    # -- the tasks ----------------------------------------------------------

    def _chat_request(self, system: str, user: str, model: str,
                      json_mode: bool, history: Sequence[Dict[str, str]] = ()) -> tuple:
        """`(url, payload)` for one chat turn, in this vendor's language.

        `history` is the prior conversation, oldest first, as `{"role",
        "content"}` with role in `user`/`assistant`. The default is empty, so
        every existing caller keeps its single-turn behavior.
        """
        raise NotImplementedError

    def _embed_request(self, text: str, model: str) -> tuple:
        """`(url, payload)` for one text, in this vendor's language."""
        raise NotImplementedError

    async def chat(self, system: str, user: str, *, model: str, json_mode: bool = False,
                   history: Sequence[Dict[str, str]] = (),
                   timeout: float = DEFAULT_TIMEOUT_S) -> ProviderReply:
        url, payload = self._chat_request(system, user, model, json_mode, history)
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
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    # Configured default model list - confirmed valid by current OpenAI docs
    SUPPORTED_MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "gpt-4",
        "gpt-3.5-turbo",
    ]

    def _chat_request(self, system: str, user: str, model: str,
                      json_mode: bool, history: Sequence[Dict[str, str]] = ()) -> tuple:
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        # Prior turns, oldest first. Only wire-valid roles with content — a
        # stray role is a 400 from the vendor, not a conversation.
        for turn in history:
            role = turn.get("role")
            content = str(turn.get("content") or "")
            if role in ("user", "assistant") and content.strip():
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user})
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            # This adapter reads one whole JSON answer synchronously. Tell the
            # server not to stream, or an SSE-by-default gateway (9Router,
            # OpenRouter) returns `data: {chunk}` lines this parser cannot decode.
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return f"{self.root()}/chat/completions", payload

    def _embed_request(self, text: str, model: str) -> tuple:
        return f"{self.root()}/embeddings", {"model": model, "input": text}

    def parse(self, raw: bytes, model: str) -> ProviderReply:
        data: Dict[str, Any]
        try:
            data = json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            return ProviderReply(False, self.name, model, error=f"unreadable response: {exc}",
                                 notes={"category": "malformed_response"})
        # The configured model may be a routing alias; the response's own `model`
        # field names the upstream that actually answered. Record it when present so
        # health diagnostics can say "smooth routed to poolside/laguna-s-2.1:free"
        # without guessing provider identity from an arbitrary string.
        upstream: Dict[str, Any] = {}
        in_model = str(data.get("model") or "") if isinstance(data, dict) else ""
        if in_model and in_model != model:
            upstream["upstream_model"] = in_model
        if isinstance(data, dict) and isinstance(data.get("provider"), str) and data.get("provider"):
            upstream["upstream_provider"] = str(data["provider"])

        if isinstance(data.get("error"), dict) or "error" in data:
            notes = {"category": "provider_error", **upstream}
            # A 9Router error body often nests the *upstream's* status + message, e.g.
            #   [route-id/model] [404]: {"error":{"code":"model_not_found"}}
            # Extract a precise classification from that nested reason when it is there.
            err = data.get("error")
            if isinstance(err, dict):
                code = str(err.get("code") or err.get("type") or "").lower()
                msg = str(err.get("message") or "")
            else:
                code, msg = "", str(err or "")
            if "model_not_found" in code or "not exist" in msg.lower() or "not found" in msg.lower():
                notes["category"] = "model_not_found"
            elif code in ("invalid_api_key", "invalid_request_error", "authentication_error"):
                notes["category"] = "auth_failed"
            return ProviderReply(False, self.name, model,
                                 error=_vendor_message(raw, 200, self.api_key),
                                 notes=notes)
        if "data" in data:
            rows = data.get("data") or []
            if not rows:
                return ProviderReply(False, self.name, model,
                                     error="provider returned no vectors",
                                     notes={"category": "empty_response", **upstream})
            return ProviderReply(True, self.name, model,
                                 vectors=[list(map(float, row.get("embedding") or []))
                                          for row in rows],
                                 notes=upstream)
        choices = data.get("choices") or []
        if not choices:
            return ProviderReply(False, self.name, model, error="provider returned no choices",
                                 notes={"category": "empty_response", **upstream})
        text = str(((choices[0].get("message") or {}).get("content")) or "")
        if not text:
            return ProviderReply(False, self.name, model, error="provider returned an empty answer",
                                 notes={"category": "empty_response", **upstream})
        return ProviderReply(True, self.name, model, text=text,
                             notes={"usage": data.get("usage") or {}, **upstream})

    def verify_connection(self, *, timeout: float = 5.0, model: str = "") -> ProviderReply:
        """Prove a real chat completion works, same path the assistant uses.

        A "connection test" that only hits ``GET /models`` cannot prove chat works —
        and many compatible endpoints (9Router, local model servers) do not expose a
        model list at all, or the list changes, so a static ``SUPPORTED_MODELS`` match
        would turn a working endpoint into a false "not configured". The truth of
        "can this provider answer" is one small completion; reuse ``_chat_request`` +
        ``_post`` so the wire shape is exactly what a real chat turn sends.

        ``model`` is required by the callers that route verify through a test key;
        it defaults to the adapter's first supported model for the optional
        zero-argument path, but callers with a configured model must pass it.
        """
        if not model:
            models = getattr(self, "SUPPORTED_MODELS", [])
            model = (models[0] if models else "")
        if not model:
            return ProviderReply(False, self.name, "verify",
                                 error="no model id configured — set a model before testing")
        url, payload = self._chat_request(
            "", "Reply with exactly: VANTAGE connection test successful",
            model, json_mode=False,
        )
        return self._post(url, payload, timeout=timeout, model=model, source="test")


class GroqProvider(OpenAIProvider):
    """Groq's OpenAI-compatible API: same wire shape, a different base URL and models.

    Verified against Groq's documented endpoint and the list of Groq model ids this
    machine's model catalog carries; nothing here invents an endpoint. Auth is the
    same Bearer key, so the OpenAI adapter is reused rather than duplicated.
    """

    name = "groq"
    DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

    SUPPORTED_MODELS = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
        "gemma2-9b-it",
    ]


class NineRouterProvider(OpenAIProvider):
    """The local 9Router gateway (127.0.0.1:20128) -- an OpenAI-compatible router.

    Its own catalog records the node as ``openai-compatible-chat``, so the wire format
    is exactly the OpenAI shape; only the base URL (and the fact that it is a local,
    on-device default) differs. Treated as its own provider so selecting it is explicit,
    never a silent substitution. Model ids are passed through unchanged (the router
    owns its model namespace, e.g. ``clinek/…``).
    """

    name = "9router"
    DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1"

    SUPPORTED_MODELS = []  # model list is whatever the running gateway reports; no
    # hand-maintained list that could go stale or pretend a model exists that doesn't.

    def parse(self, raw: bytes, model: str) -> ProviderReply:
        """The 9Router gateway appends ``data: [DONE]`` after its single JSON
        answer (it reports a streaming shape even for a non-streamed request).
        Strip that trailing sentinel before the OpenAI parser decodes the body,
        or every completion fails with "unreadable response: Extra data".

        Also extracts the route id the gateway names when an upstream fails; 9Router
        reports it as ``[openai-compatible-chat-<route-id>/<model>]`` inside the
        error message. That id identifies the *route* (not necessarily the upstream
        vendor), so it is surfaced under ``upstream_route_id`` only, never claimed to
        be a provider name.
        """
        text = raw.decode("utf-8", "replace")
        done_marker = "data: [DONE]"
        if done_marker in text:
            # Keep everything before the first trailing SSE sentinel; a normal
            # JSON body plus the sentinel is the observed shape.
            text = text[: text.rindex(done_marker)].rstrip()

        reply = super().parse(text.encode("utf-8"), model)

        # Extract a route id from a 9Router-style error body, e.g.
        #   {"error":{"message":"[openai-compatible-chat-<id>/model] [404]: ..."}}
        route_id = ""
        try:
            data = json.loads(text or "{}")
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or "")
            elif isinstance(err, str):
                msg = err
            else:
                msg = str(err or "")
            match = re.search(r"openai-compatible-chat-([A-Za-z0-9-]+)", msg)
            if match:
                route_id = match.group(1)
        except (ValueError, AttributeError):
            route_id = ""

        if route_id:
            notes = dict(reply.notes or {})
            notes["upstream_route_id"] = route_id
            # A route that answered 404 "Model not exist" from its upstream is a
            # confirmed dead route, not a withdrawn top-level model — sharpen the
            # category so the health ledger counts it as route evidence.
            if notes.get("category") == "model_not_found":
                notes["category"] = "route_unavailable"
            reply.notes = notes
        return reply

    def gateway_reachable(self, *, timeout: float = 3.0) -> bool:
        """Whether the gateway's own process answers its health endpoint.

        A cheap, side-effect-free, unrecoverable-cost check: 9Router serves
        ``/api/health`` → ``{"ok": true}``. The base URL here ends in ``/v1``, so the
        health endpoint is one segment up. This proves only that the *gateway* is
        up — never that a model can complete a request — so it is a distinct signal
        from route health.
        """
        root = self.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")] or root
        try:
            req = urllib.request.Request(f"{root}/api/health", method="GET")
            raw = http_read(req, total=timeout, gap=timeout, what="the 9Router gateway")
            data = json.loads(raw.decode() or "{}")
            return bool(data.get("ok") is True)
        except Exception:
            return False


class OpenAICompatibleProvider(OpenAIProvider):
    """A user-supplied OpenAI-compatible endpoint (zcode, opencode, LM Studio, …).

    Everything — the base URL, the model id and the key — is what the user enters,
    so the DEFAULT_BASE_URL is deliberately empty: ``effective_base_url`` then resolves
    to the persisted selection's ``llm_base_url`` (which the settings screen writes),
    and an empty/unsaved base URL means "not configured" rather than pointing at a
    vendor the user never chose. The wire shape is OpenAI's (``/chat/completions``,
    ``Bearer``, model in the body) because that is the compatibility contract.
    """

    name = "openai_compatible"
    DEFAULT_BASE_URL = ""

    #: No hand-maintained list: the model is whatever the user's own server serves.
    SUPPORTED_MODELS = []


class PollinationsProvider(OpenAIProvider):
    """Pollinations' generation gateway.

    OpenAI-shaped for chat (``/chat/completions``) and for images
    (``/images/generations``, returning ``b64_json``), and it publishes a real
    catalogue at ``/models`` whose entries declare ``output_modalities`` and
    ``supported_endpoints``. That declaration is what makes capability discovery
    possible without guessing from a model name — measured 2026-10-29: 147
    models, 16 of them image-output, each naming the endpoints it serves.
    """

    name = "pollinations"
    DEFAULT_BASE_URL = "https://gen.pollinations.ai/v1"

    #: No hand-maintained list. `discover_models` reads the catalogue, which is
    #: the provider's own statement of what it serves.
    SUPPORTED_MODELS = []

def discover_models(base_url: str, api_key: str = "", *, timeout: float = 20.0,
                    auth_header: str = "") -> Dict[str, Any]:
    """``GET {base_url}/models``, with the entries' declared capabilities.

    Returns ``{"ok": bool, "models": [...], "error": str}``. Each model carries
    ``id``, ``label``, ``output_modalities`` and ``supported_endpoints`` when the
    provider publishes them, so a caller can tell an image model from a chat
    model without inferring anything from its name.

    A failed discovery is reported as a failure with a reason — it is never an
    empty list, which would read as "this provider serves no models".
    """
    import json as _json
    import urllib.error
    import urllib.request

    from ..net import http_read

    root = (base_url or "").rstrip("/")
    if not root:
        return {"ok": False, "models": [], "error": "No base URL to discover from."}
    if not root.endswith("/models"):
        root = root + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers[auth_header or "Authorization"] = (api_key if auth_header
                                                   else f"Bearer {api_key}")
    req = urllib.request.Request(root, headers=headers, method="GET")
    try:
        raw = http_read(req, total=float(timeout), what="the provider's model list")
    except Exception as exc:
        return {"ok": False, "models": [],
                "error": f"Could not read the model list ({type(exc).__name__})."}
    try:
        parsed = _json.loads(raw or b"{}")
    except Exception:
        return {"ok": False, "models": [],
                "error": "The model list was not JSON."}
    items = parsed.get("data") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return {"ok": False, "models": [], "error": "The model list had no entries."}

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        out.append({
            "id": model_id,
            "label": str(item.get("title") or model_id),
            "description": str(item.get("description") or "")[:200],
            "output_modalities": [str(x) for x in (item.get("output_modalities") or [])],
            "input_modalities": [str(x) for x in (item.get("input_modalities") or [])],
            "supported_endpoints": [str(x) for x in (item.get("supported_endpoints") or [])],
            "category": str(item.get("category") or ""),
        })
    return {"ok": True, "models": out, "error": ""}

class GeminiProvider(AIProvider):
    """Google's REST surface: the model in the path, the key in `x-goog-api-key`, parts in the body.

    The spec named the official `google-genai` SDK. It is not installed on this machine, and adding
    a dependency to a stdlib-first bundle is a decision rather than a detail, so this speaks the
    REST API that SDK wraps. The deviation is recorded in the phase report, not left implicit.
    """

    name = "gemini"
    API_VERSION = "v1beta"

    #: Google's native REST root. `root()` appends one ``/v1beta`` segment if the URL
    #: does not already carry it, so this default ends *with* the version to avoid a
    #: doubled segment. Without a default, an unconfigured Gemini built a relative URL
    #: ("/v1beta/models/...") that can never be reached.
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    SUPPORTED_MODELS = [
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ]

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
                      json_mode: bool, history: Sequence[Dict[str, str]] = ()) -> tuple:
        # Gemini speaks a turn list, not chat messages: prior turns map
        # user->user, assistant->model. Alternation is normalized — vendors
        # reject a run of two same-role turns, so consecutive same-role turns
        # are merged, which preserves every character.
        contents: List[Dict[str, Any]] = []
        for turn in history:
            role = "model" if turn.get("role") == "assistant" else "user"
            text = str(turn.get("content") or "")
            if not text.strip():
                continue
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"][0]["text"] += "\n" + text
            else:
                contents.append({"role": role, "parts": [{"text": text}]})
        contents.append({"role": "user", "parts": [{"text": user}]})
        body: Dict[str, Any] = {
            "contents": contents,
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


class Chat2APIProvider(OpenAIProvider):
    """The local chat2api pool (``ai-gateway.mjs`` on 127.0.0.1:8099).

    A loopback connector that spreads each request across a pool of free accounts
    and fails over to the next one on a rate limit or upstream error. The wire
    shape is OpenAI's; what differs is authentication: the gateway has none (it
    binds loopback only, so there is no credential to steal or rotate), which is
    why ``REQUIRES_KEY`` is False. Its model list is whatever the running gateway
    reports (currently ``gpt-4o-mini``), so nothing is hand-maintained here.
    """

    name = "chat2api"
    DEFAULT_BASE_URL = "http://127.0.0.1:8099/v1"
    REQUIRES_KEY = False
    SUPPORTED_MODELS = []


#: The names `JARVIS_AI_PROVIDER` accepts.
PROVIDERS: Dict[str, type] = {
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "9router": NineRouterProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "chat2api": Chat2APIProvider,
    "pollinations": PollinationsProvider,
}


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
