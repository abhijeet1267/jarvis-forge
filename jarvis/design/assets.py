"""Design asset store (v1.2).

Generated artifacts live in JARVIS's own design directory — never an arbitrary
path. That keeps the Design Agent from becoming a route to writing files
anywhere: assets are addressed by id, and delete can only remove something this
store created.

Every asset records how it was made, so a diagram is never presented as if it
came from an external tool when it was generated locally.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("design.assets")


@dataclass
class DesignAsset:
    """One generated artifact."""

    asset_id: str
    title: str
    kind: str                 # diagram | ui_concept | documentation | presentation
    format: str               # svg | html | markdown
    filename: str
    created_by: str = "local"  # local | <integration name>
    project_id: Optional[str] = None
    summary: str = ""
    added_at: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "title": self.title,
            "kind": self.kind,
            "format": self.format,
            "filename": self.filename,
            "created_by": self.created_by,
            "project_id": self.project_id,
            "summary": self.summary,
            "added_at": self.added_at.isoformat() if self.added_at else None,
        }


def safe_filename(stem: str, extension: str) -> str:
    """A filesystem-safe name derived from a title."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (stem or "").strip().lower()).strip("-")
    cleaned = cleaned[:48] or "asset"
    suffix = extension.lstrip(".")
    return f"{cleaned}-{new_id('').split('-')[-1][:8]}.{suffix}"


class DesignAssetStore:
    """Persist generated artifacts."""

    def __init__(self, settings=None) -> None:
        from ..config import get_settings

        self.settings = settings or get_settings()
        self.db = get_db()

    @property
    def directory(self) -> Path:
        path = self.settings.home / "design"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- write ---------------------------------------------------------------
    def save(
        self,
        title: str,
        kind: str,
        content: str,
        *,
        format: str = "svg",
        extension: Optional[str] = None,
        created_by: str = "local",
        project_id: Optional[str] = None,
        summary: str = "",
    ) -> DesignAsset:
        """Write an artifact to the design directory and record it."""
        ext = (extension or format or "txt").lstrip(".")
        filename = safe_filename(title, ext)
        path = self.directory / filename

        path.write_text(content, encoding="utf-8")

        asset = DesignAsset(
            asset_id=new_id("ASSET"),
            title=title.strip() or "Untitled",
            kind=kind,
            format=format,
            filename=filename,
            created_by=created_by,
            project_id=project_id,
            summary=summary,
            added_at=utcnow(),
        )

        self.db.execute(
            """
            INSERT INTO design_assets
                (asset_id, title, kind, format, filename, created_by, project_id,
                 summary, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset.asset_id,
                asset.title,
                asset.kind,
                asset.format,
                asset.filename,
                asset.created_by,
                asset.project_id,
                asset.summary,
                asset.added_at.isoformat() if asset.added_at else None,
            ),
        )
        log.info("design asset saved", extra_fields={"id": asset.asset_id, "kind": kind})
        return asset

    # -- read ----------------------------------------------------------------
    @staticmethod
    def _row_to_asset(row: Any) -> DesignAsset:
        added = row["added_at"]
        return DesignAsset(
            asset_id=row["asset_id"],
            title=row["title"],
            kind=row["kind"],
            format=row["format"],
            filename=row["filename"],
            created_by=row["created_by"] or "local",
            project_id=row["project_id"],
            summary=row["summary"] or "",
            added_at=datetime.fromisoformat(added) if added else None,
        )

    def get(self, asset_id: str) -> Optional[DesignAsset]:
        rows = self.db.query("SELECT * FROM design_assets WHERE asset_id = ?", (asset_id,))
        return self._row_to_asset(rows[0]) if rows else None

    def all(self, project_id: Optional[str] = None, kind: Optional[str] = None, limit: int = 100) -> List[DesignAsset]:
        clauses, params = [], []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM design_assets{where} ORDER BY added_at DESC LIMIT ?",
            tuple(params),
        )
        return [self._row_to_asset(r) for r in rows]

    def path_of(self, asset: DesignAsset) -> Optional[Path]:
        """The file for an asset, only if it is actually inside the design dir.

        Guards against a recorded filename that has been tampered with to point
        somewhere else.
        """
        candidate = (self.directory / asset.filename).resolve()
        if self.directory.resolve() not in candidate.parents:
            return None
        return candidate if candidate.is_file() else None

    def read(self, asset_id: str) -> Optional[str]:
        asset = self.get(asset_id)
        if asset is None:
            return None
        path = self.path_of(asset)
        return path.read_text(encoding="utf-8") if path else None

    # -- maintenance ---------------------------------------------------------
    def link_project(self, asset_id: str, project_id: str) -> bool:
        asset = self.get(asset_id)
        if asset is None:
            return False
        self.db.execute(
            "UPDATE design_assets SET project_id = ? WHERE asset_id = ?", (project_id, asset_id)
        )
        return True

    def forget(self, asset_id: str) -> bool:
        """Delete an asset. Only removes files this store wrote."""
        asset = self.get(asset_id)
        if asset is None:
            return False

        path = self.path_of(asset)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

        self.db.execute("DELETE FROM design_assets WHERE asset_id = ?", (asset_id,))
        return True

    def stats(self) -> Dict[str, Any]:
        rows = self.db.query("SELECT kind, COUNT(*) AS n FROM design_assets GROUP BY kind")
        by_kind = {r["kind"]: r["n"] for r in rows}
        return {"assets": sum(by_kind.values()), "by_kind": by_kind, "directory": str(self.directory)}
