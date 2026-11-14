"""The attachment path, end to end, from a client's point of view.

Before this, no client in the product could attach anything. The routes --
upload, list, delete, process -- existed and were tested, but
``grep -rl "ai/attachment" dashboard/ macos/ android/ ios/`` returned nothing:
there was no way to attach a file to a message from any surface.

The dashboard chat view is now the first client. It does four things in order,
and the order is the contract:

    save the message -> upload the file -> process it -> read the result

The fourth step was impossible before, and for a reason worth naming: the
processing result was **returned to the caller and then dropped**. The
``metadata`` column stayed ``{}`` forever, so a client that listed a message's
attachments later saw ``processing_status: "processed"`` and had no way to
learn what was extracted. A screenshot was uploaded to be *read*.

These tests pin the whole chain, without a browser: the routes behave as the
client needs them to, the result survives the request that produced it, and
the dashboard's script calls the endpoints the contract says it should.
"""

from __future__ import annotations

import json
import re
import secrets
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.core.models import utcnow
from tests.conftest import TEST_TOKEN

AUTH = {"X-JARVIS-Token": TEST_TOKEN}


@pytest.fixture()
def client():
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _chat_with_message() -> tuple[str, str]:
    """A conversation and a message in it, as the dashboard creates them."""
    from jarvis.db.database import get_db
    db = get_db()
    now = utcnow().isoformat()
    chat_id = "chat-" + secrets.token_hex(6)
    msg_id = "msg-" + secrets.token_hex(6)
    db.execute(
        "INSERT INTO ai_chats (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (chat_id, "Test", now, now),
    )
    db.execute(
        "INSERT INTO ai_messages (id, conversation_id, role, content, provider_meta, "
        "timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, chat_id, "user", "here is a screenshot", "{}", now),
    )
    return chat_id, msg_id


def _png_bytes() -> bytes:
    pytest.importorskip("PIL")
    from io import BytesIO
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 40), "Project AMR status report", fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The client contract
# ---------------------------------------------------------------------------
def test_a_file_can_be_uploaded_against_a_message(client):
    """Step 1-2: the upload succeeds and names the attachment."""
    _, msg_id = _chat_with_message()
    response = client.post(
        "/ai/attachments/upload",
        files={"file": ("shot.png", _png_bytes(), "image/png")},
        data={"message_id": msg_id},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["attachment_id"], body
    assert body["filename"] == "shot.png", body
    assert body["status"] == "uploaded", body


def test_the_processed_result_is_stored_and_can_be_read_back_later(client):
    """Step 3-4: the result survives the request that produced it.

    This is the part that did not work before. The processing result was
    returned to the caller and then dropped on the floor, so the `metadata`
    column stayed `{}` and a later listing of the message's attachments could
    say "processed" without saying what was processed.
    """
    _, msg_id = _chat_with_message()
    upload = client.post(
        "/ai/attachments/upload",
        files={"file": ("shot.png", _png_bytes(), "image/png")},
        data={"message_id": msg_id},
        headers=AUTH,
    ).json()
    attachment_id = upload["attachment_id"]

    processed = client.post("/ai/process-attachments",
                             json={"attachment_ids": [attachment_id]}, headers=AUTH)
    assert processed.status_code == 200, processed.text
    result = processed.json()["results"][0]
    assert result["status"] == "processed", result

    # Now read the message's attachments back -- as a client would after a
    # reload. The result must be there.
    listing = client.get(f"/ai/attachments/messages/{msg_id}", headers=AUTH)
    assert listing.status_code == 200, listing.text
    attachments = listing.json()["attachments"]
    assert len(attachments) == 1, attachments
    stored = attachments[0]["result"]
    assert stored, "the processing result was not persisted"
    assert stored.get("status") == "processed", stored
    assert "AMR" in stored.get("text", ""), stored


def test_the_single_attachment_route_reports_the_result_too(client):
    """The detail route carries the result as well."""
    _, msg_id = _chat_with_message()
    attachment_id = client.post(
        "/ai/attachments/upload",
        files={"file": ("shot.png", _png_bytes(), "image/png")},
        data={"message_id": msg_id},
        headers=AUTH,
    ).json()["attachment_id"]
    client.post("/ai/process-attachments",
                json={"attachment_ids": [attachment_id]}, headers=AUTH)

    detail = client.get(f"/ai/attachments/{attachment_id}", headers=AUTH)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["processing_status"] == "processed", body
    assert body["metadata"].get("text"), body


def test_an_unprocessed_attachment_reports_no_result_rather_than_a_fake_one(client):
    """Not processed yet is not the same as processed with nothing in it."""
    _, msg_id = _chat_with_message()
    attachment_id = client.post(
        "/ai/attachments/upload",
        files={"file": ("shot.png", _png_bytes(), "image/png")},
        data={"message_id": msg_id},
        headers=AUTH,
    ).json()["attachment_id"]

    listing = client.get(f"/ai/attachments/messages/{msg_id}", headers=AUTH).json()
    stored = listing["attachments"][0]["result"]
    # No result has been produced yet, so there is nothing to report. The
    # client renders this as "not processed yet" -- never as an empty
    # extraction, which would read as "there was nothing in it".
    assert stored == {}, stored
    assert listing["attachments"][0]["processing_status"] == "pending", listing


def test_a_failed_processing_is_stored_as_the_reason_it_failed(client):
    """A failure is remembered, so the client can say what went wrong."""
    _, msg_id = _chat_with_message()
    attachment_id = client.post(
        "/ai/attachments/upload",
        files={"file": ("shot.png", b"not really a png", "image/png")},
        data={"message_id": msg_id},
        headers=AUTH,
    ).json()["attachment_id"]

    client.post("/ai/process-attachments",
                json={"attachment_ids": [attachment_id]}, headers=AUTH)

    listing = client.get(f"/ai/attachments/messages/{msg_id}", headers=AUTH).json()
    stored = listing["attachments"][0]["result"]
    assert listing["attachments"][0]["processing_status"] == "error", listing
    assert stored.get("status") == "error", stored
    assert stored.get("reason"), stored


# ---------------------------------------------------------------------------
# The dashboard is a client of that contract
# ---------------------------------------------------------------------------
def _dashboard_source() -> str:
    from jarvis.config import get_settings
    return (Path(get_settings().home).parent / "vantage" / "jarvis-forge"
            / "dashboard" / "ai_chat.html").read_text(encoding="utf-8")


def _dashboard_path() -> Path:
    # The dashboard is served from the repo's dashboard/ directory.
    here = Path(__file__).resolve().parent.parent
    return here / "dashboard" / "ai_chat.html"


def test_the_dashboard_calls_the_attachment_endpoints():
    """The dashboard is wired to the contract.

    Checked by reading the served page rather than by driving a browser: this
    pins *which* endpoints the client uses, which is the part that regresses
    when a route is renamed or a client is rewritten.
    """
    source = _dashboard_path().read_text(encoding="utf-8")
    for endpoint in ("/ai/attachments/upload", "/ai/process-attachments"):
        assert endpoint in source, f"the dashboard does not call {endpoint}"
    # The file picker and its button.
    assert 'id="attachment-input"' in source, "no file picker"
    assert 'id="attach-button"' in source, "no attach button"


def test_the_dashboard_shows_the_extracted_text_and_not_just_a_status():
    """The client renders the result, not merely "processed"."""
    source = _dashboard_path().read_text(encoding="utf-8")
    # The description of a result must reach for the text.
    assert re.search(r"result\.text", source), "the dashboard never reads result.text"
    assert "attachment-card" in source, "no card to show an attachment in"
    # The extracted text is untrusted input, so it is set as text, never as
    # markup -- a file must not be able to inject HTML into the page.
    assert "textContent" in source, "the result is not inserted as text"


def test_the_dashboard_uploads_after_the_message_is_saved():
    """An upload needs a message_id, so the order is fixed.

    The message is saved first and its id is passed to the upload. A client
    that uploaded first would have no id to upload against, and the
    attachment would have nothing to hang off.
    """
    source = _dashboard_path().read_text(encoding="utf-8")
    save_at = source.index("await saveMessage(")
    attach_at = source.index("await attachFilesToMessage(")
    assert save_at < attach_at, "the attachment is uploaded before its message exists"


def test_the_dashboard_sends_the_message_id_with_the_upload():
    """The upload carries the message id, in a multipart form."""
    source = _dashboard_path().read_text(encoding="utf-8")
    assert "FormData" in source, "the upload is not a multipart form"
    assert 'form.append("message_id"' in source, "the upload does not name the message"
    assert 'form.append("file"' in source, "the upload does not carry the file"
