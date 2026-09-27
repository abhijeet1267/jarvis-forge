"""Design analysis (v1.2).

Reviews a generated or imported asset against a small set of practical checks.
These are **measurable properties**, not aesthetic judgements — JARVIS reports
what it can actually inspect (structure, accessibility hooks, size) and does not
pretend to judge whether a design is good.

Each finding is ``(severity, check, detail)`` where severity is ``ok``,
``warn`` or ``info``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple
from xml.sax.saxutils import escape as _unused  # noqa: F401  (kept for symmetry)


def _finding(severity: str, check: str, detail: str) -> Dict[str, str]:
    return {"severity": severity, "check": check, "detail": detail}


def analyze_svg(content: str, title: str) -> List[Dict[str, str]]:
    """Checks for an SVG asset."""
    findings: List[Dict[str, str]] = []

    findings.append(_finding("ok", "format", f"SVG, {len(content)} bytes"))

    has_title = "<title" in content
    findings.append(
        _finding(
            "ok" if has_title else "warn",
            "accessible title",
            "has a <title> element" if has_title else "missing <title> — screen readers get nothing",
        )
    )

    has_desc = "<desc" in content
    findings.append(
        _finding(
            "ok" if has_desc else "info",
            "description",
            "has a <desc> element" if has_desc else "no <desc> — consider adding a summary",
        )
    )

    if 'xmlns="http://www.w3.org/2000/svg"' not in content:
        findings.append(_finding("warn", "namespace", "missing the SVG xmlns declaration"))
    else:
        findings.append(_finding("ok", "namespace", "declares the SVG namespace"))

    labels = len(re.findall(r"<text", content))
    findings.append(_finding("info", "labels", f"{labels} text label(s)"))

    # Very long labels usually overflow their box.
    long_labels = [
        match for match in re.findall(r">([^<>]{40,})<", content) if len(match) > 40
    ]
    if long_labels:
        findings.append(
            _finding("warn", "label length", f"{len(long_labels)} label(s) over 40 characters may overflow")
        )

    return findings


def analyze_html(content: str, title: str) -> List[Dict[str, str]]:
    """Checks for an HTML asset (UI concept or deck)."""
    findings: List[Dict[str, str]] = []
    findings.append(_finding("ok", "format", f"HTML, {len(content)} bytes"))

    has_lang = re.search(r"<html[^>]*\blang=", content) is not None
    findings.append(
        _finding(
            "ok" if has_lang else "warn",
            "language",
            "declares lang" if has_lang else "no lang attribute on <html>",
        )
    )

    has_viewport = "viewport" in content
    findings.append(
        _finding(
            "ok" if has_viewport else "warn",
            "responsive",
            "has a viewport meta tag" if has_viewport else "no viewport meta — will not scale on mobile",
        )
    )

    h1_count = len(re.findall(r"<h1", content))
    findings.append(
        _finding(
            "ok" if h1_count == 1 else "info",
            "heading structure",
            f"{h1_count} <h1> element(s)",
        )
    )

    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', content)
    findings.append(
        _finding(
            "ok" if not external else "warn",
            "self-contained",
            "no external resources" if not external else f"{len(external)} external reference(s) — the file will not work offline",
        )
    )

    images_without_alt = len(re.findall(r"<img(?![^>]*\balt=)", content))
    if images_without_alt:
        findings.append(_finding("warn", "image alt text", f"{images_without_alt} <img> without alt"))

    return findings


def analyze_markdown(content: str, title: str) -> List[Dict[str, str]]:
    """Checks for a Markdown document."""
    findings: List[Dict[str, str]] = []
    findings.append(_finding("ok", "format", f"Markdown, {len(content)} bytes"))

    headings = re.findall(r"^(#{1,6})\s+(.*)$", content, flags=re.M)
    findings.append(_finding("ok", "headings", f"{len(headings)} heading(s)"))

    placeholders = len(re.findall(r"_To be written\._", content))
    if placeholders:
        findings.append(
            _finding("info", "completeness", f"{placeholders} section(s) still need content")
        )

    words = len(content.split())
    findings.append(_finding("info", "length", f"about {words} words"))

    return findings


ANALYZERS = {
    "svg": analyze_svg,
    "html": analyze_html,
    "markdown": analyze_markdown,
}


def analyze(content: str, format: str, title: str = "") -> Dict[str, Any]:
    """Analyze an asset by format."""
    analyzer = ANALYZERS.get((format or "").lower())
    if analyzer is None:
        return {
            "ok": False,
            "error": f"I do not know how to analyze {format!r} assets yet.",
        }

    findings = analyzer(content, title)
    warnings = sum(1 for f in findings if f["severity"] == "warn")

    return {
        "ok": True,
        "title": title,
        "format": format,
        "findings": findings,
        "warnings": warnings,
        "summary": (
            f"{len(findings)} check(s), {warnings} warning(s)."
        ),
    }
