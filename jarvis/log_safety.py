"""Keep credentials out of the HTTP access log.

uvicorn's access log records the full request line, query string included:

    INFO: 127.0.0.1:63705 - "GET /google/callback?code=4/0Ab...&state=... HTTP/1.1" 200 OK

Measured on this machine with uvicorn's default configuration, a Google OAuth
authorization code appears verbatim in that line. It is short-lived, but it is a
credential: anyone who reads the log while it is still valid can redeem it, and
logs are exactly the artefact this project says must never hold secrets.

Google's redirect is a GET with query parameters and cannot be changed, so the
scrub happens at the logging layer. Every parameter whose *name* looks
credential-bearing -- no matter which route it arrives on -- has its value
replaced. The parameter names are kept, so the log still shows what happened.

The filter is installed on the access logger rather than on one route, because
the next endpoint to take a token in a query string would otherwise reopen the
same hole.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

MASK = "[redacted]"

#: Query-parameter names whose values must never reach a log. Matched
#: case-insensitively as whole names, so `code` matches but `barcode` does not.
SENSITIVE_PARAMETERS = frozenset({
    "code", "state", "token", "access_token", "refresh_token", "id_token",
    "session", "session_token", "api_key", "apikey", "key", "secret",
    "client_secret", "password", "bootstrap", "authorization", "credential",
})

#: The pieces of a logged request target, so only values are replaced.
_PARAM = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_\-]*)=(?P<value>[^&\s\"]*)")


def scrub_target(target: str) -> str:
    """Replace credential-bearing query values in a request target or URL."""
    if not target:
        return target

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name.lower() in SENSITIVE_PARAMETERS:
            return f"{name}={MASK}"
        return match.group(0)

    return _PARAM.sub(replace, target)


class ScrubCredentialsFilter(logging.Filter):
    """Scrub query-string credentials out of access-log records.

    uvicorn builds the message lazily through ``record.args``; both the
    pre-formatted and the argument-tuple forms are handled, because which one
    is in use depends on the uvicorn version and the logger.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            if record.args:
                args = record.args
                if isinstance(args, tuple):
                    record.args = tuple(
                        scrub_target(a) if isinstance(a, str) else a for a in args
                    )
                elif isinstance(args, dict):
                    record.args = {
                        k: (scrub_target(v) if isinstance(v, str) else v)
                        for k, v in args.items()
                    }
            else:
                record.msg = scrub_target(str(record.msg))
        except Exception:
            # A logging filter must never break the request it is describing.
            pass
        return True


def install(logger_names: Optional[Iterable[str]] = None) -> None:
    """Attach the filter to uvicorn's access logger(s).

    Idempotent: calling it twice does not stack duplicate filters, which would
    be harmless but makes the logger's filter list misleading.
    """
    names = list(logger_names or ("uvicorn.access", "uvicorn.error"))
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, ScrubCredentialsFilter) for f in logger.filters):
            logger.addFilter(ScrubCredentialsFilter())