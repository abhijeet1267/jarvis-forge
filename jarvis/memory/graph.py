"""Project-state graph (section 5).

Unlike flat chat memory, this links design decisions, code, components,
experiments, failures and fixes so JARVIS can answer questions such as
"Why did my AMR fail last time?".
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..core.models import GraphEdge, GraphNode, new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("memory.graph")

# Node kinds from section 5.
NODE_KINDS = {
    "DESIGN",
    "CODE",
    "COMPONENT",
    "EXPERIMENT",
    "SENSOR_READING",
    "FAILURE",
    "FIX",
    "FILE",
    "TASK",
    "DEVICE",
}


class ProjectGraph:
    """A lightweight property graph backed by two SQLite tables."""

    def __init__(self) -> None:
        self.db = get_db()

    # -- projects ------------------------------------------------------------
    def create_project(self, name: str, description: str = "") -> str:
        project_id = new_id("PROJ")
        self.db.execute(
            "INSERT INTO projects (project_id, name, description) VALUES (?, ?, ?)",
            (project_id, name, description),
        )
        log.info("project created", extra_fields={"project_id": project_id, "name": name})
        return project_id

    def find_project(self, name: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM projects WHERE lower(name) LIKE ? ORDER BY updated_at DESC LIMIT 1",
            (f"%{name.lower()}%",),
        )
        return dict(rows[0]) if rows else None

    def get_or_create_project(self, name: str, description: str = "") -> str:
        existing = self.find_project(name)
        return existing["project_id"] if existing else self.create_project(name, description)

    def list_projects(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM projects ORDER BY updated_at DESC")]

    # -- nodes & edges -------------------------------------------------------
    def add_node(
        self,
        project_id: str,
        kind: str,
        label: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> GraphNode:
        kind = kind.upper()
        if kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind {kind!r}; expected one of {sorted(NODE_KINDS)}")
        node = GraphNode(project_id=project_id, kind=kind, label=label, data=data or {})
        self.db.execute(
            """
            INSERT INTO graph_nodes (node_id, project_id, kind, label, data, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                node.node_id,
                project_id,
                kind,
                label,
                json.dumps(node.data),
                node.created_at.isoformat(),
            ),
        )
        self.db.execute(
            "UPDATE projects SET updated_at = ? WHERE project_id = ?",
            (utcnow().isoformat(), project_id),
        )
        return node

    def link(self, source_id: str, target_id: str, relation: str = "related_to") -> GraphEdge:
        edge = GraphEdge(source_id=source_id, target_id=target_id, relation=relation)
        self.db.execute(
            """
            INSERT INTO graph_edges (edge_id, source_id, target_id, relation, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (edge.edge_id, source_id, target_id, relation, edge.created_at.isoformat()),
        )
        return edge

    def nodes(self, project_id: str, kind: Optional[str] = None) -> List[GraphNode]:
        sql = "SELECT * FROM graph_nodes WHERE project_id = ?"
        params: List[Any] = [project_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind.upper())
        sql += " ORDER BY created_at ASC"
        return [self._row_to_node(r) for r in self.db.query(sql, tuple(params))]

    def neighbors(self, node_id: str) -> List[GraphNode]:
        rows = self.db.query(
            """
            SELECT n.* FROM graph_nodes n
            JOIN graph_edges e ON (e.target_id = n.node_id OR e.source_id = n.node_id)
            WHERE (e.source_id = ? OR e.target_id = ?) AND n.node_id != ?
            """,
            (node_id, node_id, node_id),
        )
        return [self._row_to_node(r) for r in rows]

    def summarize(self, project_id: str) -> Dict[str, Any]:
        """Return a kind-grouped view of the project, used for briefings."""
        out: Dict[str, List[str]] = {k: [] for k in sorted(NODE_KINDS)}
        for node in self.nodes(project_id):
            out[node.kind].append(node.label)
        return {k: v for k, v in out.items() if v}

    def explain_failures(self, project_id: str) -> List[Dict[str, Any]]:
        """Return FAILURE nodes with their linked FIX/EXPERIMENT context."""
        results: List[Dict[str, Any]] = []
        for failure in self.nodes(project_id, "FAILURE"):
            related = self.neighbors(failure.node_id)
            results.append(
                {
                    "failure": failure.label,
                    "detail": failure.data,
                    "related": [{"kind": n.kind, "label": n.label} for n in related],
                }
            )
        return results

    @staticmethod
    def _row_to_node(row: Any) -> GraphNode:
        return GraphNode(
            node_id=row["node_id"],
            project_id=row["project_id"],
            kind=row["kind"],
            label=row["label"],
            data=json.loads(row["data"] or "{}"),
        )
