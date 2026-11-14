"""Bounded HTTP for the media (non-text) providers.

The text gateway already has a careful HTTP client; this is the same shape for
the capabilities it does not cover — speech, images, video — where the payloads
are large and a timeout means a download that must be abandoned, not an answer
that is merely late.

Every call here:

* has a **total** deadline, not just a socket-inactivity timeout;
* sends the credential in a header and never in a query string (a query string
  lands in access logs, proxies and error messages);
* returns the body **as bytes** with a byte ceiling, because a TTS response is
  audio and an image response is an image — parsing them as JSON is how a
  wrong endpoint becomes an empty-looking success;
* scrubs the credential out of any error sentence.

A caller decides what the content type means. This layer never guesses: a 200
with `text/html` is not audio, and saying so is its job.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..net import DeadlineExceeded, http_read
from ..security.privacy import redact

#: A hard ceiling on any media response. Generous for audio and images; a
#: video generator that returns a file inline is expected to return a URL
#: instead, and this is the guard that makes that expectation real.
MAX_BYTES = 24 * 1024 * 1024

@dataclass
class HTTPResult:
    status: int
    body: bytes
    content_type: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    #: A sentence safe to show a person, when the call did not produce a body.
    error: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    def json(self) -> Optional[dict]:
        try:
            parsed = json.loads(self.body or b"{}")
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

def _scrub(text: str, secret: str) -> str:
    out = redact(text or "")
    if secret:
        out = out.replace(secret, "[redacted]")
    return out

def request(url: str, *, method: str = "GET", body: Optional[bytes] = None,
            headers: Optional[Dict[str, str]] = None, secret: str = "",
            secret_header: str = "",
            timeout: float = 60.0, max_bytes: int = MAX_BYTES,
            what: str = "the provider") -> HTTPResult:
    """One bounded exchange. Never raises for an HTTP or network failure.

    ``secret_header`` names the header a credential goes in. The default is
    ``Authorization: Bearer``, which is the OpenAI shape; Gemini uses
    ``x-goog-api-key`` with a bare value, and sending it as a bearer token gets
    a 401 that reads like a bad key rather than a wrong header.
    """
    all_headers = dict(headers or {})
    if secret:
        if secret_header:
            all_headers.setdefault(secret_header, secret)
        else:
            all_headers.setdefault("Authorization", f"Bearer {secret}")

    req = urllib.request.Request(url, data=body, headers=all_headers, method=method)
    try:
        # An image or video provider often spends many seconds *before* the
        # first byte arrives (queueing, sampling). `http_read`'s default 5s
        # socket gap measured 5.4s of silence on a healthy Pollinations request
        # and reported it as a transport failure, so the gap is widened here;
        # the total budget is still the caller's.
        raw = http_read(req, total=float(timeout), what=what, gap=30.0)
    except DeadlineExceeded:
        return HTTPResult(0, b"", error=f"{what} did not finish within {timeout:g} seconds.",
                          timed_out=True)
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read() or b""
        except Exception:
            err_body = b""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        detail = ""
        try:
            parsed = json.loads(err_body or b"{}")
            if isinstance(parsed, dict):
                err = parsed.get("error")
                if isinstance(err, dict):
                    detail = str(err.get("message") or "")
                elif isinstance(err, str):
                    detail = err
                detail = detail or str(parsed.get("detail") or parsed.get("message") or "")
        except Exception:
            detail = err_body[:200].decode("utf-8", "replace")
        return HTTPResult(exc.code, b"",
                          error=_scrub(f"{what} returned HTTP {exc.code}. {detail}".strip(),
                                       secret),
                          headers=dict(getattr(exc, "headers", {}) or {}))
    except Exception as exc:
        return HTTPResult(0, b"",
                          error=_scrub(f"Could not reach {what} ({type(exc).__name__}).",
                                       secret))

    if len(raw) > max_bytes:
        return HTTPResult(200, b"",
                          error=f"{what} returned {len(raw)} bytes, over the "
                                f"{max_bytes // (1024 * 1024)} MiB limit. It was not used.")
    return HTTPResult(200, raw, content_type=sniff(raw))

def sniff(raw: bytes) -> str:
    """The content type, from the bytes themselves.

    ``http_read`` returns a body, not a response object, so the header is gone.
    Magic numbers are what remain, and they are *more* trustworthy than a
    header anyway: a service that answers 200 with an HTML error page must not
    be read as audio, and its `Content-Type` is exactly what would lie.
    """
    head = (raw or b"")[:16]
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if head.startswith(b"\xff\xfb") or head.startswith(b"\xff\xf3") or head.startswith(b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"RIFF") and len(raw) > 12 and raw[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"RIFF") and len(raw) > 12 and raw[8:12] == b"WEBP":
        return "image/webp"
    # ISO base media (MP4/MOV/others): a 4-byte size, then `ftyp` at offset 4.
    if len(raw) > 12 and raw[4:8] == b"ftyp":
        return "video/mp4"
    if head.lstrip()[:1] in (b"{", b"["):
        return "application/json"
    if head.lstrip()[:1] == b"<":
        return "text/html"
    return "application/octet-stream"

def is_audio(content_type: str) -> bool:
    return content_type.startswith("audio/")

def is_image(content_type: str) -> bool:
    return content_type.startswith("image/")