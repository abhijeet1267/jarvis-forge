"""Mac-side transfer server (v0.6).

Listens on loopback over TLS, authenticates each device by HMAC, and receives
files into a designated inbox. Every state change is recorded in the
``transfers`` / ``transfer_events`` tables so progress and history are auditable.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import Settings, get_settings
from ..core.models import new_id, new_secret_id, utcnow
from ..db.database import get_db
from ..devices.pairing import PairingManager
from ..logging_setup import get_logger
from .protocol import (
    MESSAGE_TIMEOUT,
    STREAM_LIMIT,
    Offer,
    ProtocolError,
    TransferState,
    can_transition,
    decode,
    decode_chunk,
    encode,
    new_challenge,
    unique_destination,
)
from .tls import TLSUnavailable, server_context

log = get_logger("transport.server")


class TransferServer:
    """Receives files from paired devices."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.pairing = PairingManager(self.settings)
        self.db = get_db()
        self._server: Optional[asyncio.AbstractServer] = None
        self.host = "127.0.0.1"
        self.port = 0  # assigned on start

    @property
    def inbox(self) -> Path:
        """Where received files land. Never the user's Desktop by default."""
        return self.settings.home / "inbox"

    # -- lifecycle -----------------------------------------------------------
    async def start(self, port: int = 0) -> int:
        try:
            context, _material = server_context(self.settings)
        except TLSUnavailable as exc:
            log.info("transfer server unavailable", extra_fields={"error": str(exc)})
            raise

        self._server = await asyncio.start_server(
            self._handle,
            host=self.host,
            port=port,
            ssl=context,
            # Must exceed one base64 chunk, or any multi-chunk file fails.
            limit=STREAM_LIMIT,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("transfer server listening", extra_fields={"port": self.port})
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def serve_forever(self) -> None:  # pragma: no cover - long-running
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    # -- bookkeeping ---------------------------------------------------------
    def _record(self, transfer_id: str, state: TransferState, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO transfer_events (event_id, transfer_id, state, detail) VALUES (?, ?, ?, ?)",
            (new_id("TEV"), transfer_id, state.value, detail),
        )

    def _create_transfer(
        self, device_id: str, offer: Offer, direction: str = "device_to_mac"
    ) -> str:
        transfer_id = new_secret_id("XFER")
        self.db.execute(
            """
            INSERT INTO transfers
                (transfer_id, direction, device_id, filename, size_bytes, sha256, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transfer_id,
                direction,
                device_id,
                offer.filename,
                offer.size,
                offer.sha256,
                TransferState.PENDING.value,
            ),
        )
        self._record(transfer_id, TransferState.PENDING, "offer received")
        return transfer_id

    def _advance(self, transfer_id: str, state: TransferState, detail: str = "") -> None:
        row = self.db.query("SELECT state FROM transfers WHERE transfer_id = ?", (transfer_id,))
        if not row:
            return
        current = TransferState(row[0]["state"])
        if not can_transition(current, state):
            log.info(
                "refused illegal transition",
                extra_fields={"from": current.value, "to": state.value},
            )
            return
        self.db.execute(
            "UPDATE transfers SET state = ? WHERE transfer_id = ?", (state.value, transfer_id)
        )
        self._record(transfer_id, state, detail)

    def _fail(self, transfer_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE transfers SET state = ?, error = ?, completed_at = ? WHERE transfer_id = ?",
            (TransferState.FAILED.value, error, utcnow().isoformat(), transfer_id),
        )
        self._record(transfer_id, TransferState.FAILED, error)

    # -- connection handling -------------------------------------------------
    async def _read(self, reader: asyncio.StreamReader) -> Dict[str, Any]:
        line = await asyncio.wait_for(reader.readline(), timeout=MESSAGE_TIMEOUT)
        if not line:
            raise ProtocolError("connection closed")
        return decode(line)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        transfer_id = ""
        try:
            # --- handshake --------------------------------------------------
            hello = await self._read(reader)
            if hello.get("type") != "hello":
                raise ProtocolError("expected hello")

            device_id = str(hello.get("device_id", "")).strip()
            challenge = new_challenge()
            writer.write(encode({"type": "challenge", "challenge": challenge}))
            await writer.drain()

            auth = await self._read(reader)
            if auth.get("type") != "auth":
                raise ProtocolError("expected auth")

            if not self.pairing.authenticate(device_id, challenge, str(auth.get("proof", ""))):
                # Do not say *why* -- an unpaired device learns nothing.
                writer.write(encode({"type": "error", "message": "authentication failed"}))
                await writer.drain()
                log.info("transfer auth rejected", extra_fields={"device": device_id})
                return

            writer.write(encode({"type": "ready", "session": new_id("SESS")}))
            await writer.drain()
            log.info("device authenticated", extra_fields={"device": device_id, "peer": str(peer)})

            # --- transfer ---------------------------------------------------
            message = await self._read(reader)
            if message.get("type") != "offer":
                raise ProtocolError("expected offer")

            offer = Offer.parse(message)
            transfer_id = self._create_transfer(device_id, offer)

            destination = unique_destination(self.inbox, offer.filename)
            offer.destination = destination

            self._advance(transfer_id, TransferState.CONFIRMED, "accepted by policy")
            self._advance(transfer_id, TransferState.CONNECTING, "stream open")
            self._advance(transfer_id, TransferState.TRANSFERRING, f"-> {destination.name}")

            writer.write(
                encode(
                    {
                        "type": "accept",
                        "transfer_id": transfer_id,
                        "filename": destination.name,
                    }
                )
            )
            await writer.drain()

            # Write to a temporary file; only rename once verified.
            temp_path = destination.with_suffix(destination.suffix + ".part")
            received = 0

            with temp_path.open("wb") as handle:
                while True:
                    message = await self._read(reader)
                    kind = message.get("type")

                    if kind == "chunk":
                        payload = decode_chunk(message)
                        if received + len(payload) > offer.size:
                            raise ProtocolError("device sent more bytes than offered")
                        handle.write(payload)
                        offer.digest.update(payload)
                        received += len(payload)

                    elif kind == "cancel":
                        handle.close()
                        temp_path.unlink(missing_ok=True)
                        self.db.execute(
                            "UPDATE transfers SET state = ?, completed_at = ? WHERE transfer_id = ?",
                            (TransferState.CANCELLED.value, utcnow().isoformat(), transfer_id),
                        )
                        self._record(transfer_id, TransferState.CANCELLED, "cancelled by device")
                        writer.write(encode({"type": "cancelled", "transfer_id": transfer_id}))
                        await writer.drain()
                        return

                    elif kind == "done":
                        break

                    else:
                        raise ProtocolError(f"unexpected message {kind!r}")

            # --- verify -----------------------------------------------------
            self._advance(transfer_id, TransferState.VERIFYING, "checking checksum")

            if received != offer.size:
                raise ProtocolError(
                    f"size mismatch: expected {offer.size} bytes, received {received}"
                )

            actual = offer.digest.hexdigest()
            if actual != offer.sha256:
                raise ProtocolError("checksum mismatch — file may be corrupted")

            temp_path.replace(destination)  # atomic

            self.db.execute(
                """
                UPDATE transfers
                SET state = ?, bytes_sent = ?, dest_path = ?, completed_at = ?
                WHERE transfer_id = ?
                """,
                (
                    TransferState.COMPLETED.value,
                    received,
                    str(destination),
                    utcnow().isoformat(),
                    transfer_id,
                ),
            )
            self._record(transfer_id, TransferState.COMPLETED, f"verified {actual[:16]}")

            writer.write(
                encode({"type": "verified", "transfer_id": transfer_id, "bytes": received})
            )
            await writer.drain()
            log.info(
                "transfer complete",
                extra_fields={"transfer_id": transfer_id, "bytes": received},
            )

        except asyncio.TimeoutError:
            if transfer_id:
                self._fail(transfer_id, "timed out waiting for the device")
        except ProtocolError as exc:
            if transfer_id:
                self._fail(transfer_id, str(exc))
            try:
                writer.write(encode({"type": "error", "message": str(exc)}))
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            log.info("transfer protocol error", extra_fields={"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            if transfer_id:
                self._fail(transfer_id, f"unexpected error: {exc}")
            log.info("transfer failed", extra_fields={"error": str(exc)})
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    # -- queries -------------------------------------------------------------
    def get_transfer(self, transfer_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,))
        return dict(rows[0]) if rows else None

    def recent(self, limit: int = 20) -> list:
        rows = self.db.query(
            "SELECT * FROM transfers ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        rows = self.db.query("SELECT state, COUNT(*) AS n FROM transfers GROUP BY state")
        by_state = {r["state"]: r["n"] for r in rows}
        total = sum(by_state.values())
        completed = by_state.get(TransferState.COMPLETED.value, 0)
        return {
            "total": total,
            "by_state": by_state,
            "completed": completed,
            "success_rate": round(completed / total, 3) if total else 0.0,
        }
