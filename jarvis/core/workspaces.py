"""Saved workspaces: a named set of things a person opens together.

A workspace is a project folder, the applications that should be running, the
files worth having open, and the URLs worth loading. It is deliberately a *set
of references*, not an automation: launching one opens what is named and reports
what was actually observed.

Three rules shape the behaviour:

* **A launch reports per member.** "Workspace launched" would be a lie the first
  time an application fails to start. Every member gets its own result -- opened,
  already running, refused, not found -- and ``verified`` is true only when every
  member that can be observed was observed.
* **Names are unique, case-insensitively.** Two workspaces called "Writing" and
  "writing" would be indistinguishable in a list, and a rename that creates the
  collision is refused rather than silently disambiguated.
* **A project directory must exist to be saved.** A workspace pointing at a path
  that was never there is a launch that is guaranteed to fail later, discovered
  at the least useful moment.

Applications launch through :mod:`jarvis.core.maccontrol`, so the same closed
registry and the same observe-don't-assume rule apply here. Files and URLs are
opened with ``open``, which is the macOS mechanism for handing a reference to
whatever owns it; the result records the command's exit, which is all that can
honestly be claimed for them.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.workspaces")

#: How many members one workspace may hold. A saved workspace is a handful of
#: things a person uses together; hundreds is a data-entry accident or an
#: attempt to make one button do unbounded work.
MAX_MEMBERS = 50

#: The member lists a workspace can hold, and the JSON column for each.
_MEMBER_FIELDS = ("apps", "files", "urls")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _clean_members(values: Any) -> List[str]:
    """Trimmed, non-empty, de-duplicated members in their original order."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError("members must be a list")
    seen = set()
    out: List[str] = []
    for raw in values:
        member = str(raw).strip()
        if not member or member in seen:
            continue
        seen.add(member)
        out.append(member)
    if len(out) > MAX_MEMBERS:
        raise ValueError(f"a workspace may hold at most {MAX_MEMBERS} members")
    return out


class WorkspaceService:
    """Create, read, edit, duplicate, delete and launch saved workspaces."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # -- reads ---------------------------------------------------------------

    def list(self) -> List[Dict[str, Any]]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM workspaces ORDER BY updated_at DESC, name ASC")
            return [self._row(row) for row in cur.fetchall()]

    def get(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM workspaces WHERE workspace_id = ?",
                        (workspace_id,))
            row = cur.fetchone()
        return self._row(row) if row else None

    def _by_name(self, name: str, exclude: str = "") -> Optional[Dict[str, Any]]:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM workspaces WHERE name = ? COLLATE NOCASE",
                        (name,))
            row = cur.fetchone()
        if row is None:
            return None
        found = self._row(row)
        return None if found["workspace_id"] == exclude else found

    # -- writes --------------------------------------------------------------

    def create(self, name: str, *, description: str = "", project_dir: str = "",
               apps: Any = None, files: Any = None, urls: Any = None,
               workspace_id: str = "") -> Dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("a workspace needs a name")
        if self._by_name(clean_name):
            raise ValueError(f"a workspace named {clean_name!r} already exists")

        directory = str(project_dir or "").strip()
        if directory:
            from pathlib import Path

            if not Path(directory).expanduser().is_dir():
                raise ValueError(f"{directory} is not a directory")

        now = _now_iso()
        row = {
            "workspace_id": workspace_id or f"WS-{uuid.uuid4().hex[:12]}",
            "name": clean_name,
            "description": str(description or "").strip(),
            "project_dir": directory,
            "apps": _clean_members(apps),
            "files": _clean_members(files),
            "urls": _clean_members(urls),
            "created_at": now,
            "updated_at": now,
        }
        with self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO workspaces (workspace_id, name, description, project_dir, "
                "apps, files, urls, created_at, updated_at, launch_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (row["workspace_id"], row["name"], row["description"],
                 row["project_dir"], json.dumps(row["apps"]),
                 json.dumps(row["files"]), json.dumps(row["urls"]),
                 row["created_at"], row["updated_at"]))
        result = self.get(row["workspace_id"])
        assert result is not None
        return result

    def update(self, workspace_id: str, **changes: Any) -> Dict[str, Any]:
        existing = self.get(workspace_id)
        if existing is None:
            raise KeyError(workspace_id)

        name = existing["name"]
        if "name" in changes:
            name = str(changes["name"] or "").strip()
            if not name:
                raise ValueError("a workspace needs a name")
            if self._by_name(name, exclude=workspace_id):
                raise ValueError(f"a workspace named {name!r} already exists")

        directory = existing["project_dir"]
        if "project_dir" in changes:
            directory = str(changes["project_dir"] or "").strip()
            if directory:
                from pathlib import Path

                if not Path(directory).expanduser().is_dir():
                    raise ValueError(f"{directory} is not a directory")

        description = existing["description"]
        if "description" in changes:
            description = str(changes["description"] or "").strip()

        members = {}
        for field_name in _MEMBER_FIELDS:
            members[field_name] = (_clean_members(changes[field_name])
                                   if field_name in changes
                                   else existing[field_name])

        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET name = ?, description = ?, project_dir = ?, "
                "apps = ?, files = ?, urls = ?, updated_at = ? "
                "WHERE workspace_id = ?",
                (name, description, directory, json.dumps(members["apps"]),
                 json.dumps(members["files"]), json.dumps(members["urls"]),
                 _now_iso(), workspace_id))
        result = self.get(workspace_id)
        assert result is not None
        return result

    def duplicate(self, workspace_id: str, name: str = "") -> Dict[str, Any]:
        source = self.get(workspace_id)
        if source is None:
            raise KeyError(workspace_id)
        target = str(name or "").strip() or self._free_copy_name(source["name"])
        return self.create(
            target,
            description=source["description"],
            project_dir=source["project_dir"],
            apps=source["apps"], files=source["files"], urls=source["urls"])

    def _free_copy_name(self, base: str) -> str:
        for index in range(1, 1000):
            candidate = f"{base} copy" if index == 1 else f"{base} copy {index}"
            if not self._by_name(candidate):
                return candidate
        raise ValueError("could not find a free name for the copy")

    def delete(self, workspace_id: str) -> bool:
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM workspaces WHERE workspace_id = ?",
                        (workspace_id,))
            return cur.rowcount > 0

    # -- launch --------------------------------------------------------------

    def launch(self, workspace_id: str) -> Dict[str, Any]:
        """Open every member and report what was observed for each one.

        The project directory is opened first, because an application that
        depends on it should find it present. Members open independently: one
        application that fails does not stop the rest, and its failure is in
        its own result rather than hidden behind a summary.
        """
        workspace = self.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)

        results: List[Dict[str, Any]] = []

        if workspace["project_dir"]:
            results.append(self._open_reference("project_dir",
                                                workspace["project_dir"],
                                                workspace["project_dir"]))

        for app_key in workspace["apps"]:
            results.append(self._open_app(app_key))

        for path in workspace["files"]:
            results.append(self._open_reference("file", path, path))

        for url in workspace["urls"]:
            if not (url.startswith("http://") or url.startswith("https://")):
                results.append({
                    "kind": "url", "target": url, "ok": False,
                    "detail": "only http and https URLs can be opened",
                })
                continue
            results.append(self._open_reference("url", url, url))

        # "Verified" is reserved for the case where every member that was
        # attempted reported success. One refusal makes the whole launch
        # incomplete, and that is the honest summary.
        verified = bool(results) and all(r["ok"] for r in results)
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET launch_count = launch_count + 1, "
                "last_launched_at = ? WHERE workspace_id = ?",
                (_now_iso(), workspace_id))
        log.info("workspace launched",
                 extra_fields={"workspace": workspace_id,
                               "verified": verified,
                               "members": len(results)})
        return {
            "workspace_id": workspace_id,
            "name": workspace["name"],
            "results": results,
            "verified": verified,
            "summary": self._summary(workspace["name"], results, verified),
        }

    def _summary(self, name: str, results: List[Dict[str, Any]],
                 verified: bool) -> str:
        if not results:
            return f"{name} has nothing to open."
        if verified:
            return f"{name}: all {len(results)} item(s) opened."
        failed = [r for r in results if not r["ok"]]
        return (f"{name}: {len(results) - len(failed)} of {len(results)} item(s) "
                f"opened; {len(failed)} did not.")

    def _open_app(self, app_key: str) -> Dict[str, Any]:
        from .maccontrol import open_application

        result = open_application(app_key)
        if result.get("observed_running"):
            detail = result.get("detail") or f"{app_key} is running"
        elif result.get("launched"):
            detail = result.get("detail") or (
                "the launch was accepted but the application was not observed running")
        else:
            detail = result.get("detail") or f"{app_key} could not be launched"
        return {"kind": "app", "target": app_key, "ok": bool(result.get("ok")),
                "detail": detail}

    def _open_reference(self, kind: str, target: str,
                        path: str) -> Dict[str, Any]:
        """Hand a path or URL to the system, and report what ``open`` said."""
        import os
        from pathlib import Path

        if kind in ("project_dir", "file"):
            if not Path(path).expanduser().exists():
                return {"kind": kind, "target": target, "ok": False,
                        "detail": f"{path} does not exist"}
        try:
            completed = subprocess.run(["open", os.path.expanduser(path)],
                                       capture_output=True, text=True, timeout=20)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            return {"kind": kind, "target": target, "ok": False,
                    "detail": f"could not open: {exc}"}
        if completed.returncode != 0:
            return {"kind": kind, "target": target, "ok": False,
                    "detail": (completed.stderr or "open reported a failure").strip()[:300]}
        # For a path or URL the honest claim stops at "handed over": unlike an
        # application there is no process name to observe afterwards.
        return {"kind": kind, "target": target, "ok": True,
                "detail": "opened" if kind != "project_dir" else "opened in Finder"}

    # -- row mapping ---------------------------------------------------------

    def _row(self, row: Any) -> Dict[str, Any]:
        def members(column: str) -> List[str]:
            try:
                value = json.loads(row[column] or "[]")
            except (ValueError, TypeError):
                return []
            return [str(item) for item in value] if isinstance(value, list) else []

        return {
            "workspace_id": row["workspace_id"],
            "name": row["name"],
            "description": row["description"],
            "project_dir": row["project_dir"],
            "apps": members("apps"),
            "files": members("files"),
            "urls": members("urls"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "launch_count": int(row["launch_count"] or 0),
            "last_launched_at": row["last_launched_at"],
        }