"""Which domain a request is about, from the words in it.

**A calendar request became a file move.**

The model planner was shown every proposable tool — 137 of them, about
17,000 characters of one-per-line definitions — and asked to pick. Given

    "move my Design Review event to tomorrow at 3pm"

it answered:

    {"kind": "action", "tool": "move_file",
     "params": {"source": "my Design Review event",
                "destination": "tomorrow at 3pm"}}

That is a file rename proposed for a calendar request, and the person
would have spent their approval on it. With only the calendar tools
visible, the same request came back as a search for the event — the right
first step.

This module is the cheap, deterministic half of that fix: it reads the
sentence, decides which **domain** it is about, and the planner then shows
the model only that domain's tools.

Three properties matter, and they are why this is a word list rather than
a model call:

1. **It is cheap and it is deterministic.** A classifier that needs a
   round trip to the model is a classifier that fails when the model is
   down — and this runs on the path that decides *what* the model is asked
   to do.

2. **It never guesses past the evidence.** A sentence that names no domain
   gets no narrowing, and the full catalogue is shown. A wrong narrowing
   would hide the right tool; no narrowing only means a big prompt.

3. **It is conservative about which word means what.** "schedule" is a
   calendar word *and* a scheduler word; "book" is a calendar word and a
   reading word. The words are grouped per domain and a sentence is
   narrowed only when exactly one domain's words appear.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

#: The domains, and the words that name one.
#:
#: Each domain is a **tool tag**, so the narrowed catalogue is the tools
#: that carry it. A word appears in more than one domain only when the
#: meaning really is ambiguous, and the ambiguity is resolved by the
#: "exactly one domain matched" rule below.
_DOMAIN_WORDS: Dict[str, Tuple[str, ...]] = {
    "calendar": (
        "calendar", "event", "meeting", "appointment", "invite",
        "invitation", "reschedule", "rebook",
        "attendee", "availability", "free time", "agenda",
    ),
    "email": (
        "email", "e-mail", "mailbox", "inbox", "sender", "recipient",
        "reply", "replying", "forward", "cc", "bcc", "draft",
        "subject line", "unsubscribe",
    ),
    "files": (
        "file", "files", "folder", "folders", "directory", "filename",
        "file name", "rename", "trash", "recycle bin", "zip", "archive",
        "copy", "move it", "path",
    ),
    "coding": (
        "code", "commit", "diff", "git", "repository", "repo", "branch",
        "workspace", "refactor", "function", "bug in the code",
        "compile", "build error", "unit test", "lint",
    ),
    "design": (
        "architecture diagram", "flowchart", "slide", "slides", "deck",
        "mockup", "wireframe", "design asset", "design file", "figma",
    ),
    "browser": (
        "browser", "webpage", "web page", "website", "tab", "url",
        "http", "bookmark", "navigate to",
    ),
    "memory": (
        "memory", "remember", "memories", "forget", "recall", "note to self",
        "knowledge",
    ),
    "research": (
        "paper", "papers", "arxiv", "citation", "cite", "literature",
        "paper dataset", "bibliography",
    ),
    "scheduler": (
        "reminder", "remind", "remind me", "briefing", "recurring",
        "every day", "every morning", "every hour", "cron",
        "scheduled job", "daily routine",
    ),
    "app": (
        "application", "app", "launch", "open the app", "quit the app",
        "running apps", "frontmost",
    ),
    "desktop": (
        "screen", "desktop", "click", "typing", "type into", "hotkey",
        "screenshot",
    ),
    "home": (
        "living room", "kitchen", "bedroom", "light", "thermostat",
        "home device", "smart home",
    ),
    "car": (
        "car", "vehicle", "engine", "tyre", "tire", "dashboard of the car",
        "fuel level", "service interval",
    ),
    "watch": (
        "watch", "wrist", "vibrate", "haptic",
    ),
    "phone": (
        "phone", "mobile", "handset",
    ),
    "location": (
        "location", "where i am", "where am i", "places visited", "gps",
    ),
    "storage": (
        "disk space", "storage", "free space", "disk usage", "how much space",
    ),
    "vision": (
        "image", "screenshot text", "ocr", "picture", "photo",
    ),
    "robotics": (
        "robot", "rover", "motor", "actuator", "telemetry of the robot",
    ),
}

#: The compiled matcher for each domain.
_MATCHERS: Dict[str, re.Pattern] = {
    domain: re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I)
    for domain, words in _DOMAIN_WORDS.items()
}

#: The order in which domains are considered when more than one matches.
#:
#: A sentence that names a calendar event *and* a scheduler word is a
#: calendar request ("schedule a meeting"). A sentence that names an email
#: *and* a scheduler word is an email request ("remind me about the
#: email"). The domain whose own noun the sentence used wins, and the
#: scheduler is the fallback for a scheduler word on its own.
_PRIORITY: Tuple[str, ...] = (
    "calendar",
    "email",
    "files",
    "coding",
    "design",
    "browser",
    "memory",
    "research",
    "desktop",
    "app",
    "home",
    "car",
    "watch",
    "phone",
    "location",
    "storage",
    "vision",
    "robotics",
    "scheduler",
)


def detect_domains(text: str) -> List[str]:
    """The domains the sentence names, in priority order.

    A sentence that names one domain returns that one. A sentence that
    names several returns all of them, because a request that spans
    domains ("email me the calendar for tomorrow") is a real request and
    narrowing it to one domain would hide half the answer.
    """
    if not text:
        return []
    found = {domain for domain, matcher in _MATCHERS.items()
             if matcher.search(text)}
    if not found:
        return []
    return [domain for domain in _PRIORITY if domain in found]


def narrow_domain(text: str) -> Optional[str]:
    """The single domain to narrow to, or ``None`` for the full catalogue.

    ``None`` is returned when the sentence names no domain, or names more
    than one — in both cases showing the full catalogue is safer than
    hiding the right tool behind a wrong narrowing.
    """
    domains = detect_domains(text)
    if len(domains) != 1:
        return None
    return domains[0]
