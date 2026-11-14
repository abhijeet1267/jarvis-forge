"""PDF text extraction for the attachment pipeline (v1).

Extracts real text from an uploaded PDF via ``pypdf``. It is honest about the three
ways a PDF can fail to yield text, because each needs a different message — and none of
them is "the document was understood":

* unavailable (pypdf not installed) — a missing dependency, not a document problem;
* encrypted (a password-protected PDF) — we must not pretend it was read;
* unreadable (malformed/zero-page/other) — the file could not be parsed.

The extracted text is bounded, so a pathological PDF cannot blow up the reply or memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


def _bounded(text: str, limit: int = 20_000) -> str:
    return text[:limit]


def available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        return False


def extract_text(path: str, *, limit: int = 20_000) -> Dict[str, Any]:
    """Extract text from a PDF. Returns ``{"ok": True, "text": ..., "pages": N}`` on
    success, or ``{"ok": False, "reason": ...}`` with a truthful failure category."""
    target = Path(path).expanduser()
    if not target.is_file():
        return {"ok": False, "reason": "unreadable", "error": f"I could not find {path}."}

    try:
        from pypdf import PdfReader
    except Exception:
        return {
            "ok": False,
            "reason": "unavailable",
            "error": "pypdf is not installed, so I cannot extract PDF text.",
        }

    try:
        reader = PdfReader(str(target))
    except Exception as exc:
        # ``PdfReadError`` covers both "not a PDF" and the common encryption case where
        # pypdf raises before ``is_encrypted`` is even consultable. Name it precisely
        # when we can.
        if "encrypt" in str(exc).lower() or "password" in str(exc).lower():
            return {"ok": False, "reason": "encrypted",
                    "error": "That PDF is encrypted and I cannot read it."}
        return {"ok": False, "reason": "unreadable", "error": f"I could not read that PDF: {exc}"}

    if getattr(reader, "is_encrypted", False):
        return {"ok": False, "reason": "encrypted",
                "error": "That PDF is encrypted and I cannot read it."}

    try:
        pages = len(reader.pages)
    except Exception:
        return {"ok": False, "reason": "unreadable",
                "error": "I could not read the pages of that PDF."}

    parts: list[str] = []
    total = 0
    truncated = False
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(text)
            total += len(text) + 2
            if total >= limit:
                truncated = True
                break

    text = "\n\n".join(parts)
    truncated = truncated or len(text) > limit
    return {
        "ok": True,
        "text": _bounded(text, limit),
        "pages": pages,
        "truncated": truncated,
    }