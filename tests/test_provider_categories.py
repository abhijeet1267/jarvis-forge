"""Provider categories: seven tabs, independent configuration, real tests.

The rules this file protects are the ones the previous implementation got wrong:

* saving one category must not change another — the writer touches one block;
* a provider that cannot do a job is refused for that job;
* a category's key is its own keychain entry, never another category's;
* "Test" performs the category's real operation, and a wrong response shape is
  a failure even with HTTP 200;
* discovery narrows to models that can actually do the job when the provider
  publishes its capabilities;
* a key never appears in any response.
"""

from __future__ import annotations

import json

import pytest

from jarvis.gateway import category_checks, provider_categories as pc

# ---------------------------------------------------------------------------
# Isolation and persistence
# ---------------------------------------------------------------------------

@pytest.fixture()
def home(tmp_path):
    return tmp_path

def test_every_category_is_present_and_empty_by_default(home):
    settings = pc.load_all(home)
    assert set(settings) == set(pc.CATEGORIES)
    assert len(pc.CATEGORIES) == 7, "seven tabs, no more and no fewer"
    for category in pc.CATEGORIES:
        assert settings[category].provider == ""
        assert settings[category].configured is False


def test_saving_one_category_leaves_the_others_alone(home):
    pc.set_category("image", provider="pollinations",
                    model="black-forest-labs/flux.1-schnell",
                    base_url="https://gen.pollinations.ai/v1", home=home)
    pc.set_category("speech", provider="gemini", model="gemini-3.8-flash-tts",
                    base_url="https://generativelanguage.googleapis.com/v1beta",
                    home=home)

    # Change only text.
    pc.set_category("text", provider="openai", model="gpt-4o-mini",
                    base_url="https://api.openai.com/v1", home=home)

    settings = pc.load_all(home)
    assert settings["text"].model == "gpt-4o-mini"
    assert settings["image"].model == "black-forest-labs/flux.1-schnell"
    assert settings["image"].provider == "pollinations"
    assert settings["speech"].provider == "gemini"


def test_saving_a_category_preserves_its_own_test_record(home):
    pc.set_category("image", provider="pollinations",
                    base_url="https://gen.pollinations.ai/v1", home=home)
    pc.record_test("image", ok=True, note="15730 bytes of image/jpeg", home=home)
    # Re-saving the same category must not erase what its test established.
    pc.set_category("image", model="flux.1-schnell", home=home)
    settings = pc.load("image", home)
    assert settings.test_ok is True
    assert "15730" in settings.test_note


def test_a_missing_file_reads_as_unconfigured_not_an_error(home):
    # Nothing has ever been written.
    assert pc.load("vision", home).provider == ""


def test_a_corrupt_file_does_not_crash_any_category(home):
    path = pc._path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert pc.load_all(home)["text"].provider == ""

    # And a write still works afterwards.
    pc.set_category("text", provider="openai", home=home)
    assert pc.load("text", home).provider == "openai"


def test_an_unknown_category_is_refused(home):
    with pytest.raises(ValueError, match="not a provider category"):
        pc.set_category("nonsense", provider="openai", home=home)


def test_a_provider_cannot_be_assigned_to_a_job_it_cannot_do():
    # The route enforces CATEGORY_PROVIDERS; this asserts the map itself is the
    # contract, so a category never offers a provider it will refuse.
    assert "pollinations" in pc.CATEGORY_PROVIDERS["image"]
    assert "openrouter" in pc.CATEGORY_PROVIDERS["speech"]
    assert "gemini" not in pc.CATEGORY_PROVIDERS["video"], \
        "Gemini has no video adapter, so it must not be offered for video"


def test_capability_must_belong_to_the_category(home):
    pc.set_category("image", provider="pollinations", home=home)
    with pytest.raises(ValueError, match="not a capability"):
        pc.set_category("image", capability="text_to_speech", home=home)
    # A valid one is accepted.
    pc.set_category("image", capability="image_editing", home=home)
    assert pc.load("image", home).capability == "image_editing"


def test_changing_the_provider_rederives_the_capability(home):
    pc.set_category("image", provider="pollinations",
                    capability="image_editing", home=home)
    pc.set_category("image", provider="openai", home=home)
    assert pc.load("image", home).capability == pc.CATEGORY_CAPABILITIES["image"][0]


def test_timeout_and_options_are_bounded(home):
    pc.set_category("image", provider="pollinations", timeout=99999,
                    options={"size": "x" * 5000, "n": 1}, home=home)
    settings = pc.load("image", home)
    assert settings.timeout == 600.0
    assert len(settings.options["size"]) <= pc.MAX_OPTION_VALUE


def test_the_secret_is_never_in_the_saved_file(home):
    pc.set_category("image", provider="pollinations",
                    base_url="https://gen.pollinations.ai/v1", home=home)
    # Simulate the key having been stored: only a *reference* is recorded.
    data = json.loads(pc._path(home).read_text())
    entry = data[pc.BLOCK_KEY]["image"]
    entry["key_ref"] = pc.key_ref_for("image")
    pc._write(data, home)
    blob = pc._path(home).read_text()
    assert "sk-" not in blob.lower()
    assert "key_ref" in blob
    assert pc.load("image", home).public()["has_key"] is True


def test_public_never_carries_a_key():
    settings = pc.CategorySettings(category="image", provider="pollinations",
                                   key_ref="category.image")
    assert "key" not in json.dumps(settings.public()).replace("has_key", "").lower()


def test_each_category_has_its_own_key_ref():
    refs = {pc.key_ref_for(c) for c in pc.CATEGORIES}
    assert len(refs) == len(pc.CATEGORIES), \
        "two categories sharing a keychain entry would overwrite each other"


def test_the_catalogue_carries_every_tab_and_its_providers(home):
    catalogue = pc.catalogue(home)
    assert [c["category"] for c in catalogue] == list(pc.CATEGORIES)
    for entry in catalogue:
        assert entry["providers"], f"{entry['category']} offers no providers"
        assert entry["purpose"]
        assert "settings" in entry
        for provider in entry["providers"]:
            assert provider["id"] in pc.CATEGORY_PROVIDERS[entry["category"]]

# ---------------------------------------------------------------------------
# The real operations
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, error: str = ""):
        self.body = body
        self.status = status
        self.error = error

def install(monkeypatch, responses):
    """Patch `category_checks._post` with a queue of responses."""
    queue = list(responses)

    def fake(url, payload, *, key="", auth_header="", timeout=60.0):
        if not queue:
            return {"status": 0, "body": b"", "error": "no scripted reply"}
        reply = queue.pop(0)
        return {"status": reply.status, "body": reply.body, "error": reply.error}

    monkeypatch.setattr(category_checks, "_post", fake)

def settings(category: str, provider: str = "pollinations", model: str = "m",
             base_url: str = "https://example.test/v1", **kw) -> pc.CategorySettings:
    return pc.CategorySettings(category=category, provider=provider, model=model,
                               base_url=base_url, **kw)


def test_text_test_requires_an_actual_answer(monkeypatch):
    install(monkeypatch, [FakeResponse(json.dumps(
        {"choices": [{"message": {"content": "ready"}}]}).encode())])
    result = category_checks.run("text", settings("text"))
    assert result["ok"] is True
    assert "ready" in result["detail"]


def test_text_test_fails_when_the_answer_is_empty(monkeypatch):
    install(monkeypatch, [FakeResponse(json.dumps({"choices": []}).encode())])
    result = category_checks.run("text", settings("text"))
    assert result["ok"] is False
    assert "no message text" in result["error"]


def test_speech_test_requires_audio_bytes_not_a_200(monkeypatch):
    install(monkeypatch, [FakeResponse(b"<html>error page</html>")])
    result = category_checks.run("speech", settings("speech"))
    assert result["ok"] is False
    assert "not audio" in result["error"]


def test_speech_test_succeeds_on_real_audio(monkeypatch):
    install(monkeypatch, [FakeResponse(b"ID3" + b"\x00" * 500)])
    result = category_checks.run("speech", settings("speech"))
    assert result["ok"] is True
    assert result["content_type"] == "audio/mpeg"
    assert result["bytes"] > 500


def test_image_test_decodes_base64_to_an_image(monkeypatch):
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    install(monkeypatch, [FakeResponse(json.dumps(
        {"data": [{"b64_json": base64.b64encode(png).decode()}]}).encode())])
    result = category_checks.run("image", settings("image"))
    assert result["ok"] is True
    assert result["content_type"] == "image/png"


def test_image_test_rejects_prose(monkeypatch):
    install(monkeypatch, [FakeResponse(json.dumps(
        {"data": [], "message": "I cannot draw that"}).encode())])
    result = category_checks.run("image", settings("image"))
    assert result["ok"] is False


def test_vision_test_sends_the_image_and_needs_text_back(monkeypatch):
    install(monkeypatch, [FakeResponse(json.dumps(
        {"choices": [{"message": {"content": "white"}}]}).encode())])
    result = category_checks.run("vision", settings("vision"))
    assert result["ok"] is True
    assert "white" in result["detail"]


def test_video_test_reports_a_job_and_refuses_to_call_it_verified(monkeypatch):
    install(monkeypatch, [FakeResponse(json.dumps(
        {"id": "job_7", "status": "queued"}).encode())])
    result = category_checks.run("video", settings("video"))
    assert result["ok"] is True
    assert result["job_id"] == "job_7"
    assert result["verified"] is False, "a queued job is not a finished video"


def test_documents_test_reports_whether_the_document_was_read(monkeypatch):
    install(monkeypatch, [FakeResponse(json.dumps(
        {"choices": [{"message": {"content": "MAINT-4711"}}]}).encode())])
    result = category_checks.run("documents", settings("documents"))
    assert result["ok"] is True
    assert result["read_the_document"] is True


def test_documents_test_is_honest_when_the_reference_is_missing(monkeypatch):
    install(monkeypatch, [FakeResponse(json.dumps(
        {"choices": [{"message": {"content": "I think it is a notice."}}]}).encode())])
    result = category_checks.run("documents", settings("documents"))
    assert result["ok"] is True
    assert result["read_the_document"] is False
    assert "may not have read" in result["detail"]


def test_an_http_error_is_reported_with_the_provider_message(monkeypatch):
    install(monkeypatch, [FakeResponse(b"", status=401,
                                       error="HTTP 401. invalid api key")])
    result = category_checks.run("text", settings("text"))
    assert result["ok"] is False
    assert "401" in result["error"]


def test_an_unreachable_provider_is_a_failure_not_an_exception(monkeypatch):
    install(monkeypatch, [FakeResponse(b"", status=0,
                                       error="Could not reach the provider (URLError).")])
    result = category_checks.run("image", settings("image"))
    assert result["ok"] is False
    assert "Could not reach" in result["error"]

# ---------------------------------------------------------------------------
# Gemini's own wire shape
# ---------------------------------------------------------------------------

def test_gemini_speech_reads_its_audio_part(monkeypatch):
    import base64

    audio = b"\xff\xfb" + b"\x00" * 300
    body = json.dumps({"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/L16;rate=24000",
                        "data": base64.b64encode(audio).decode()}}]}}]}).encode()
    captured = {}

    def fake(url, payload, *, key="", auth_header="", timeout=60.0):
        captured["url"] = url
        captured["auth"] = auth_header
        return {"status": 200, "body": body, "error": ""}

    monkeypatch.setattr(category_checks, "_post", fake)
    result = category_checks.run(
        "speech", settings("speech", provider="gemini",
                           base_url="https://generativelanguage.googleapis.com/v1beta",
                           model="gemini-3.8-flash-tts"))
    assert result["ok"] is True
    assert ":generateContent" in captured["url"]
    assert captured["auth"] == "x-goog-api-key"
    assert result["bytes"] == len(audio)


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def test_discovery_returns_the_provider_capabilities(monkeypatch):
    from jarvis.gateway import providers as gw

    body = json.dumps({"data": [
        {"id": "chat-model", "output_modalities": ["text"]},
        {"id": "image-model", "output_modalities": ["image"],
         "supported_endpoints": ["/v1/images/generations"]},
    ]}).encode()

    # `discover_models` imports `net.http_read` inside the function, so the
    # substitution has to land on the module it imports *from*.
    monkeypatch.setattr("jarvis.net.http_read",
                        lambda req, total=0, what="", gap=0: body)
    found = gw.discover_models("https://example.test/v1", "key")
    assert found["ok"] is True
    assert [m["id"] for m in found["models"]] == ["chat-model", "image-model"]
    assert found["models"][1]["supported_endpoints"] == ["/v1/images/generations"]
    assert found["models"][1]["output_modalities"] == ["image"]


def test_a_failed_discovery_says_why(monkeypatch):
    from jarvis.gateway import providers as gw

    def boom(req, total=0, what="", gap=0):
        raise OSError("connection refused")

    monkeypatch.setattr("jarvis.net.http_read", boom)
    found = gw.discover_models("https://example.test/v1")
    assert found["ok"] is False
    assert found["models"] == []
    assert "Could not read" in found["error"]


def test_discovery_with_no_url_is_refused():
    from jarvis.gateway.providers import discover_models

    assert discover_models("")["ok"] is False


def test_modality_matching_narrows_to_the_job():
    from jarvis.api.app import _model_fits

    image_model = {"output_modalities": ["image"]}
    chat_model = {"output_modalities": ["text"]}
    silent = {}
    assert _model_fits(image_model, "image")
    assert not _model_fits(chat_model, "image")
    assert _model_fits(silent, "image"), "a provider that publishes nothing is not narrowed"