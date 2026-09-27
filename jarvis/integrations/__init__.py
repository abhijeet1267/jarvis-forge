"""External integrations (v0.7).

Email and calendar providers. Each has a real local backend that works with no
account, and a real remote backend that activates when credentials are
configured. Which one is in use is always reported, never implied.
"""

from .calendar_provider import (
    CalDavProvider,
    CalendarEvent,
    CalendarProvider,
    LocalCalendarProvider,
    describe_calendar,
    events_overlap,
    from_ics,
    get_calendar_provider,
    parse_datetime,
    to_ics,
)
from .email_provider import (
    EmailDraft,
    EmailProvider,
    EmailSummary,
    ImapSmtpProvider,
    LocalMailProvider,
    describe_email,
    get_email_provider,
    looks_like_address,
)

__all__ = [
    "CalendarEvent",
    "CalendarProvider",
    "LocalCalendarProvider",
    "CalDavProvider",
    "get_calendar_provider",
    "describe_calendar",
    "events_overlap",
    "parse_datetime",
    "to_ics",
    "from_ics",
    "EmailDraft",
    "EmailProvider",
    "EmailSummary",
    "LocalMailProvider",
    "ImapSmtpProvider",
    "get_email_provider",
    "describe_email",
    "looks_like_address",
]
