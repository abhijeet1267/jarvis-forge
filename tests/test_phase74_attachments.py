"""Phase 74 - Attachment System Security Tests.

Tests multimodal AI attachment handling:
1. File type validation prevents injection attacks
2. Size limits enforced (10MB)
3. MIME type matching actual content
4. Secure storage path generation
5. Database integrity on upload/delete
6. No unauthorized access to attachment metadata
7. Processing pipeline isolation
8. Message association validation
"""

from __future__ import annotations

import secrets
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from jarvis.core.models import utcnow
from tests.conftest import TEST_TOKEN

AUTH = {"X-JARVIS-Token": TEST_TOKEN}


def _ensure_message(db, message_id: str) -> None:
    """Create the ai_chats + ai_messages rows an attachment's message_id must
    reference, so the ai_attachments FOREIGN KEY (message_id) is satisfied."""
    now = utcnow().isoformat()
    conv_id = "conv-" + message_id
    db.execute(
        "INSERT OR IGNORE INTO ai_chats (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (conv_id, "Test Chat", now, now)
    )
    db.execute(
        "INSERT OR IGNORE INTO ai_messages (id, conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (message_id, conv_id, "user", "test", now)
    )
@pytest.fixture
def client():
    """Test client with fresh database.

    Loopback base_url is required: the app rejects any Host header that is not a
    loopback address (anti-DNS-rebinding) before auth runs, so "http://test" would
    400 every request.
    """
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


class TestAttachmentUpload:
    """File upload security and validation tests."""
    
    def test_rejects_unsupported_mime_types(self, client):
        """Only approved file types can be uploaded."""
        # Try to upload HTML as if it's an image
        
        # (filename, content, content_type) is the httpx multipart tuple shape
        response = client.post(
            "/ai/attachments/upload",
            files={"file": ("fake.html",
                            BytesIO(b'<!DOCTYPE html><html><body>Not an image</body></html>'),
                            "text/html")},
            data={
                "message_id": "msg-123",
                "file_type": "image/png",
                "filename": "fake.png"
            },
            headers=AUTH
        )
        
        # Should reject due to actual MIME mismatch
        assert response.status_code == 400
        assert "Unsupported" in response.json().get("detail", "")
    
    def test_enforces_size_limit(self, client):
        """Files larger than 10MB are rejected at upload time."""
        
        large_content = BytesIO(b'A' * (11 * 1024 * 1024))

        response = client.post(
            "/ai/attachments/upload",
            files={"file": ("big.txt", large_content, "text/plain")},
            data={
                "message_id": "msg-123",
                "file_type": "application/octet-stream",
                "filename": "big.bin"
            },
            headers=AUTH
        )
        
        assert response.status_code == 400
        assert "too large" in response.json().get("detail", "").lower()
    
    def test_validates_message_exists(self, client, monkeypatch):
        """Attachments must reference valid messages."""
        from jarvis.db.database import get_db
        db = get_db()
        
        msg_id = "test-msg-" + secrets.token_hex(8)
        now = utcnow().isoformat()
        conv_id = "conv-test-" + secrets.token_hex(8)
        
        db.execute(
            "INSERT OR REPLACE INTO ai_chats (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, "Test Chat", now, now)
        )
        
        db.execute(
            "INSERT OR REPLACE INTO ai_messages (id, conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (msg_id, conv_id, "user", "test", now)
        )
        
        response = client.post(
            "/ai/attachments/upload",
            files={"file": ("test.txt", BytesIO(b"hello"), "text/plain")},
            data={
                "message_id": msg_id,
                "file_type": "text/plain",
                "filename": "test.txt"
            },
            headers=AUTH
        )
        
        assert response.status_code == 200
        assert response.json()["success"] is True
    
    def test_file_saved_to_secure_location(self, client, tmp_path):
        """Uploaded files go to isolated attachments directory."""
        # Mock settings to use temp dir
        from jarvis.db.database import get_db
        _ensure_message(get_db(), "msg-123")

        with tempfile.TemporaryDirectory() as tmpdir:
            home_dir = Path(tmpdir)
            
            with patch('jarvis.config.get_settings') as mock_get_settings:
                settings = type('Settings', (), {
                    'home': home_dir,
                    'db_path': home_dir / 'test.db',
                    'lan_mode': False,
                    'auth_enabled': True,
                    'token_path': home_dir / 'token',
                    'host': 'localhost',
                    'port': 8080,
                    'api_docs': False,
                    'paranoid': False,
                })()
                
                mock_get_settings.return_value = settings
                
                response = client.post(
                    "/ai/attachments/upload",
                    files={"file": ("test.pdf", b"%PDF-1.4", "application/pdf")},
                    data={
                        "message_id": "msg-123",
                        "file_type": "application/pdf",
                        "filename": "test.pdf"
                    },
                    headers=AUTH,
                    follow_redirects=False
                )
                
                # Check file was created in right location
                attachments_dir = home_dir / "attachments" / "ai"
                assert attachments_dir.exists()
                
                # Should have files with secure naming pattern
                files = list(attachments_dir.glob("*"))
                assert len(files) >= 1
                
                # Filename is {YYYYMMDD}-{HHMMSS}-{random_token}{ext}; the random part is the
                # final segment (the timestamp itself contains a dash).
                filename = files[0].name
                assert "-" in filename  # Has separator
                # The token comes from ``secrets.token_urlsafe(12)`` and is base64url,
                # so it can itself contain `-`. The timestamp is exactly two
                # dash-separated segments (YYYYMMDD, HHMMSS); everything after the
                # second dash is the whole token. Splitting on the *last* dash (the old
                # ``split("-")[-1]``) flakily grabbed a trailing fragment when a token
                # contained a dash (e.g. "37"), so split only on the first two instead.
                stem = filename.rsplit(".", 1)[0]
                random_part = stem.split("-", 2)[2]
                assert len(random_part) > 8, f"no random component in {filename}"


class TestAttachmentAccess:
    """File access control and metadata tests."""
    
    def test_attachment_metadata_accessible_via_owner_token(self, client):
        """Only authenticated users can view attachment metadata."""
        response = client.get(
            "/ai/attachments/test-id",
            headers={}  # No auth
        )
        
        assert response.status_code in [401, 403]
    
    def test_nonexistent_attachment_returns_404(self, client):
        """Missing attachments return proper error."""
        response = client.get(
            "/ai/attachments/nonexistent-id-12345",
            headers=AUTH
        )
        
        assert response.status_code == 404
        assert "not found" in response.json().get("detail", "").lower()
    
    def test_list_attachments_by_message(self, client, monkeypatch):
        """List all attachments for a specific message."""
        from jarvis.db.database import get_db
        db = get_db()
        
        # Create a test message
        msg_id = "test-msg-list-" + secrets.token_hex(8)
        now = utcnow().isoformat()
        
        # Create conversation first
        conv_id = "conv-test-list-" + secrets.token_hex(8)
        db.execute(
            "INSERT OR REPLACE INTO ai_chats (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conv_id, "Test Chat", now, now)
        )
        
        # Create message
        db.execute(
            "INSERT OR REPLACE INTO ai_messages (id, conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (msg_id, conv_id, "user", "Hello", now)
        )
        
        # Upload multiple attachments
        for i in range(3):
            db.execute(
                "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"att-{i}-{secrets.token_hex(4)}",
                    msg_id,
                    "image/jpeg",
                    1024,
                    f"test-{i}.jpg",
                    f"/tmp/fake-path-{i}",
                    "uploaded",
                    "processed",
                    now
                )
            )
        
        # List attachments
        response = client.get(
            f"/ai/attachments/messages/{msg_id}",
            headers=AUTH
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["message_id"] == msg_id
        assert data["total_count"] == 3
        assert len(data["attachments"]) == 3


class TestAttachmentDeletion:
    """Cleanup and deletion tests."""
    
    def test_delete_removes_from_database_and_fs(self, client, tmp_path):
        """Deleting attachment removes both DB record and file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file
            test_file = tmp_path / "test-upload" / "to-delete.pdf"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_bytes(b"%PDF-1.4")
            
            from jarvis.db.database import get_db
            db = get_db()
            _ensure_message(db, "msg-123")

            # Insert attachment record
            att_id = secrets.token_urlsafe(16)
            now = utcnow().isoformat()
            db.execute(
                "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    att_id,
                    "msg-123",
                    "application/pdf",
                    len(test_file.read_bytes()),
                    "to-delete.pdf",
                    str(test_file),
                    "uploaded",
                    "processed",
                    now
                )
            )
            
            # Verify file exists and record in DB
            assert test_file.exists()
            assert db.query("SELECT * FROM ai_attachments WHERE id = ?", (att_id,))
            
            # Delete attachment
            response = client.delete(
                f"/ai/attachments/{att_id}",
                headers=AUTH
            )
            
            assert response.status_code == 200
            assert response.json()["success"] is True
            
            # Verify file deleted
            assert not test_file.exists()
            
            # Verify record removed from DB
            rows = db.query("SELECT * FROM ai_attachments WHERE id = ?", (att_id,))
            assert len(rows) == 0
    
    def test_delete_nonexistent_attachment(self, client):
        """Deleting non-existent attachment returns 404."""
        response = client.delete(
            "/ai/attachments/nonexistent-id",
            headers=AUTH
        )
        
        assert response.status_code == 404


class TestProcessingPipeline:
    """Attachment processing workflow tests."""
    
    def test_process_attachments_updates_status(self, client):
        """Image attachments now route to the REAL vision pipeline, not a hardcoded
        'not implemented' placeholder. Proved by patching the vision seam and asserting
        it is actually invoked (so a regression back to the placeholder fails), and that
        a genuine "processed" result flows through to the DB.
        """
        from jarvis.db.database import get_db
        db = get_db()

        # A real file path on disk (content irrelevant — the vision seam is patched).
        import tempfile, pathlib
        real = pathlib.Path(tempfile.mkdtemp()) / "real.png"
        real.write_bytes(b"\x89PNG\r\n\x1a\n fake")

        att_id = "pending-" + secrets.token_hex(8)
        now = utcnow().isoformat()
        _ensure_message(db, "msg-123")
        db.execute(
            "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                att_id, "msg-123", "image/png", real.stat().st_size, "real.png",
                str(real), "uploaded", "pending", now
            )
        )

        calls = []
        def fake_inspect(path):
            calls.append(path)
            return {"ok": True, "summary": "a real 32x16 image", "width": 32,
                    "height": 16, "format": "PNG", "mode": "RGB"}

        with patch('jarvis.vision.image.inspect', fake_inspect):
            response = client.post(
                "/ai/process-attachments",
                json={"attachment_ids": [att_id]},
                headers=AUTH
            )

        assert response.status_code == 200
        data = response.json()
        assert data["processed_count"] == 1, data
        result = data["results"][0]
        assert result["status"] == "processed", result
        assert result.get("width") == 32 and result.get("height") == 16, result
        assert result.get("summary"), result
        # The real vision seam was invoked with the stored file path.
        assert calls and str(real) in calls[0], calls

        rows = db.query("SELECT processing_status FROM ai_attachments WHERE id = ?", (att_id,))
        assert rows[0]["processing_status"] == "processed"

    def test_process_attachments_unreadable_image_is_an_error_not_a_success(self, client):
        """An image the vision pipeline cannot read is an honest error, never a fake
        'processed' and never silently 'not_processed' when a pipeline exists."""
        from jarvis.db.database import get_db
        db = get_db()

        att_id = "pending-" + secrets.token_hex(8)
        now = utcnow().isoformat()
        _ensure_message(db, "msg-123")
        db.execute(
            "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                att_id, "msg-123", "image/png", 2048, "missing.png",
                "/tmp/does-not-exist.png", "uploaded", "pending", now
            )
        )

        response = client.post(
            "/ai/process-attachments",
            json={"attachment_ids": [att_id]},
            headers=AUTH
        )
        assert response.status_code == 200
        data = response.json()
        assert data["processed_count"] == 0
        result = data["results"][0]
        assert result["status"] in ("error", "not_processed"), result

    def _minimal_pdf(self, text: str) -> bytes:
        """A tiny, valid, text-bearing PDF built by hand — no external fixture, no pypdf
        needed to *create* it (only to parse it, which the pipeline does)."""
        import io

        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        ]
        out = io.BytesIO()
        out.write(b"%PDF-1.4\n")
        offsets = []
        for i, obj in enumerate(objects, start=1):
            offsets.append(out.tell())
            out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref = out.tell()
        out.write(f"xref\n0 {len(objects)+1}\n".encode())
        out.write(b"0000000000 65535 f \n")
        for off in offsets:
            out.write(f"{off:010d} 00000 n \n".encode())
        out.write(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        return out.getvalue()

    def test_pdf_attachment_extracts_real_text(self, client, tmp_path):
        """A real text-bearing PDF is processed to its actual text (not a placeholder),
        and maps to processed/processed in the DB."""
        from jarvis.db.database import get_db
        db = get_db()

        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(self._minimal_pdf("Hello Vantage"))

        att_id = "pending-" + secrets.token_hex(8)
        now = utcnow().isoformat()
        _ensure_message(db, "msg-pdf")
        db.execute(
            "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (att_id, "msg-pdf", "application/pdf", pdf.stat().st_size, "doc.pdf",
             str(pdf), "uploaded", "pending", now)
        )

        response = client.post("/ai/process-attachments", json={"attachment_ids": [att_id]},
                               headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["processed_count"] == 1, data
        result = data["results"][0]
        assert result["status"] == "processed", result
        assert "Hello Vantage" in result.get("text", ""), result
        assert result.get("pages") == 1, result

        rows = db.query("SELECT processing_status FROM ai_attachments WHERE id = ?", (att_id,))
        assert rows[0]["processing_status"] == "processed"

    def test_pdf_attachment_encrypted_or_unreadable_is_error(self, client, tmp_path):
        """A corrupt/encrypted PDF is an honest error, never a fake 'processed'."""
        from jarvis.db.database import get_db
        db = get_db()

        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4 this is not a real pdf")

        att_id = "pending-" + secrets.token_hex(8)
        now = utcnow().isoformat()
        _ensure_message(db, "msg-badpdf")
        db.execute(
            "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (att_id, "msg-badpdf", "application/pdf", bad.stat().st_size, "bad.pdf",
             str(bad), "uploaded", "pending", now)
        )

        response = client.post("/ai/process-attachments", json={"attachment_ids": [att_id]},
                               headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["processed_count"] == 0
        result = data["results"][0]
        assert result["status"] == "error", result

    def test_batch_processing_works(self, client):
        """Process multiple attachments in single request."""
        from jarvis.db.database import get_db
        db = get_db()
        
        # Create multiple pending attachments
        att_ids = []
        now = utcnow().isoformat()
        
        for i in range(5):
            att_id = f"batch-{i}-{secrets.token_hex(4)}"
            att_ids.append(att_id)
            _ensure_message(db, "msg-batch")

            db.execute(
                "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    att_id,
                    "msg-batch",
                    "audio/wav",
                    4096,
                    f"test-{i}.wav",
                    f"/tmp/test-{i}.wav",
                    "uploaded",
                    "pending",
                    now
                )
            )
        
        # Process all
        response = client.post(
            "/ai/process-attachments",
            json={"attachment_ids": att_ids},
            headers=AUTH
        )
        
        data = response.json()
        # 5 attachments were attempted, each reported honestly. The audio path now runs
        # the real STT backend, and these fixture files do not exist, so each is an
        # honest error (or not_processed where no real backend exists) — never a false
        # 'processed'. processed_count is 0 — not 5.
        assert data["processed_count"] == 0
        assert len(data["results"]) == 5
        assert all(r["status"] in ("error", "not_processed") for r in data["results"]), data["results"]
        # None of them may claim to have been transcribed.
        assert not any("text" in r for r in data["results"]), data["results"]

        # Bind each id individually: `id IN (?)` with a single comma-joined string
        # matches *zero* rows in SQLite (it compares id against the literal
        # "a,b,c,..." string), so the original `pending_count == 0` assertion was a
        # vacuous false-pass. Per-id placeholders actually read the rows.
        placeholders = ",".join("?" for _ in att_ids)
        pending_count = db.query(
            f"SELECT COUNT(*) as cnt FROM ai_attachments WHERE id IN ({placeholders}) AND processing_status = 'pending'",
            tuple(att_ids)
        )[0]["cnt"]
        assert pending_count == 0

        # The audio path runs the real STT backend, so these non-existent files land in
        # 'error' (or 'not_processed' where no real backend exists) — never 'processed',
        # and never left 'pending' (which would mean re-attempted forever).
        errored = db.query(
            f"SELECT COUNT(*) as cnt FROM ai_attachments WHERE id IN ({placeholders}) AND processing_status = 'error'",
            tuple(att_ids)
        )[0]["cnt"]
        not_processed = db.query(
            f"SELECT COUNT(*) as cnt FROM ai_attachments WHERE id IN ({placeholders}) AND processing_status = 'not_processed'",
            tuple(att_ids)
        )[0]["cnt"]
        assert errored + not_processed == 5, (errored, not_processed)

    def test_audio_attachment_transcribes_through_the_stt_backend(self, client, tmp_path):
        """A real STT backend that returns text makes the attachment genuinely
        processed: the transcript is returned and the DB says 'processed'.

        The STT seam is mocked here (a real microphone is not available in a test), but
        the contract under test is the endpoint's: it calls the configured backend, and
        only a *real* backend's successful result may be reported as processed.
        """
        from jarvis.db.database import get_db
        from jarvis.voice.stt import STTResult
        db = get_db()

        wav = tmp_path / "clip.wav"
        wav.write_bytes(b"RIFF....WAVEfmt ")

        att_id = "pending-" + secrets.token_hex(8)
        now = utcnow().isoformat()
        _ensure_message(db, "msg-audio")
        db.execute(
            "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (att_id, "msg-audio", "audio/wav", wav.stat().st_size, "clip.wav",
             str(wav), "uploaded", "pending", now)
        )

        class _RealEars:
            name = "test-ears"
            real = True

            async def transcribe(self, path):
                return STTResult(True, self.name, text="the recorded words",
                                 confidence=0.9)

        with patch("jarvis.voice.stt.get_stt", return_value=_RealEars()):
            response = client.post("/ai/process-attachments",
                                   json={"attachment_ids": [att_id]}, headers=AUTH)

        assert response.status_code == 200
        data = response.json()
        assert data["processed_count"] == 1, data
        result = data["results"][0]
        assert result["status"] == "processed", result
        assert result.get("text") == "the recorded words", result

        rows = db.query("SELECT processing_status FROM ai_attachments WHERE id = ?", (att_id,))
        assert rows[0]["processing_status"] == "processed"

    def test_audio_backend_that_is_not_real_ears_is_never_processed(self, client, tmp_path):
        """A non-real (mock) STT backend must never be reported as having heard the
        attachment — the same honesty rule as /voice/transcribe."""
        from jarvis.db.database import get_db
        from jarvis.voice.stt import STTResult
        db = get_db()

        wav = tmp_path / "clip2.wav"
        wav.write_bytes(b"RIFF....WAVEfmt ")

        att_id = "pending-" + secrets.token_hex(8)
        now = utcnow().isoformat()
        _ensure_message(db, "msg-audio2")
        db.execute(
            "INSERT INTO ai_attachments (id, message_id, file_type, file_size, filename, storage_path, upload_status, processing_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (att_id, "msg-audio2", "audio/wav", wav.stat().st_size, "clip2.wav",
             str(wav), "uploaded", "pending", now)
        )

        class _FakeEars:
            name = "transcript"
            real = False

            async def transcribe(self, path):
                return STTResult(True, self.name, text="echoed text")

        with patch("jarvis.voice.stt.get_stt", return_value=_FakeEars()):
            response = client.post("/ai/process-attachments",
                                   json={"attachment_ids": [att_id]}, headers=AUTH)

        assert response.status_code == 200
        data = response.json()
        assert data["processed_count"] == 0, data
        result = data["results"][0]
        assert result["status"] == "not_processed", result
        assert "text" not in result, result


# ---------------------------------------------------------------------------
# Integration with existing security
# ---------------------------------------------------------------------------

def test_attachments_require_same_auth_as_chat(client):
    """Attachment routes use same Protected dependency as chat routes."""
    # Both require valid token, so both should get 200 or both get 401/403
    
    # Get dashboard (requires auth)
    dash_resp = client.get("/dashboard", headers=AUTH)

    # Get attachment metadata (also requires auth)
    att_resp = client.get(
        "/ai/attachments/test-id",
        headers=AUTH
    )
    
    # Both should have similar authentication behavior
    # (200 if authenticated properly, error if not)
    # For this test, we just verify both return non-401 status (require auth)
    assert dash_resp.status_code != 401  # Dashboard requires auth
    assert att_resp.status_code != 401  # Attachment route requires auth


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
