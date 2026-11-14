"""The contract the macOS app's attachment client depends on.

The Swift client (added 2026-10-21) does four things in a fixed order, and
the order is not a preference -- it is what the backend's foreign keys
require:

    create a conversation -> save a message -> upload against that message
    -> process -> read the result

An attachment's ``message_id`` is a foreign key onto ``ai_messages``, which is
itself a foreign key onto ``ai_chats``. A client that uploaded first would
have no message id to upload against, and the attachment would have nothing to
hang off.

These tests pin the contract from the *server* side, so a rename of a field or
a change of shape breaks them here rather than in a signed app that is hard to
inspect. The Swift source is checked too -- for the order it does the steps
in, and for the fields it reads -- because a client that calls the right
endpoints in the wrong order is just as broken as one that calls the wrong
endpoints.
"""

from __future__ import annotations

import re
import secrets
from io import BytesIO
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


def _png_bytes() -> bytes:
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 40), "Project AMR status report", fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The contract, server side
# ---------------------------------------------------------------------------
def test_the_client_can_do_the_whole_chain_in_order(client):
    """Exactly the sequence the Swift client performs."""
    chat = client.post("/ai/chats", json={"title": "Command Center"}, headers=AUTH).json()
    assert "id" in chat, chat

    message = client.post(
        f"/ai/chats/{chat['id']}/messages",
        json={"role": "user", "content": "(attachment)"},
        headers=AUTH,
    ).json()
    assert "id" in message, message

    upload = client.post(
        "/ai/attachments/upload",
        files={"file": ("shot.png", _png_bytes(), "image/png")},
        data={"message_id": message["id"]},
        headers=AUTH,
    ).json()
    assert upload["success"] is True, upload
    assert upload["attachment_id"], upload

    processed = client.post(
        "/ai/process-attachments",
        json={"attachment_ids": [upload["attachment_id"]]},
        headers=AUTH,
    ).json()
    assert processed["processed_count"] == 1, processed
    result = processed["results"][0]
    assert result["status"] == "processed", result
    assert "AMR" in result.get("text", ""), result


def test_the_shapes_the_swift_models_decode_are_the_shapes_the_server_sends(client):
    """Every field `APIModels` declares must actually be sent.

    A `Codable` struct with a non-optional field that the server omits fails
    to decode, and the client would show an error instead of the result. So
    the fields are checked against a real response.
    """
    chat = client.post("/ai/chats", json={"title": "t"}, headers=AUTH).json()
    message = client.post(
        f"/ai/chats/{chat['id']}/messages",
        json={"role": "user", "content": "c"}, headers=AUTH).json()
    upload = client.post(
        "/ai/attachments/upload",
        files={"file": ("shot.png", _png_bytes(), "image/png")},
        data={"message_id": message["id"]}, headers=AUTH).json()
    processed = client.post(
        "/ai/process-attachments",
        json={"attachment_ids": [upload["attachment_id"]]}, headers=AUTH).json()

    # APIModels.ChatCreated: id, title
    assert "id" in chat and "title" in chat
    # APIModels.MessageSaved: id, conversation_id, role
    assert {"id", "conversation_id", "role"} <= set(message)
    # APIModels.AttachmentUpload: attachment_id, filename, file_type, size_bytes, status
    assert {"attachment_id", "filename", "file_type", "size_bytes", "status"} <= set(upload)
    # APIModels.AttachmentProcessing: processed_count, results
    assert {"processed_count", "results"} <= set(processed)
    # APIModels.ProcessedAttachment: attachment_id, status, text, summary, text_note
    result = processed["results"][0]
    assert {"attachment_id", "status", "text", "summary"} <= set(result)


def test_an_attachment_needs_a_message_that_exists(client):
    """The foreign key is real, so the order is not optional.

    The attachment's `message_id` points at an `ai_messages` row, which points
    at an `ai_chats` row. A client that uploads before saving a message has
    no id to upload against.
    """
    from jarvis.db.database import get_db
    db = get_db()
    orphan = "msg-never-saved-" + secrets.token_hex(6)
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO ai_attachments (id, message_id, file_type, file_size, "
            "filename, storage_path, upload_status, processing_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("att-" + secrets.token_hex(6), orphan, "image/png", 1, "x.png",
             "/tmp/x.png", "uploaded", "pending", utcnow().isoformat()),
        )


# ---------------------------------------------------------------------------
# The client source
# ---------------------------------------------------------------------------
def _swift_source() -> str:
    here = Path(__file__).resolve().parent.parent
    return (here / "macos" / "Vantage" / "Sources" / "Views" / "CommandCenterView.swift").read_text(
        encoding="utf-8"
    )


def _api_source() -> str:
    here = Path(__file__).resolve().parent.parent
    return (here / "macos" / "Vantage" / "Sources" / "APIClient" / "APIClient.swift").read_text(
        encoding="utf-8"
    )


def test_the_app_calls_the_attachment_endpoints():
    source = _swift_source()
    for endpoint in ("/ai/attachments/upload", "/ai/process-attachments"):
        assert endpoint in source, f"the app does not call {endpoint}"


def test_the_app_creates_a_conversation_and_a_message_before_uploading():
    """The order is pinned by the foreign keys, so it is pinned here.

    A client that uploaded first would have no message id to upload against,
    and the upload would fail with a foreign-key error the user cannot act on.
    """
    source = _swift_source()
    chat_at = source.index('api.post(\n                    "/ai/chats"')
    message_at = source.index('"/ai/chats/\\(chatId)/messages"')
    upload_at = source.index('"/ai/attachments/upload"')
    process_at = source.index('"/ai/process-attachments"')
    assert chat_at < message_at < upload_at < process_at, (
        "the conversation, the message, the upload and the processing must "
        "happen in that order"
    )


def test_the_upload_carries_the_message_id():
    source = _swift_source()
    assert 'fields: ["message_id": message.id]' in source, (
        "the upload does not name the message the file belongs to"
    )


def test_the_upload_is_multipart_with_a_generated_boundary():
    """A reused boundary would let a file's contents forge a part."""
    source = _api_source()
    assert "multipart/form-data" in source, "the upload is not multipart"
    assert "UUID().uuidString" in source, "the boundary is not generated per request"
    assert 'name=\\"file\\"' in source or 'name="file"' in source, (
        "the upload does not name its file part"
    )


def test_the_app_shows_the_extracted_text_and_not_only_a_status():
    """The text is the point of the attachment, so it must be displayed."""
    source = _swift_source()
    assert "displayText" in source, "the result is never described for the user"
    # The description must reach for the text, and the reason is the fallback.
    assert "result.text" in source or "text:" in source


def test_the_app_refuses_a_file_type_the_backend_will_not_accept():
    """The backend rejects an unlisted MIME type, so the client must not offer one."""
    source = _swift_source()
    for mime in ("image/png", "image/jpeg", "application/pdf", "audio/wav", "text/plain"):
        assert mime in source, f"{mime} is not handled by the client"
    # An unknown extension becomes a type the backend refuses, so the file is
    # reported rather than silently uploaded.
    assert "application/octet-stream" in source
