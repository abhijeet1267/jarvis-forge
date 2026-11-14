"""The attachment pipeline reads the text in an image, not just its dimensions.

The image branch of ``/ai/process-attachments`` measured the image — size,
mode, brightness, dominant colours — and reported ``processed``. That is a
genuine measurement, but it is the envelope of the picture, not the picture: a
screenshot was uploaded, and what the user got back described its pixel
dimensions while its *contents* were never read.

A working recogniser already existed and was not called from this path:
``jarvis/vision/ocr.py`` runs the macOS Vision framework and reads real text
(measured here: "Project AMR status report" / "Battery 74 percent charging").

These tests pin the wiring and, more importantly, the honesty around it. The
three outcomes that must stay distinct:

* text was read → the text is reported;
* the image was read and contains no text → said so, and the text field is
  empty (not absent, and not invented);
* the recogniser could not read it → said so, and no text is claimed.

A measurement is still reported in every case — the pixel facts did not stop
being true because the text could not be read.
"""

from __future__ import annotations

import secrets
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jarvis.core.models import utcnow
from tests.conftest import TEST_TOKEN

AUTH = {"X-JARVIS-Token": TEST_TOKEN}


@pytest.fixture()
def client():
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _ensure_message(db, message_id: str) -> None:
    """The ai_chats + ai_messages rows an attachment's message_id must reference.

    The attachment's foreign key points at an ``ai_messages`` row, which in turn
    points at an ``ai_chats`` row, so both have to exist.
    """
    now = utcnow().isoformat()
    conv_id = "conv-" + message_id
    db.execute(
        "INSERT OR IGNORE INTO ai_chats (id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (conv_id, "Test Chat", now, now),
    )
    db.execute(
        "INSERT OR IGNORE INTO ai_messages (id, conversation_id, role, content, "
        "provider_meta, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, conv_id, "user", "here is a screenshot", "{}", now),
    )


def _pending_image(db, *, filename="shot.png", content=b"\x89PNG\r\n\x1a\n fake"):
    """A stored image attachment waiting to be processed."""
    real = Path(tempfile.mkdtemp()) / filename
    real.write_bytes(content)
    att_id = "pending-" + secrets.token_hex(8)
    _ensure_message(db, "msg-ocr")
    db.execute(
        "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, "
        "storage_path, upload_status, processing_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (att_id, "msg-ocr", "image/png", real.stat().st_size, filename,
         str(real), "uploaded", "pending", utcnow().isoformat()),
    )
    return att_id, real


def _fake_inspect(path):
    return {"ok": True, "summary": "shot.png: 600x200 PNG", "width": 600,
            "height": 200, "format": "PNG", "mode": "RGB"}


def test_an_image_with_readable_text_reports_that_text(client):
    """The pipeline reads what the image says, and reports it."""
    from jarvis.db.database import get_db
    db = get_db()
    att_id, real = _pending_image(db)

    with patch("jarvis.vision.image.inspect", _fake_inspect), \
         patch("jarvis.vision.ocr.read_text",
               return_value={"ok": True,
                             "text": "Project AMR status report",
                             "lines": [{"text": "Project AMR status report"}]}):
        response = client.post("/ai/process-attachments",
                                json={"attachment_ids": [att_id]}, headers=AUTH)

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "processed", result
    assert result["text"] == "Project AMR status report", result
    assert result["text_lines"] == 1, result
    # The measurement is still reported — the pixel facts did not stop being
    # true because text was also read.
    assert result["width"] == 600 and result["height"] == 200, result


def test_an_image_with_no_text_says_so_rather_than_claiming_text(client):
    """A picture of a landscape has no text in it, and that is a fact."""
    from jarvis.db.database import get_db
    db = get_db()
    att_id, _ = _pending_image(db, filename="landscape.png")

    with patch("jarvis.vision.image.inspect", _fake_inspect), \
         patch("jarvis.vision.ocr.read_text",
               return_value={"ok": True, "text": "", "lines": []}):
        response = client.post("/ai/process-attachments",
                                json={"attachment_ids": [att_id]}, headers=AUTH)

    result = response.json()["results"][0]
    assert result["status"] == "processed", result
    # The text field is present and empty — not absent, and not invented.
    assert result["text"] == "", result
    assert "no text" in result["text_note"].lower(), result
    assert result["width"] == 600, result


def test_a_recogniser_that_cannot_read_reports_no_text_and_keeps_the_measurement(client):
    """An OCR failure is reported as a failure of OCR, not as a failure of the image."""
    from jarvis.db.database import get_db
    db = get_db()
    att_id, _ = _pending_image(db)

    with patch("jarvis.vision.image.inspect", _fake_inspect), \
         patch("jarvis.vision.ocr.read_text",
               return_value={"ok": False, "error": "the recogniser did not answer"}):
        response = client.post("/ai/process-attachments",
                                json={"attachment_ids": [att_id]}, headers=AUTH)

    result = response.json()["results"][0]
    # The image WAS processed — the measurement succeeded.
    assert result["status"] == "processed", result
    # No text is claimed, and the reason is stated.
    assert "text" not in result or not result.get("text"), result
    assert "could not read" in result["text_note"].lower(), result
    # The measurement survived the OCR failure.
    assert result["width"] == 600, result


def test_the_ocr_seam_is_actually_invoked_with_the_stored_path(client):
    """A regression back to measurement-only must fail this test.

    Patching the seam and asserting it was called with the stored file path is
    what makes the wiring observable, rather than inferred from the result.
    """
    from jarvis.db.database import get_db
    db = get_db()
    att_id, real = _pending_image(db)

    calls = []

    def fake_ocr(path):
        calls.append(path)
        return {"ok": True, "text": "read", "lines": [{"text": "read"}]}

    with patch("jarvis.vision.image.inspect", _fake_inspect), \
         patch("jarvis.vision.ocr.read_text", fake_ocr):
        client.post("/ai/process-attachments",
                    json={"attachment_ids": [att_id]}, headers=AUTH)

    assert calls, "the OCR seam was never invoked — the pipeline measured only"
    assert str(real) in calls[0], calls


def test_the_real_recogniser_reads_a_real_image_end_to_end(client):
    """The whole path, with the real recogniser and a real image.

    This is the acceptance test: a PNG with words in it, uploaded and
    processed, comes back with those words in it. It is skipped where the
    macOS Vision framework is unavailable, and it is the one test here that
    touches the real recogniser rather than a stub.
    """
    from jarvis.db.database import get_db
    from jarvis.vision.ocr import available

    if not available():
        pytest.skip("the macOS Vision recogniser is unavailable on this machine")

    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    db = get_db()
    real = Path(tempfile.mkdtemp()) / "real_text.png"
    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 40), "Project AMR status report", fill="black")
    image.save(real)

    att_id = "pending-" + secrets.token_hex(8)
    _ensure_message(db, "msg-ocr")
    db.execute(
        "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, "
        "storage_path, upload_status, processing_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (att_id, "msg-ocr", "image/png", real.stat().st_size, real.name,
         str(real), "uploaded", "pending", utcnow().isoformat()),
    )

    response = client.post("/ai/process-attachments",
                            json={"attachment_ids": [att_id]}, headers=AUTH)
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "processed", result
    # The words drawn on the image come back out of the pipeline.
    assert "AMR" in result.get("text", ""), result
    assert result.get("text_lines", 0) >= 1, result

    rows = db.query("SELECT processing_status FROM ai_attachments WHERE id = ?", (att_id,))
    assert rows[0]["processing_status"] == "processed"
