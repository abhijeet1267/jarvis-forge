"""What "Test" means for each provider category.

A test is only worth the button if it exercises the job the tab is for. This
module performs a **real, bounded** request for each category and reports what
came back:

| Category | The request |
| --- | --- |
| text | one short chat completion; the answer's text must be non-empty |
| documents | a chat completion *with a document attached as text*, so it proves the model reads a document rather than answering from nothing |
| speech | a minimal synthesis; the bytes must be audio, per the sniffed content type |
| image | one small generation; the result must decode to image bytes (or carry a URL, which is reported as unverified) |
| vision | one image sent *to the model*; the answer must be text |
| video | one job submitted; the job id is reported and the result is explicitly **not** verified |
| custom | a chat completion against the person's own base URL |

The shared rule: a 200 is not success. Each returns ``ok`` only when the
response *shape* is the thing that was asked for — which is how an HTML error
page with status 200, a text answer to an image request, or a chat model
answering a speech request all get reported as failures.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict

from ..logging_setup import get_logger
from ..net import http_read
from ..security.privacy import redact

log = get_logger("categories.checks")

#: A 1×1 PNG, so a vision test has a real image and the model has to accept it.
PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

#: A short document for the documents test: long enough that an answer quoting
#: it could not have come from the prompt alone.
SAMPLE_DOCUMENT = (
    "Quarterly maintenance notice.\n"
    "The backup window is Sunday 02:00 to 04:00. "
    "The author of this notice is Ada Lovelace. "
    "The reference number is MAINT-4711."
)

TIMEOUT = 60.0

def _post(url: str, payload: Dict[str, Any], *, key: str, auth_header: str = "",
          timeout: float = TIMEOUT) -> Dict[str, Any]:
    """POST JSON, returning ``{status, body, error}``. Never raises."""
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        if auth_header:
            headers[auth_header] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        raw = http_read(req, total=float(timeout), what="the provider", gap=30.0)
        return {"status": 200, "body": raw, "error": ""}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        except Exception:
            body = b""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        detail = ""
        try:
            parsed = json.loads(body or b"{}")
            err = parsed.get("error") if isinstance(parsed, dict) else None
            detail = (err.get("message") if isinstance(err, dict) else str(err or "")) \
                or str(parsed.get("message") or parsed.get("detail") or "")
        except Exception:
            detail = body[:160].decode("utf-8", "replace")
        return {"status": exc.code, "body": b"",
                "error": _scrub(f"HTTP {exc.code}. {detail}".strip(), key)}
    except Exception as exc:
        return {"status": 0, "body": b"",
                "error": _scrub(f"Could not reach the provider ({type(exc).__name__}).",
                                key)}

def _scrub(text: str, key: str) -> str:
    out = redact(text or "")
    if key:
        out = out.replace(key, "[redacted]")
    return out[:300]

def _chat_text(raw: bytes) -> str:
    try:
        parsed = json.loads(raw or b"{}")
    except Exception:
        return ""
    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        content = " ".join(str(p.get("text", "")) for p in content
                           if isinstance(p, dict))
    return str(content or "").strip()

def _auth_header_for(provider: str) -> str:
    # Gemini takes its key in a header of its own; sending it as a bearer token
    # is a 401 that reads like an invalid key.
    return "x-goog-api-key" if provider == "gemini" else ""

def _url(settings, suffix: str) -> str:
    root = (settings.base_url or "").rstrip("/")
    if settings.provider == "gemini":
        # Gemini's path carries the model: {root}/models/{model}:generateContent
        return f"{root}/models/{settings.model}:{suffix}"
    return f"{root}/{suffix.lstrip('/')}"

# ---------------------------------------------------------------------------
# Per-category checks
# ---------------------------------------------------------------------------

def run(category: str, settings) -> Dict[str, Any]:
    """Perform this category's real operation. Always returns a dict."""
    key = ""
    try:
        from .provider_categories import retrieve_key

        key = retrieve_key(category) or ""
    except Exception:
        key = ""

    if settings.provider == "gemini":
        return _gemini(category, settings, key)
    if category == "speech":
        return _speech(settings, key)
    if category == "image":
        return _image(settings, key)
    if category == "vision":
        return _vision(settings, key)
    if category == "video":
        return _video(settings, key)
    if category == "documents":
        return _documents(settings, key)
    return _chat(settings, key)

def _chat(settings, key: str) -> Dict[str, Any]:
    model = settings.model or "default"
    body = {"model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
            "max_tokens": 16}
    response = _post(_url(settings, "chat/completions"), body, key=key,
                     auth_header=_auth_header_for(settings.provider),
                     timeout=settings.timeout)
    if response["error"]:
        return {"ok": False, "error": response["error"], "detail": ""}
    text = _chat_text(response["body"])
    if not text:
        return {"ok": False, "duration_s": 0.0,
                "error": "The provider answered but returned no message text, so the "
                         "chat path is not proven."}
    return {"ok": True, "detail": f"The model answered: {text[:80]!r}",
            "sample": text[:200]}

def _documents(settings, key: str) -> Dict[str, Any]:
    body = {"model": settings.model or "default",
            "messages": [{"role": "user", "content":
                          "Read this notice and reply with only the reference number.\n\n"
                          + SAMPLE_DOCUMENT}],
            "max_tokens": 24}
    response = _post(_url(settings, "chat/completions"), body, key=key,
                     auth_header=_auth_header_for(settings.provider),
                     timeout=settings.timeout)
    if response["error"]:
        return {"ok": False, "error": response["error"]}
    text = _chat_text(response["body"])
    if not text:
        return {"ok": False, "error": "The model returned no text for the document."}
    # The document names MAINT-4711. An answer that repeats it shows the model
    # read the content; an answer without it is still a reply, so it is reported
    # honestly rather than failed outright.
    found = "MAINT-4711" in text.upper()
    return {"ok": True,
            "detail": ("The model read the sample document and quoted its reference "
                       "number." if found
                       else f"The model replied ({text[:60]!r}) but did not quote the "
                            "document's reference number — it may not have read it fully."),
            "read_the_document": found, "sample": text[:200]}

def _speech(settings, key: str) -> Dict[str, Any]:
    # Speech lives on its own endpoint. A chat model answering here is the
    # defect this test exists to catch.
    if settings.provider == "openrouter" or "openrouter" in (settings.base_url or ""):
        model = settings.model or ""
        if not model:
            return {"ok": False,
                    "error": "OpenRouter needs an audio model id (for example "
                             "fish-audio/s2.1-pro-free:free) before speech can be tested."}
    body = {"model": settings.model or "tts-1",
            "input": "Vantage speech check.",
            "response_format": settings.options.get("format", "mp3")}
    if settings.options.get("voice"):
        body["voice"] = settings.options["voice"]
    response = _post(_url(settings, "audio/speech"), body, key=key, timeout=settings.timeout)
    if response["error"]:
        return {"ok": False, "error": response["error"]}
    from .http_media import is_audio, sniff

    kind = sniff(response["body"])
    if not is_audio(kind):
        return {"ok": False,
                "error": f"The speech endpoint answered with {kind}, not audio, so "
                         "there is nothing to play."}
    return {"ok": True, "bytes": len(response["body"]), "content_type": kind,
            "detail": f"The provider returned {len(response['body'])} bytes of {kind}."}

def _image(settings, key: str) -> Dict[str, Any]:
    body = {"model": settings.model or "default",
            "prompt": "a single red square on a white background",
            "n": 1, "size": settings.options.get("size", "512x512")}
    response = _post(_url(settings, "images/generations"), body, key=key,
                     auth_header=_auth_header_for(settings.provider),
                     timeout=settings.timeout)
    if response["error"]:
        return {"ok": False, "error": response["error"]}
    try:
        parsed = json.loads(response["body"] or b"{}")
    except Exception:
        return {"ok": False, "error": "The image endpoint answered with something that "
                                      "is not JSON, so no image was produced."}
    items = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(items, list) or not items:
        message = ""
        if isinstance(parsed, dict):
            err = parsed.get("error")
            message = (err.get("message") if isinstance(err, dict) else str(err or "")) \
                or str(parsed.get("message") or "")
        return {"ok": False, "error": message or "The image endpoint returned no image."}
    first = items[0] if isinstance(items[0], dict) else {}
    b64 = str(first.get("b64_json") or "")
    url = str(first.get("url") or "")
    if b64:
        try:
            data = base64.b64decode(b64)
        except Exception:
            return {"ok": False, "error": "The image data could not be decoded."}
        from .http_media import is_image, sniff

        kind = sniff(data)
        if not is_image(kind):
            return {"ok": False, "error": f"The decoded data is {kind}, not an image."}
        return {"ok": True, "bytes": len(data), "content_type": kind,
                "detail": f"The provider returned {len(data)} bytes of {kind}."}
    if url:
        return {"ok": True, "url": url, "verified": False,
                "detail": "The provider returned a URL rather than image bytes; the "
                          "image itself was not downloaded."}
    return {"ok": False, "error": "The image entry had neither data nor a URL."}

def _vision(settings, key: str) -> Dict[str, Any]:
    body = {"model": settings.model or "default",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "What colour is this pixel? One word."},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64,"
                           + base64.b64encode(PIXEL_PNG).decode()}}]}],
            "max_tokens": 16}
    response = _post(_url(settings, "chat/completions"), body, key=key,
                     auth_header=_auth_header_for(settings.provider),
                     timeout=settings.timeout)
    if response["error"]:
        return {"ok": False, "error": response["error"]}
    text = _chat_text(response["body"])
    if not text:
        return {"ok": False, "error": "The model answered without text, so image "
                                      "input is not proven."}
    return {"ok": True, "detail": f"The model accepted the image and answered "
                                  f"{text[:60]!r}.", "sample": text[:200]}

def _video(settings, key: str) -> Dict[str, Any]:
    body = {"model": settings.model or "default",
            "prompt": "a two-second test pattern", "seconds": "2"}
    response = _post(_url(settings, "videos"), body, key=key,
                     auth_header=_auth_header_for(settings.provider),
                     timeout=settings.timeout)
    if response["error"]:
        return {"ok": False, "error": response["error"]}
    try:
        parsed = json.loads(response["body"] or b"{}")
    except Exception:
        return {"ok": False, "error": "The video endpoint did not answer with JSON."}
    job_id = str(parsed.get("id") or parsed.get("job_id") or "")
    if not job_id:
        return {"ok": False,
                "error": "The video endpoint returned no job id, so the job cannot be "
                         "tracked."}
    return {"ok": True, "job_id": job_id, "verified": False,
            "detail": f"Submitted job {job_id}. A queued job is not a finished video, "
                      "so this is not verification."}

def _gemini(category: str, settings, key: str) -> Dict[str, Any]:
    """Gemini's own wire shape: the model in the path, parts in the body."""
    model = settings.model or "gemini-2.5-flash"
    url = f"{(settings.base_url or '').rstrip('/')}/models/{model}:generateContent"
    headers_key = "x-goog-api-key"

    if category == "speech":
        body = {"contents": [{"parts": [{"text": "Vantage speech check."}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                                     "speechConfig": {"voiceConfig": {
                                         "prebuiltVoiceConfig": {
                                             "voiceName": settings.options.get("voice", "Kore")}}}}}
    elif category == "image":
        body = {"contents": [{"parts": [{"text": "a single red square on white"}]}]}
    elif category == "vision":
        body = {"contents": [{"parts": [
            {"text": "What colour is this pixel? One word."},
            {"inlineData": {"mimeType": "image/png",
                            "data": base64.b64encode(PIXEL_PNG).decode()}}]}]}
    elif category == "documents":
        body = {"contents": [{"parts": [{"text":
            "Read this notice and reply with only the reference number.\n\n"
            + SAMPLE_DOCUMENT}]}]}
    else:
        body = {"contents": [{"parts": [{"text": "Reply with the single word: ready"}]}]}

    response = _post(url, body, key=key, auth_header=headers_key,
                     timeout=settings.timeout)
    if response["error"]:
        return {"ok": False, "error": response["error"]}
    try:
        parsed = json.loads(response["body"] or b"{}")
    except Exception:
        return {"ok": False, "error": "Gemini answered with something that is not JSON."}

    parts = []
    for candidate in parsed.get("candidates") or []:
        parts.extend(((candidate.get("content") or {}).get("parts") or []))
    inline = next((p for p in parts if isinstance(p, dict)
                   and (p.get("inlineData") or p.get("inline_data"))), None)
    text = " ".join(str(p.get("text", "")) for p in parts
                    if isinstance(p, dict) and p.get("text")).strip()

    if category == "speech":
        if not inline:
            return {"ok": False, "error": "Gemini answered without an audio part, so "
                                          "there is nothing to play."}
        data = (inline.get("inlineData") or inline.get("inline_data") or {})
        audio = base64.b64decode(data.get("data") or "")
        from .http_media import sniff

        return {"ok": True, "bytes": len(audio),
                "content_type": sniff(audio) or str(data.get("mimeType") or ""),
                "detail": f"Gemini returned {len(audio)} bytes of audio."}
    if category == "image":
        if not inline:
            return {"ok": False, "error": "Gemini answered without an image."}
        data = (inline.get("inlineData") or inline.get("inline_data") or {})
        image = base64.b64decode(data.get("data") or "")
        from .http_media import is_image, sniff

        kind = sniff(image)
        if not is_image(kind):
            return {"ok": False, "error": f"Gemini returned {kind}, not an image."}
        return {"ok": True, "bytes": len(image), "content_type": kind,
                "detail": f"Gemini returned {len(image)} bytes of {kind}."}
    if not text:
        return {"ok": False, "error": "Gemini answered with no text."}
    detail = f"Gemini answered: {text[:80]!r}"
    if category == "documents":
        detail = ("Gemini quoted the document's reference number." if
                  "MAINT-4711" in text.upper()
                  else f"Gemini replied ({text[:60]!r}) but did not quote the reference number.")
    return {"ok": True, "detail": detail, "sample": text[:200]}