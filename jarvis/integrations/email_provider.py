"""Email providers (v0.7).

Two backends, both real:

* :class:`ImapSmtpProvider` — reads over IMAP and sends over SMTP using a real
  mail account. Enabled when credentials are configured.
* :class:`LocalMailProvider` — a local maildir. Reads from
  ``<home>/mail/inbox`` and writes real RFC822 ``.eml`` files to
  ``<home>/mail/sent`` and ``<home>/mail/drafts``.

The local backend is not a stub: it produces genuine ``.eml`` files that any
mail client can open, and it means email works out of the box with no account.
Which backend is in use is always reported, never implied.

Reads report *what they encountered* as well as what they found:
:meth:`EmailProvider.search` returns a :class:`ProviderResults` list whose
``status`` separates "this mailbox is empty" (``REAL_EMPTY``) from "I could not
reach it" (``UNAVAILABLE``), "my credentials were rejected" (``AUTH_ERROR``),
"the connection broke" (``NETWORK_ERROR``) and "the server refused the command"
(``PROVIDER_ERROR``). A single message is fetched with :meth:`read`, where
``None`` means only "no such message" and a failure raises
:class:`MailReadError`.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import json
import os
import re
import smtplib
import ssl
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage as StdEmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from ..security.privacy import redact
from .calendar_provider import (
    # One vocabulary for what a read encountered. Both integration modules live
    # in this package and must not drift into two enums with the same names, so
    # the mail side imports the shared states rather than restating them.
    # ``read_status``/``status_token``/``subset_read`` come along as the mail
    # tools' import surface, so nothing mail-facing has to reach into the
    # calendar module to describe a mailbox.
    ProviderResults,
    ProviderStatus,
    classify_transport_error,
    error_detail,
    read_status,
    status_token,
    subset_read,
)

log = get_logger("integrations.email")

#: Hard ceiling on how much of a message body we will return inline.
MAX_BODY_CHARS = 20_000


class MailReadError(Exception):
    """A single-message read that could not be carried out.

    :meth:`EmailProvider.read` returns ``Optional[dict]``, and ``None`` already
    means "this mailbox answered and has no such message" -- a meaning two
    consumers that are not part of this change rely on. So a *failure* is
    reported by raising instead of by returning ``None``, which is the only way
    to keep "there is no such message" and "I could not look" apart here.

    Carrying the :class:`ProviderStatus` on the exception lets the tool say
    "check your mail password" and "cannot reach your mail server" as two
    different sentences.
    """

    def __init__(self, status: ProviderStatus, detail: str = "", endpoint: str = "") -> None:
        self.status = status
        self.detail = detail or status.value
        self.endpoint = endpoint
        super().__init__(f"{status.value}: {self.detail}")


@dataclass
class EmailSummary:
    """One message as JARVIS presents it."""

    uid: str
    sender: str = ""
    recipients: List[str] = field(default_factory=list)
    subject: str = ""
    date: str = ""
    snippet: str = ""
    unread: bool = False
    has_attachments: bool = False
    folder: str = "inbox"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "from": self.sender,
            "to": self.recipients,
            "subject": self.subject,
            "date": self.date,
            "snippet": self.snippet,
            "unread": self.unread,
            "has_attachments": self.has_attachments,
            "folder": self.folder,
        }


@dataclass
class EmailDraft:
    """A message about to be sent, after validation."""

    to: List[str]
    subject: str
    body: str
    attachments: List[Path] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)

    def preview(self) -> str:
        lines = [
            f"To: {', '.join(self.to) or '(none)'}",
            f"Subject: {self.subject or '(none)'}",
        ]
        if self.cc:
            lines.append(f"Cc: {', '.join(self.cc)}")
        if self.attachments:
            lines.append("Attachments: " + ", ".join(p.name for p in self.attachments))
        body = self.body.strip()
        if len(body) > 400:
            body = body[:400] + "…"
        lines.append("")
        lines.append(body)
        return "\n".join(lines)


class EmailProvider(ABC):
    """Read, search and send email."""

    name: str = "base"
    is_remote: bool = False

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    @abstractmethod
    def available(self) -> bool:
        """True when this provider can actually be used."""

    @abstractmethod
    def search(self, query: str = "", limit: int = 10, unread_only: bool = False) -> "ProviderResults":
        """Find messages. An empty query means 'recent messages'.

        Returns a :class:`ProviderResults` list: empty *and* labelled
        ``REAL_EMPTY`` only when the mailbox actually answered.
        """

    @abstractmethod
    def read(self, uid: str) -> Optional[Dict[str, Any]]:
        """Return one message in full."""

    @abstractmethod
    def save_draft(self, draft: EmailDraft) -> Dict[str, Any]:
        """Store a draft without sending it."""

    @abstractmethod
    def send(self, draft: EmailDraft) -> Dict[str, Any]:
        """Send a message. Callers must have confirmed first."""

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "available": self.available(), "remote": self.is_remote}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def looks_like_address(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", (value or "").strip()))


def normalise_addresses(values: Iterable[str]) -> List[str]:
    out = []
    for value in values:
        address = (value or "").strip()
        if address and address not in out:
            out.append(address)
    return out


def build_message(draft: EmailDraft, sender: str = "jarvis@localhost") -> StdEmailMessage:
    """Build a real RFC822 message, with attachments if present."""
    message = StdEmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(draft.to)
    if draft.cc:
        message["Cc"] = ", ".join(draft.cc)
    message["Subject"] = draft.subject
    message["Date"] = email.utils.formatdate(localtime=True)
    message["Message-ID"] = email.utils.make_msgid(domain="jarvis-forge")
    message.set_content(draft.body or "")

    for path in draft.attachments:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        message.add_attachment(
            data,
            maintype="application",
            subtype="octet-stream",
            filename=path.name,
        )
    return message


def parse_message(raw: bytes, uid: str, folder: str = "inbox") -> Dict[str, Any]:
    """Turn raw RFC822 bytes into a readable dict."""
    message = email.message_from_bytes(raw, policy=email.policy.default)

    body = ""
    attachments = []
    if message.is_multipart():
        for part in message.walk():
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                attachments.append(part.get_filename() or "(unnamed)")
                continue
            if part.get_content_type() == "text/plain" and not body:
                try:
                    body = part.get_content()
                except Exception:
                    body = ""
    else:
        try:
            body = message.get_content()
        except Exception:
            body = str(message.get_payload())

    if not isinstance(body, str):
        body = str(body)
    body = body[:MAX_BODY_CHARS]

    return {
        "uid": uid,
        "from": str(message.get("From", "")),
        "to": [a.strip() for a in str(message.get("To", "")).split(",") if a.strip()],
        "subject": str(message.get("Subject", "")),
        "date": str(message.get("Date", "")),
        "body": body,
        "attachments": attachments,
        "folder": folder,
    }


def _safe_component(value: str) -> str:
    """Make a string safe to use as a filename."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return cleaned.strip("_") or "message"


def classify_smtp_error(exc: BaseException) -> ProviderStatus:
    """What an SMTP failure actually was, in terms of what the user can do.

    A rejected login is not an unreachable server: one needs an app password,
    the other needs the network or the host name.
    """
    lowered = str(exc).lower()
    code = getattr(exc, "smtp_code", 0) or 0

    if isinstance(exc, smtplib.SMTPAuthenticationError) or code in (401, 419, 454, 534, 535, 538):
        return ProviderStatus.AUTH_ERROR
    if any(marker in lowered for marker in ("invalid credentials", "log in", "login", "password", "authenticat")):
        return ProviderStatus.AUTH_ERROR
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return ProviderStatus.UNAVAILABLE
    if isinstance(exc, smtplib.SMTPException):
        return ProviderStatus.PROVIDER_ERROR
    return classify_transport_error(exc)


# ---------------------------------------------------------------------------
# Local maildir provider
# ---------------------------------------------------------------------------
class LocalMailProvider(EmailProvider):
    """A local maildir. Real files, no account required."""

    name = "local_maildir"
    is_remote = False

    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__(settings)
        self.root = self.settings.home / "mail"
        self.sender = "jarvis@localhost"

    def available(self) -> bool:
        return True

    # -- folders -------------------------------------------------------------
    def folder(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _messages(self, folder: str) -> List[Path]:
        directory = self.folder(folder)
        files = [p for p in directory.glob("*.eml") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files

    # -- read ----------------------------------------------------------------
    def search(self, query: str = "", limit: int = 10, unread_only: bool = False) -> ProviderResults:
        needle = (query or "").strip().lower()
        results: List[EmailSummary] = []

        for path in self._messages("inbox"):
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            parsed = parse_message(raw, uid=path.stem, folder="inbox")

            haystack = " ".join(
                [parsed["subject"], parsed["from"], parsed["body"]]
            ).lower()
            if needle and needle not in haystack:
                continue

            unread = not path.with_suffix(".read").exists()
            if unread_only and not unread:
                continue

            results.append(
                EmailSummary(
                    uid=path.stem,
                    sender=parsed["from"],
                    recipients=parsed["to"],
                    subject=parsed["subject"],
                    date=parsed["date"],
                    snippet=parsed["body"].strip().replace("\n", " ")[:160],
                    unread=unread,
                    has_attachments=bool(parsed["attachments"]),
                )
            )
            if len(results) >= limit:
                break

        # The maildir was read; nothing matching is genuinely nothing matching.
        return ProviderResults(results, ProviderStatus.HAS_RESULTS if results else ProviderStatus.REAL_EMPTY)

    def read(self, uid: str) -> Optional[Dict[str, Any]]:
        path = self.folder("inbox") / f"{uid}.eml"
        if not path.exists():
            return None
        parsed = parse_message(path.read_bytes(), uid=uid)
        # Mark read by creating a sidecar marker (keeps the .eml untouched).
        try:
            path.with_suffix(".read").touch()
        except OSError:
            pass
        return parsed

    # -- write ---------------------------------------------------------------
    def _write(self, folder: str, draft: EmailDraft) -> Path:
        message = build_message(draft, self.sender)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{_safe_component(draft.subject)[:40]}-{uuid.uuid4().hex[:6]}.eml"
        path = self.folder(folder) / name
        path.write_bytes(message.as_bytes())
        return path

    def save_draft(self, draft: EmailDraft) -> Dict[str, Any]:
        path = self._write("drafts", draft)
        return {
            "ok": True,
            "backend": self.name,
            "sent": False,
            "path": str(path),
            "message": f"Draft saved to {path.name}.",
        }

    def send(self, draft: EmailDraft) -> Dict[str, Any]:
        """'Sending' locally means writing a real .eml to the outbox."""
        path = self._write("sent", draft)
        return {
            "ok": True,
            "backend": self.name,
            "sent": True,
            "path": str(path),
            "message": (
                f"Written to your local outbox as {path.name}. No mail account is "
                "configured, so nothing left this machine."
            ),
        }

    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        base.update(
            {
                "root": str(self.root),
                "inbox_count": len(self._messages("inbox")),
                "note": "Local maildir — real .eml files, no account required.",
            }
        )
        return base


# ---------------------------------------------------------------------------
# IMAP / SMTP provider
# ---------------------------------------------------------------------------
class ImapSmtpProvider(EmailProvider):
    """A real mail account over IMAP and SMTP."""

    name = "imap_smtp"
    is_remote = True

    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__(settings)
        self.host = settings.email_imap_host
        self.port = settings.email_imap_port
        self.smtp_host = settings.email_smtp_host
        self.smtp_port = settings.email_smtp_port
        self.user = settings.email_address
        self.password = settings.email_password
        self.sender = settings.email_address or "jarvis@localhost"

    def available(self) -> bool:
        return bool(self.host and self.user and self.password)

    # -- connection ----------------------------------------------------------
    def _imap(self) -> imaplib.IMAP4_SSL:
        connection = imaplib.IMAP4_SSL(self.host, self.port, timeout=20)
        connection.login(self.user, self.password)
        return connection

    def _endpoint(self) -> str:
        host = (self.host or "").strip()
        return f"{host}:{self.port}" if host and self.port else host

    def _failure(self, status: ProviderStatus, detail: str) -> ProviderResults:
        """An empty result that says why it is empty."""
        return ProviderResults([], status, detail=detail, endpoint=self._endpoint())

    def _write_failure(self, status: ProviderStatus, detail: str) -> Dict[str, Any]:
        """A send/draft failure carrying the same states a read can report.

        ``{"ok": False, "error": ...}`` is the contract the tools already
        branch on; ``status`` is what lets them tell "your mail password was
        rejected" from "your mail server is not reachable" -- two different
        things for the user to fix.
        """
        return {
            "ok": False,
            "backend": self.name,
            "sent": False,
            "error": detail,
            "status": status.value,
            "detail": detail,
            "endpoint": self._endpoint(),
        }

    def _decode(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    # -- read ----------------------------------------------------------------
    def search(self, query: str = "", limit: int = 10, unread_only: bool = False) -> ProviderResults:
        try:
            connection = self._imap()
        except Exception as exc:
            # Cannot connect, cannot log in, connection dropped: none of these
            # is an empty inbox, and they are not each other either.
            status = classify_transport_error(exc)
            detail = error_detail(exc, self.settings)
            log.info("imap session could not start", extra_fields={"outcome": status.value})
            return self._failure(status, detail)

        try:
            select_status, _ = connection.select("INBOX")
            if select_status != "OK":
                return self._failure(
                    ProviderStatus.PROVIDER_ERROR, f"IMAP SELECT INBOX returned {select_status}"
                )

            criteria = []
            if unread_only:
                criteria.append("UNSEEN")
            if query:
                safe = query.replace('"', "")
                criteria += ["OR", f'SUBJECT "{safe}"', f'FROM "{safe}"']
            if not criteria:
                criteria = ["ALL"]

            status, data = connection.search(None, *criteria)
            if status != "OK":
                # The server understood the search and refused it. Returning the
                # empty list here is what made "no messages matching" a lie.
                return self._failure(ProviderStatus.PROVIDER_ERROR, f"IMAP SEARCH returned {status}")

            raw_ids = data[0] if data and data[0] else b""
            if isinstance(raw_ids, str):
                raw_ids = raw_ids.encode()
            uids = raw_ids.split()[-limit:][::-1]

            results: List[EmailSummary] = []
            undeliverable = 0
            for raw_uid in uids:
                uid = self._decode(raw_uid)
                fetch_status, payload = connection.fetch(raw_uid, "(RFC822 FLAGS)")
                if fetch_status != "OK" or not payload:
                    undeliverable += 1
                    continue
                for part in payload:
                    if isinstance(part, tuple) and len(part) > 1:
                        parsed = parse_message(part[1], uid=uid)
                        flags = self._decode(part[0])
                        results.append(
                            EmailSummary(
                                uid=uid,
                                sender=parsed["from"],
                                recipients=parsed["to"],
                                subject=parsed["subject"],
                                date=parsed["date"],
                                snippet=parsed["body"].strip().replace("\n", " ")[:160],
                                unread="\\Seen" not in flags,
                                has_attachments=bool(parsed["attachments"]),
                            )
                        )

            if not results and undeliverable:
                # It named messages and then would not hand any of them over.
                # That is a failed read, not an empty mailbox.
                return self._failure(
                    ProviderStatus.PROVIDER_ERROR,
                    f"IMAP FETCH refused all {undeliverable} message(s) the search named",
                )

            return ProviderResults(
                results,
                ProviderStatus.HAS_RESULTS if results else ProviderStatus.REAL_EMPTY,
                endpoint=self._endpoint(),
            )
        except Exception as exc:
            status = classify_transport_error(exc)
            detail = error_detail(exc, self.settings)
            log.info("imap search failed", extra_fields={"outcome": status.value})
            return self._failure(status, detail)
        finally:
            try:
                connection.logout()
            except Exception:  # pragma: no cover
                pass

    def read(self, uid: str) -> Optional[Dict[str, Any]]:
        """One message, or ``None`` when this mailbox genuinely has no such uid.

        A failure to reach or authenticate against the mailbox raises
        :class:`MailReadError` instead: ``None`` is reserved for "answered, not
        here", so the two can never be confused downstream.
        """
        try:
            connection = self._imap()
        except Exception as exc:
            raise MailReadError(
                classify_transport_error(exc), error_detail(exc, self.settings), self._endpoint()
            ) from exc

        try:
            select_status, _ = connection.select("INBOX")
            if select_status != "OK":
                raise MailReadError(
                    ProviderStatus.PROVIDER_ERROR,
                    f"IMAP SELECT INBOX returned {select_status}",
                    self._endpoint(),
                )
            status, payload = connection.fetch(uid.encode(), "(RFC822)")
            if status != "OK" or not payload:
                return None
            for part in payload:
                if isinstance(part, tuple) and len(part) > 1:
                    return parse_message(part[1], uid=uid)
            return None
        except MailReadError:
            raise
        except Exception as exc:
            raise MailReadError(
                classify_transport_error(exc), error_detail(exc, self.settings), self._endpoint()
            ) from exc
        finally:
            try:
                connection.logout()
            except Exception:  # pragma: no cover
                pass

    # -- write ---------------------------------------------------------------
    def save_draft(self, draft: EmailDraft) -> Dict[str, Any]:
        """Append to the server's Drafts folder rather than sending."""
        try:
            connection = self._imap()
        except Exception as exc:
            return self._write_failure(
                classify_transport_error(exc),
                f"could not save the draft: {error_detail(exc, self.settings)}",
            )
        try:
            message = build_message(draft, self.sender)
            status, _ = connection.append(
                "Drafts", "(\\Draft)", imaplib.Time2Internaldate(time.time()),
                message.as_bytes(),
            )
            if status != "OK":
                return self._write_failure(
                    ProviderStatus.PROVIDER_ERROR,
                    f"could not save the draft: IMAP APPEND returned {status}",
                )
            return {
                "ok": True,
                "backend": self.name,
                "sent": False,
                "message": "Draft saved to your Drafts folder.",
            }
        finally:
            try:
                connection.logout()
            except Exception:  # pragma: no cover
                pass

    def send(self, draft: EmailDraft) -> Dict[str, Any]:
        message = build_message(draft, self.sender)
        context = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30, context=context) as server:
                server.login(self.user, self.password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            # Never include credentials in the error.
            return self._write_failure(
                classify_smtp_error(exc),
                redact(f"send failed: {exc}", self.settings),
            )
        except Exception as exc:  # pragma: no cover - defensive
            return self._write_failure(
                classify_transport_error(exc),
                redact(f"send failed: {exc}", self.settings),
            )

        return {
            "ok": True,
            "backend": self.name,
            "sent": True,
            "message": f"Sent to {', '.join(draft.to)} via {self.smtp_host}.",
        }

    def describe(self) -> Dict[str, Any]:
        base = super().describe()
        base.update(
            {
                "imap_host": self.host,
                "smtp_host": self.smtp_host,
                "account": self.user,
                "note": "Real mail account. Credentials are never logged.",
            }
        )
        return base


def get_email_provider(settings: Optional[Settings] = None) -> EmailProvider:
    """Prefer the configured account; otherwise use the local maildir."""
    settings = settings or get_settings()
    remote = ImapSmtpProvider(settings)
    if remote.available():
        log.info("email provider selected", extra_fields={"provider": remote.name})
        return remote
    return LocalMailProvider(settings)


def describe_email(settings: Optional[Settings] = None) -> Dict[str, Any]:
    settings = settings or get_settings()
    remote = ImapSmtpProvider(settings)
    local = LocalMailProvider(settings)
    active = get_email_provider(settings)
    return {
        "active": active.name,
        "configured_account": remote.available(),
        "local": local.describe(),
        "sending_requires_confirmation": True,
    }
