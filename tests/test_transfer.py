"""Secure device-to-device transfer tests (v0.6).

The properties that matter here are the safety ones: a device that is not
paired (or has been revoked) can never transfer, a failed transfer leaves no
partial file, and an existing file is never silently overwritten.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

import pytest

from jarvis.devices.pairing import PairingManager, derive_proof
from jarvis.registry.devices import DeviceRegistry
from jarvis.transport import (
    DeviceEndpoint,
    ProtocolError,
    TransferServer,
    TransferState,
    can_transition,
    safe_filename,
    unique_destination,
)
from jarvis.transport.protocol import Offer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ensure_paired(device_id: str = "phone_main") -> bytes:
    """Pair a device if needed and return its secret."""
    manager = PairingManager()
    registry = DeviceRegistry()
    device = registry.get(device_id)

    if device is not None and device.is_paired:
        secret = manager.load_secret(device_id)
        if secret:
            return secret

    request = manager.request(device_id, f"Test {device_id}")
    approved = manager.approve(request.pairing_id)
    return base64.b64decode(approved["secret_b64"])


@pytest.fixture(scope="module")
def server() -> TransferServer:
    """A server handle for querying transfers and the inbox.

    Note: the listening socket is opened per-test inside :func:`run_transfer`,
    because a server started in one event loop is dead to a client running in
    another — the accept loop has to be alive on the same loop as the client.
    """
    return TransferServer()


def run_transfer(
    source: Path,
    *,
    device_id: str = "phone_main",
    secret: bytes | None = None,
    server: TransferServer | None = None,
):
    """Send one file to a freshly started receiver, in a single event loop."""

    async def _go():
        receiver = server or TransferServer()
        port = await receiver.start()
        try:
            endpoint = DeviceEndpoint(device_id, secret or ensure_paired(device_id))
            result = await endpoint.send_file("127.0.0.1", port, source)
        finally:
            await receiver.stop()
        return result

    return asyncio.run(_go())


@pytest.fixture()
def sample_file(tmp_area: Path):
    def _make(name: str = "sample.txt", size: int = 2048) -> Path:
        path = tmp_area / name
        if size <= 4096:
            path.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
        else:
            path.write_bytes(b"A" * size)
        return path

    return _make


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------
def test_safe_filename_strips_directory_traversal() -> None:
    """A client must not be able to influence *where* a file lands."""
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("/absolute/path/report.pdf") == "report.pdf"
    assert safe_filename("..") == "received_file"
    assert safe_filename("") == "received_file"
    assert safe_filename("normal-file_v2.txt") == "normal-file_v2.txt"


def test_unique_destination_never_overwrites(tmp_area: Path) -> None:
    first = unique_destination(tmp_area, "report.pdf")
    first.write_text("original")

    second = unique_destination(tmp_area, "report.pdf")
    assert second != first
    assert not second.exists()
    assert first.read_text() == "original"


def test_offer_rejects_oversized_file() -> None:
    with pytest.raises(ProtocolError, match="above the"):
        Offer.parse({"filename": "big.bin", "size": 10 ** 12, "sha256": "a" * 64})


def test_offer_rejects_bad_checksum() -> None:
    with pytest.raises(ProtocolError, match="sha256"):
        Offer.parse({"filename": "x", "size": 10, "sha256": "not-a-digest"})


def test_offer_rejects_negative_size() -> None:
    with pytest.raises(ProtocolError):
        Offer.parse({"filename": "x", "size": -1, "sha256": "a" * 64})


def test_state_machine_refuses_illegal_transitions() -> None:
    assert can_transition(TransferState.PENDING, TransferState.CONFIRMED)
    assert can_transition(TransferState.TRANSFERRING, TransferState.VERIFYING)
    # A completed transfer is terminal.
    assert not can_transition(TransferState.COMPLETED, TransferState.TRANSFERRING)
    # Nothing may jump straight from pending to completed.
    assert not can_transition(TransferState.PENDING, TransferState.COMPLETED)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_multi_chunk_transfer_verifies(server, sample_file, tmp_area: Path) -> None:
    """A file spanning several chunks must arrive byte-identical."""
    source = sample_file("multi.bin", size=200_000)
    result = run_transfer(source, server=server)

    assert result["ok"], result.get("error")
    assert result["verified"] is True

    landed = server.inbox / result["filename"]
    assert landed.exists()
    assert hashlib.sha256(landed.read_bytes()).hexdigest() == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()


def test_empty_file_transfers(server, sample_file) -> None:
    source = sample_file("empty.bin", size=0)
    result = run_transfer(source, server=server)
    assert result["ok"], result.get("error")
    assert result["bytes"] == 0


def test_existing_file_is_not_overwritten(server, sample_file) -> None:
    """Silently clobbering a file on the receiver is never acceptable."""
    first = sample_file("collide.txt")
    first.write_text("first")
    r1 = run_transfer(first, server=server)

    second = sample_file("collide.txt")
    second.write_text("second")
    r2 = run_transfer(second, server=server)

    assert r1["filename"] != r2["filename"], "the second send must not reuse the name"
    assert (server.inbox / r1["filename"]).read_text() == "first"
    assert (server.inbox / r2["filename"]).read_text() == "second"


def test_no_partial_file_left_behind(server, sample_file) -> None:
    source = sample_file("clean.bin", size=1000)
    run_transfer(source, server=server)
    assert list(server.inbox.glob("*.part")) == []


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def test_wrong_secret_is_rejected(server, sample_file) -> None:
    source = sample_file("auth.txt")
    result = run_transfer(source, secret=b"\x00" * 32, server=server)
    assert not result["ok"]
    assert result.get("rejected") is True


def test_unpaired_device_cannot_transfer(server, sample_file) -> None:
    source = sample_file("unpaired.txt")
    # watch_main is seeded but never paired.
    result = run_transfer(source, device_id="watch_main", secret=ensure_paired(), server=server)
    assert not result["ok"]
    assert result.get("rejected") is True


def test_revoked_device_cannot_transfer(server, sample_file) -> None:
    """Revocation must take effect immediately, including for a valid secret."""
    manager = PairingManager()

    # Pair a throwaway device so revoking it cannot disturb other tests.
    request = manager.request("phone_temp", "Temp Phone")
    approved = manager.approve(request.pairing_id)
    secret = base64.b64decode(approved["secret_b64"])

    source = sample_file("revoked.txt")

    before = run_transfer(source, device_id="phone_temp", secret=secret, server=server)
    assert before["ok"], before.get("error")

    manager.revoke("phone_temp")

    after = run_transfer(source, device_id="phone_temp", secret=secret, server=server)
    assert not after["ok"]
    assert after.get("rejected") is True


def test_revocation_destroys_key_material() -> None:
    manager = PairingManager()
    request = manager.request("phone_gone", "Gone Phone")
    manager.approve(request.pairing_id)

    assert manager.load_secret("phone_gone") is not None
    manager.revoke("phone_gone")
    assert manager.load_secret("phone_gone") is None


def test_hmac_proof_is_required() -> None:
    manager = PairingManager()
    request = manager.request("phone_hmac", "HMAC Phone")
    approved = manager.approve(request.pairing_id)
    secret = base64.b64decode(approved["secret_b64"])

    challenge = "a-fixed-challenge"
    assert manager.authenticate("phone_hmac", challenge, derive_proof(secret, challenge))
    assert not manager.authenticate("phone_hmac", challenge, "00" * 32)
    assert not manager.authenticate("phone_hmac", challenge, "")
    # A proof for a different challenge must not be accepted.
    assert not manager.authenticate(
        "phone_hmac", "other-challenge", derive_proof(secret, challenge)
    )


# ---------------------------------------------------------------------------
# Transfer records
# ---------------------------------------------------------------------------
def test_transfer_is_recorded_and_queryable(server, sample_file) -> None:
    source = sample_file("recorded.txt")
    result = run_transfer(source, server=server)
    record = server.get_transfer(result["transfer_id"])

    assert record is not None
    assert record["state"] == TransferState.COMPLETED.value
    assert record["device_id"] == "phone_main"
    assert record["bytes_sent"] == source.stat().st_size


def test_stats_report_success_rate(server) -> None:
    stats = server.stats()
    assert {"total", "by_state", "completed", "success_rate"} <= set(stats)
    assert 0.0 <= stats["success_rate"] <= 1.0
