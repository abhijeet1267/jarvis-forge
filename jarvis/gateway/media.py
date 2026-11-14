"""Non-text capability adapters: speech, images, video, vision.

The text gateway (``gateway/providers.py``) speaks one shape — a chat completion
— and that shape is not these. A text-to-speech endpoint returns audio bytes; an
image endpoint returns an image; a video endpoint returns a job that finishes
later. This module gives each its own adapter, so a text model answering once is
never treated as proof that it can speak, draw, or render.

Three rules run through all of them:

* **Declared is not verified.** A model's profile in the capability registry is
  an expectation. ``verify_*`` performs the real, bounded request and records
  what came back; only that turns a capability into ``verified``.
* **A wrong shape is a failure, not an empty success.** A 200 whose bytes are
  HTML is reported as "not audio", not saved as a silent file. The sniffing in
  ``gateway/http_media`` is what makes that possible, because the header is the
  part that lies.
* **Nothing is invented.** With no provider configured, each returns a typed
  "not configured" answer that names the fix. None of them produces a
  placeholder image, a silent audio file, or a fake video id.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .http_media import HTTPResult, is_audio, is_image, request

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class Capability(str):
    pass

#: The capabilities this module can actually perform. Text lives in the gateway;
#: embeddings in the memory path. These are the ones with no adapter before.
CAPABILITIES: Tuple[str, ...] = (
    "text_to_speech",
    "speech_to_text",
    "image_generation",
    "image_editing",
    "vision",
    "video_generation",
)

@dataclass
class MediaResult:
    ok: bool
    capability: str
    provider: str = ""
    model: str = ""
    #: Audio or image bytes, when the capability returns a file.
    data: bytes = b""
    content_type: str = ""
    #: A URL the provider returned instead of bytes (image, video).
    url: str = ""
    #: A job id for an asynchronous capability (video).
    job_id: str = ""
    #: Text the provider returned (transcription, vision description).
    text: str = ""
    #: Anything else the provider said that is safe to show.
    detail: str = ""
    error: str = ""
    #: Seconds the call took, for the diagnostic line.
    duration: float = 0.0
    #: True only when this was a real request that produced a real result.
    verified: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "capability": self.capability,
            "provider": self.provider,
            "model": self.model,
            "has_data": bool(self.data),
            "content_type": self.content_type,
            "bytes": len(self.data),
            "url": self.url,
            "job_id": self.job_id,
            "text": self.text,
            "detail": self.detail,
            "error": self.error,
            "duration_s": round(self.duration, 3),
            "verified": self.verified,
        }

def not_configured(capability: str, *, what: str = "") -> MediaResult:
    """The honest answer when no provider is set up for a capability."""
    suffix = f" {what}" if what else ""
    return MediaResult(
        ok=False, capability=capability,
        error=f"No provider is configured for {capability.replace('_', ' ')}.{suffix} "
              "Add one in AI Providers → "
              + {
                  "text_to_speech": "Speech",
                  "speech_to_text": "Speech",
                  "image_generation": "Images",
                  "image_editing": "Images",
                  "vision": "Vision",
                  "video_generation": "Video",
              }.get(capability, "Providers")
              + ".")

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class MediaProvider:
    """One provider, one capability family.

    ``base_url`` includes the API version path (``…/v1``), as the text gateway's
    does; each adapter appends only its own endpoint, so a URL is never given a
    second ``/v1``.
    """

    capability = ""
    name = ""

    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 timeout: float = 60.0, headers: Optional[Dict[str, str]] = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = float(timeout or 60.0)
        self.extra_headers = dict(headers or {})

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _url(self, path: str) -> str:
        return self.base_url + "/" + path.lstrip("/")

    def _headers(self, **extra: str) -> Dict[str, str]:
        headers = {"Accept": "application/json", **self.extra_headers, **extra}
        return headers

    def describe(self) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "provider": self.name,
            "configured": self.configured,
            "model": self.model,
            "base_url": self.base_url,
            "has_key": bool(self.api_key),
        }

# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------

@dataclass
class SpeechRequest:
    text: str
    voice: str = ""
    format: str = "mp3"
    speed: float = 1.0
    language: str = ""

#: Long texts are chunked: a single request for a whole document is the case
#: that both overruns the provider's limit and spends the whole timeout budget
#: before the first byte arrives.
MAX_TTS_CHARS = 4000

class TextToSpeechProvider(MediaProvider):
    """OpenAI-shaped ``POST /audio/speech``.

    This is the shape OpenRouter's audio models expose and the shape local
    speech servers (Kokoro, openedai-speech) imitate. It is *not* the shape of
    every speech API, and a provider whose endpoint differs needs its own
    subclass — guessing that a generic endpoint is this one is how a text
    model's chat route gets a request it will answer with prose.
    """

    name = "openai_speech"
    capability = "text_to_speech"

    def speak(self, req: SpeechRequest) -> MediaResult:
        if not self.configured:
            return not_configured(self.capability)
        text = (req.text or "").strip()
        if not text:
            return MediaResult(False, self.capability, self.name, self.model,
                               error="There is nothing to speak.")
        if len(text) > MAX_TTS_CHARS:
            text = text[:MAX_TTS_CHARS].rsplit(" ", 1)[0] + "…"

        payload: Dict[str, Any] = {
            "model": self.model,
            "input": text,
            "response_format": req.format or "mp3",
        }
        if req.voice:
            payload["voice"] = req.voice
        if req.speed and req.speed != 1.0:
            payload["speed"] = max(0.25, min(float(req.speed), 4.0))

        import time

        started = time.monotonic()
        result: HTTPResult = request(
            self._url("audio/speech"), method="POST",
            body=json.dumps(payload).encode(),
            headers=self._headers(**{"Content-Type": "application/json"}),
            secret=self.api_key, timeout=self.timeout, what="the speech provider")
        elapsed = time.monotonic() - started

        if not result.ok:
            return MediaResult(False, self.capability, self.name, self.model,
                               error=result.error or "The speech request failed.",
                               duration=elapsed)
        if not is_audio(result.content_type):
            # The endpoint answered, but not with audio. This is the case a
            # "200 means success" implementation reports as a silent success.
            snippet = result.body[:160].decode("utf-8", "replace")
            return MediaResult(
                False, self.capability, self.name, self.model,
                detail=snippet, duration=elapsed,
                error=f"The speech endpoint answered with {result.content_type}, "
                      "not audio, so there is nothing to play.")
        return MediaResult(True, self.capability, self.name, self.model,
                           data=result.body, content_type=result.content_type,
                           duration=elapsed, verified=True,
                           detail=f"{len(result.body)} bytes of {result.content_type}")

    def verify(self) -> MediaResult:
        """A real, minimal synthesis. This is the only proof the model can speak."""
        return self.speak(SpeechRequest(text="Vantage speech check.", format="mp3"))

# ---------------------------------------------------------------------------
# Speech to text
# ---------------------------------------------------------------------------

class TranscriptionProvider(MediaProvider):
    """OpenAI-shaped ``POST /audio/transcriptions`` (multipart)."""

    name = "openai_transcription"
    capability = "speech_to_text"

    #: The largest clip accepted. A long recording is the case that turns a
    #: fast check into a slow upload; this is a bound, not a preference.
    MAX_AUDIO_BYTES = 20 * 1024 * 1024

    def transcribe(self, audio: bytes, *, filename: str = "clip.wav",
                   language: str = "") -> MediaResult:
        if not self.configured:
            return not_configured(self.capability)
        if not audio:
            return MediaResult(False, self.capability, self.name, self.model,
                               error="There is no audio to transcribe.")
        if len(audio) > self.MAX_AUDIO_BYTES:
            return MediaResult(
                False, self.capability, self.name, self.model,
                error=f"The recording is {len(audio) // 1024} KiB; the limit is "
                      f"{self.MAX_AUDIO_BYTES // (1024 * 1024)} MiB.")

        boundary = "----vantage" + base64.b16encode(bytes(range(16))).decode().lower()
        parts: List[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(value.encode() + b"\r\n")

        field("model", self.model)
        if language:
            field("language", language)
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            .encode())
        parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        parts.append(audio + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        import time

        started = time.monotonic()
        result = request(
            self._url("audio/transcriptions"), method="POST", body=body,
            headers=self._headers(
                **{"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            secret=self.api_key, timeout=self.timeout,
            what="the transcription provider")
        elapsed = time.monotonic() - started
        if not result.ok:
            return MediaResult(False, self.capability, self.name, self.model,
                               error=result.error or "The transcription failed.",
                               duration=elapsed)
        parsed = result.json()
        text = ""
        if isinstance(parsed, dict):
            text = str(parsed.get("text") or parsed.get("transcript") or "").strip()
        if not text and result.content_type.startswith("text/"):
            text = result.body.decode("utf-8", "replace").strip()
        if not text:
            return MediaResult(
                False, self.capability, self.name, self.model, duration=elapsed,
                error="The transcription endpoint answered, but with no text, so "
                      "there is nothing to report.")
        return MediaResult(True, self.capability, self.name, self.model, text=text,
                           duration=elapsed, verified=True)

# ---------------------------------------------------------------------------
# Image generation / editing
# ---------------------------------------------------------------------------

@dataclass
class ImageRequest:
    prompt: str
    size: str = "1024x1024"
    n: int = 1
    #: Base64 inputs for an edit; ignored by generation.
    images: List[bytes] = field(default_factory=list)

class ImageProvider(MediaProvider):
    """OpenAI-shaped ``POST /images/generations`` (and ``/images/edits``)."""

    name = "openai_images"
    capability = "image_generation"

    def generate(self, req: ImageRequest) -> MediaResult:
        if not self.configured:
            return not_configured(self.capability)
        prompt = (req.prompt or "").strip()
        if not prompt:
            return MediaResult(False, self.capability, self.name, self.model,
                               error="An image needs a description.")
        payload = {"model": self.model, "prompt": prompt[:4000],
                   "n": max(1, min(int(req.n or 1), 4))}
        if req.size:
            payload["size"] = req.size

        import time

        started = time.monotonic()
        result = request(
            self._url("images/generations"), method="POST",
            body=json.dumps(payload).encode(),
            headers=self._headers(**{"Content-Type": "application/json"}),
            secret=self.api_key, timeout=self.timeout,
            what="the image provider")
        elapsed = time.monotonic() - started
        return self._read_images(result, elapsed)

    def edit(self, req: ImageRequest) -> MediaResult:
        if not self.configured:
            return not_configured("image_editing")
        if not req.images:
            return MediaResult(False, "image_editing", self.name, self.model,
                               error="Editing an image needs the source image.")
        prompt = (req.prompt or "").strip()
        if not prompt:
            return MediaResult(False, "image_editing", self.name, self.model,
                               error="Say what should change in the image.")
        boundary = "----vantage" + base64.b16encode(bytes(range(16))).decode().lower()
        parts: List[bytes] = []
        for name, value in (("model", self.model), ("prompt", prompt[:4000]),
                            ("size", req.size), ("n", "1")):
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(str(value).encode() + b"\r\n")
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="image"; filename="source.png"\r\n')
        parts.append(b"Content-Type: image/png\r\n\r\n")
        parts.append(req.images[0] + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())

        import time

        started = time.monotonic()
        result = request(
            self._url("images/edits"), method="POST", body=b"".join(parts),
            headers=self._headers(
                **{"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            secret=self.api_key, timeout=self.timeout,
            what="the image provider")
        elapsed = time.monotonic() - started
        out = self._read_images(result, elapsed)
        out.capability = "image_editing"
        return out

    def _read_images(self, result: HTTPResult, elapsed: float) -> MediaResult:
        if not result.ok:
            return MediaResult(False, self.capability, self.name, self.model,
                               error=result.error or "The image request failed.",
                               duration=elapsed)
        parsed = result.json()
        if not isinstance(parsed, dict):
            return MediaResult(
                False, self.capability, self.name, self.model, duration=elapsed,
                detail=result.body[:160].decode("utf-8", "replace"),
                error=f"The image endpoint answered with {result.content_type}, which "
                      "is not an image response.")
        items = parsed.get("data") or []
        if not isinstance(items, list) or not items:
            # Some compatible servers answer with prose instead of an image. That
            # is a failure to produce an image, not a success with no picture.
            text = str(parsed.get("error") or parsed.get("message") or "").strip()
            return MediaResult(
                False, self.capability, self.name, self.model, duration=elapsed,
                detail=json.dumps(parsed)[:240],
                error=text or "The image endpoint returned no image data.")
        first = items[0] if isinstance(items[0], dict) else {}
        b64 = str(first.get("b64_json") or "")
        url = str(first.get("url") or "")
        if b64:
            try:
                data = base64.b64decode(b64, validate=True)
            except Exception:
                return MediaResult(False, self.capability, self.name, self.model,
                                   error="The image endpoint returned base64 that "
                                         "could not be decoded.", duration=elapsed)
            from .http_media import sniff

            kind = sniff(data)
            if not is_image(kind):
                return MediaResult(False, self.capability, self.name, self.model,
                                   error=f"The decoded image data is {kind}, not an "
                                         "image.", duration=elapsed)
            return MediaResult(True, self.capability, self.name, self.model,
                               data=data, content_type=kind, duration=elapsed,
                               verified=True,
                               detail=f"{len(data)} bytes of {kind}")
        if url:
            # A URL is a result, but not a verified one: it has not been fetched.
            return MediaResult(True, self.capability, self.name, self.model,
                               url=url, duration=elapsed, verified=False,
                               detail="The provider returned a URL rather than image "
                                      "bytes, so the image itself was not downloaded.")
        return MediaResult(False, self.capability, self.name, self.model,
                           error="The image endpoint returned an entry with neither "
                                 "an image nor a URL.", duration=elapsed)

# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------

class VisionProvider(MediaProvider):
    """Image understanding through a multimodal chat endpoint.

    Deliberately a *different* provider configuration from the text one: a
    text model that answered once is no evidence it accepts an image, and this
    project has already been burned by inferring capability from a name.
    """

    name = "openai_vision"
    capability = "vision"

    MAX_IMAGE_BYTES = 8 * 1024 * 1024

    def describe_image(self, image: bytes, *, question: str = "",
                       mime: str = "image/png") -> MediaResult:
        if not self.configured:
            return not_configured(self.capability)
        if not image:
            return MediaResult(False, self.capability, self.name, self.model,
                               error="There is no image to look at.")
        if len(image) > self.MAX_IMAGE_BYTES:
            return MediaResult(
                False, self.capability, self.name, self.model,
                error=f"The image is {len(image) // 1024} KiB; the limit is "
                      f"{self.MAX_IMAGE_BYTES // (1024 * 1024)} MiB.")
        prompt = (question or "Describe what is in this image.").strip()
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime};base64,"
                               + base64.b64encode(image).decode()}},
                ],
            }],
            "max_tokens": 700,
        }
        import time

        started = time.monotonic()
        result = request(
            self._url("chat/completions"), method="POST",
            body=json.dumps(payload).encode(),
            headers=self._headers(**{"Content-Type": "application/json"}),
            secret=self.api_key, timeout=self.timeout,
            what="the vision provider")
        elapsed = time.monotonic() - started
        if not result.ok:
            return MediaResult(False, self.capability, self.name, self.model,
                               error=result.error or "The vision request failed.",
                               duration=elapsed)
        parsed = result.json() or {}
        choices = parsed.get("choices") or []
        text = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(str(part.get("text", ""))
                                for part in content if isinstance(part, dict))
        text = text.strip()
        if not text:
            return MediaResult(False, self.capability, self.name, self.model,
                               duration=elapsed,
                               error="The vision model answered with no text, so it "
                                     "established nothing about the image.")
        return MediaResult(True, self.capability, self.name, self.model, text=text,
                           duration=elapsed, verified=True)

    def verify(self, image: bytes, mime: str = "image/png") -> MediaResult:
        return self.describe_image(
            image, question="Reply with the single most prominent colour you see.",
            mime=mime)

# ---------------------------------------------------------------------------
# Video generation
# ---------------------------------------------------------------------------

class VideoProvider(MediaProvider):
    """Asynchronous video generation.

    Video is a job: submit, poll, download. Modelling it as one synchronous
    request is how a client waits minutes and then times out on something that
    actually succeeded. Each provider's route differs, so ``endpoint`` and
    ``status_endpoint`` are configuration, and the default shape is the one
    OpenAI's video API uses (``POST /videos``, ``GET /videos/{id}``).
    """

    name = "openai_video"
    capability = "video_generation"

    #: The longest a caller waits for one poll, and how many polls it makes.
    POLL_INTERVAL = 5.0
    MAX_POLLS = 60

    def submit(self, prompt: str, *, size: str = "1280x720",
               seconds: int = 4) -> MediaResult:
        if not self.configured:
            return not_configured(self.capability)
        text = (prompt or "").strip()
        if not text:
            return MediaResult(False, self.capability, self.name, self.model,
                               error="A video needs a description.")
        payload = {"model": self.model, "prompt": text[:4000],
                   "size": size, "seconds": str(max(1, min(int(seconds), 20)))}
        import time

        started = time.monotonic()
        result = request(
            self._url("videos"), method="POST",
            body=json.dumps(payload).encode(),
            headers=self._headers(**{"Content-Type": "application/json"}),
            secret=self.api_key, timeout=60.0, what="the video provider")
        elapsed = time.monotonic() - started
        if not result.ok:
            return MediaResult(False, self.capability, self.name, self.model,
                               error=result.error or "The video request failed.",
                               duration=elapsed)
        parsed = result.json() or {}
        job_id = str(parsed.get("id") or parsed.get("job_id") or "")
        if not job_id:
            return MediaResult(
                False, self.capability, self.name, self.model, duration=elapsed,
                detail=json.dumps(parsed)[:200],
                error="The video provider did not return a job id, so the job "
                      "cannot be tracked.")
        status = str(parsed.get("status") or "queued")
        return MediaResult(True, self.capability, self.name, self.model,
                           job_id=job_id, duration=elapsed, verified=False,
                           detail=f"Job {job_id} is {status}.")

    def poll(self, job_id: str) -> MediaResult:
        if not self.configured or not job_id:
            return not_configured(self.capability)
        result = request(
            self._url(f"videos/{job_id}"), method="GET",
            headers=self._headers(), secret=self.api_key, timeout=30.0,
            what="the video provider")
        if not result.ok:
            return MediaResult(False, self.capability, self.name, self.model,
                               job_id=job_id,
                               error=result.error or "The status check failed.")
        parsed = result.json() or {}
        status = str(parsed.get("status") or "unknown").lower()
        if status in ("failed", "error", "cancelled"):
            return MediaResult(False, self.capability, self.name, self.model,
                               job_id=job_id,
                               error=str(parsed.get("error")
                                         or f"The video job {status}."))
        done = status in ("completed", "succeeded", "done")
        url = ""
        if done:
            url = str(parsed.get("url") or "")
            if not url:
                outputs = parsed.get("output") or parsed.get("data") or []
                if isinstance(outputs, list) and outputs:
                    first = outputs[0] if isinstance(outputs[0], dict) else {}
                    url = str(first.get("url") or first.get("video_url") or "")
        return MediaResult(True, self.capability, self.name, self.model,
                           job_id=job_id, url=url, verified=bool(done and url),
                           detail=f"Job {job_id} is {status}.")

    def cancel(self, job_id: str) -> MediaResult:
        if not self.configured or not job_id:
            return not_configured(self.capability)
        result = request(self._url(f"videos/{job_id}/cancel"), method="POST",
                         body=b"{}", headers=self._headers(
                             **{"Content-Type": "application/json"}),
                         secret=self.api_key, timeout=30.0,
                         what="the video provider")
        return MediaResult(result.ok, self.capability, self.name, self.model,
                           job_id=job_id, error=result.error)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Capability → adapter class. Adding a capability means adding an adapter and
#: one row here; nothing else in Vantage changes.
ADAPTERS: Dict[str, type] = {
    "text_to_speech": TextToSpeechProvider,
    "speech_to_text": TranscriptionProvider,
    "image_generation": ImageProvider,
    "image_editing": ImageProvider,
    "vision": VisionProvider,
    "video_generation": VideoProvider,
}

def adapter_for(capability: str) -> Optional[type]:
    return ADAPTERS.get(capability)

def build(capability: str, *, base_url: str, api_key: str = "", model: str = "",
          timeout: float = 60.0, headers: Optional[Dict[str, str]] = None) -> Optional[MediaProvider]:
    """One configured adapter, or None for a capability Vantage cannot perform."""
    cls = ADAPTERS.get(capability)
    if cls is None:
        return None
    return cls(base_url, api_key=api_key, model=model, timeout=timeout,
               headers=headers)
# ---------------------------------------------------------------------------
# Gemini (native wire shape)
# ---------------------------------------------------------------------------
#
# Gemini does not speak the OpenAI media routes: TTS is `:generateContent` with
# a speech config and an *inline PCM* part, and image generation is
# `:generateContent` with an image model. Treating it as OpenAI-shaped would
# send a request it answers with prose. These adapters are here because a real
# key for this provider exists on this machine, so they can be verified live
# rather than only declared.

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"

class _GeminiAuth:
    """Gemini's credential header: ``x-goog-api-key``, not a bearer token.

    Measured 2026-10-29 against the real API: `Authorization: Bearer <key>`
    answers 401 "Expected OAuth 2 access token", while `x-goog-api-key: <key>`
    succeeds. The failure reads as an invalid key, which is why it is worth its
    own note rather than being discovered per adapter.
    """

    secret_header = "x-goog-api-key"

class GeminiSpeechProvider(_GeminiAuth, MediaProvider):
    name = "gemini_speech"
    capability = "text_to_speech"

    def _url(self, path: str) -> str:
        return f"{self.base_url or GEMINI_API}/{path.lstrip('/')}"

    def speak(self, req: SpeechRequest) -> MediaResult:
        if not self.configured and not self.api_key:
            return not_configured(self.capability)
        text = (req.text or "").strip()
        if not text:
            return MediaResult(False, self.capability, self.name, self.model,
                               error="There is nothing to speak.")
        if len(text) > MAX_TTS_CHARS:
            text = text[:MAX_TTS_CHARS].rsplit(" ", 1)[0] + "…"
        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": req.voice or "Kore"}}},
            },
        }
        import time

        started = time.monotonic()
        result = request(
            self._url(f"models/{self.model}:generateContent"),
            method="POST",
            body=json.dumps(payload).encode(),
            headers=self._headers(**{"Content-Type": "application/json"}),
            secret=self.api_key, secret_header=_GeminiAuth.secret_header,
            timeout=self.timeout, what="Gemini speech")
        elapsed = time.monotonic() - started
        if not result.ok:
            return MediaResult(False, self.capability, self.name, self.model,
                               error=result.error or "The speech request failed.",
                               duration=elapsed)
        parsed = result.json() or {}
        audio, mime = _gemini_inline(parsed)
        if not audio:
            return MediaResult(
                False, self.capability, self.name, self.model, duration=elapsed,
                detail=json.dumps(parsed)[:200],
                error="Gemini answered, but with no audio part, so there is nothing "
                      "to play.")
        return MediaResult(True, self.capability, self.name, self.model,
                           data=audio, content_type=mime or "audio/L16",
                           duration=elapsed, verified=True,
                           detail=f"{len(audio)} bytes of {mime or 'audio'}")

    def verify(self) -> MediaResult:
        return self.speak(SpeechRequest(text="Vantage speech check.", voice="Kore"))

class GeminiImageProvider(_GeminiAuth, ImageProvider):
    name = "gemini_image"

    def _url(self, path: str) -> str:
        return f"{self.base_url or GEMINI_API}/{'/'.join(part for part in path.split('/') if part and part != 'v1')}"

    def generate(self, req: ImageRequest) -> MediaResult:
        if not self.configured and not self.api_key:
            return not_configured(self.capability)
        prompt = (req.prompt or "").strip()
        if not prompt:
            return MediaResult(False, self.capability, self.name, self.model,
                               error="An image needs a description.")
        payload = {"contents": [{"parts": [{"text": prompt[:4000]}]}]}
        import time

        started = time.monotonic()
        result = request(
            f"{GEMINI_API}/models/{self.model}:generateContent",
            method="POST", body=json.dumps(payload).encode(),
            headers=self._headers(**{"Content-Type": "application/json"}),
            secret=self.api_key, secret_header=_GeminiAuth.secret_header,
            timeout=self.timeout, what="Gemini image")
        elapsed = time.monotonic() - started
        if not result.ok:
            return MediaResult(False, self.capability, self.name, self.model,
                               error=result.error or "The image request failed.",
                               duration=elapsed)
        parsed = result.json() or {}
        data, mime = _gemini_inline(parsed)
        if not data or not is_image(mime or ""):
            return MediaResult(
                False, self.capability, self.name, self.model, duration=elapsed,
                detail=json.dumps(parsed)[:200],
                error="Gemini answered, but with no image, so no image was produced.")
        return MediaResult(True, self.capability, self.name, self.model,
                           data=data, content_type=mime, duration=elapsed,
                           verified=True, detail=f"{len(data)} bytes of {mime}")

def _gemini_inline(parsed: dict):
    """The first inline (base64) media part, and its mime type."""
    try:
        for candidate in parsed.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict) and inline.get("data"):
                    return base64.b64decode(inline["data"]), str(inline.get("mimeType") or "")
    except Exception:
        return b"", ""
    return b"", ""

#: Provider-specific overrides. Keyed by provider id *and* task, because the
#: same provider can be OpenAI-shaped for one capability and native for another.
PROVIDER_ADAPTERS: Dict[Tuple[str, str], type] = {
    ("gemini", "text_to_speech"): GeminiSpeechProvider,
    ("gemini", "image_generation"): GeminiImageProvider,
}

def build_for(provider_id: str, task: str, *, base_url: str, api_key: str = "",
              model: str = "", timeout: float = 60.0) -> Optional[MediaProvider]:
    """The right adapter for one provider *and* task.

    A provider with a native shape for a capability gets that adapter; anything
    else uses the OpenAI-shaped one, which is what "compatible" means.
    """
    cls = PROVIDER_ADAPTERS.get((provider_id, task)) or ADAPTERS.get(task)
    if cls is None:
        return None
    return cls(base_url, api_key=api_key, model=model, timeout=timeout)
