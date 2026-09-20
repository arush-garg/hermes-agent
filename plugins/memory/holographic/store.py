"""SQLite-backed fact store with entity resolution and trust scoring (single-user Hermes memory plugin)."""

import json
import os
import re
import sqlite3
import threading
from pathlib import Path

from . import holographic as hrr

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_fact_id  INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE RESTRICT,
    target_fact_id  INTEGER NOT NULL REFERENCES facts(fact_id) ON DELETE RESTRICT,
    relation_type   TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'archived')),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    archived_at     TIMESTAMP,
    archive_reason  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_active_unique
    ON edges(source_fact_id, target_fact_id, relation_type)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_edges_outgoing
    ON edges(source_fact_id, relation_type, status, edge_id);
CREATE INDEX IF NOT EXISTS idx_edges_incoming
    ON edges(target_fact_id, relation_type, status, edge_id);
CREATE INDEX IF NOT EXISTS idx_edges_status
    ON edges(status, edge_id);
"""

_HELPFUL_DELTA, _UNHELPFUL_DELTA = 0.05, -0.10

# Entity extraction patterns
_RE_CAPITALIZED  = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")
_RE_AKA          = re.compile(
    r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)',
    re.IGNORECASE,
)
_RE_RELATION_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _clamp_trust(value: float) -> float:
    return max(0.0, min(1.0, value))


class MemoryStore:
    """SQLite-backed fact store with entity resolution and trust scoring.

    Process-wide shared connection registry: SQLite allows one writer at a time and several providers
    coexist per process (main agent + every delegate_task subagent), so all instances for the same database
    share ONE connection and ONE re-entrant lock — writes are fully serialized and "database is locked" is
    impossible. Refcounted: closing one instance never tears the connection out from under a sibling."""

    _shared: dict = {}
    _shared_guard = threading.Lock()

    def __init__(self, db_path: "str | Path | None" = None, default_trust: float = 0.5, hrr_dim: int = 1024) -> None:
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = str(get_hermes_home() / "memory_store.db")
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust, self.hrr_dim, self._hrr_available = _clamp_trust(default_trust), hrr_dim, hrr._HAS_NUMPY
        try:  # resolve() so symlinked/relative paths to the same file share ONE connection
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)
        with MemoryStore._shared_guard:
            entry = MemoryStore._shared.get(self._key)
            if entry is None:
                # Autocommit: a write that raises mid-method can't leave a dangling transaction (and its
                # write lock) open; the explicit commit() calls in _write are then harmless no-ops.
                conn = sqlite3.connect(self._key, check_same_thread=False, timeout=10.0, isolation_level=None)
                conn.row_factory = sqlite3.Row
                entry = {
                    "conn": conn,
                    "lock": threading.RLock(),
                    "refs": 0,
                    "ready": False,
                    "fact_columns": set(),
                }
                MemoryStore._shared[self._key] = entry
            entry["refs"] += 1
            self._entry, self._conn, self._lock = entry, entry["conn"], entry["lock"]
        with self._lock:  # schema initialised once per shared connection
            if not entry["ready"]:
                self._init_db()
                self._entry["ready"] = True
            self._fact_columns = set(self._entry["fact_columns"])

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create schema, enable WAL via the shared fallback helper (NFS/SMB/FUSE degrade gracefully), add hrr_vector to pre-HRR DBs."""
        from hermes_state_wal import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="memory_store.db (holographic)")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        # Migrate: add hrr_vector column if missing (safe for existing databases)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "hrr_vector" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN hrr_vector BLOB")
        self._entry["fact_columns"] = columns | {"hrr_vector"}
        self._conn.commit()

    def _one(self, sql: str, params=()):
        return self._conn.execute(sql, params).fetchone()

    def _write(self, sql: str, params=()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def add_fact(self, content: str, category: str = "general", tags: str = "") -> int:
        """Insert a fact and return its fact_id; on duplicate content (UNIQUE) return the existing fact_id untouched.
        Links extracted entities and rebuilds the category bank."""
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")
            try:
                fact_id: int = self._write("INSERT INTO facts (content, category, tags, trust_score) VALUES (?, ?, ?, ?)",
                                           (content, category, tags, self.default_trust)).lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                return int(self._one("SELECT fact_id FROM facts WHERE content = ?", (content,))["fact_id"])
            self._link_entities(fact_id, content)
            self._compute_hrr_vector(fact_id, content)
            self._rebuild_bank(category)
            return fact_id

    def update_fact(self, fact_id: int, content: str | None = None, trust_delta: float | None = None,
                    tags: str | None = None, category: str | None = None) -> bool:
        """Partially update a fact (trust clamped to [0, 1]). Returns True if the row existed."""
        with self._lock:
            query = query.strip()
            if not query:
                return []

            # FTS5 AND-joins tokens by default, which zeroes out recall on
            # natural-language queries. Reuse the retriever's sanitizer
            # (stopword drop + OR-join content tokens). Imported lazily to
            # avoid a store->retrieval import cycle.
            from plugins.memory.holographic.retrieval import FactRetriever

            match_query = FactRetriever._sanitize_fts_query(query)
            params: list = [match_query, min_trust]
            category_clause = ""
            if category is not None:
                category_clause = "AND f.category = ?"
                params.append(category)
            params.append(limit)

            sql = f"""
                SELECT f.fact_id, f.content, f.category, f.tags,
                       f.trust_score, f.retrieval_count, f.helpful_count,
                       f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON fts.rowid = f.fact_id
                WHERE facts_fts MATCH ?
                  AND f.trust_score >= ?
                  {category_clause}
                ORDER BY fts.rank, f.trust_score DESC
                LIMIT ?
            """

            rows = self._conn.execute(sql, params).fetchall()
            results = [self._row_to_dict(r) for r in rows]

            if results:
                ids = [r["fact_id"] for r in results]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({placeholders})",
                    ids,
                )
                self._conn.commit()

            return results

    def update_fact(
        self,
        fact_id: int,
        content: str | None = None,
        trust_delta: float | None = None,
        tags: str | None = None,
        category: str | None = None,
    ) -> bool:
        """Partially update a fact. Trust is clamped to [0, 1].

        Returns True if the row existed, False otherwise.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score, category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False
            changes = {col: val for col, val in {
                "content": content.strip() if content is not None else None, "tags": tags, "category": category,
                "trust_score": _clamp_trust(row["trust_score"] + trust_delta) if trust_delta is not None else None,
            }.items() if val is not None}
            assignments = ", ".join(["updated_at = CURRENT_TIMESTAMP"] + [f"{col} = ?" for col in changes])
            self._write(f"UPDATE facts SET {assignments} WHERE fact_id = ?", [*changes.values(), fact_id])
            if content is not None:  # re-extract entities and recompute the HRR vector
                self._write("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
                self._link_entities(fact_id, content)
                self._compute_hrr_vector(fact_id, content)
            # Rebuild bank for relevant category
            old_category = row["category"]
            cat = category or old_category
            self._rebuild_bank(cat)
            if category is not None and old_category != category:
                self._rebuild_bank(old_category)

            return True

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact and its entity links. Returns True if the row existed."""
        with self._lock:
            row = self._one("SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                return False
            self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
            self._write("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            self._rebuild_bank(row["category"])
            return True

    def list_facts(self, category: str | None = None, min_trust: float = 0.0, limit: int = 50) -> list[dict]:
        """Browse facts ordered by trust_score descending, optionally filtered by category / min trust."""
        with self._lock:
            category_clause = "AND category = ? " if category is not None else ""
            params = [min_trust] + ([category] if category is not None else []) + [limit]
            sql = ("SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, "
                   f"created_at, updated_at FROM facts WHERE trust_score >= ? {category_clause}"
                   "ORDER BY trust_score DESC LIMIT ?")
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def record_feedback(self, fact_id: int, helpful: bool) -> dict:
        """Adjust trust asymmetrically: helpful -> +0.05 and helpful_count += 1; unhelpful -> -0.10.
        Returns {fact_id, old_trust, new_trust, helpful_count}. Raises KeyError if fact_id is unknown."""
        with self._lock:
            row = self._one("SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?", (fact_id,))
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")
            old_trust: float = row["trust_score"]
            delta = _HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA
            new_trust = _clamp_trust(old_trust + delta)

            helpful_increment = 1 if helpful else 0
            self._conn.execute(
                """
                UPDATE facts
                SET trust_score    = ?,
                    helpful_count  = helpful_count + ?,
                    updated_at     = CURRENT_TIMESTAMP
                WHERE fact_id = ?
                """,
                (new_trust, helpful_increment, fact_id),
            )
            self._conn.commit()

            return {
                "fact_id":      fact_id,
                "old_trust":    old_trust,
                "new_trust":    new_trust,
                "helpful_count": row["helpful_count"] + helpful_increment,
            }

    # ------------------------------------------------------------------
    # Native graph API
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_relation_type(relation_type: str) -> str:
        relation = str(relation_type or "").strip().lower().replace("-", "_")
        if not _RE_RELATION_TYPE.fullmatch(relation):
            raise ValueError(
                "relation_type must be a lower-case slug of 1-64 letters, digits, or underscores"
            )
        return relation

    @classmethod
    def _normalize_relation_types(cls, relation_types) -> list[str]:
        if relation_types is None:
            return []
        values = [relation_types] if isinstance(relation_types, str) else list(relation_types)
        return list(dict.fromkeys(cls._normalize_relation_type(value) for value in values))

    @staticmethod
    def _normalize_direction(direction: str) -> str:
        normalized = str(direction or "out").strip().lower()
        if normalized not in {"out", "in", "both"}:
            raise ValueError("direction must be one of: out, in, both")
        return normalized

    @staticmethod
    def _bounded_int(value, *, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        if isinstance(value, int):
            result = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            result = int(value)
        else:
            raise ValueError(f"{name} must be an integer")
        if not minimum <= result <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return result

    @staticmethod
    def _json_safe_row(row: sqlite3.Row) -> dict:
        return {
            key: value
            for key, value in dict(row).items()
            if value is None or isinstance(value, (str, int, float, bool))
        }

    def _fact_row(self, fact_id: int, *, active_only: bool = True) -> sqlite3.Row | None:
        active_clause = ""
        if active_only and "status" in self._fact_columns:
            active_clause = " AND (status IS NULL OR status = 'active')"
        return self._conn.execute(
            f"SELECT * FROM facts WHERE fact_id = ?{active_clause}",
            (fact_id,),
        ).fetchone()

    def _edge_to_dict(self, row: sqlite3.Row) -> dict:
        edge = self._json_safe_row(row)
        raw_metadata = edge.pop("metadata_json", "{}") or "{}"
        try:
            edge["metadata"] = json.loads(raw_metadata)
        except (TypeError, json.JSONDecodeError):
            edge["metadata"] = {}
        return edge

    def add_edge(
        self,
        source_fact_id: int,
        target_fact_id: int,
        relation_type: str,
        metadata: dict | None = None,
    ) -> dict:
        """Create one directed typed edge without deleting archived history."""
        with self._lock:
            source_fact_id = self._bounded_int(
                source_fact_id, name="source_fact_id", minimum=1, maximum=2**63 - 1
            )
            target_fact_id = self._bounded_int(
                target_fact_id, name="target_fact_id", minimum=1, maximum=2**63 - 1
            )
            relation = self._normalize_relation_type(relation_type)
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            metadata_json = json.dumps(
                metadata or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )

            for field, fact_id in (
                ("source_fact_id", source_fact_id),
                ("target_fact_id", target_fact_id),
            ):
                if self._fact_row(fact_id, active_only=True) is None:
                    raise KeyError(f"{field} {fact_id} not found or not active")

            active = self._conn.execute(
                """
                SELECT * FROM edges
                WHERE source_fact_id = ? AND target_fact_id = ?
                  AND relation_type = ? AND status = 'active'
                ORDER BY edge_id LIMIT 1
                """,
                (source_fact_id, target_fact_id, relation),
            ).fetchone()
            if active is not None:
                result = self._edge_to_dict(active)
                result.update({"created": False, "reactivated": False})
                return result

            archived = self._conn.execute(
                """
                SELECT edge_id FROM edges
                WHERE source_fact_id = ? AND target_fact_id = ?
                  AND relation_type = ? AND status = 'archived'
                ORDER BY edge_id DESC LIMIT 1
                """,
                (source_fact_id, target_fact_id, relation),
            ).fetchone()
            previous_archived_edge_id = (
                int(archived["edge_id"]) if archived is not None else None
            )
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO edges
                        (source_fact_id, target_fact_id, relation_type, metadata_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (source_fact_id, target_fact_id, relation, metadata_json),
                )
                edge_id = int(cur.lastrowid)
                created = True
                reactivated = False
            except sqlite3.IntegrityError:
                # The RLock is process-local. Another MCP process can win
                # the same active-edge insert between our SELECT and
                # INSERT; the partial unique index is the cross-process
                # arbiter. Return its row so add remains idempotent.
                winner = self._conn.execute(
                    """
                    SELECT * FROM edges
                    WHERE source_fact_id = ? AND target_fact_id = ?
                      AND relation_type = ? AND status = 'active'
                    ORDER BY edge_id LIMIT 1
                    """,
                    (source_fact_id, target_fact_id, relation),
                ).fetchone()
                if winner is None:
                    raise
                result = self._edge_to_dict(winner)
                result.update({
                    "created": False,
                    "reactivated": False,
                    "previous_archived_edge_id": previous_archived_edge_id,
                })
                return result

            row = self._conn.execute(
                "SELECT * FROM edges WHERE edge_id = ?", (edge_id,)
            ).fetchone()
            result = self._edge_to_dict(row)
            result.update({
                "created": created,
                "reactivated": reactivated,
                "previous_archived_edge_id": previous_archived_edge_id,
            })
            return result

    def archive_edge(self, edge_id: int, reason: str | None = None) -> dict:
        """Archive an edge in place. The row is never deleted."""
        with self._lock:
            edge_id = self._bounded_int(
                edge_id, name="edge_id", minimum=1, maximum=2**63 - 1
            )
            row = self._conn.execute(
                "SELECT * FROM edges WHERE edge_id = ?", (edge_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"edge_id {edge_id} not found")
            already = row["status"] == "archived"
            if not already:
                clean_reason = str(reason).strip()[:200] if reason else None
                self._conn.execute(
                    """
                    UPDATE edges
                    SET status = 'archived', archived_at = CURRENT_TIMESTAMP,
                        archive_reason = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE edge_id = ?
                    """,
                    (clean_reason, edge_id),
                )
                row = self._conn.execute(
                    "SELECT * FROM edges WHERE edge_id = ?", (edge_id,)
                ).fetchone()
            result = self._edge_to_dict(row)
            result.update({"archived": True, "already": already, "deleted": False})
            return result

    def _edge_rows_for_nodes(
        self,
        fact_ids: list[int],
        *,
        relation_types=None,
        direction: str = "out",
        active_only: bool = True,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        if not fact_ids:
            return []
        direction = self._normalize_direction(direction)
        relations = self._normalize_relation_types(relation_types)
        placeholders = ",".join("?" for _ in fact_ids)
        params: list = []
        if direction == "out":
            endpoint_clause = f"source_fact_id IN ({placeholders})"
            params.extend(fact_ids)
        elif direction == "in":
            endpoint_clause = f"target_fact_id IN ({placeholders})"
            params.extend(fact_ids)
        else:
            endpoint_clause = (
                f"(source_fact_id IN ({placeholders}) OR "
                f"target_fact_id IN ({placeholders}))"
            )
            params.extend(fact_ids)
            params.extend(fact_ids)
        clauses = [endpoint_clause]
        if active_only:
            clauses.append("status = 'active'")
        if relations:
            relation_placeholders = ",".join("?" for _ in relations)
            clauses.append(f"relation_type IN ({relation_placeholders})")
            params.extend(relations)
        sql = "SELECT * FROM edges WHERE " + " AND ".join(clauses) + " ORDER BY edge_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self._conn.execute(sql, params).fetchall()

    def neighbors(
        self,
        fact_id: int,
        relation_types=None,
        direction: str = "out",
        active_only: bool = True,
        limit: int = 200,
    ) -> dict:
        """Return directed neighboring facts and their stored edges."""
        with self._lock:
            fact_id = self._bounded_int(
                fact_id, name="fact_id", minimum=1, maximum=2**63 - 1
            )
            direction = self._normalize_direction(direction)
            limit = self._bounded_int(limit, name="limit", minimum=1, maximum=1000)
            if self._fact_row(fact_id, active_only=active_only) is None:
                raise KeyError(f"fact_id {fact_id} not found or not active")
            rows = self._edge_rows_for_nodes(
                [fact_id], relation_types=relation_types, direction=direction,
                active_only=active_only, limit=limit,
            )
            items = []
            for row in rows:
                source = int(row["source_fact_id"])
                target = int(row["target_fact_id"])
                if direction == "out" or (direction == "both" and source == fact_id):
                    neighbor_id = target
                    edge_direction = "out"
                else:
                    neighbor_id = source
                    edge_direction = "in"
                neighbor = self._fact_row(neighbor_id, active_only=active_only)
                if neighbor is None:
                    continue
                items.append({
                    "direction": edge_direction,
                    "edge": self._edge_to_dict(row),
                    "fact": self._json_safe_row(neighbor),
                })
            return {
                "fact_id": fact_id,
                "direction": direction,
                "count": len(items),
                "neighbors": items,
            }

    def traverse(
        self,
        start_fact_id: int,
        relation_types=None,
        direction: str = "out",
        max_depth: int = 3,
        max_nodes: int = 200,
        active_only: bool = True,
    ) -> dict:
        """Breadth-first, cycle-safe traversal with hard depth and node bounds."""
        with self._lock:
            start_fact_id = self._bounded_int(
                start_fact_id, name="start_fact_id", minimum=1, maximum=2**63 - 1
            )
            direction = self._normalize_direction(direction)
            max_depth = self._bounded_int(
                max_depth, name="max_depth", minimum=0, maximum=10
            )
            max_nodes = self._bounded_int(
                max_nodes, name="max_nodes", minimum=1, maximum=1000
            )
            start = self._fact_row(start_fact_id, active_only=active_only)
            if start is None:
                raise KeyError(f"start_fact_id {start_fact_id} not found or not active")

            visited = {start_fact_id}
            depth_by_id = {start_fact_id: 0}
            nodes = [{"depth": 0, "fact": self._json_safe_row(start)}]
            edge_by_id: dict[int, dict] = {}
            frontier = [start_fact_id]
            truncated = False

            for depth in range(max_depth):
                if not frontier:
                    break
                rows = self._edge_rows_for_nodes(
                    frontier, relation_types=relation_types, direction=direction,
                    active_only=active_only,
                )
                next_frontier: list[int] = []
                frontier_set = set(frontier)
                for row in rows:
                    source = int(row["source_fact_id"])
                    target = int(row["target_fact_id"])
                    candidates: list[int] = []
                    if direction in {"out", "both"} and source in frontier_set:
                        candidates.append(target)
                    if direction in {"in", "both"} and target in frontier_set:
                        candidates.append(source)
                    accepted_edge = False
                    for neighbor_id in dict.fromkeys(candidates):
                        neighbor = self._fact_row(neighbor_id, active_only=active_only)
                        if neighbor is None:
                            continue
                        if neighbor_id not in visited:
                            if len(visited) >= max_nodes:
                                truncated = True
                                continue
                            visited.add(neighbor_id)
                            depth_by_id[neighbor_id] = depth + 1
                            next_frontier.append(neighbor_id)
                            nodes.append({
                                "depth": depth + 1,
                                "fact": self._json_safe_row(neighbor),
                            })
                        accepted_edge = True
                    if accepted_edge:
                        edge_by_id[int(row["edge_id"])] = self._edge_to_dict(row)
                frontier = next_frontier
                if truncated:
                    break

            return {
                "start_fact_id": start_fact_id,
                "direction": direction,
                "max_depth": max_depth,
                "max_nodes": max_nodes,
                "truncated": truncated,
                "node_count": len(nodes),
                "edge_count": len(edge_by_id),
                "nodes": nodes,
                "edges": [edge_by_id[key] for key in sorted(edge_by_id)],
                "depth_by_fact_id": depth_by_id,
            }

    def list_subgraph(
        self,
        category: str,
        relation_types=None,
        active_only: bool = True,
        include_isolated: bool = True,
        limit_nodes: int = 1000,
    ) -> dict:
        """Return the category-induced subgraph, including isolated nodes by default."""
        with self._lock:
            category = str(category or "").strip()
            if not category:
                raise ValueError("category must be non-empty")
            if len(category) > 80:
                raise ValueError("category must be at most 80 characters")
            limit_nodes = self._bounded_int(
                limit_nodes, name="limit_nodes", minimum=1, maximum=5000
            )
            active_clause = ""
            if active_only and "status" in self._fact_columns:
                active_clause = " AND (status IS NULL OR status = 'active')"
            node_rows = self._conn.execute(
                f"SELECT * FROM facts WHERE category = ?{active_clause} ORDER BY fact_id LIMIT ?",
                (category, limit_nodes + 1),
            ).fetchall()
            truncated = len(node_rows) > limit_nodes
            node_rows = node_rows[:limit_nodes]
            fact_ids = [int(row["fact_id"]) for row in node_rows]
            edge_rows: list[sqlite3.Row] = []
            if fact_ids:
                placeholders = ",".join("?" for _ in fact_ids)
                clauses = [
                    f"source_fact_id IN ({placeholders})",
                    f"target_fact_id IN ({placeholders})",
                ]
                params: list = fact_ids + fact_ids
                if active_only:
                    clauses.append("status = 'active'")
                relations = self._normalize_relation_types(relation_types)
                if relations:
                    relation_placeholders = ",".join("?" for _ in relations)
                    clauses.append(f"relation_type IN ({relation_placeholders})")
                    params.extend(relations)
                edge_rows = self._conn.execute(
                    "SELECT * FROM edges WHERE " + " AND ".join(clauses) + " ORDER BY edge_id",
                    params,
                ).fetchall()
            if not include_isolated:
                connected = {
                    int(value)
                    for row in edge_rows
                    for value in (row["source_fact_id"], row["target_fact_id"])
                }
                node_rows = [row for row in node_rows if int(row["fact_id"]) in connected]
            return {
                "category": category,
                "node_count": len(node_rows),
                "edge_count": len(edge_rows),
                "truncated": truncated,
                "nodes": [self._json_safe_row(row) for row in node_rows],
                "edges": [self._edge_to_dict(row) for row in edge_rows],
            }

    # ------------------------------------------------------------------
    # Entity helpers
    # ------------------------------------------------------------------

    def _extract_entities(self, text: str) -> list[str]:
        """Regex entity candidates (see the pattern table), deduplicated case-insensitively in first-seen order."""
        raw = [m.group(1) for pattern in _RE_SINGLE_ENTITY for m in pattern.finditer(text)]
        for m in _RE_AKA.finditer(text):
            raw += [m.group(1), m.group(2)]
        uniq: dict[str, str] = {}  # lower-cased key -> first-seen spelling, insertion-ordered
        for name in filter(None, (n.strip() for n in raw)):
            uniq.setdefault(name.lower(), name)
        return list(uniq.values())

    def _link_entities(self, fact_id: int, content: str) -> None:
        """Extract entities from content, resolve/create them, and link each to the fact."""
        for name in self._extract_entities(content):
            self._write("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
                        (fact_id, self._resolve_entity(name)))

    def _resolve_entity(self, name: str) -> int:
        """Return the entity_id for a case-insensitive name or alias match, creating the entity if absent."""
        for sql in _ENTITY_LOOKUPS:
            row = self._one(sql, (name,))
            if row is not None:
                return int(row["entity_id"])
        return int(self._write("INSERT INTO entities (name) VALUES (?)", (name,)).lastrowid)  # type: ignore[arg-type]

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and store the HRR vector for a fact (linked entities as roles). No-op without numpy."""
        if not self._hrr_available:
            return
        entities = [row["name"] for row in self._conn.execute(_ENTITY_NAMES_SQL, (fact_id,)).fetchall()]
        blob = hrr.phases_to_bytes(hrr.encode_fact(content, entities, self.hrr_dim))
        self._write("UPDATE facts SET hrr_vector = ? WHERE fact_id = ?", (blob, fact_id))

    def _rebuild_bank(self, category: str) -> None:
        """Full rebuild of a category's memory bank from all its fact vectors."""
        if not self._hrr_available:
            return
        bank_name = f"cat:{category}"
        rows = self._conn.execute("SELECT hrr_vector FROM facts WHERE category = ? AND hrr_vector IS NOT NULL", (category,)).fetchall()
        if not rows:
            self._write("DELETE FROM memory_banks WHERE bank_name = ?", (bank_name,))
            return
        bank_vector = hrr.bundle(*[hrr.bytes_to_phases(row["hrr_vector"], dim=self.hrr_dim) for row in rows])
        hrr.snr_estimate(self.hrr_dim, len(rows))  # warns when near capacity
        self._write("INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(bank_name) DO UPDATE SET "
                    "vector = excluded.vector, dim = excluded.dim, fact_count = excluded.fact_count, "
                    "updated_at = excluded.updated_at", (bank_name, hrr.phases_to_bytes(bank_vector), self.hrr_dim, len(rows)))

    @classmethod
    def release_all_under(cls, directory: "str | Path") -> int:
        """Force-close every shared connection whose database lives under ``directory``; returns the count.
        close() is refcount-driven, so a live holder (e.g. an agent's provider) keeps a profile's SQLite handle
        open, which on Windows makes rmtree of the profile fail. The directory is going away, so later use by a
        stale holder is expected to fail.

        That is exactly what a profile delete must break on Windows: the desktop's main ``serve`` process
        opens ``memory_store.db`` for every known profile, and ``rmtree`` of the profile directory fails
        with ``WinError 32`` while any of those handles is open (#88347). In a process that holds none (e.g.
        the CLI deleting from outside serve) this is a harmless no-op returning 0.
        """
        root = os.path.normcase(str(Path(directory).expanduser().resolve())) + os.sep
        with cls._shared_guard:
            doomed = [cls._shared.pop(key) for key in list(cls._shared) if os.path.normcase(key).startswith(root)]
            for entry in doomed:
                try:
                    with entry["lock"]:
                        entry["conn"].close()
                except Exception:
                    pass  # an already-closed/broken connection must not abort releasing siblings
        return len(doomed)

    def close(self) -> None:
        """Release this instance's reference; the connection closes with the last holder. Idempotent."""
        with MemoryStore._shared_guard:
            entry = getattr(self, "_entry", None)
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    # Pop only OUR entry: after release_all_under() a same-path store may have
                    # registered a FRESH entry under this key; a stale late close() must not evict it.
                    # See #88347.
                    if MemoryStore._shared.get(self._key) is entry:
                        MemoryStore._shared.pop(self._key, None)
            self._entry = None

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
