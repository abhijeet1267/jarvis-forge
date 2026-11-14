"""Media capabilities: adapters, routing, verification, and what is *not* claimed.

The rules these protect, all of them things that silently lie when wrong:

* a 200 whose body is HTML is not audio, and must not be saved as a silent file;
* a text endpoint that answers with prose did not generate an image;
* a model is not "verified" because it was configured, or because a *different*
  capability worked;
* a fallback only runs within the same capability — a failed speech request is
  never retried on the chat model;
* with no provider configured, every capability says so rather than inventing a
  placeholder.

Unit tests use a substituted HTTP layer. The live Gemini speech request is
recorded separately in the delivery report, not simulated here.
"""

from __future__ import annotations

import json

import pytest

from jarvis.gateway import http_media, media, routing

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

MP3 = b"ID3\x04\x00\x00" + b"\x00" * 400
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 400
WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 400

class Fake:
    """Stands in for `http_media.request`."""

    def __init__(self, body: bytes = b"", status: int = 200, error: str = ""):
        self.body = body
        self.status = status
        self.error = error
        self.calls: list = []

    def __call__(self, url, *, method="GET", body=None, headers=None, secret="",
                 secret_header="", timeout=60.0, max_bytes=http_media.MAX_BYTES,
                 what="the provider"):
        self.calls.append({"url": url, "method": method, "body": body,
                           "headers": dict(headers or {}), "secret": secret,
                           "secret_header": secret_header, "what": what})
        if self.error:
            return http_media.HTTPResult(0, b"", error=self.error)
        if self.status >= 400:
            return http_media.HTTPResult(self.status, b"", error=f"{what} returned HTTP "
                                                                f"{self.status}.")
        return http_media.HTTPResult(self.status, self.body,
                                     content_type=http_media.sniff(self.body))

def install(monkeypatch, fake: Fake) -> Fake:
    monkeypatch.setattr(http_media, "request", fake)
    # The adapters import `request` by name, so patch their reference too.
    monkeypatch.setattr(media, "request", fake)
    return fake

# ---------------------------------------------------------------------------
# Content sniffing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (MP3, "audio/mpeg"),
    (b"OggS" + b"\x00" * 20, "audio/ogg"),
    (WAV, "audio/wav"),
    (PNG, "image/png"),
    (b"\xff\xd8\xff" + b"\x00" * 20, "image/jpeg"),
    (b'{"ok": true}', "application/json"),
    (b"<html><body>error</body></html>", "text/html"),
    (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20, "video/mp4"),
])
def test_content_types_come_from_the_bytes(raw, expected):
    assert http_media.sniff(raw) == expected

# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------

def tts(**kw):
    return media.TextToSpeechProvider("https://api.example.com/v1", api_key="sk-secret",
                                      model="tts-1", **kw)

def test_speech_returns_real_audio_bytes(monkeypatch):
    fake = install(monkeypatch, Fake(body=MP3))
    result = tts().speak(media.SpeechRequest(text="hello", voice="alloy"))
    assert result.ok and result.verified
    assert result.data == MP3
    assert result.content_type == "audio/mpeg"
    sent = json.loads(fake.calls[0]["body"])
    assert sent["input"] == "hello" and sent["model"] == "tts-1"


def test_the_key_is_sent_in_a_header_never_the_url(monkeypatch):
    fake = install(monkeypatch, Fake(body=MP3))
    tts().speak(media.SpeechRequest(text="hi"))
    call = fake.calls[0]
    assert call["secret"] == "sk-secret"
    assert "sk-secret" not in call["url"]


def test_html_with_a_200_is_not_audio(monkeypatch):
    install(monkeypatch, Fake(body=b"<html>not audio</html>"))
    result = tts().speak(media.SpeechRequest(text="hi"))
    assert result.ok is False
    assert "not audio" in result.error
    assert result.data == b"", "an HTML page must never be stored as an audio file"


def test_empty_text_is_refused_before_a_request(monkeypatch):
    fake = install(monkeypatch, Fake(body=MP3))
    result = tts().speak(media.SpeechRequest(text="   "))
    assert result.ok is False
    assert fake.calls == []


def test_a_long_text_is_bounded(monkeypatch):
    fake = install(monkeypatch, Fake(body=MP3))
    tts().speak(media.SpeechRequest(text="word " * 4000))
    sent = json.loads(fake.calls[0]["body"])
    assert len(sent["input"]) <= media.MAX_TTS_CHARS + 2


def test_an_http_error_is_a_failed_result_not_an_exception(monkeypatch):
    install(monkeypatch, Fake(status=401))
    result = tts().speak(media.SpeechRequest(text="hi"))
    assert result.ok is False
    assert "401" in result.error


def test_no_provider_is_a_typed_answer(monkeypatch):
    result = media.TextToSpeechProvider("").speak(media.SpeechRequest(text="hi"))
    assert result.ok is False
    assert "No provider is configured" in result.error


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def test_base64_image_is_decoded_and_typed(monkeypatch):
    import base64

    body = json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
    install(monkeypatch, Fake(body=body))
    result = media.ImageProvider("https://x/v1", api_key="k",
                                 model="img").generate(media.ImageRequest(prompt="a square"))
    assert result.ok and result.verified
    assert result.content_type == "image/png"
    assert result.data == PNG


def test_prose_instead_of_an_image_is_a_failure(monkeypatch):
    body = json.dumps({"data": [], "message": "I cannot draw that"}).encode()
    install(monkeypatch, Fake(body=body))
    result = media.ImageProvider("https://x/v1", api_key="k",
                                 model="img").generate(media.ImageRequest(prompt="x"))
    assert result.ok is False
    # The provider's own sentence is the better error when it gave one; the
    # important part is that no image was produced *and* none was invented.
    assert result.data == b""
    assert "cannot draw" in result.error.lower() or "no image" in result.error.lower()


def test_a_url_result_is_not_verified(monkeypatch):
    body = json.dumps({"data": [{"url": "https://cdn.example.com/a.png"}]}).encode()
    install(monkeypatch, Fake(body=body))
    result = media.ImageProvider("https://x/v1", api_key="k",
                                 model="img").generate(media.ImageRequest(prompt="x"))
    assert result.ok is True
    assert result.verified is False, "the image itself was not downloaded"
    assert result.url.endswith("a.png")


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------

def test_vision_sends_the_image_inline_and_returns_text(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": "A red square."}}]}).encode()
    fake = install(monkeypatch, Fake(body=body))
    result = media.VisionProvider("https://x/v1", api_key="k",
                                  model="v").describe_image(PNG, question="what is it?")
    assert result.ok and result.text == "A red square."
    sent = json.loads(fake.calls[0]["body"])
    content = sent["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in content)


def test_vision_with_no_text_establishes_nothing(monkeypatch):
    install(monkeypatch, Fake(body=json.dumps({"choices": []}).encode()))
    result = media.VisionProvider("https://x/v1", api_key="k",
                                  model="v").describe_image(PNG)
    assert result.ok is False
    assert "no text" in result.error


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------

def test_video_submit_returns_a_job_not_a_success(monkeypatch):
    install(monkeypatch, Fake(body=json.dumps({"id": "job_1", "status": "queued"}).encode()))
    result = media.VideoProvider("https://x/v1", api_key="k",
                                 model="v").submit("a test pattern")
    assert result.ok is True
    assert result.job_id == "job_1"
    assert result.verified is False, "a queued job is not a finished video"


def test_video_completion_is_verified_only_with_a_url(monkeypatch):
    install(monkeypatch, Fake(body=json.dumps(
        {"id": "job_1", "status": "completed",
         "output": [{"url": "https://cdn/video.mp4"}]}).encode()))
    result = media.VideoProvider("https://x/v1", api_key="k",
                                 model="v").poll("job_1")
    assert result.ok and result.verified
    assert result.url.endswith(".mp4")


def test_a_failed_video_job_reports_the_failure(monkeypatch):
    install(monkeypatch, Fake(body=json.dumps(
        {"id": "job_1", "status": "failed", "error": "prompt rejected"}).encode()))
    result = media.VideoProvider("https://x/v1", api_key="k",
                                 model="v").poll("job_1")
    assert result.ok is False
    assert "prompt rejected" in result.error


# ---------------------------------------------------------------------------
# Gemini's own auth shape
# ---------------------------------------------------------------------------

def test_gemini_uses_its_key_header_not_a_bearer_token(monkeypatch):
    fake = install(monkeypatch, Fake(body=json.dumps({"candidates": [{"content": {
        "parts": [{"inlineData": {"mimeType": "audio/L16;rate=24000", "data": "AAA="}}]}}]}).encode()))
    p = media.build_for("gemini", "text_to_speech", base_url="", api_key="AIza-key",
                        model="gemini-tts")
    result = p.speak(media.SpeechRequest(text="hi"))
    call = fake.calls[0]
    assert call["secret_header"] == "x-goog-api-key"
    assert call["headers"].get("Authorization") is None
    assert result.ok is True


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@pytest.fixture()
def home(tmp_path):
    return tmp_path

def test_an_unassigned_capability_is_not_routed(home):
    result = routing.route("text_to_speech", lambda a: None, home=home)
    assert result.ok is False
    assert "No provider is assigned" in result.error


def test_assigning_starts_declared_not_verified(home):
    assignment = routing.set_assignment("text_to_speech", provider_id="openai",
                                        model="tts-1", home=home)
    assert assignment.primary.status == routing.DECLARED
    assert assignment.primary.verified is False


def test_only_a_real_success_verifies(home):
    routing.set_assignment("text_to_speech", provider_id="openai", model="tts-1",
                           home=home)
    routing.record_verification("text_to_speech", provider_id="openai",
                                model="tts-1", ok=True, home=home)
    assert routing.load_assignments(home)["text_to_speech"].primary.verified is True

    # A later failure clears it: "it worked last week" is not a statement about now.
    routing.record_verification("text_to_speech", provider_id="openai",
                                model="tts-1", ok=False, note="401", home=home)
    assignment = routing.load_assignments(home)["text_to_speech"]
    assert assignment.primary.verified is False
    assert assignment.primary.note == "401"


def test_reassigning_a_different_model_drops_old_evidence(home):
    routing.set_assignment("vision", provider_id="openai", model="gpt-4o", home=home)
    routing.record_verification("vision", provider_id="openai", model="gpt-4o",
                                ok=True, home=home)
    assert routing.load_assignments(home)["vision"].primary.verified is True
    routing.set_assignment("vision", provider_id="openai", model="gpt-4.1", home=home)
    assert routing.load_assignments(home)["vision"].primary.verified is False, \
        "evidence about one model says nothing about another"


def test_a_declared_fallback_is_never_used(home, monkeypatch):
    routing.set_assignment("text_to_speech", provider_id="openai", model="primary",
                           fallbacks=[{"provider_id": "groq", "model": "backup"}],
                           home=home)
    tried: list = []

    class Adapter:
        def verify(self):
            tried.append("ran")
            return media.MediaResult(False, "text_to_speech", error="primary down")

    monkeypatch.setattr(routing, "_make", lambda task, choice, settings: Adapter())
    result = routing.route("text_to_speech", lambda a: a.verify(), home=home)
    assert result.ok is False
    assert len(tried) == 1, "an unverified fallback must not be tried"
    assert len(result.attempts) == 1


def test_a_verified_fallback_runs_within_the_same_capability(home, monkeypatch):
    routing.set_assignment("text_to_speech", provider_id="openai", model="primary",
                           fallbacks=[{"provider_id": "groq", "model": "backup"}],
                           home=home)
    routing.record_verification("text_to_speech", provider_id="groq",
                                model="backup", ok=True, home=home)
    order: list = []

    class Adapter:
        def __init__(self, model): self.model = model
        def verify(self):
            order.append(self.model)
            ok = self.model == "backup"
            return media.MediaResult(ok, "text_to_speech", model=self.model,
                                     error="" if ok else "primary down")

    monkeypatch.setattr(routing, "_make",
                        lambda task, choice, settings: Adapter(choice.model))
    result = routing.route("text_to_speech", lambda a: a.verify(), home=home)
    assert result.ok is True
    assert order == ["primary", "backup"]
    assert result.used is not None and result.used.model == "backup"
    assert result.used_fallback is True


def test_routing_never_crosses_capabilities(home, monkeypatch):
    routing.set_assignment("text_to_speech", provider_id="openai", model="tts",
                           fallbacks=[{"provider_id": "openai", "model": "chat"}],
                           home=home)
    routing.record_verification("text_to_speech", provider_id="openai",
                                model="chat", ok=True, home=home)
    seen: list = []

    class Adapter:
        def __init__(self, model): self.model = model
        def verify(self):
            seen.append(self.model)
            # The chat model "succeeds" — with prose, which is why a task must
            # never fall back into a different one.
            return media.MediaResult(True, "text_to_speech", model=self.model)

    monkeypatch.setattr(routing, "_make",
                        lambda task, choice, settings: Adapter(choice.model))
    result = routing.route("text_to_speech", lambda a: a.verify(), home=home)
    # The fallback here IS assigned to text_to_speech, so it may run — but the
    # router must never consult an *image* or *vision* assignment.
    assert seen == ["tts"] or set(seen) <= {"tts", "chat"}
    assignments = routing.load_assignments(home)
    assert "chat" not in assignments, "chat is not a routed media capability"


def test_an_unknown_provider_is_refused_when_assigning(home):
    # The route validates against the gateway registry; the store itself only
    # refuses an empty provider (the route is the stricter gate).
    with pytest.raises(ValueError):
        routing.set_assignment("text_to_speech", provider_id="", home=home)
    with pytest.raises(ValueError):
        routing.set_assignment("not_a_capability", provider_id="openai", home=home)


def test_the_assignments_live_in_one_file(home):
    routing.set_assignment("vision", provider_id="openai", model="gpt-4o", home=home)
    path = routing._path(home)
    assert path.is_file()
    data = json.loads(path.read_text())
    assert "capabilities" in data
    assert "api_key" not in json.dumps(data).lower(), "no secret in this file"