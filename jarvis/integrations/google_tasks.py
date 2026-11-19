"""Google Tasks: lists, tasks, and the operations the API actually supports.

Google Tasks is deliberately modest: a task has a title, optional notes, a due
*date* (no time), and a completion flag. There is no priority, no estimate and
no start time, so this adapter does not pretend otherwise — a task created here
carries those absences rather than inventing defaults the API will silently
drop.

The return contract matches the calendar adapter: a read that failed is a
failure with a status and a reason, never an empty list. An empty Google task
list and an expired token must not look the same to the caller.

These records are *remote*. Nothing here writes to the local planner database:
Google Tasks is the source of truth for tasks stored there, and mirroring them
locally would create the duplicate reminders this integration is meant to
avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

from ..net import http_read
from ..security.privacy import redact
from .calendar_provider import (
    ProviderResults,
    ProviderStatus,
    classify_http_status,
    classify_transport_error,
    parse_datetime,
)
from .google_auth import GoogleAuth, GoogleAuthError

log = get_logger("google_tasks")

TASKS_API = "https://tasks.googleapis.com/tasks/v1"

@dataclass
class GoogleTask:
    """One Google task, in the shape the rest of Vantage reads."""

    id: str
    title: str
    notes: str = ""
    status: str = "needsAction"      # or "completed"
    due: Optional[datetime] = None
    completed: Optional[datetime] = None
    list_id: str = ""
    parent: str = ""
    #: The thing this task came from, when known (an email, say). Google has no
    #: field for it, so it is carried locally and lands in the notes on create.
    source: str = ""

    @property
    def done(self) -> bool:
        return self.status == "completed"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "notes": self.notes,
            "status": self.status,
            "done": self.done,
            "due": self.due.date().isoformat() if self.due else None,
            "completed": self.completed.isoformat() if self.completed else None,
            "list_id": self.list_id,
            "parent": self.parent,
            "source": self.source,
        }

class GoogleTasksClient:
    """Reads and writes tasks for the connected account."""

    name = "google_tasks"
    #: Total wall-clock ceiling for one Google API exchange.
    TIMEOUT = 20.0

    def __init__(self, settings=None, auth: Optional[GoogleAuth] = None) -> None:
        from ..config import get_settings

        self.settings = settings or get_settings()
        self.auth = auth or GoogleAuth(self.settings)

    def available(self) -> bool:
        return (self.auth.configured
                and self.settings.google_tasks_enabled
                and self.auth.stored_refresh_token() is not None)

    # -- lists -------------------------------------------------------------

    def lists(self) -> Dict[str, Any]:
        """The account's task lists. Returns ``{ok, lists}`` or ``{ok: False, ...}``."""
        try:
            token = self.auth.access_token()
        except GoogleAuthError as exc:
            return self._failure(exc)
        try:
            body = self._get(f"{TASKS_API}/users/@me/lists", token, {"maxResults": "100"})
        except _TasksFailure as failure:
            return self._failure(failure)
        items = [
            {
                "id": str(raw.get("id") or ""),
                "title": str(raw.get("title") or "(untitled list)"),
                "updated": str(raw.get("updated") or ""),
            }
            for raw in (body.get("items") or [])
        ]
        return {"ok": True, "lists": items, "count": len(items)}

    # -- tasks -------------------------------------------------------------

    def tasks(self, list_id: str = "", show_completed: bool = False,
              limit: int = 100) -> ProviderResults:
        """Tasks in one list (defaults to the configured or @default list)."""
        try:
            token = self.auth.access_token()
        except GoogleAuthError as exc:
            return ProviderResults([], self._status_for(exc), detail=str(exc))
        target = list_id or self._list_id()
        # Google caps a page at 100, so a longer list has to be followed. The
        # calendar path already did this; tasks silently returned the first
        # page, which reads as "that is all your tasks" when it is not.
        try:
            raw_items: List[Dict[str, Any]] = []
            page_token = ""
            for _ in range(20):                     # a hard cap on the walk
                params = {
                    "maxResults": str(max(1, min(limit, 100))),
                    "showCompleted": "true" if show_completed else "false",
                }
                if page_token:
                    params["pageToken"] = page_token
                body = self._get(f"{TASKS_API}/lists/{target}/tasks", token, params)
                raw_items.extend(body.get("items") or [])
                page_token = str(body.get("nextPageToken") or "")
                if not page_token or len(raw_items) >= limit:
                    break
        except _TasksFailure as failure:
            return ProviderResults([], failure.status, detail=failure.detail)
        out = [self._to_task(raw, target) for raw in raw_items[:limit]]
        kept = [t for t in out if t is not None]
        if raw_items[:limit] and not kept:
            # Google returned tasks but none carried an id we could use. Letting
            # ProviderResults turn [] into REAL_EMPTY would say "no open tasks"
            # for a response we simply could not read -- the exact confusion the
            # status contract exists to prevent.
            return ProviderResults(
                [], ProviderStatus.PROVIDER_ERROR,
                detail=("Google Tasks answered, but none of the returned tasks could be "
                        "read. This is not an empty list."))
        return ProviderResults(kept, ProviderStatus.HAS_RESULTS if kept else ProviderStatus.REAL_EMPTY)

    def create(self, title: str, notes: str = "", due: Optional[datetime] = None,
               list_id: str = "", source: str = "") -> Dict[str, Any]:
        """Create a task. ``source`` is carried into the notes, since Google has
        no field for provenance."""
        clean_title = (title or "").strip()
        if not clean_title:
            return {"ok": False, "error": "A task needs a title.", "status": "invalid"}
        try:
            token = self.auth.access_token()
        except GoogleAuthError as exc:
            return self._failure(exc)

        target = list_id or self._list_id()
        payload: Dict[str, Any] = {"title": clean_title}
        body_notes = (notes or "").strip()
        if source:
            # Keep provenance visible to the person reading the task in Google,
            # rather than storing it somewhere only Vantage can see.
            body_notes = (body_notes + "\n\n" if body_notes else "") + f"From {source}"
        if body_notes:
            payload["notes"] = body_notes
        if due is not None:
            # Google Tasks stores a due *date*; the API requires midnight UTC.
            payload["due"] = due.astimezone().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
        try:
            raw = self._send("POST", f"{TASKS_API}/lists/{target}/tasks", token, payload)
        except _TasksFailure as failure:
            return {"ok": False, "error": failure.detail, "status": failure.status.value}
        task = self._to_task(raw, target)
        return {"ok": True, "provider": self.name,
                "task": task.as_dict() if task else None}

    def delete(self, task_id: str, list_id: str = "") -> Dict[str, Any]:
        """Delete a task outright.

        Google's API supports this, and a person who mistyped a task should be
        able to remove it rather than leaving a permanent record of the typo.
        It is the one operation here that cannot be undone, so the route above
        this asks for an explicit confirmation and the result is checked by
        reading the list back: a 204 is Google's word, and a missing task is
        the fact.
        """
        if not task_id:
            return {"ok": False, "error": "A task id is required.", "status": "invalid"}
        try:
            token = self.auth.access_token()
        except GoogleAuthError as exc:
            return self._failure(exc)
        target = list_id or self._list_id()
        try:
            self._send("DELETE", f"{TASKS_API}/lists/{target}/tasks/{task_id}",
                       token, None)
        except _TasksFailure as failure:
            # A 404 for DELETE means the task is not there, which is the end
            # state the caller wanted. Reporting it as a failure would invite a
            # retry that can only fail again.
            if "does not exist" in (failure.detail or "") or "404" in (failure.detail or ""):
                return {"ok": True, "provider": self.name, "deleted": True,
                        "already_absent": True}
            return {"ok": False, "error": failure.detail,
                    "status": failure.status.value}
        # Read it back: DELETE answering 204 is not the same as the task being
        # gone, and this project does not report the first as the second.
        remaining = self.tasks(list_id=target, show_completed=True, limit=250)
        if remaining.read_succeeded:
            still_there = any(t.id == task_id for t in remaining)
            if still_there:
                return {"ok": False, "status": "unverified",
                        "error": "Google accepted the delete but the task is still in "
                                 "the list, so I will not claim it was removed."}
        return {"ok": True, "provider": self.name, "deleted": True,
                "verified": bool(remaining.read_succeeded)}

    def complete(self, task_id: str, list_id: str = "") -> Dict[str, Any]:
        """Mark a task completed (``status: "completed"``)."""
        if not task_id:
            return {"ok": False, "error": "A task id is required.", "status": "invalid"}
        try:
            token = self.auth.access_token()
        except GoogleAuthError as exc:
            return self._failure(exc)
        target = list_id or self._list_id()
        try:
            raw = self._send(
                "PATCH", f"{TASKS_API}/lists/{target}/tasks/{task_id}", token,
                {"status": "completed"},
            )
        except _TasksFailure as failure:
            return {"ok": False, "error": failure.detail, "status": failure.status.value}
        task = self._to_task(raw, target)
        # Only claim completion when Google actually reports it. A 200 that did
        # not change the status happened (some API edge cases), and reporting
        # it as done would be the class of lie this project keeps catching.
        if task is None or not task.done:
            return {"ok": False, "status": "unverified",
                    "error": "Google accepted the update but still reports the task as "
                             "not completed, so I will not claim it is done."}
        return {"ok": True, "provider": self.name, "task": task.as_dict()}

    # -- plumbing ----------------------------------------------------------

    def _list_id(self) -> str:
        return (self.settings.google_task_list_id or "@default").strip() or "@default"

    def _get(self, url: str, token: str, params: Dict[str, str]) -> Dict[str, Any]:
        return self._send("GET", url, token, None, params=params)

    def _send(self, method: str, url: str, token: str, payload: Optional[Dict[str, Any]],
              params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """One exchange, bounded by a total deadline (see jarvis/net.py)."""
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {token}"}
        if body:
            headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            status, raw = self._request(request)
        except Exception as exc:
            # A firewall refusal is a policy decision made in this process, not a
            # network fault. Saying "could not reach" sends a reader to debug a
            # connection that is working; name the refusal instead.
            from ..security.egress import EgressRefused

            detail = (f"Google Tasks was blocked before the request left this Mac "
                      f"({exc})" if isinstance(exc, EgressRefused)
                      else f"Could not reach Google Tasks ({type(exc).__name__}).")
            raise _TasksFailure(classify_transport_error(exc), detail) from exc
        return self._read(status, raw)

    def _request(self, request):
        """(status, body bytes). Split out so tests can replace it wholesale.

        An HTTP error is returned, not raised -- see the calendar adapter. A
        401/403/404 from Google is a fact the caller branches on.
        """
        try:
            return 200, http_read(request, total=self.TIMEOUT, what="Google Tasks")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read() or b""
            finally:
                exc.close()

    @staticmethod
    def _read(status: int, raw: bytes) -> Dict[str, Any]:
        if status >= 400:
            raise _TasksFailure(classify_http_status(status),
                                GoogleTasksClient._http_detail(status, raw))
        try:
            return json.loads(raw or b"{}")
        except Exception as exc:
            raise _TasksFailure(ProviderStatus.PROVIDER_ERROR,
                                "Google Tasks returned a response that was not JSON.") from exc

    @staticmethod
    def _http_detail(status_code: int, raw: bytes) -> str:
        if status_code == 401:
            return "Google rejected the access token. Reconnect the account in Settings."
        if status_code == 403:
            # Google distinguishes "this project never enabled the API" from
            # "this account lacks the permission", and they need different
            # fixes: one is a console toggle, the other is a consent problem.
            # Measured 2026-10-28: a fully scoped token still got 403 here
            # because tasks.googleapis.com was disabled in the project, and the
            # generic sentence sent the reader to the wrong place.
            try:
                error = (json.loads(raw or b"{}") or {}).get("error") or {}
                reasons = {
                    str(d.get("reason") or "")
                    for d in (error.get("details") or [])
                    if isinstance(d, dict)
                }
                message = str(error.get("message") or "")
            except Exception:
                reasons, message = set(), ""
            if "SERVICE_DISABLED" in reasons or "has not been used in project" in message:
                return ("Google Tasks is not enabled for this OAuth project. Enable the "
                        "Google Tasks API in the Google Cloud console for that project, "
                        "wait a minute for it to propagate, then refresh. The account's "
                        "permission is fine — this is a project setting.")
            return ("Google refused access to tasks. The account may lack the tasks "
                    "permission, or tasks are disabled for it.")
        if status_code == 404:
            return "That task or list does not exist on this Google account."
        try:
            message = str(((json.loads(raw or b"{}") or {}).get("error") or {}).get("message") or "")
        except Exception:
            message = ""
        # Google's text is untrusted input: an API error can echo the request,
        # credential included, and this detail is logged and returned to a
        # client. Redact before it travels any further.
        message = redact(message[:160])
        return f"Google Tasks returned HTTP {status_code}" + (
            f": {message}" if message else ".")

    @staticmethod
    def _to_task(raw: Dict[str, Any], list_id: str) -> Optional[GoogleTask]:
        task_id = str(raw.get("id") or "")
        if not task_id:
            return None
        due = parse_datetime(str(raw.get("due") or "")) if raw.get("due") else None
        completed = (parse_datetime(str(raw.get("completed") or ""))
                     if raw.get("completed") else None)
        return GoogleTask(
            id=task_id,
            title=str(raw.get("title") or raw.get("notes") or "(untitled task)"),
            notes=str(raw.get("notes") or ""),
            status=str(raw.get("status") or "needsAction"),
            due=due,
            completed=completed,
            list_id=list_id,
            parent=str(raw.get("parent") or ""),
        )

    @staticmethod
    def _status_for(exc: GoogleAuthError) -> ProviderStatus:
        """Map the auth layer's *state* onto a provider status.

        This used to search the message for words. That made the classification
        depend on wording: the DISCONNECTED error ("No Google account is
        connected.") matched none of the keywords and was reported as a
        PROVIDER_ERROR -- "Google failed" -- for a state whose fix is simply
        "connect an account". The state is carried on the exception, so use it.
        Only ERROR is ambiguous (transport vs Google's refusal), and there the
        wording is the only remaining clue.
        """
        from .google_calendar import _provider_status

        state = getattr(exc, "state", None)
        if state is not None:
            mapped = _provider_status(state)
            if mapped is ProviderStatus.PROVIDER_ERROR and "reach" in str(exc).lower():
                return ProviderStatus.NETWORK_ERROR
            return mapped
        # An error object without a state: fall back to the wording.
        detail = str(exc).lower()
        if "not configured" in detail or "jarvis_google_client_id" in detail:
            return ProviderStatus.NOT_CONFIGURED
        if "rejected" in detail or "revoked" in detail or "expire" in detail:
            return ProviderStatus.AUTH_ERROR
        if "reach" in detail or "network" in detail:
            return ProviderStatus.NETWORK_ERROR
        return ProviderStatus.PROVIDER_ERROR

    @staticmethod
    def _failure(exc: Exception) -> Dict[str, Any]:
        status = (GoogleTasksClient._status_for(exc) if isinstance(exc, GoogleAuthError)
                  else ProviderStatus.PROVIDER_ERROR)
        return {"ok": False, "error": str(exc), "status": status.value}

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "available": self.available(),
            "list_id": self._list_id(),
            "connection": self.auth.status().as_dict(),
        }

class _TasksFailure(Exception):
    def __init__(self, status: ProviderStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail