"""Persistent, transactional Markdown knowledge graph service.

Markdown notes are authoritative.  ``graph-index.sqlite`` is a disposable query
index rebuilt from those notes, while ``graph-state.json`` supplies optimistic
concurrency, human-edit detection, and crash recovery for multi-note patches.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unicodedata
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

from pydantic import ValidationError

from ..workspace import (
    atomic_write_json,
    atomic_write_text,
    ensure_path_confined,
    sha256_file,
    sha256_text,
)
from .markdown import (
    GENERATED_END,
    GENERATED_START,
    GraphMarkdownError,
    exact_statement,
    generated_section,
    machine_hash,
    new_generated_body,
    parse_node_note,
    render_node_note,
    replace_generated_section,
    statement_hash,
    wikilink_for,
)
from .models import (
    NODE_ID_PREFIXES,
    NODE_TYPE_DIRECTORIES,
    ClaimType,
    EpistemicStatus,
    GraphChangeRecord,
    GraphContextNode,
    GraphContextSlice,
    GraphDiff,
    GraphEdge,
    GraphFrontier,
    GraphMergeResult,
    GraphNode,
    GraphNodeSummary,
    GraphPatch,
    GraphState,
    GraphStatus,
    GraphValidationIssue,
    GraphValidationReport,
    NodeType,
    RelationType,
    WorkflowStatus,
)

GRAPH_SCHEMA_VERSION = 1
GRAPH_VAULT_RELATIVE = Path(".ascend") / "knowledge"
GRAPH_DIRECTORIES = (
    "Problems",
    "Definitions",
    "Claims",
    "Proofs",
    "Approaches",
    "Counterexamples",
    "Experiments",
    "Sources",
    "Tasks",
    "Audits",
    "Formalizations",
    "Runs",
    "Artifacts",
    "Human Notes",
    "Dashboards",
)

_REVISION = re.compile(r"\A\d{8}-[0-9a-f]{16}\Z")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


class KnowledgeGraphError(RuntimeError):
    """Base error for graph integrity or persistence failures."""


class GraphNotInitializedError(KnowledgeGraphError):
    pass


class GraphConflictError(KnowledgeGraphError):
    pass


class GraphValidationError(KnowledgeGraphError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _deterministic_id(node_type: NodeType, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest().upper()[:20]
    return f"{NODE_ID_PREFIXES[node_type]}-{digest}"


def _new_id(node_type: NodeType) -> str:
    return f"{NODE_ID_PREFIXES[node_type]}-{secrets.token_hex(10).upper()}"


def _slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", errors="ignore")
        .decode("ascii")
        .casefold()
    )
    return _SLUG_UNSAFE.sub("-", ascii_value).strip("-")[:56] or "note"


def _revision(number: int, node_hashes: Mapping[str, str]) -> str:
    digest = hashlib.sha256(_canonical_json(dict(sorted(node_hashes.items()))).encode()).hexdigest()
    return f"{number:08d}-{digest[:16]}"


def _node_summary(node: GraphNode) -> GraphNodeSummary:
    return GraphNodeSummary(
        ascend_id=node.ascend_id,
        node_type=node.node_type,
        title=node.title,
        epistemic_status=node.epistemic_status,
        workflow_status=node.workflow_status,
        path=node.path or "",
        statement_version=node.statement_version,
        invalidation_reasons=node.invalidation_reasons,
    )


def _unique_edges(edges: Iterable[GraphEdge]) -> list[GraphEdge]:
    result: list[GraphEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        key = (edge.source_id, edge.relation.value, edge.target_id)
        if key not in seen:
            seen.add(key)
            result.append(edge)
    return sorted(result, key=lambda item: (item.source_id, item.relation.value, item.target_id))


class KnowledgeGraph:
    """One project-scoped graph supporting multiple stable problem nodes.

    ASCEND's existing security contract permits automatic writes only beneath
    ``.ascend/``.  The Obsidian vault therefore lives at ``.ascend/knowledge``;
    opening that directory in Obsidian behaves like any other Markdown vault while
    keeping ordinary workflow runs out of the user's source tree.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        maximum_context_nodes: int = 48,
        maximum_context_characters: int = 60_000,
    ) -> None:
        root = project_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise KnowledgeGraphError(f"project root is not a directory: {project_root}")
        self.project_root = root
        self.ascend_root = ensure_path_confined(root, root / ".ascend")
        self.vault_root = ensure_path_confined(root, root / GRAPH_VAULT_RELATIVE)
        self.state_path = ensure_path_confined(root, self.ascend_root / "graph-state.json")
        self.schema_path = ensure_path_confined(root, self.ascend_root / "graph-schema.json")
        self.index_path = ensure_path_confined(root, self.ascend_root / "graph-index.sqlite")
        self.pending_path = ensure_path_confined(root, self.ascend_root / "graph-pending.json")
        self.snapshots_root = ensure_path_confined(root, self.ascend_root / "snapshots")
        self.locks_root = ensure_path_confined(root, self.ascend_root / "locks")
        self.lock_path = ensure_path_confined(root, self.locks_root / "graph.lock")
        self._clock = clock or _utc_now
        self.maximum_context_nodes = maximum_context_nodes
        self.maximum_context_characters = maximum_context_characters
        if maximum_context_nodes < 4 or maximum_context_characters < 1_000:
            raise ValueError("graph context limits are too small for a useful bounded slice")

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise KnowledgeGraphError("graph clock must return a datetime")
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.ascend_root.mkdir(mode=0o700, exist_ok=True)
        self.locks_root.mkdir(mode=0o700, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise KnowledgeGraphError(f"graph lock is not a regular file: {self.lock_path}")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @property
    def initialized(self) -> bool:
        return self.state_path.is_file() and self.vault_root.is_dir()

    def _ensure_layout(self) -> None:
        self.ascend_root.mkdir(mode=0o700, exist_ok=True)
        self.vault_root.mkdir(mode=0o700, exist_ok=True)
        self.snapshots_root.mkdir(mode=0o700, exist_ok=True)
        self.locks_root.mkdir(mode=0o700, exist_ok=True)
        for relative in GRAPH_DIRECTORIES:
            ensure_path_confined(self.vault_root, self.vault_root / relative).mkdir(
                parents=True, exist_ok=True
            )
        obsidian = ensure_path_confined(self.vault_root, self.vault_root / ".obsidian")
        obsidian.mkdir(mode=0o700, exist_ok=True)
        app_config = ensure_path_confined(self.vault_root, obsidian / "app.json")
        if not app_config.exists():
            atomic_write_json(app_config, {}, confinement_root=self.vault_root)

    def _write_schema(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "schema_version": GRAPH_SCHEMA_VERSION,
            "description": "ASCEND Markdown knowledge graph node and patch schemas",
            "node": GraphNode.model_json_schema(),
            "patch": GraphPatch.model_json_schema(),
            "relation_types": [item.value for item in RelationType],
            "node_types": [item.value for item in NodeType],
        }
        atomic_write_json(self.schema_path, schema, confinement_root=self.ascend_root)

    def initialize(self) -> GraphState:
        """Create an empty portable vault and derived index idempotently."""

        with self._locked():
            self._ensure_layout()
            self._write_schema()
            if self.state_path.is_file():
                self._recover_pending_unlocked()
                state = self._load_state_unlocked()
            else:
                now = self._now()
                empty_revision = _revision(0, {})
                state = GraphState(
                    revision=empty_revision,
                    created_at=now,
                    updated_at=now,
                )
                atomic_write_json(self.state_path, state, confinement_root=self.ascend_root)
                self._write_snapshot_unlocked(state, [])
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            self._write_navigation_unlocked(state, nodes)
            self._rebuild_index_unlocked(state, nodes)
            return state

    def _load_state_unlocked(self) -> GraphState:
        if not self.state_path.is_file():
            raise GraphNotInitializedError(
                "knowledge graph is not initialized; run 'ascend graph init' in "
                f"{self.project_root}"
            )
        try:
            state = GraphState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError) as exc:
            raise GraphValidationError(f"graph state is invalid: {exc}") from exc
        if not _REVISION.fullmatch(state.revision):
            raise GraphValidationError("graph state contains an invalid revision identifier")
        return state

    def load_state(self) -> GraphState:
        with self._locked():
            self._recover_pending_unlocked()
            return self._load_state_unlocked()

    def _recover_pending_unlocked(self) -> None:
        if not self.pending_path.is_file():
            return
        try:
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            writes = pending["writes"]
            raw_state = pending["state_after"]
            if not isinstance(writes, list) or not isinstance(raw_state, dict):
                raise TypeError("transaction fields have invalid types")
            state_after = GraphState.model_validate(raw_state)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValidationError,
        ) as exc:
            raise GraphValidationError(f"pending graph transaction is invalid: {exc}") from exc
        for write in writes:
            if not isinstance(write, dict):
                raise GraphValidationError("pending graph transaction contains an invalid write")
            relative = write.get("path")
            contents = write.get("contents")
            digest = write.get("sha256")
            if not all(isinstance(item, str) for item in (relative, contents, digest)):
                raise GraphValidationError("pending graph transaction write is incomplete")
            target = ensure_path_confined(self.vault_root, self.vault_root / cast(str, relative))
            if sha256_text(cast(str, contents)) != digest:
                raise GraphValidationError("pending graph transaction content hash is invalid")
            if not target.is_file() or sha256_file(target) != digest:
                atomic_write_text(
                    target, cast(str, contents), confinement_root=self.vault_root, mode=0o600
                )
        atomic_write_json(self.state_path, state_after, confinement_root=self.ascend_root)
        nodes = self._load_nodes_unlocked(include_human_notes=True)
        self._write_snapshot_unlocked(state_after, nodes)
        self._write_navigation_unlocked(state_after, nodes)
        self._rebuild_index_unlocked(state_after, nodes)
        self.pending_path.unlink(missing_ok=True)

    def _load_nodes_unlocked(self, *, include_human_notes: bool) -> list[GraphNode]:
        if not self.vault_root.is_dir():
            raise GraphNotInitializedError("knowledge graph vault is missing")
        nodes: list[GraphNode] = []
        seen: set[str] = set()
        problem_ids: list[str] = []
        candidates = sorted(self.vault_root.rglob("*.md"))
        for path in candidates:
            relative = path.relative_to(self.vault_root).as_posix()
            if relative == "Home.md" or relative.startswith("Dashboards/"):
                continue
            try:
                prefix = path.read_text(encoding="utf-8")[:2048]
            except (OSError, UnicodeError) as exc:
                raise GraphMarkdownError(f"cannot read graph note {path}: {exc}") from exc
            if "ascend_id:" not in prefix:
                if include_human_notes:
                    node_id = _deterministic_id(NodeType.HUMAN_NOTE, relative)
                    stat_result = path.stat()
                    timestamp = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
                    problem_id = problem_ids[0] if problem_ids else node_id
                    text = path.read_text(encoding="utf-8")
                    nodes.append(
                        GraphNode(
                            ascend_id=node_id,
                            node_type=NodeType.HUMAN_NOTE,
                            problem_id=problem_id,
                            title=path.stem,
                            created_in_run="HUMAN",
                            last_modified_run="HUMAN",
                            author_role="human",
                            created_at=timestamp,
                            updated_at=timestamp,
                            body=text,
                            tags=["ascend/human-note"],
                            path=relative,
                            content_hash=sha256_file(path),
                        )
                    )
                continue
            node = parse_node_note(path, relative_path=relative)
            if node.ascend_id in seen:
                raise GraphValidationError(f"duplicate graph node ID: {node.ascend_id}")
            seen.add(node.ascend_id)
            if node.node_type is NodeType.PROBLEM:
                problem_ids.append(node.ascend_id)
            nodes.append(node)
        return nodes

    def load_nodes(self, *, include_human_notes: bool = True) -> list[GraphNode]:
        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            return self._load_nodes_unlocked(include_human_notes=include_human_notes)

    def _node_path(self, node: GraphNode, state: GraphState) -> str:
        # A parsed node path is authoritative for an allowed human rename. Stable
        # identity comes from frontmatter, never from the filename.
        existing = node.path or state.node_paths.get(node.ascend_id)
        if existing:
            return existing
        directory = NODE_TYPE_DIRECTORIES[node.node_type]
        return f"{directory}/{node.ascend_id}--{_slug(node.title)}.md"

    def _commit_nodes_unlocked(
        self,
        *,
        state: GraphState,
        all_nodes: Sequence[GraphNode],
        changed_node_ids: Sequence[str],
        run_id: str,
        author: str,
        reason: str,
        operation_id: str,
        source_artifacts: Sequence[str] = (),
        result_status: Literal["merged", "partially_merged"] = "merged",
        stale_node_ids: Sequence[str] = (),
    ) -> GraphMergeResult:
        if operation_id in state.processed_operations:
            previous = state.processed_operations[operation_id]
            return previous.model_copy(update={"status": "already_applied"})
        nodes = {node.ascend_id: node.model_copy(deep=True) for node in all_nodes}
        changed = list(dict.fromkeys(changed_node_ids))
        missing = sorted(set(changed) - nodes.keys())
        if missing:
            raise GraphValidationError("cannot commit missing graph nodes: " + ", ".join(missing))
        previous_hashes = {node_id: state.node_hashes.get(node_id) for node_id in changed}
        writes: list[dict[str, str]] = []
        next_hashes = dict(state.node_hashes)
        next_paths = dict(state.node_paths)
        next_machine = dict(state.machine_hashes)
        next_statements = dict(state.statement_hashes)
        for node_id in changed:
            node = nodes[node_id]
            relative = self._node_path(node, state)
            target = ensure_path_confined(self.vault_root, self.vault_root / relative)
            contents = render_node_note(node)
            digest = sha256_text(contents)
            node.path = relative
            node.content_hash = digest
            next_paths[node_id] = relative
            next_hashes[node_id] = digest
            next_machine[node_id] = machine_hash(node)
            next_statements[node_id] = statement_hash(node)
            writes.append({"path": relative, "contents": contents, "sha256": digest})
        # A human may rename an unchanged note. Preserve the discovered location in state.
        for node in nodes.values():
            if node.path and node.node_type is not NodeType.HUMAN_NOTE:
                next_paths[node.ascend_id] = node.path
                if node.content_hash:
                    next_hashes.setdefault(node.ascend_id, node.content_hash)
                next_machine.setdefault(node.ascend_id, machine_hash(node))
                next_statements.setdefault(node.ascend_id, statement_hash(node))
        now = self._now()
        next_number = state.revision_number + 1
        next_revision = _revision(next_number, next_hashes)
        result = GraphMergeResult(
            operation_id=operation_id,
            status=result_status,
            base_revision=state.revision,
            previous_revision=state.revision,
            new_revision=next_revision,
            created_node_ids=[node_id for node_id in changed if previous_hashes[node_id] is None],
            updated_node_ids=[
                node_id for node_id in changed if previous_hashes[node_id] is not None
            ],
            stale_node_ids=list(dict.fromkeys(stale_node_ids)),
        )
        next_state = state.model_copy(deep=True)
        next_state.revision_number = next_number
        next_state.revision = next_revision
        next_state.updated_at = now
        next_state.node_paths = next_paths
        next_state.node_hashes = next_hashes
        next_state.machine_hashes = next_machine
        next_state.statement_hashes = next_statements
        next_state.processed_operations[operation_id] = result
        next_state.changes.append(
            GraphChangeRecord(
                revision=next_revision,
                previous_revision=state.revision,
                run_id=run_id,
                author=author,
                timestamp=now,
                reason=reason,
                operation_id=operation_id,
                changed_nodes=changed,
                previous_hashes=previous_hashes,
                new_hashes={node_id: next_hashes.get(node_id) for node_id in changed},
                source_artifacts=list(source_artifacts),
            )
        )
        transaction = {
            "schema_version": 1,
            "operation_id": operation_id,
            "previous_revision": state.revision,
            "new_revision": next_revision,
            "writes": writes,
            "state_after": next_state.model_dump(mode="json"),
        }
        atomic_write_json(self.pending_path, transaction, confinement_root=self.ascend_root)
        for write in writes:
            target = ensure_path_confined(self.vault_root, self.vault_root / write["path"])
            atomic_write_text(
                target, write["contents"], confinement_root=self.vault_root, mode=0o600
            )
        atomic_write_json(self.state_path, next_state, confinement_root=self.ascend_root)
        committed_nodes = list(nodes.values())
        self._write_snapshot_unlocked(next_state, committed_nodes)
        self._write_navigation_unlocked(next_state, committed_nodes)
        self._rebuild_index_unlocked(next_state, committed_nodes)
        self.pending_path.unlink(missing_ok=True)
        return result

    def _write_snapshot_unlocked(self, state: GraphState, nodes: Sequence[GraphNode]) -> None:
        path = ensure_path_confined(
            self.snapshots_root, self.snapshots_root / f"{state.revision}.json"
        )
        if path.is_file():
            return
        payload = {
            "schema_version": 1,
            "revision": state.revision,
            "created_at": state.updated_at.isoformat(),
            "node_hashes": dict(sorted(state.node_hashes.items())),
            "nodes": [
                node.model_dump(mode="json")
                for node in sorted(nodes, key=lambda item: item.ascend_id)
                if node.node_type is not NodeType.HUMAN_NOTE or node.ascend_id in state.node_paths
            ],
            "edges": [
                edge.model_dump(mode="json")
                for edge in _unique_edges(edge for node in nodes for edge in node.relations)
            ],
        }
        atomic_write_json(path, payload, confinement_root=self.snapshots_root)

    def _snapshot_unlocked(self, revision: str) -> dict[str, Any]:
        if not _REVISION.fullmatch(revision):
            raise GraphValidationError(f"invalid graph revision: {revision!r}")
        path = ensure_path_confined(self.snapshots_root, self.snapshots_root / f"{revision}.json")
        if not path.is_file():
            raise GraphValidationError(f"graph revision snapshot does not exist: {revision}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GraphValidationError(f"graph revision snapshot is invalid: {revision}") from exc
        if not isinstance(value, dict) or value.get("revision") != revision:
            raise GraphValidationError(
                f"graph revision snapshot has inconsistent identity: {revision}"
            )
        return value

    def _rebuild_index_unlocked(
        self, state: GraphState, nodes: Sequence[GraphNode] | None = None
    ) -> Path:
        selected = (
            list(nodes)
            if nodes is not None
            else self._load_nodes_unlocked(include_human_notes=True)
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".graph-index.", suffix=".sqlite", dir=self.ascend_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            connection = sqlite3.connect(temporary)
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode=DELETE;
                    PRAGMA foreign_keys=OFF;
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE nodes (
                        ascend_id TEXT PRIMARY KEY,
                        node_type TEXT NOT NULL,
                        problem_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        epistemic_status TEXT NOT NULL,
                        workflow_status TEXT NOT NULL,
                        claim_type TEXT,
                        statement_version INTEGER NOT NULL,
                        path TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        body TEXT NOT NULL,
                        invalidation_reasons_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        tombstone INTEGER NOT NULL
                    );
                    CREATE TABLE edges (
                        source_id TEXT NOT NULL,
                        relation TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        PRIMARY KEY (source_id, relation, target_id)
                    );
                    CREATE TABLE tags (
                        ascend_id TEXT NOT NULL,
                        tag TEXT NOT NULL,
                        PRIMARY KEY (ascend_id, tag)
                    );
                    CREATE TABLE changes (
                        revision TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        author TEXT NOT NULL,
                        reason TEXT NOT NULL
                    );
                    CREATE INDEX nodes_problem_status ON nodes(problem_id, epistemic_status);
                    CREATE INDEX edges_target ON edges(target_id, relation);
                    """
                )
                connection.execute(
                    "INSERT INTO metadata VALUES (?, ?)", ("revision", state.revision)
                )
                connection.execute(
                    "INSERT INTO metadata VALUES (?, ?)",
                    ("schema_version", str(GRAPH_SCHEMA_VERSION)),
                )
                for node in selected:
                    content_hash = node.content_hash or sha256_text(render_node_note(node))
                    connection.execute(
                        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            node.ascend_id,
                            node.node_type.value,
                            node.problem_id,
                            node.title,
                            node.epistemic_status.value,
                            node.workflow_status.value,
                            node.claim_type.value if node.claim_type is not None else None,
                            node.statement_version,
                            node.path or "",
                            content_hash,
                            node.body,
                            _canonical_json(node.invalidation_reasons),
                            _canonical_json(node.metadata),
                            int(node.tombstone),
                        ),
                    )
                    connection.executemany(
                        "INSERT OR IGNORE INTO tags VALUES (?, ?)",
                        [(node.ascend_id, tag) for tag in node.tags],
                    )
                    connection.executemany(
                        "INSERT OR IGNORE INTO edges VALUES (?, ?, ?)",
                        [
                            (edge.source_id, edge.relation.value, edge.target_id)
                            for edge in node.relations
                        ],
                    )
                connection.executemany(
                    "INSERT INTO changes VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            change.revision,
                            change.operation_id,
                            change.timestamp.isoformat(),
                            change.run_id,
                            change.author,
                            change.reason,
                        )
                        for change in state.changes
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.index_path

    def rebuild_index(self) -> Path:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            self._write_navigation_unlocked(state, nodes)
            return self._rebuild_index_unlocked(state, nodes)

    @staticmethod
    def _relation_issue(edge: GraphEdge, by_id: Mapping[str, GraphNode]) -> str | None:
        source = by_id[edge.source_id]
        target = by_id[edge.target_id]
        allowed: dict[RelationType, tuple[set[NodeType] | None, set[NodeType] | None]] = {
            RelationType.PROVES: ({NodeType.PROOF}, {NodeType.CLAIM}),
            RelationType.DISPROVES: ({NodeType.PROOF}, {NodeType.CLAIM}),
            RelationType.AUDITS: ({NodeType.AUDIT}, {NodeType.PROOF, NodeType.CLAIM}),
            RelationType.DEPENDS_ON: (
                {NodeType.CLAIM, NodeType.DEFINITION, NodeType.PROOF},
                {NodeType.CLAIM, NodeType.DEFINITION},
            ),
            RelationType.FORMALIZES: ({NodeType.FORMALIZATION}, {NodeType.CLAIM}),
            RelationType.REFUTES: ({NodeType.COUNTEREXAMPLE}, {NodeType.CLAIM}),
            RelationType.TARGETS: ({NodeType.TASK}, None),
            RelationType.CITES: (None, {NodeType.SOURCE}),
            RelationType.CREATED_DURING: (None, {NodeType.RUN}),
        }
        constraint = allowed.get(edge.relation)
        if constraint is None:
            return None
        source_types, target_types = constraint
        if source_types is not None and source.node_type not in source_types:
            return (
                f"{edge.relation.value} cannot originate at {source.node_type.value} "
                f"node {source.ascend_id}"
            )
        if target_types is not None and target.node_type not in target_types:
            return (
                f"{edge.relation.value} cannot target {target.node_type.value} "
                f"node {target.ascend_id}"
            )
        return None

    @staticmethod
    def _dependency_cycle(nodes: Sequence[GraphNode]) -> list[str] | None:
        graph: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            for edge in node.relations:
                if edge.relation is RelationType.DEPENDS_ON:
                    graph[edge.source_id].append(edge.target_id)
        visiting: set[str] = set()
        visited: set[str] = set()
        path: list[str] = []

        def visit(node_id: str) -> list[str] | None:
            if node_id in visiting:
                start = path.index(node_id)
                return [*path[start:], node_id]
            if node_id in visited:
                return None
            visiting.add(node_id)
            path.append(node_id)
            for target in graph.get(node_id, []):
                cycle = visit(target)
                if cycle is not None:
                    return cycle
            path.pop()
            visiting.remove(node_id)
            visited.add(node_id)
            return None

        for node_id in sorted(graph):
            cycle = visit(node_id)
            if cycle is not None:
                return cycle
        return None

    def _validate_unlocked(
        self, state: GraphState, nodes: Sequence[GraphNode]
    ) -> GraphValidationReport:
        issues: list[GraphValidationIssue] = []
        by_id = {node.ascend_id: node for node in nodes}
        managed = {node_id: node for node_id, node in by_id.items() if node_id in state.node_paths}
        for node_id, relative in state.node_paths.items():
            node = by_id.get(node_id)
            if node is None:
                issues.append(
                    GraphValidationIssue(
                        severity="error",
                        code="missing_node",
                        message=f"managed node {node_id} is missing",
                        path=relative,
                        node_id=node_id,
                    )
                )
                continue
            if node.path != relative:
                issues.append(
                    GraphValidationIssue(
                        severity="warning",
                        code="human_rename",
                        message=(
                            f"node {node_id} was renamed from {relative!r} to {node.path!r}; "
                            "its stable ID remains unchanged"
                        ),
                        path=node.path,
                        node_id=node_id,
                    )
                )
            expected_machine = state.machine_hashes.get(node_id)
            if expected_machine is not None and machine_hash(node) != expected_machine:
                issues.append(
                    GraphValidationIssue(
                        severity="error",
                        code="machine_field_changed",
                        message=(
                            f"machine-managed frontmatter changed for {node_id}; restore the "
                            "managed fields or apply a validated graph patch"
                        ),
                        path=node.path,
                        node_id=node_id,
                    )
                )
                continue
            expected_content = state.node_hashes.get(node_id)
            if expected_content is not None and node.content_hash != expected_content:
                code = (
                    "claim_statement_changed"
                    if node.node_type is NodeType.CLAIM
                    and statement_hash(node) != state.statement_hashes.get(node_id, "")
                    else "human_prose_changed"
                )
                issues.append(
                    GraphValidationIssue(
                        severity="warning",
                        code=code,
                        message=(
                            f"human-editable content changed for {node_id}; the next run will "
                            "preserve it and record any required invalidation"
                        ),
                        path=node.path,
                        node_id=node_id,
                    )
                )
        for node in nodes:
            if node.node_type is not NodeType.HUMAN_NOTE and node.problem_id not in by_id:
                issues.append(
                    GraphValidationIssue(
                        severity="error",
                        code="missing_problem",
                        message=(
                            f"node {node.ascend_id} references missing problem {node.problem_id}"
                        ),
                        path=node.path,
                        node_id=node.ascend_id,
                    )
                )
            for edge in node.relations:
                if edge.target_id not in by_id:
                    issues.append(
                        GraphValidationIssue(
                            severity="error",
                            code="missing_relation_target",
                            message=(
                                f"{edge.source_id} --{edge.relation.value}--> "
                                f"{edge.target_id} has no target node"
                            ),
                            path=node.path,
                            node_id=node.ascend_id,
                        )
                    )
                    continue
                relation_issue = self._relation_issue(edge, by_id)
                if relation_issue:
                    issues.append(
                        GraphValidationIssue(
                            severity="error",
                            code="invalid_relation_types",
                            message=relation_issue,
                            path=node.path,
                            node_id=node.ascend_id,
                        )
                    )
        cycle = self._dependency_cycle(nodes)
        if cycle is not None:
            issues.append(
                GraphValidationIssue(
                    severity="error",
                    code="dependency_cycle",
                    message="mathematical dependency cycle: " + " -> ".join(cycle),
                )
            )
        if self.index_path.is_file():
            try:
                connection = sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)
                try:
                    row = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'revision'"
                    ).fetchone()
                finally:
                    connection.close()
                if row is None or row[0] != state.revision:
                    issues.append(
                        GraphValidationIssue(
                            severity="warning",
                            code="index_stale",
                            message="derived SQLite index is stale; run ascend graph rebuild-index",
                        )
                    )
            except sqlite3.Error as exc:
                issues.append(
                    GraphValidationIssue(
                        severity="warning",
                        code="index_invalid",
                        message=f"derived SQLite index is unreadable and rebuildable: {exc}",
                    )
                )
        elif managed:
            issues.append(
                GraphValidationIssue(
                    severity="warning",
                    code="index_missing",
                    message="derived SQLite index is missing and rebuildable",
                )
            )
        return GraphValidationReport(
            valid=not any(issue.severity == "error" for issue in issues),
            revision=state.revision,
            node_count=len(nodes),
            edge_count=sum(len(node.relations) for node in nodes),
            issues=issues,
        )

    def validate(self) -> GraphValidationReport:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            try:
                nodes = self._load_nodes_unlocked(include_human_notes=True)
            except (GraphMarkdownError, GraphValidationError) as exc:
                return GraphValidationReport(
                    valid=False,
                    revision=state.revision,
                    node_count=0,
                    edge_count=0,
                    issues=[
                        GraphValidationIssue(
                            severity="error",
                            code="malformed_note",
                            message=str(exc),
                        )
                    ],
                )
            return self._validate_unlocked(state, nodes)

    def _render_dashboard(self, title: str, nodes: Sequence[GraphNode], *, description: str) -> str:
        lines = [f"# {title}", "", GENERATED_START, description, ""]
        if nodes:
            lines.extend(f"- {wikilink_for(node)}" for node in nodes)
        else:
            lines.append("_No matching nodes._")
        lines.extend([GENERATED_END, ""])
        return "\n".join(lines)

    def _write_navigation_unlocked(self, state: GraphState, nodes: Sequence[GraphNode]) -> None:
        by_problem: dict[str, list[GraphNode]] = defaultdict(list)
        for node in nodes:
            by_problem[node.problem_id].append(node)
        problems = sorted(
            (node for node in nodes if node.node_type is NodeType.PROBLEM),
            key=lambda item: item.title.casefold(),
        )
        established = [
            node
            for node in nodes
            if node.node_type is NodeType.CLAIM
            and node.epistemic_status
            in {
                EpistemicStatus.PROVED_INFORMALLY,
                EpistemicStatus.AUDIT_PASSED,
                EpistemicStatus.LEAN_VERIFIED,
            }
        ]
        obligations = [
            node
            for node in nodes
            if node.node_type is NodeType.CLAIM
            and node.epistemic_status
            in {EpistemicStatus.OPEN, EpistemicStatus.CONJECTURED, EpistemicStatus.CANDIDATE}
        ]
        active_tasks = [
            node
            for node in nodes
            if node.node_type is NodeType.TASK
            and node.workflow_status
            in {WorkflowStatus.QUEUED, WorkflowStatus.ACTIVE, WorkflowStatus.IN_PROGRESS}
        ]
        blocked = [
            node
            for node in nodes
            if node.node_type is NodeType.APPROACH
            and node.workflow_status
            in {WorkflowStatus.BLOCKED, WorkflowStatus.ABANDONED, WorkflowStatus.SUPERSEDED}
        ]
        contradictions = [
            node
            for node in nodes
            if node.epistemic_status is EpistemicStatus.INCONSISTENT
            or any(edge.relation is RelationType.CONTRADICTS for edge in node.relations)
        ]
        recent_runs = sorted(
            (node for node in nodes if node.node_type is NodeType.RUN),
            key=lambda item: item.updated_at,
            reverse=True,
        )[:12]
        formalizations = [node for node in nodes if node.node_type is NodeType.FORMALIZATION]
        home_generated: list[str] = [
            "## Exact main problem",
            "",
            *(f"- {wikilink_for(problem)}" for problem in problems),
            "",
            "## Overall status",
            "",
            f"Graph revision: `{state.revision}`",
            f"Tracked problems: {len(problems)}; nodes: {len(nodes)}.",
            "",
            "## Strongest established results",
            "",
            *(f"- {wikilink_for(node)} — `{node.epistemic_status.value}`" for node in established),
            "",
            "## Current proof architecture",
            "",
            "See [[Dashboards/Main Proof Architecture.canvas|Main Proof Architecture]].",
            "",
            "## Unresolved main obligations",
            "",
            *(f"- {wikilink_for(node)}" for node in obligations),
            "",
            "## Active tasks",
            "",
            *(f"- {wikilink_for(node)}" for node in active_tasks),
            "",
            "## Blocked or refuted routes",
            "",
            *(f"- {wikilink_for(node)}" for node in blocked),
            "",
            "## Unresolved contradictions",
            "",
            *(f"- {wikilink_for(node)}" for node in contradictions),
            "",
            "## Recent run summaries",
            "",
            *(f"- {wikilink_for(node)}" for node in recent_runs),
            "",
            "## Lean verification status",
            "",
            *(
                f"- {wikilink_for(node)} — `{node.epistemic_status.value}`"
                for node in formalizations
            ),
            "",
            "## Dashboards",
            "",
            "- [[Dashboards/Open Claims]]",
            "- [[Dashboards/Candidate Proofs Awaiting Audit]]",
            "- [[Dashboards/Audit-Passed Results]]",
            "- [[Dashboards/Lean-Verified Results]]",
            "- [[Dashboards/Active Tasks]]",
            "- [[Dashboards/Blocked Approaches]]",
            "- [[Dashboards/Unresolved Contradictions]]",
            "- [[Dashboards/Unverified Sources]]",
            "- [[Dashboards/Recent Changes]]",
        ]
        home = self.vault_root / "Home.md"
        human = ""
        if home.is_file():
            existing = home.read_text(encoding="utf-8")
            end = existing.find(GENERATED_END)
            human = existing[end + len(GENERATED_END) :].strip() if end >= 0 else existing
        home_text = new_generated_body("ASCEND Knowledge Graph", "\n".join(home_generated), human)
        atomic_write_text(home, home_text, confinement_root=self.vault_root)

        candidate_proofs = [
            node
            for node in nodes
            if node.node_type is NodeType.PROOF
            and node.epistemic_status
            in {EpistemicStatus.CANDIDATE, EpistemicStatus.PROVED_INFORMALLY}
        ]
        audit_passed = [
            node for node in nodes if node.epistemic_status is EpistemicStatus.AUDIT_PASSED
        ]
        lean_verified = [
            node for node in nodes if node.epistemic_status is EpistemicStatus.LEAN_VERIFIED
        ]
        unverified_sources = [
            node
            for node in nodes
            if node.node_type is NodeType.SOURCE
            and not bool(node.metadata.get("ascend_verified", False))
        ]
        recent_changed_nodes: list[GraphNode] = []
        for node_id in reversed(
            list(
                dict.fromkeys(
                    node_id for change in state.changes[-20:] for node_id in change.changed_nodes
                )
            )
        ):
            matched = next((item for item in nodes if item.ascend_id == node_id), None)
            if matched is not None:
                recent_changed_nodes.append(matched)
        dashboards: dict[str, tuple[str, list[GraphNode]]] = {
            "Open Claims": (
                "Claims that remain open, conjectured, candidate, or stale.",
                [
                    node
                    for node in nodes
                    if node.node_type is NodeType.CLAIM
                    and node.epistemic_status
                    in {
                        EpistemicStatus.OPEN,
                        EpistemicStatus.CONJECTURED,
                        EpistemicStatus.CANDIDATE,
                        EpistemicStatus.STALE,
                    }
                ],
            ),
            "Candidate Proofs Awaiting Audit": (
                "Candidate proof nodes that have not passed independent audit.",
                candidate_proofs,
            ),
            "Audit-Passed Results": ("Claims and proofs with passing audits.", audit_passed),
            "Lean-Verified Results": (
                "Claims and formalizations certified by deterministic Lean checks.",
                lean_verified,
            ),
            "Active Tasks": ("Queued or active graph-scoped research tasks.", active_tasks),
            "Blocked Approaches": ("Blocked, abandoned, or superseded approaches.", blocked),
            "Unresolved Contradictions": (
                "Inconsistent nodes or nodes participating in contradiction edges.",
                contradictions,
            ),
            "Unverified Sources": (
                "Source nodes without independently verified identifiers.",
                unverified_sources,
            ),
            "Recent Changes": (
                "Nodes touched by recent graph revisions.",
                recent_changed_nodes,
            ),
        }
        dashboard_root = self.vault_root / "Dashboards"
        for title, (description, selected) in dashboards.items():
            atomic_write_text(
                dashboard_root / f"{title}.md",
                self._render_dashboard(title, selected, description=description),
                confinement_root=self.vault_root,
            )
        self._write_canvases_unlocked(nodes)

    def _write_canvases_unlocked(self, nodes: Sequence[GraphNode]) -> None:
        specifications: dict[str, tuple[set[NodeType], set[RelationType]]] = {
            "Main Proof Architecture": (
                {NodeType.CLAIM, NodeType.PROOF, NodeType.DEFINITION},
                {RelationType.DEPENDS_ON, RelationType.PROVES},
            ),
            "Active Research Routes": (
                {NodeType.APPROACH, NodeType.TASK, NodeType.CLAIM},
                {RelationType.TARGETS, RelationType.MOTIVATES, RelationType.RELATED_TO},
            ),
            "Dependency Bottlenecks": (
                {NodeType.CLAIM, NodeType.DEFINITION, NodeType.COUNTEREXAMPLE},
                {RelationType.DEPENDS_ON, RelationType.BLOCKED_BY, RelationType.REFUTES},
            ),
            "Formalization Map": (
                {NodeType.CLAIM, NodeType.FORMALIZATION, NodeType.ARTIFACT},
                {RelationType.FORMALIZES, RelationType.RELATED_TO},
            ),
        }
        by_id = {node.ascend_id: node for node in nodes}
        for title, (node_types, relations) in specifications.items():
            selected = [
                node
                for node in nodes
                if node.node_type in node_types and node.node_type is not NodeType.HUMAN_NOTE
            ][:40]
            selected_ids = {node.ascend_id for node in selected}
            canvas_nodes = [
                {
                    "id": node.ascend_id,
                    "type": "file",
                    "file": node.path,
                    "x": (index % 5) * 360,
                    "y": (index // 5) * 240,
                    "width": 320,
                    "height": 180,
                }
                for index, node in enumerate(selected)
                if node.path
            ]
            canvas_edges = [
                {
                    "id": hashlib.sha256(
                        f"{edge.source_id}:{edge.relation.value}:{edge.target_id}".encode()
                    ).hexdigest()[:16],
                    "fromNode": edge.source_id,
                    "toNode": edge.target_id,
                    "label": edge.relation.value,
                }
                for node in selected
                for edge in node.relations
                if edge.relation in relations
                and edge.target_id in selected_ids
                and edge.target_id in by_id
            ]
            atomic_write_json(
                self.vault_root / "Dashboards" / f"{title}.canvas",
                {"nodes": canvas_nodes, "edges": canvas_edges},
                confinement_root=self.vault_root,
            )

    def status(self) -> GraphStatus:
        if not self.initialized:
            return GraphStatus(
                initialized=False,
                vault_path=str(self.vault_root),
                revision=None,
                node_count=0,
                edge_count=0,
                problem_count=0,
                stale_count=0,
                active_task_count=0,
            )
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            return GraphStatus(
                initialized=True,
                vault_path=str(self.vault_root),
                revision=state.revision,
                node_count=len(nodes),
                edge_count=sum(len(node.relations) for node in nodes),
                problem_count=sum(node.node_type is NodeType.PROBLEM for node in nodes),
                stale_count=sum(node.epistemic_status is EpistemicStatus.STALE for node in nodes),
                active_task_count=sum(
                    node.node_type is NodeType.TASK
                    and node.workflow_status
                    in {WorkflowStatus.QUEUED, WorkflowStatus.ACTIVE, WorkflowStatus.IN_PROGRESS}
                    for node in nodes
                ),
                last_change_at=state.updated_at,
            )

    @staticmethod
    def _select_problem_id(nodes: Sequence[GraphNode], problem_id: str | None) -> str:
        problems = [node.ascend_id for node in nodes if node.node_type is NodeType.PROBLEM]
        if problem_id is not None:
            if problem_id not in problems:
                raise GraphValidationError(f"unknown graph problem ID: {problem_id}")
            return problem_id
        if len(problems) == 1:
            return problems[0]
        if not problems:
            raise GraphValidationError("knowledge graph has no problem node")
        raise GraphValidationError(
            "knowledge graph tracks multiple problems; pass an explicit problem ID"
        )

    def _frontier_unlocked(
        self, state: GraphState, nodes: Sequence[GraphNode], problem_id: str
    ) -> GraphFrontier:
        selected = [node for node in nodes if node.problem_id == problem_id]
        proof_targets = {
            edge.target_id
            for node in selected
            if node.node_type is NodeType.PROOF
            and node.epistemic_status
            in {EpistemicStatus.CANDIDATE, EpistemicStatus.PROVED_INFORMALLY}
            for edge in node.relations
            if edge.relation is RelationType.PROVES
        }
        audited_targets = {
            edge.target_id
            for node in selected
            if node.node_type is NodeType.AUDIT
            and node.epistemic_status
            in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
            for edge in node.relations
            if edge.relation is RelationType.AUDITS
        }
        missing_dependency_sources = {
            node.ascend_id
            for node in selected
            if any(
                edge.relation is RelationType.DEPENDS_ON
                and edge.target_id not in {item.ascend_id for item in nodes}
                for edge in node.relations
            )
            or "missing_dependency" in node.invalidation_reasons
        }
        unresolved_claims = [
            node
            for node in selected
            if node.node_type is NodeType.CLAIM
            and node.epistemic_status
            in {
                EpistemicStatus.OPEN,
                EpistemicStatus.CONJECTURED,
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.STALE,
            }
        ]
        candidate_proofs = [
            node
            for node in selected
            if node.node_type is NodeType.PROOF
            and node.epistemic_status
            in {EpistemicStatus.CANDIDATE, EpistemicStatus.PROVED_INFORMALLY}
            and node.ascend_id not in audited_targets
            and any(
                edge.relation is RelationType.PROVES and edge.target_id in proof_targets
                for edge in node.relations
            )
        ]
        return GraphFrontier(
            problem_id=problem_id,
            graph_revision=state.revision,
            unresolved_claims=[_node_summary(node) for node in unresolved_claims],
            candidate_proofs_awaiting_audit=[_node_summary(node) for node in candidate_proofs],
            blocked_approaches=[
                _node_summary(node)
                for node in selected
                if node.node_type is NodeType.APPROACH
                and node.workflow_status
                in {WorkflowStatus.BLOCKED, WorkflowStatus.DORMANT, WorkflowStatus.ABANDONED}
            ],
            unresolved_contradictions=[
                _node_summary(node)
                for node in selected
                if node.epistemic_status is EpistemicStatus.INCONSISTENT
                or any(edge.relation is RelationType.CONTRADICTS for edge in node.relations)
            ],
            missing_dependencies=[
                _node_summary(node)
                for node in selected
                if node.ascend_id in missing_dependency_sources
            ],
            high_value_tasks=[
                _node_summary(node)
                for node in selected
                if node.node_type is NodeType.TASK
                and node.workflow_status
                in {WorkflowStatus.QUEUED, WorkflowStatus.ACTIVE, WorkflowStatus.IN_PROGRESS}
            ],
            prior_runs=[
                _node_summary(node)
                for node in sorted(
                    (item for item in selected if item.node_type is NodeType.RUN),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )[:20]
            ],
            refuted_or_unproductive_routes=[
                _node_summary(node)
                for node in selected
                if (
                    node.epistemic_status is EpistemicStatus.REFUTED
                    or node.workflow_status
                    in {
                        WorkflowStatus.ABANDONED,
                        WorkflowStatus.SUPERSEDED,
                        WorkflowStatus.DORMANT,
                    }
                )
                and node.node_type in {NodeType.APPROACH, NodeType.CLAIM, NodeType.PROOF}
            ],
            unverified_sources=[
                _node_summary(node)
                for node in selected
                if node.node_type is NodeType.SOURCE
                and not bool(node.metadata.get("ascend_verified", False))
            ],
        )

    def frontier(self, problem_id: str | None = None) -> GraphFrontier:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            selected = self._select_problem_id(nodes, problem_id)
            return self._frontier_unlocked(state, nodes, selected)

    def _context_slice_unlocked(
        self,
        state: GraphState,
        nodes: Sequence[GraphNode],
        *,
        problem_id: str,
        task_id: str,
    ) -> GraphContextSlice:
        by_id = {node.ascend_id: node for node in nodes}
        task = by_id.get(task_id)
        if task is None or task.node_type is not NodeType.TASK:
            raise GraphValidationError(f"graph task does not exist: {task_id}")
        target_ids = [
            edge.target_id for edge in task.relations if edge.relation is RelationType.TARGETS
        ] or [problem_id]
        reverse: dict[str, list[GraphEdge]] = defaultdict(list)
        for node in nodes:
            for edge in node.relations:
                reverse[edge.target_id].append(edge)
        queue: deque[tuple[str, int]] = deque(
            [(problem_id, 0), (task_id, 0), *((target, 0) for target in target_ids)]
        )
        selected_ids: list[str] = []
        seen: set[str] = set()
        while queue and len(selected_ids) < self.maximum_context_nodes:
            node_id, depth = queue.popleft()
            if node_id in seen or node_id not in by_id:
                continue
            node = by_id[node_id]
            if node.problem_id != problem_id and node.ascend_id != problem_id:
                continue
            seen.add(node_id)
            selected_ids.append(node_id)
            if depth >= 3:
                continue
            for edge in node.relations:
                if edge.relation in {
                    RelationType.DEPENDS_ON,
                    RelationType.PROVES,
                    RelationType.REFUTES,
                    RelationType.CITES,
                    RelationType.AUDITS,
                    RelationType.FORMALIZES,
                    RelationType.BLOCKED_BY,
                    RelationType.RELATED_TO,
                }:
                    queue.append((edge.target_id, depth + 1))
            for edge in reverse.get(node_id, []):
                if edge.relation in {
                    RelationType.DEPENDS_ON,
                    RelationType.PROVES,
                    RelationType.REFUTES,
                    RelationType.AUDITS,
                    RelationType.FORMALIZES,
                    RelationType.TARGETS,
                }:
                    queue.append((edge.source_id, depth + 1))
        context_nodes: list[GraphContextNode] = []
        remaining_characters = self.maximum_context_characters
        for node_id in selected_ids:
            node = by_id[node_id]
            excerpt = generated_section(node.body)
            excerpt = excerpt[: min(6_000, remaining_characters)]
            remaining_characters -= len(excerpt)
            context_nodes.append(
                GraphContextNode(
                    summary=_node_summary(node),
                    body_excerpt=excerpt,
                    outgoing=node.relations,
                    content_hash=node.content_hash or sha256_text(render_node_note(node)),
                )
            )
            if remaining_characters <= 0:
                break
        problem_node_count = sum(
            node.problem_id == problem_id or node.ascend_id == problem_id for node in nodes
        )
        return GraphContextSlice(
            graph_revision=state.revision,
            problem_id=problem_id,
            task_id=task_id,
            target_node_ids=target_ids,
            exact_task=generated_section(task.body),
            nodes=context_nodes,
            omitted_node_count=max(0, problem_node_count - len(context_nodes)),
        )

    def context_slice(self, problem_id: str, task_id: str) -> GraphContextSlice:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            return self._context_slice_unlocked(
                state, nodes, problem_id=problem_id, task_id=task_id
            )

    def show(self, node_id: str) -> GraphNode:
        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            node = next((item for item in nodes if item.ascend_id == node_id), None)
            if node is None:
                raise GraphValidationError(f"graph node does not exist: {node_id}")
            return node

    def traverse(
        self, node_id: str, *, downstream: bool, relation: RelationType = RelationType.DEPENDS_ON
    ) -> list[GraphNodeSummary]:
        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes}
            if node_id not in by_id:
                raise GraphValidationError(f"graph node does not exist: {node_id}")
            adjacency: dict[str, list[str]] = defaultdict(list)
            for node in nodes:
                for edge in node.relations:
                    if edge.relation is relation:
                        if downstream:
                            adjacency[edge.target_id].append(edge.source_id)
                        else:
                            adjacency[edge.source_id].append(edge.target_id)
            result: list[GraphNodeSummary] = []
            queue = deque(adjacency.get(node_id, []))
            seen = {node_id}
            while queue:
                current = queue.popleft()
                if current in seen or current not in by_id:
                    continue
                seen.add(current)
                result.append(_node_summary(by_id[current]))
                queue.extend(adjacency.get(current, []))
            return result

    def list_stale(self, problem_id: str | None = None) -> list[GraphNodeSummary]:
        nodes = self.load_nodes()
        selected = self._select_problem_id(nodes, problem_id)
        return [
            _node_summary(node)
            for node in nodes
            if node.problem_id == selected
            and (node.epistemic_status is EpistemicStatus.STALE or node.invalidation_reasons)
        ]

    def list_tasks(self, problem_id: str | None = None) -> list[GraphNodeSummary]:
        nodes = self.load_nodes()
        selected = self._select_problem_id(nodes, problem_id)
        return [
            _node_summary(node)
            for node in nodes
            if node.problem_id == selected and node.node_type is NodeType.TASK
        ]

    def tombstone(self, node_id: str, *, reason: str, run_id: str = "HUMAN") -> GraphMergeResult:
        """Retain a deleted/superseded identity without breaking incoming links."""

        if not reason.strip():
            raise GraphValidationError("tombstoning a node requires a reason")
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes}
            node = by_id.get(node_id)
            if node is None:
                raise GraphValidationError(f"graph node does not exist: {node_id}")
            if node.node_type in {NodeType.PROBLEM, NodeType.RUN}:
                raise GraphValidationError("problem and run nodes cannot be tombstoned")
            if node.tombstone:
                operation_id = f"tombstone:{node_id}:{sha256_text(reason.strip())[:16]}"
                previous = state.processed_operations.get(operation_id)
                if previous is not None:
                    return previous.model_copy(update={"status": "already_applied"})
            node.tombstone = True
            node.workflow_status = WorkflowStatus.SUPERSEDED
            node.epistemic_status = EpistemicStatus.STALE
            node.invalidation_reasons = list(
                dict.fromkeys([*node.invalidation_reasons, "tombstoned"])
            )
            node.metadata["ascend_tombstone_reason"] = reason.strip()
            node.last_modified_run = run_id
            node.author_role = "human"
            node.updated_at = self._now()
            stale = self._propagate_staleness(
                by_id, [node_id], "dependency_tombstoned_requires_reaudit"
            )
            operation_id = f"tombstone:{node_id}:{sha256_text(reason.strip())[:16]}"
            return self._commit_nodes_unlocked(
                state=state,
                all_nodes=list(by_id.values()),
                changed_node_ids=[node_id, *stale],
                run_id=run_id,
                author="human",
                reason=f"Tombstone {node_id}: {reason.strip()}",
                operation_id=operation_id,
                stale_node_ids=[node_id, *stale],
            )

    def diff(self, revision_a: str, revision_b: str) -> GraphDiff:
        with self._locked():
            self._recover_pending_unlocked()
            first = self._snapshot_unlocked(revision_a)
            second = self._snapshot_unlocked(revision_b)
            first_hashes = cast(dict[str, str], first.get("node_hashes", {}))
            second_hashes = cast(dict[str, str], second.get("node_hashes", {}))
            first_edges = {
                (item["source_id"], item["relation"], item["target_id"])
                for item in cast(list[dict[str, str]], first.get("edges", []))
            }
            second_edges = {
                (item["source_id"], item["relation"], item["target_id"])
                for item in cast(list[dict[str, str]], second.get("edges", []))
            }

            def edge(value: tuple[str, str, str]) -> GraphEdge:
                return GraphEdge(
                    source_id=value[0], relation=RelationType(value[1]), target_id=value[2]
                )

            return GraphDiff(
                revision_a=revision_a,
                revision_b=revision_b,
                added_nodes=sorted(second_hashes.keys() - first_hashes.keys()),
                removed_nodes=sorted(first_hashes.keys() - second_hashes.keys()),
                changed_nodes=sorted(
                    node_id
                    for node_id in first_hashes.keys() & second_hashes.keys()
                    if first_hashes[node_id] != second_hashes[node_id]
                ),
                added_edges=[edge(value) for value in sorted(second_edges - first_edges)],
                removed_edges=[edge(value) for value in sorted(first_edges - second_edges)],
            )

    def export(self, *, output_format: Literal["json", "graphviz", "mermaid"] = "json") -> str:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            edges = _unique_edges(edge for node in nodes for edge in node.relations)
            if output_format == "json":
                return (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "revision": state.revision,
                            "nodes": [node.model_dump(mode="json") for node in nodes],
                            "edges": [edge.model_dump(mode="json") for edge in edges],
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
            if output_format == "graphviz":
                lines = ["digraph ASCEND {"]
                for node in nodes:
                    label = json.dumps(f"{node.ascend_id}\\n{node.title}")
                    lines.append(f'  "{node.ascend_id}" [label={label}];')
                for edge in edges:
                    lines.append(
                        f'  "{edge.source_id}" -> "{edge.target_id}" '
                        f'[label="{edge.relation.value}"];'
                    )
                lines.append("}")
                return "\n".join(lines) + "\n"
            lines = ["flowchart TD"]
            for node in nodes:
                safe_title = node.title.replace('"', "'").replace("[", "(").replace("]", ")")
                lines.append(f'  {node.ascend_id.replace("-", "_")}["{safe_title}"]')
            for edge in edges:
                lines.append(
                    f"  {edge.source_id.replace('-', '_')} -->|{edge.relation.value}| "
                    f"{edge.target_id.replace('-', '_')}"
                )
            return "\n".join(lines) + "\n"

    def open_in_obsidian(self) -> tuple[bool, Path, str]:
        """Launch Obsidian when discoverable, otherwise return a graceful remedy."""

        if not self.initialized:
            self.initialize()
        executable = shutil.which("obsidian")
        url = f"obsidian://open?path={quote(str(self.vault_root))}"
        if executable is not None:
            try:
                subprocess.Popen(
                    [executable, url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                return False, self.vault_root, f"Obsidian could not be launched: {exc}"
            return True, self.vault_root, "Opened the ASCEND vault in Obsidian."
        return (
            False,
            self.vault_root,
            "Obsidian is not installed or not on PATH; open this directory as a vault manually.",
        )

    @staticmethod
    def _propagate_staleness(
        nodes: dict[str, GraphNode], seeds: Sequence[str], reason: str
    ) -> list[str]:
        changed: list[str] = []
        queue: deque[str] = deque(seeds)
        visited = set(seeds)
        while queue:
            changed_id = queue.popleft()
            changed_node = nodes.get(changed_id)
            if changed_node is None:
                continue
            affected: set[str] = set()
            for node in nodes.values():
                for edge in node.relations:
                    if edge.relation is RelationType.DEPENDS_ON and edge.target_id == changed_id:
                        affected.add(edge.source_id)
                    if edge.relation in {RelationType.AUDITS, RelationType.FORMALIZES} and (
                        edge.target_id == changed_id
                    ):
                        affected.add(edge.source_id)
                    if edge.relation is RelationType.PROVES:
                        if edge.source_id == changed_id:
                            affected.add(edge.target_id)
                        elif edge.target_id == changed_id:
                            affected.add(edge.source_id)
            for node_id in sorted(affected):
                node = nodes[node_id]
                if node.node_type in {
                    NodeType.CLAIM,
                    NodeType.PROOF,
                    NodeType.AUDIT,
                    NodeType.FORMALIZATION,
                }:
                    node.epistemic_status = EpistemicStatus.STALE
                    node.invalidation_reasons = list(
                        dict.fromkeys([*node.invalidation_reasons, reason])
                    )
                    if node.node_type is NodeType.FORMALIZATION:
                        node.workflow_status = WorkflowStatus.BLOCKED
                    if node_id not in changed:
                        changed.append(node_id)
                if node_id not in visited:
                    visited.add(node_id)
                    queue.append(node_id)
        return changed

    def reconcile_human_edits(self, *, run_id: str) -> GraphMergeResult | None:
        """Preserve allowed human prose/renames and invalidate changed mathematics."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes}
            conflicts: list[str] = []
            changed: list[str] = []
            stale: list[str] = []
            now = self._now()
            for node_id, expected_machine in state.machine_hashes.items():
                node = by_id.get(node_id)
                if node is None:
                    conflicts.append(f"managed node {node_id} was deleted; use a tombstone")
                    continue
                if machine_hash(node) != expected_machine:
                    conflicts.append(f"machine-managed frontmatter changed for {node_id}")
                    continue
                renamed = node.path != state.node_paths.get(node_id)
                content_changed = node.content_hash != state.node_hashes.get(node_id)
                if not (renamed or content_changed):
                    continue
                if (
                    content_changed
                    and node.node_type is NodeType.CLAIM
                    and (statement_hash(node) != state.statement_hashes.get(node_id, ""))
                ):
                    node.statement_version += 1
                    node.epistemic_status = EpistemicStatus.STALE
                    node.invalidation_reasons = list(
                        dict.fromkeys(
                            [*node.invalidation_reasons, "statement_changed_requires_reaudit"]
                        )
                    )
                    stale.append(node_id)
                    stale.extend(
                        self._propagate_staleness(
                            by_id, [node_id], "dependency_changed_requires_reaudit"
                        )
                    )
                elif content_changed and node.node_type is NodeType.PROOF:
                    node.epistemic_status = EpistemicStatus.STALE
                    node.invalidation_reasons = list(
                        dict.fromkeys(
                            [*node.invalidation_reasons, "proof_changed_requires_reaudit"]
                        )
                    )
                    stale.append(node_id)
                    stale.extend(
                        self._propagate_staleness(
                            by_id, [node_id], "proof_changed_requires_reaudit"
                        )
                    )
                node.author_role = "human"
                node.last_modified_run = run_id
                node.updated_at = now
                changed.append(node_id)
            if conflicts:
                raise GraphConflictError(
                    "knowledge vault contains conflicting manual changes: " + "; ".join(conflicts)
                )
            changed = list(dict.fromkeys([*changed, *stale]))
            if not changed:
                return None
            return self._commit_nodes_unlocked(
                state=state,
                all_nodes=list(by_id.values()),
                changed_node_ids=changed,
                run_id=run_id,
                author="human",
                reason="Preserve human vault edits and invalidate affected evidence.",
                operation_id=f"human-reconcile:{run_id}:{state.revision}",
                stale_node_ids=stale,
            )

    def _problem_file_key(self, source_path: Path) -> str:
        resolved = source_path.expanduser().resolve(strict=True)
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
            return f"external-{digest}-{resolved.name}"

    def initialize_problem(
        self,
        *,
        source_path: Path,
        problem_text: str,
        run_id: str,
    ) -> tuple[str, str]:
        """Create or load the stable problem node and one run node."""

        if not self.initialized:
            self.initialize()
        self.reconcile_human_edits(run_id=run_id)
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes}
            key = self._problem_file_key(source_path)
            problem_id = state.problem_files.get(key)
            now = self._now()
            changed: list[str] = []
            if problem_id is None:
                problem_id = _new_id(NodeType.PROBLEM)
                state.problem_files[key] = problem_id
                problem = GraphNode(
                    ascend_id=problem_id,
                    node_type=NodeType.PROBLEM,
                    problem_id=problem_id,
                    title=source_path.stem.replace("_", " ").replace("-", " ").title(),
                    epistemic_status=EpistemicStatus.OPEN,
                    workflow_status=WorkflowStatus.ACTIVE,
                    created_in_run=run_id,
                    last_modified_run=run_id,
                    author_role="ascend-intake",
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(
                        source_path.stem,
                        "## Exact main problem\n\n"
                        + problem_text.strip()
                        + "\n\n## Overall status\n\nResearch not yet compiled.",
                    ),
                    tags=["ascend/problem"],
                    source_artifacts=[f".ascend/runs/{run_id}/input/problem.md"],
                )
                by_id[problem_id] = problem
                changed.append(problem_id)
            else:
                existing_problem = by_id.get(problem_id)
                if existing_problem is None:
                    raise GraphValidationError(
                        f"problem mapping {key!r} references missing node {problem_id}"
                    )
                problem = existing_problem
                current_problem = exact_statement(problem.body)
                if problem_text.strip() not in current_problem:
                    problem.body = replace_generated_section(
                        problem.body,
                        problem.title,
                        "## Exact main problem\n\n"
                        + problem_text.strip()
                        + "\n\n## Overall status\n\nA later ASCEND run updated the problem input.",
                    )
                    problem.updated_at = now
                    problem.last_modified_run = run_id
                    problem.author_role = "ascend-intake"
                    problem.invalidation_reasons = list(
                        dict.fromkeys([*problem.invalidation_reasons, "problem_statement_changed"])
                    )
                    changed.append(problem_id)
                    changed.extend(
                        self._propagate_staleness(by_id, [problem_id], "problem_statement_changed")
                    )
            run_node_id = _deterministic_id(NodeType.RUN, problem_id, run_id)
            if run_node_id not in by_id:
                run_node = GraphNode(
                    ascend_id=run_node_id,
                    node_type=NodeType.RUN,
                    problem_id=problem_id,
                    title=f"ASCEND run {run_id}",
                    epistemic_status=EpistemicStatus.OPEN,
                    workflow_status=WorkflowStatus.IN_PROGRESS,
                    created_in_run=run_id,
                    last_modified_run=run_id,
                    author_role="ascend-workflow",
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(
                        f"ASCEND run {run_id}",
                        "## Run summary\n\nWorkflow started.\n\n"
                        "## Run artifacts\n\n"
                        f"- `.ascend/runs/{run_id}/`",
                    ),
                    tags=["ascend/run"],
                    relations=[
                        GraphEdge(
                            source_id=run_node_id,
                            relation=RelationType.RELATED_TO,
                            target_id=problem_id,
                        )
                    ],
                    source_artifacts=[f".ascend/runs/{run_id}/state.json"],
                )
                by_id[run_node_id] = run_node
                changed.append(run_node_id)
            if changed:
                result = self._commit_nodes_unlocked(
                    state=state,
                    all_nodes=list(by_id.values()),
                    changed_node_ids=changed,
                    run_id=run_id,
                    author="ascend-workflow",
                    reason="Initialize or resume the persistent problem graph.",
                    operation_id=f"run-start:{run_id}",
                )
                revision = result.new_revision
            else:
                revision = state.revision
            return problem_id, revision

    def _upsert_generated_nodes_unlocked(
        self,
        *,
        state: GraphState,
        nodes: dict[str, GraphNode],
        proposed: Sequence[GraphNode],
        run_id: str,
        author: str,
        reason: str,
        operation_id: str,
        source_artifacts: Sequence[str] = (),
        stale_node_ids: Sequence[str] = (),
    ) -> GraphMergeResult:
        if operation_id in state.processed_operations:
            prior = state.processed_operations[operation_id]
            return prior.model_copy(update={"status": "already_applied"})
        changed: list[str] = []
        epistemic_rank = {
            EpistemicStatus.OPEN: 0,
            EpistemicStatus.CONJECTURED: 1,
            EpistemicStatus.CANDIDATE: 2,
            EpistemicStatus.PROVED_INFORMALLY: 3,
            EpistemicStatus.AUDIT_PASSED: 4,
            EpistemicStatus.LEAN_VERIFIED: 5,
        }
        for incoming in proposed:
            existing = nodes.get(incoming.ascend_id)
            if existing is None:
                nodes[incoming.ascend_id] = incoming
                changed.append(incoming.ascend_id)
                continue
            # Preserve human prose outside the generated block and the stable creation record.
            existing.title = incoming.title
            existing.body = replace_generated_section(
                existing.body, incoming.title, generated_section(incoming.body)
            )
            # A later workflow generation may refresh a deterministic note, but it
            # must never silently erase stronger evidence established in an earlier
            # run. Negative/invalidation states remain explicit and authoritative.
            if incoming.epistemic_status in {
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            } or existing.epistemic_status in {
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            }:
                existing.epistemic_status = incoming.epistemic_status
            elif (
                epistemic_rank[incoming.epistemic_status]
                >= epistemic_rank[existing.epistemic_status]
            ):
                existing.epistemic_status = incoming.epistemic_status
            existing.workflow_status = incoming.workflow_status
            existing.claim_type = incoming.claim_type
            existing.statement_version = max(existing.statement_version, incoming.statement_version)
            existing.last_modified_run = run_id
            existing.author_role = author
            existing.updated_at = incoming.updated_at
            existing.tags = list(dict.fromkeys([*existing.tags, *incoming.tags]))
            existing.relations = _unique_edges([*existing.relations, *incoming.relations])
            existing.invalidation_reasons = list(
                dict.fromkeys([*existing.invalidation_reasons, *incoming.invalidation_reasons])
            )
            existing.dependency_versions = list(
                dict.fromkeys([*existing.dependency_versions, *incoming.dependency_versions])
            )
            existing.source_artifacts = list(
                dict.fromkeys([*existing.source_artifacts, *incoming.source_artifacts])
            )
            existing.evidence = list(dict.fromkeys([*existing.evidence, *incoming.evidence]))
            existing.manuscript_mappings = list(
                dict.fromkeys([*existing.manuscript_mappings, *incoming.manuscript_mappings])
            )
            existing.metadata.update(incoming.metadata)
            changed.append(existing.ascend_id)
        return self._commit_nodes_unlocked(
            state=state,
            all_nodes=list(nodes.values()),
            changed_node_ids=changed,
            run_id=run_id,
            author=author,
            reason=reason,
            operation_id=operation_id,
            source_artifacts=source_artifacts,
            stale_node_ids=stale_node_ids,
        )

    @staticmethod
    def _epistemic_transition_allowed(current: EpistemicStatus, target: EpistemicStatus) -> bool:
        allowed: dict[EpistemicStatus, set[EpistemicStatus]] = {
            EpistemicStatus.OPEN: {
                EpistemicStatus.CONJECTURED,
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.CONJECTURED: {
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.CANDIDATE: {
                EpistemicStatus.PROVED_INFORMALLY,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.PROVED_INFORMALLY: {
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.AUDIT_PASSED,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.AUDIT_PASSED: {
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.LEAN_VERIFIED,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.LEAN_VERIFIED: {
                EpistemicStatus.STALE,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
            },
            EpistemicStatus.REFUTED: {EpistemicStatus.STALE, EpistemicStatus.OPEN},
            EpistemicStatus.INCONSISTENT: {EpistemicStatus.STALE, EpistemicStatus.OPEN},
            EpistemicStatus.STALE: {
                EpistemicStatus.OPEN,
                EpistemicStatus.CONJECTURED,
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.PROVED_INFORMALLY,
                EpistemicStatus.AUDIT_PASSED,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
            },
        }
        return target is current or target in allowed[current]

    @staticmethod
    def _workflow_transition_allowed(current: WorkflowStatus, target: WorkflowStatus) -> bool:
        if current is target:
            return True
        if current is WorkflowStatus.COMPLETE:
            return target in {
                WorkflowStatus.ACTIVE,
                WorkflowStatus.BLOCKED,
                WorkflowStatus.SUPERSEDED,
            }
        if current is WorkflowStatus.SUPERSEDED:
            return target is WorkflowStatus.ACTIVE
        return True

    def merge_patch(
        self,
        patch: GraphPatch,
        *,
        problem_id: str,
        operation_id: str,
    ) -> GraphMergeResult:
        """Validate and atomically merge a proposed agent patch.

        A stale base revision may rebase only when every touched source node is
        unchanged from that snapshot and explicit node hashes still match.  This
        permits independent concurrent additions while detecting true edit conflicts.
        """

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            if operation_id in state.processed_operations:
                prior = state.processed_operations[operation_id]
                return prior.model_copy(update={"status": "already_applied"})
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            validation = self._validate_unlocked(state, nodes_list)
            errors = [issue.message for issue in validation.issues if issue.severity == "error"]
            if errors:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=errors,
                )
            by_id = {node.ascend_id: node.model_copy(deep=True) for node in nodes_list}
            task = by_id.get(patch.task_id)
            if task is None or task.node_type is not NodeType.TASK:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=[f"patch task does not exist: {patch.task_id}"],
                )
            if task.problem_id != problem_id:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=["patch task belongs to a different problem"],
                )
            try:
                base_snapshot = self._snapshot_unlocked(patch.base_graph_revision)
            except GraphValidationError as exc:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="conflict",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=[str(exc)],
                )
            base_hashes = cast(dict[str, str], base_snapshot.get("node_hashes", {}))
            touched = {
                *(update.ascend_id for update in patch.update_nodes),
                *(change.ascend_id for change in patch.proposed_status_changes),
                *(edge.source_id for edge in [*patch.add_edges, *patch.remove_edges]),
            }
            proposed_ids = [item.ascend_id for item in patch.create_nodes if item.ascend_id]
            conflicts: list[str] = []
            for node_id in touched:
                current = by_id.get(node_id)
                if current is None and node_id not in proposed_ids:
                    conflicts.append(f"patch touches missing node {node_id}")
                    continue
                if current is None:
                    continue
                if patch.base_graph_revision != state.revision and (
                    base_hashes.get(node_id) != current.content_hash
                ):
                    conflicts.append(
                        f"node {node_id} changed after base revision {patch.base_graph_revision}"
                    )
            for update in patch.update_nodes:
                current = by_id.get(update.ascend_id)
                if current is not None and current.content_hash != update.expected_content_hash:
                    conflicts.append(f"content hash conflict for {update.ascend_id}")
            for change in patch.proposed_status_changes:
                current = by_id.get(change.ascend_id)
                if current is not None and current.content_hash != change.expected_content_hash:
                    conflicts.append(f"content hash conflict for {change.ascend_id}")
            if len(proposed_ids) != len(set(proposed_ids)):
                conflicts.append("patch proposes duplicate stable node IDs")
            conflicts.extend(
                f"proposed stable node ID already exists: {node_id}"
                for node_id in proposed_ids
                if node_id in by_id
            )
            existing_signatures = {
                (node.node_type, node.title.casefold().strip()): node.ascend_id
                for node in by_id.values()
                if not node.tombstone
            }
            for item in patch.create_nodes:
                duplicate = existing_signatures.get((item.node_type, item.title.casefold().strip()))
                if duplicate is not None:
                    conflicts.append(
                        f"likely duplicate {item.node_type.value} node {item.title!r}: {duplicate}"
                    )
            if conflicts:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="conflict",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=list(dict.fromkeys(conflicts)),
                )
            now = self._now()
            changed: list[str] = []
            created_ids: list[str] = []
            for item in patch.create_nodes:
                node_id = item.ascend_id or _new_id(item.node_type)
                node = GraphNode(
                    ascend_id=node_id,
                    node_type=item.node_type,
                    problem_id=problem_id,
                    title=item.title,
                    epistemic_status=item.epistemic_status,
                    workflow_status=item.workflow_status,
                    claim_type=item.claim_type,
                    created_in_run=patch.run_id,
                    last_modified_run=patch.run_id,
                    author_role=patch.agent_role,
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(item.title, item.body),
                    tags=list(dict.fromkeys([f"ascend/{item.node_type.value}", *item.tags])),
                    evidence=list(dict.fromkeys([*item.evidence, *patch.evidence])),
                    source_artifacts=item.source_artifacts,
                )
                by_id[node_id] = node
                changed.append(node_id)
                created_ids.append(node_id)
            for update in patch.update_nodes:
                node = by_id[update.ascend_id]
                if update.title is not None:
                    node.title = update.title.strip()
                if update.body is not None:
                    old_statement = exact_statement(node.body)
                    node.body = replace_generated_section(node.body, node.title, update.body)
                    if (
                        node.node_type is NodeType.CLAIM
                        and exact_statement(node.body) != old_statement
                    ):
                        node.statement_version += 1
                        node.epistemic_status = EpistemicStatus.STALE
                        node.invalidation_reasons = list(
                            dict.fromkeys(
                                [*node.invalidation_reasons, "statement_changed_requires_reaudit"]
                            )
                        )
                if update.tags is not None:
                    node.tags = list(dict.fromkeys(update.tags))
                node.evidence = list(
                    dict.fromkeys([*node.evidence, *patch.evidence, *update.evidence])
                )
                node.source_artifacts = list(
                    dict.fromkeys([*node.source_artifacts, *update.source_artifacts])
                )
                node.updated_at = now
                node.last_modified_run = patch.run_id
                node.author_role = patch.agent_role
                changed.append(node.ascend_id)
            remove_keys = {
                (edge.source_id, edge.relation, edge.target_id) for edge in patch.remove_edges
            }
            for source_id, relation, target_id in remove_keys:
                source = by_id[source_id]
                source.relations = [
                    edge
                    for edge in source.relations
                    if (edge.source_id, edge.relation, edge.target_id)
                    != (source_id, relation, target_id)
                ]
                source.updated_at = now
                source.last_modified_run = patch.run_id
                changed.append(source_id)
            for edge in patch.add_edges:
                if edge.target_id not in by_id:
                    conflicts.append(f"edge target does not exist: {edge.target_id}")
                    continue
                issue = self._relation_issue(edge, by_id)
                if issue:
                    conflicts.append(issue)
                    continue
                source = by_id[edge.source_id]
                source.relations = _unique_edges([*source.relations, edge])
                if edge.relation is RelationType.DEPENDS_ON:
                    target = by_id[edge.target_id]
                    version = (
                        f"{target.ascend_id}@{target.statement_version}:"
                        f"{target.content_hash or sha256_text(render_node_note(target))}"
                    )
                    source.dependency_versions = list(
                        dict.fromkeys([*source.dependency_versions, version])
                    )
                source.updated_at = now
                source.last_modified_run = patch.run_id
                changed.append(source.ascend_id)
            if conflicts:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=list(dict.fromkeys(conflicts)),
                )
            for change in patch.proposed_status_changes:
                node = by_id[change.ascend_id]
                if change.epistemic_status is EpistemicStatus.LEAN_VERIFIED:
                    conflicts.append("only deterministic Lean verification may set lean_verified")
                    continue
                if (
                    change.epistemic_status is EpistemicStatus.AUDIT_PASSED
                    and not patch.agent_role.startswith("research-auditor")
                ):
                    conflicts.append("only a recorded independent audit may set audit_passed")
                    continue
                if change.epistemic_status is not None and not self._epistemic_transition_allowed(
                    node.epistemic_status, change.epistemic_status
                ):
                    conflicts.append(
                        f"invalid epistemic transition for {node.ascend_id}: "
                        f"{node.epistemic_status.value} -> {change.epistemic_status.value}"
                    )
                    continue
                if change.workflow_status is not None and not self._workflow_transition_allowed(
                    node.workflow_status, change.workflow_status
                ):
                    conflicts.append(
                        f"invalid workflow transition for {node.ascend_id}: "
                        f"{node.workflow_status.value} -> {change.workflow_status.value}"
                    )
                    continue
                if change.epistemic_status is not None:
                    node.epistemic_status = change.epistemic_status
                if change.workflow_status is not None:
                    node.workflow_status = change.workflow_status
                node.updated_at = now
                node.last_modified_run = patch.run_id
                node.author_role = patch.agent_role
                node.evidence = list(dict.fromkeys([*node.evidence, *patch.evidence]))
                changed.append(node.ascend_id)
            if conflicts:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=list(dict.fromkeys(conflicts)),
                )
            cycle = self._dependency_cycle(list(by_id.values()))
            if cycle is not None:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=["patch creates dependency cycle: " + " -> ".join(cycle)],
                )
            stale_seeds = [
                node_id
                for node_id in changed
                if by_id[node_id].epistemic_status
                in {EpistemicStatus.STALE, EpistemicStatus.REFUTED}
            ]
            stale = self._propagate_staleness(
                by_id,
                stale_seeds,
                (
                    "dependency_refuted_requires_reaudit"
                    if any(
                        by_id[node_id].epistemic_status is EpistemicStatus.REFUTED
                        for node_id in stale_seeds
                    )
                    else "dependency_changed_requires_reaudit"
                ),
            )
            changed = list(dict.fromkeys([*changed, *stale]))
            result = self._commit_nodes_unlocked(
                state=state,
                all_nodes=list(by_id.values()),
                changed_node_ids=changed,
                run_id=patch.run_id,
                author=patch.agent_role,
                reason=f"Merge validated graph patch for task {patch.task_id}.",
                operation_id=operation_id,
                source_artifacts=[
                    *patch.evidence,
                    *(
                        artifact
                        for item in patch.create_nodes
                        for artifact in item.source_artifacts
                    ),
                    *(
                        artifact
                        for item in patch.update_nodes
                        for artifact in item.source_artifacts
                    ),
                ],
                stale_node_ids=stale,
            )
            # Preserve the worker-supplied base in the returned audit record. The
            # durable transaction also records the actual previous revision.
            return result.model_copy(
                update={
                    "base_revision": patch.base_graph_revision,
                    "created_node_ids": list(
                        dict.fromkeys([*result.created_node_ids, *created_ids])
                    ),
                }
            )

    @staticmethod
    def main_claim_id(problem_id: str) -> str:
        return _deterministic_id(NodeType.CLAIM, problem_id, "main-target")

    def record_compiled_problem(
        self,
        *,
        problem_id: str,
        run_id: str,
        compiled_problem: Mapping[str, Any],
    ) -> GraphMergeResult:
        """Materialize the exact target, claim contract, and verified source ledger."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes}
            problem = by_id.get(problem_id)
            if problem is None or problem.node_type is not NodeType.PROBLEM:
                raise GraphValidationError(f"problem node does not exist: {problem_id}")
            now = self._now()
            title = str(compiled_problem.get("title") or problem.title).strip()
            normalized_statement = str(
                compiled_problem.get("normalized_statement") or generated_section(problem.body)
            ).strip()
            raw_contract = compiled_problem.get("claim_contract", {})
            contract = _canonical_json(raw_contract)
            literature_status = str(compiled_problem.get("literature_status", "unknown"))
            literature_summary = compiled_problem.get("literature_resolution_summary")
            problem.title = title
            problem.body = replace_generated_section(
                problem.body,
                title,
                "## Exact main problem\n\n"
                + normalized_statement
                + "\n\n## Claim contract\n\n```json\n"
                + json.dumps(raw_contract, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n```\n\n## Prior-literature status\n\n"
                + f"`{literature_status}`"
                + (f"\n\n{literature_summary}" if literature_summary else ""),
            )
            problem.last_modified_run = run_id
            problem.updated_at = now
            problem.metadata["ascend_literature_status"] = literature_status
            target_id = self.main_claim_id(problem_id)
            target = GraphNode(
                ascend_id=target_id,
                node_type=NodeType.CLAIM,
                problem_id=problem_id,
                title=f"Main target — {title}",
                epistemic_status=EpistemicStatus.CONJECTURED,
                workflow_status=WorkflowStatus.ACTIVE,
                claim_type=ClaimType.THEOREM,
                created_in_run=run_id,
                last_modified_run=run_id,
                author_role="prompt-compiler",
                created_at=now,
                updated_at=now,
                body=new_generated_body(
                    f"Main target — {title}",
                    "## Exact statement\n\n"
                    + normalized_statement
                    + "\n\n## Scope and conventions\n\n"
                    + contract
                    + "\n\n## Current significance\n\n"
                    + "This is the exact claim governed by the compiled ASCEND claim contract.",
                ),
                tags=["ascend/claim", "ascend/theorem", "ascend/main-target"],
                relations=[
                    GraphEdge(
                        source_id=target_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                    )
                ],
                source_artifacts=[
                    f".ascend/runs/{run_id}/prompts/compiled_problem.json",
                    f".ascend/runs/{run_id}/prompts/compiled_research_prompt.md",
                ],
                metadata={"ascend_claim_contract_sha256": sha256_text(contract)},
            )
            existing_target = by_id.get(target_id)
            stale_nodes: list[str] = []
            if existing_target is not None:
                prior_statement_hash = statement_hash(existing_target)
                incoming_statement_hash = statement_hash(target)
                if prior_statement_hash != incoming_statement_hash:
                    target.statement_version = existing_target.statement_version + 1
                    target.epistemic_status = EpistemicStatus.STALE
                    target.invalidation_reasons = ["statement_changed_requires_reaudit"]
                    stale_nodes = self._propagate_staleness(
                        by_id,
                        [target_id],
                        "dependency_changed_requires_reaudit",
                    )
                else:
                    target.statement_version = existing_target.statement_version
            proposed: list[GraphNode] = [problem, target]
            source_nodes: list[GraphNode] = []
            raw_sources = compiled_problem.get("source_ledger", [])
            if isinstance(raw_sources, list):
                for raw_source in raw_sources:
                    if not isinstance(raw_source, Mapping):
                        continue
                    source_key = str(
                        raw_source.get("source_id")
                        or _canonical_json(raw_source.get("identifiers", []))
                        or raw_source.get("title")
                    )
                    source_id = _deterministic_id(NodeType.SOURCE, problem_id, source_key)
                    identifiers = [
                        str(item) for item in raw_source.get("identifiers", []) if str(item).strip()
                    ]
                    verified = bool(raw_source.get("verified", False))
                    source = GraphNode(
                        ascend_id=source_id,
                        node_type=NodeType.SOURCE,
                        problem_id=problem_id,
                        title=str(raw_source.get("title") or source_key),
                        epistemic_status=(
                            EpistemicStatus.AUDIT_PASSED if verified else EpistemicStatus.OPEN
                        ),
                        workflow_status=(
                            WorkflowStatus.COMPLETE if verified else WorkflowStatus.ACTIVE
                        ),
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="prompt-source-verifier",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            str(raw_source.get("title") or source_key),
                            "## Stable identifiers\n\n"
                            + ("\n".join(f"- `{item}`" for item in identifiers) or "_None._")
                            + "\n\n## Verification\n\n"
                            + str(
                                raw_source.get("verification_detail")
                                or ("Verified." if verified else "Not independently verified.")
                            ),
                        ),
                        tags=[
                            "ascend/source",
                            "ascend/source-verified" if verified else "ascend/source-open",
                        ],
                        source_artifacts=[f".ascend/runs/{run_id}/prompts/source_ledger.json"],
                        metadata={
                            "ascend_source_id": source_key,
                            "ascend_identifiers": identifiers,
                            "ascend_verified": verified,
                        },
                    )
                    source_nodes.append(source)
                    target.relations.append(
                        GraphEdge(
                            source_id=target_id,
                            relation=RelationType.CITES,
                            target_id=source_id,
                        )
                    )
            proposed.extend(source_nodes)
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed,
                run_id=run_id,
                author="prompt-compiler",
                reason="Compile the exact target and source ledger into the persistent graph.",
                operation_id=f"prompt-compiled:{run_id}",
                source_artifacts=[f".ascend/runs/{run_id}/prompts/compiled_problem.json"],
                stale_node_ids=[target_id, *stale_nodes] if stale_nodes else (),
            )

    def record_assignment_tasks(
        self,
        *,
        problem_id: str,
        run_id: str,
        decision_id: int,
        assignments: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, str], dict[str, GraphContextSlice], str]:
        """Create graph-scoped task nodes for one coordinator decision."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes_list}
            if problem_id not in by_id:
                raise GraphValidationError(f"problem node does not exist: {problem_id}")
            run_node_id = _deterministic_id(NodeType.RUN, problem_id, run_id)
            target_default = (
                self.main_claim_id(problem_id)
                if self.main_claim_id(problem_id) in by_id
                else problem_id
            )
            now = self._now()
            proposed: list[GraphNode] = []
            assignment_to_task: dict[str, str] = {}
            for assignment in assignments:
                assignment_id = str(assignment.get("id") or "").strip()
                if not assignment_id:
                    raise GraphValidationError("research assignment has no stable ID")
                task_id = _deterministic_id(NodeType.TASK, problem_id, run_id, assignment_id)
                raw_targets = assignment.get("target_node_ids", [])
                target_ids = (
                    [str(item) for item in raw_targets] if isinstance(raw_targets, list) else []
                )
                target_ids = [item for item in target_ids if item in by_id] or [target_default]
                task_text = str(assignment.get("task") or "Research assignment").strip()
                expected = str(assignment.get("expected_output") or "Concrete mathematical result")
                stop = str(
                    assignment.get("stopping_condition")
                    or "Return concrete content or an exact obstruction."
                )
                relations = [
                    *(
                        GraphEdge(
                            source_id=task_id,
                            relation=RelationType.TARGETS,
                            target_id=target_id,
                        )
                        for target_id in target_ids
                    ),
                    GraphEdge(
                        source_id=task_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=run_node_id,
                    ),
                ]
                proposed.append(
                    GraphNode(
                        ascend_id=task_id,
                        node_type=NodeType.TASK,
                        problem_id=problem_id,
                        title=f"Task {assignment_id}: {task_text[:72]}",
                        epistemic_status=EpistemicStatus.OPEN,
                        workflow_status=WorkflowStatus.QUEUED,
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="research-coordinator",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Task {assignment_id}",
                            "## Exact requested task\n\n"
                            + task_text
                            + "\n\n## Expected output\n\n"
                            + expected
                            + "\n\n## Stopping condition\n\n"
                            + stop
                            + "\n\n## Approach family\n\n"
                            + str(assignment.get("approach_family") or "unspecified"),
                        ),
                        tags=["ascend/task", "ascend/task-active"],
                        relations=relations,
                        source_artifacts=[
                            f".ascend/runs/{run_id}/research/coordinator/decisions/"
                            f"{decision_id:08d}.json"
                        ],
                        metadata={
                            "ascend_assignment_id": assignment_id,
                            "ascend_decision_id": decision_id,
                            "ascend_priority": "high"
                            if "audit" in task_text.casefold()
                            else "normal",
                        },
                    )
                )
                assignment_to_task[assignment_id] = task_id
            if proposed:
                self._upsert_generated_nodes_unlocked(
                    state=state,
                    nodes=by_id,
                    proposed=proposed,
                    run_id=run_id,
                    author="research-coordinator",
                    reason=f"Create graph-scoped tasks from coordinator decision {decision_id}.",
                    operation_id=f"coordinator-tasks:{run_id}:{decision_id}",
                )
                state = self._load_state_unlocked()
                nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            contexts = {
                assignment_id: self._context_slice_unlocked(
                    state,
                    nodes_list,
                    problem_id=problem_id,
                    task_id=task_id,
                )
                for assignment_id, task_id in assignment_to_task.items()
            }
            return assignment_to_task, contexts, state.revision

    def coordinator_memory(self, problem_id: str) -> dict[str, object]:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            frontier = self._frontier_unlocked(state, nodes, problem_id)
            return {
                "graph_revision": state.revision,
                "problem_id": problem_id,
                "frontier": frontier.model_dump(mode="json"),
                "instruction": (
                    "Use stable node IDs in target_node_ids. Do not reopen a blocked or "
                    "refuted route unless new evidence is stated explicitly."
                ),
            }

    def integrate_worker_report(
        self,
        *,
        problem_id: str,
        run_id: str,
        assignment: Mapping[str, Any],
        task_id: str,
        report: Mapping[str, Any],
        proposed_patch: GraphPatch | None,
        source_artifact: str,
        operation_id: str,
    ) -> GraphMergeResult:
        """Merge an agent patch, then always preserve a distilled worker summary."""

        proposal_issues: list[str] = []
        proposal_result: GraphMergeResult | None = None
        if proposed_patch is not None:
            if proposed_patch.run_id != run_id:
                proposal_issues.append("worker graph patch run_id does not match its run")
            elif proposed_patch.task_id != task_id:
                proposal_issues.append("worker graph patch task_id does not match its assignment")
            else:
                proposal_result = self.merge_patch(
                    proposed_patch,
                    problem_id=problem_id,
                    operation_id=f"{operation_id}:proposal",
                )
                if not proposal_result.committed:
                    proposal_issues.extend(proposal_result.issues)
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            if operation_id in state.processed_operations:
                previous = state.processed_operations[operation_id]
                return previous.model_copy(update={"status": "already_applied"})
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes_list}
            task = by_id.get(task_id)
            if task is None or task.node_type is not NodeType.TASK:
                raise GraphValidationError(f"worker graph task is missing: {task_id}")
            now = self._now()
            assignment_id = str(assignment.get("id") or report.get("assignment_id") or "unknown")
            family = str(assignment.get("approach_family") or "unspecified")
            mechanism = str(report.get("mechanism") or assignment.get("task") or family)
            status = str(report.get("status") or "progress")
            formal_results = [
                str(item) for item in report.get("formal_results", []) if str(item).strip()
            ]
            exact_gap = str(report.get("exact_gap") or "").strip()
            counterexamples = [
                str(item) for item in report.get("counterexamples", []) if str(item).strip()
            ]
            dependencies = [
                str(item) for item in report.get("dependencies", []) if str(item).strip()
            ]
            assumptions = [str(item) for item in report.get("assumptions", []) if str(item).strip()]
            classification = {
                "blocked": "blocked_local_gap",
                "refuted": "refuted_by_counterexample",
                "candidate_complete": "candidate",
            }.get(status, "partial_progress")
            approach_id = _deterministic_id(NodeType.APPROACH, problem_id, family.casefold())
            approach_workflow = {
                "blocked": WorkflowStatus.BLOCKED,
                "refuted": WorkflowStatus.ABANDONED,
                "candidate_complete": WorkflowStatus.COMPLETE,
            }.get(status, WorkflowStatus.ACTIVE)
            approach_epistemic = (
                EpistemicStatus.REFUTED if status == "refuted" else EpistemicStatus.CANDIDATE
            )
            partial = "\n".join(f"- {item}" for item in formal_results) or "_None established._"
            failure = exact_gap or (
                "The route was refuted." if status == "refuted" else "No exact failure recorded."
            )
            reopen = (
                "Reopen only if a new mechanism resolves the exact gap or defeats the recorded "
                "counterexample."
                if status in {"blocked", "refuted"}
                else "Continue when a coordinator task targets the remaining obligation."
            )
            approach = GraphNode(
                ascend_id=approach_id,
                node_type=NodeType.APPROACH,
                problem_id=problem_id,
                title=f"Approach: {family}",
                epistemic_status=approach_epistemic,
                workflow_status=approach_workflow,
                created_in_run=run_id,
                last_modified_run=run_id,
                author_role="research-worker",
                created_at=now,
                updated_at=now,
                body=new_generated_body(
                    f"Approach: {family}",
                    "## Exact route attempted\n\n"
                    + mechanism
                    + "\n\n## Proposed invariant or mechanism\n\n"
                    + mechanism
                    + "\n\n## Strongest valid partial result\n\n"
                    + partial
                    + "\n\n## Exact failure point\n\n"
                    + failure
                    + "\n\n## Classification\n\n"
                    + f"`{classification}`"
                    + "\n\n## Reopen condition\n\n"
                    + reopen,
                ),
                tags=["ascend/approach", f"ascend/{classification}"],
                relations=[
                    GraphEdge(
                        source_id=approach_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                    ),
                    GraphEdge(
                        source_id=approach_id,
                        relation=RelationType.RELATED_TO,
                        target_id=task_id,
                    ),
                ],
                source_artifacts=[source_artifact],
                evidence=[*formal_results, *counterexamples],
                metadata={
                    "ascend_assignment_ids": [assignment_id],
                    "ascend_assumptions": assumptions,
                    "ascend_dependencies": dependencies,
                    "ascend_worker_status": status,
                },
            )
            proposed_nodes: list[GraphNode] = [approach]
            result_claim_ids: list[str] = []
            for index, result_text in enumerate(formal_results, start=1):
                claim_id = _deterministic_id(
                    NodeType.CLAIM, problem_id, run_id, assignment_id, str(index)
                )
                result_claim_ids.append(claim_id)
                proposed_nodes.append(
                    GraphNode(
                        ascend_id=claim_id,
                        node_type=NodeType.CLAIM,
                        problem_id=problem_id,
                        title=f"Result from {assignment_id} #{index}",
                        epistemic_status=EpistemicStatus.CANDIDATE,
                        workflow_status=WorkflowStatus.ACTIVE,
                        claim_type=ClaimType.LEMMA,
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="research-worker",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Result from {assignment_id} #{index}",
                            "## Exact statement\n\n"
                            + result_text
                            + "\n\n## Scope and conventions\n\n"
                            + ("\n".join(f"- {item}" for item in assumptions) or "_None recorded._")
                            + "\n\n## Current significance\n\n"
                            + f"Candidate result distilled from task {task_id}.",
                        ),
                        tags=["ascend/claim", "ascend/lemma", "ascend/candidate"],
                        relations=[
                            GraphEdge(
                                source_id=claim_id,
                                relation=RelationType.CREATED_DURING,
                                target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                            ),
                            GraphEdge(
                                source_id=claim_id,
                                relation=RelationType.MOTIVATES,
                                target_id=self.main_claim_id(problem_id),
                            ),
                        ],
                        source_artifacts=[source_artifact],
                        evidence=[result_text],
                    )
                )
            proof_content = str(report.get("proof_content") or "").strip()
            if proof_content and (formal_results or status == "candidate_complete"):
                proof_id = _deterministic_id(NodeType.PROOF, problem_id, run_id, assignment_id)
                proof_targets = result_claim_ids or [self.main_claim_id(problem_id)]
                proposed_nodes.append(
                    GraphNode(
                        ascend_id=proof_id,
                        node_type=NodeType.PROOF,
                        problem_id=problem_id,
                        title=f"Candidate proof from {assignment_id}",
                        epistemic_status=EpistemicStatus.CANDIDATE,
                        workflow_status=(
                            WorkflowStatus.COMPLETE
                            if status == "candidate_complete"
                            else WorkflowStatus.ACTIVE
                        ),
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="research-worker",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Candidate proof from {assignment_id}",
                            "## Proof content\n\n"
                            + proof_content
                            + "\n\n## Exact gap\n\n"
                            + (
                                exact_gap
                                or "_No gap declared by the worker; independent audit required._"
                            ),
                        ),
                        tags=["ascend/proof", "ascend/candidate"],
                        relations=[
                            *(
                                GraphEdge(
                                    source_id=proof_id,
                                    relation=RelationType.PROVES,
                                    target_id=claim_id,
                                )
                                for claim_id in proof_targets
                            ),
                            GraphEdge(
                                source_id=proof_id,
                                relation=RelationType.CREATED_DURING,
                                target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                            ),
                        ],
                        source_artifacts=[source_artifact],
                    )
                )
            for index, counterexample in enumerate(counterexamples, start=1):
                counterexample_id = _deterministic_id(
                    NodeType.COUNTEREXAMPLE, problem_id, run_id, assignment_id, str(index)
                )
                relation = RelationType.REFUTES if status == "refuted" else RelationType.RELATED_TO
                target = self.main_claim_id(problem_id) if status == "refuted" else approach_id
                proposed_nodes.append(
                    GraphNode(
                        ascend_id=counterexample_id,
                        node_type=NodeType.COUNTEREXAMPLE,
                        problem_id=problem_id,
                        title=f"Counterexample from {assignment_id} #{index}",
                        # Worker-declared counterexamples remain candidates until an
                        # independent audit verifies them.
                        epistemic_status=EpistemicStatus.CANDIDATE,
                        workflow_status=WorkflowStatus.COMPLETE,
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="research-worker",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Counterexample from {assignment_id} #{index}",
                            "## Explicit counterexample\n\n" + counterexample,
                        ),
                        tags=["ascend/counterexample"],
                        relations=[
                            GraphEdge(
                                source_id=counterexample_id,
                                relation=relation,
                                target_id=target,
                            )
                        ],
                        source_artifacts=[source_artifact],
                    )
                )
            raw_sources = report.get("sources", [])
            if isinstance(raw_sources, list):
                for raw_source in raw_sources:
                    if not isinstance(raw_source, Mapping):
                        continue
                    key = str(raw_source.get("source_id") or raw_source.get("title") or "source")
                    source_id = _deterministic_id(NodeType.SOURCE, problem_id, key)
                    verified = bool(raw_source.get("verified", False))
                    proposed_nodes.append(
                        GraphNode(
                            ascend_id=source_id,
                            node_type=NodeType.SOURCE,
                            problem_id=problem_id,
                            title=str(raw_source.get("title") or key),
                            epistemic_status=(
                                EpistemicStatus.AUDIT_PASSED if verified else EpistemicStatus.OPEN
                            ),
                            workflow_status=(
                                WorkflowStatus.COMPLETE if verified else WorkflowStatus.ACTIVE
                            ),
                            created_in_run=run_id,
                            last_modified_run=run_id,
                            author_role="research-source-verifier",
                            created_at=now,
                            updated_at=now,
                            body=new_generated_body(
                                str(raw_source.get("title") or key),
                                "## Source record\n\n```json\n"
                                + json.dumps(
                                    raw_source, ensure_ascii=False, indent=2, sort_keys=True
                                )
                                + "\n```",
                            ),
                            tags=["ascend/source"],
                            source_artifacts=[source_artifact],
                            metadata={
                                "ascend_source_id": key,
                                "ascend_verified": verified,
                            },
                        )
                    )
                    approach.relations.append(
                        GraphEdge(
                            source_id=approach_id,
                            relation=RelationType.CITES,
                            target_id=source_id,
                        )
                    )
            task.workflow_status = (
                WorkflowStatus.BLOCKED if status == "blocked" else WorkflowStatus.COMPLETE
            )
            task.epistemic_status = (
                EpistemicStatus.REFUTED if status == "refuted" else EpistemicStatus.CANDIDATE
            )
            task.updated_at = now
            task.last_modified_run = run_id
            task.author_role = "research-worker"
            task.body = replace_generated_section(
                task.body,
                task.title,
                generated_section(task.body)
                + "\n\n## Worker outcome\n\n"
                + f"`{status}`\n\n"
                + (partial if formal_results else failure),
            )
            task.relations = _unique_edges(
                [
                    *task.relations,
                    GraphEdge(
                        source_id=task_id,
                        relation=RelationType.RELATED_TO,
                        target_id=approach_id,
                    ),
                ]
            )
            proposed_nodes.append(task)
            auto_result = self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed_nodes,
                run_id=run_id,
                author="research-worker",
                reason=f"Distill worker report {assignment_id} into reusable mathematical memory.",
                operation_id=operation_id,
                source_artifacts=[source_artifact],
            )
            combined_issues = list(
                dict.fromkeys(
                    [
                        *proposal_issues,
                        *(proposal_result.issues if proposal_result is not None else []),
                        *auto_result.issues,
                    ]
                )
            )
            return auto_result.model_copy(
                update={
                    "status": "partially_merged" if combined_issues else auto_result.status,
                    "issues": combined_issues,
                    "created_node_ids": list(
                        dict.fromkeys(
                            [
                                *(proposal_result.created_node_ids if proposal_result else []),
                                *auto_result.created_node_ids,
                            ]
                        )
                    ),
                    "updated_node_ids": list(
                        dict.fromkeys(
                            [
                                *(proposal_result.updated_node_ids if proposal_result else []),
                                *auto_result.updated_node_ids,
                            ]
                        )
                    ),
                }
            )

    def record_research_result(
        self,
        *,
        problem_id: str,
        run_id: str,
        research_result: Mapping[str, Any],
    ) -> GraphMergeResult:
        """Bind candidate proofs and independent audits to separate graph nodes."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes_list}
            target_id = self.main_claim_id(problem_id)
            target = by_id.get(target_id)
            if target is None:
                raise GraphValidationError("compiled main claim is missing from the graph")
            now = self._now()
            outcome = str(research_result.get("outcome") or "partial")
            candidate = research_result.get("candidate")
            candidate_map = candidate if isinstance(candidate, Mapping) else None
            accepted = outcome == "accepted" and bool(research_result.get("acceptance_gate"))
            proposed: list[GraphNode] = []
            proof_id: str | None = None
            if candidate_map is not None:
                exact_theorem = str(candidate_map.get("exact_theorem") or "").strip()
                full_proof = str(candidate_map.get("full_proof") or "").strip()
                if exact_theorem:
                    old_statement = exact_statement(target.body)
                    if exact_theorem != old_statement:
                        target.statement_version += 1
                    target.body = replace_generated_section(
                        target.body,
                        target.title,
                        "## Exact statement\n\n"
                        + exact_theorem
                        + "\n\n## Scope and conventions\n\n"
                        + "Frozen by the accepted candidate package and the original "
                        "claim contract."
                        + "\n\n## Current significance\n\n"
                        + (
                            "Every mandatory independent research audit passed."
                            if accepted
                            else (
                                "A candidate package exists but has not passed the acceptance gate."
                            )
                        ),
                    )
                target.epistemic_status = (
                    EpistemicStatus.AUDIT_PASSED if accepted else EpistemicStatus.CANDIDATE
                )
                target.workflow_status = (
                    WorkflowStatus.COMPLETE if accepted else WorkflowStatus.ACTIVE
                )
                target.last_modified_run = run_id
                target.updated_at = now
                target.author_role = "research-acceptance-gate"
                target.source_artifacts = list(
                    dict.fromkeys(
                        [
                            *target.source_artifacts,
                            f".ascend/runs/{run_id}/research/candidate/package.json",
                            f".ascend/runs/{run_id}/research/verdict.json",
                        ]
                    )
                )
                proof_id = _deterministic_id(
                    NodeType.PROOF, problem_id, run_id, "accepted-candidate"
                )
                proposed.append(
                    GraphNode(
                        ascend_id=proof_id,
                        node_type=NodeType.PROOF,
                        problem_id=problem_id,
                        title="Accepted candidate proof" if accepted else "Audited candidate proof",
                        epistemic_status=(
                            EpistemicStatus.AUDIT_PASSED if accepted else EpistemicStatus.CANDIDATE
                        ),
                        workflow_status=(
                            WorkflowStatus.COMPLETE if accepted else WorkflowStatus.BLOCKED
                        ),
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="candidate-packager",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            "Accepted candidate proof" if accepted else "Audited candidate proof",
                            "## Theorem\n\n"
                            + str(candidate_map.get("exact_theorem") or "")
                            + "\n\n## Full proof\n\n"
                            + full_proof
                            + "\n\n## Unresolved items\n\n"
                            + (
                                "\n".join(
                                    f"- {item}"
                                    for item in candidate_map.get("unresolved_items", [])
                                )
                                or "_None._"
                            ),
                        ),
                        tags=[
                            "ascend/proof",
                            "ascend/audit-passed" if accepted else "ascend/candidate",
                        ],
                        relations=[
                            GraphEdge(
                                source_id=proof_id,
                                relation=RelationType.PROVES,
                                target_id=target_id,
                            ),
                            GraphEdge(
                                source_id=proof_id,
                                relation=RelationType.CREATED_DURING,
                                target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                            ),
                        ],
                        source_artifacts=[
                            f".ascend/runs/{run_id}/research/candidate/package.json",
                            f".ascend/runs/{run_id}/research/candidate/proof.md",
                        ],
                        metadata={
                            "ascend_quantitative_or_algorithmic": bool(
                                candidate_map.get("quantitative_or_algorithmic", False)
                            ),
                            "ascend_acceptance_gate_passed": accepted,
                        },
                    )
                )
            proposed.append(target)
            audits = research_result.get("audits", {})
            if isinstance(audits, Mapping):
                for name, raw_audit in audits.items():
                    if not isinstance(raw_audit, Mapping):
                        continue
                    verdict = str(raw_audit.get("verdict") or "fail")
                    audit_id = _deterministic_id(NodeType.AUDIT, problem_id, run_id, str(name))
                    audit_passed = verdict == "pass"
                    target_of_audit = proof_id or target_id
                    proposed.append(
                        GraphNode(
                            ascend_id=audit_id,
                            node_type=NodeType.AUDIT,
                            problem_id=problem_id,
                            title=f"{str(name).title()} research audit",
                            epistemic_status=(
                                EpistemicStatus.AUDIT_PASSED
                                if audit_passed
                                else EpistemicStatus.INCONSISTENT
                            ),
                            workflow_status=WorkflowStatus.COMPLETE,
                            created_in_run=run_id,
                            last_modified_run=run_id,
                            author_role="research-auditor",
                            created_at=now,
                            updated_at=now,
                            body=new_generated_body(
                                f"{str(name).title()} research audit",
                                "## Verdict\n\n"
                                + f"`{verdict}`"
                                + "\n\n## Issues\n\n"
                                + (
                                    "\n".join(
                                        "- " + str(item.get("description") or item)
                                        for item in raw_audit.get("issues", [])
                                    )
                                    or "_None._"
                                )
                                + "\n\n## Unresolved obligations\n\n"
                                + (
                                    "\n".join(
                                        f"- {item}"
                                        for item in raw_audit.get("unresolved_obligations", [])
                                    )
                                    or "_None._"
                                ),
                            ),
                            tags=["ascend/audit", f"ascend/audit-{verdict}"],
                            relations=[
                                GraphEdge(
                                    source_id=audit_id,
                                    relation=RelationType.AUDITS,
                                    target_id=target_of_audit,
                                ),
                                GraphEdge(
                                    source_id=audit_id,
                                    relation=RelationType.CREATED_DURING,
                                    target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                                ),
                            ],
                            source_artifacts=[f".ascend/runs/{run_id}/research/audits/{name}.json"],
                            metadata={"ascend_audit_verdict": verdict},
                        )
                    )
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed,
                run_id=run_id,
                author="research-acceptance-gate",
                reason=f"Record research outcome {outcome} with separate proof and audit nodes.",
                operation_id=f"research-result:{run_id}",
                source_artifacts=[f".ascend/runs/{run_id}/research/result.json"],
            )

    def manuscript_context(self, problem_id: str) -> dict[str, object]:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=False)
            accepted = [
                node
                for node in nodes
                if node.problem_id == problem_id
                and node.node_type
                in {
                    NodeType.DEFINITION,
                    NodeType.CLAIM,
                    NodeType.PROOF,
                    NodeType.SOURCE,
                    NodeType.AUDIT,
                }
                and (
                    node.node_type in {NodeType.DEFINITION, NodeType.SOURCE}
                    or node.epistemic_status
                    in {
                        EpistemicStatus.PROVED_INFORMALLY,
                        EpistemicStatus.AUDIT_PASSED,
                        EpistemicStatus.LEAN_VERIFIED,
                    }
                )
            ]
            return {
                "graph_revision": state.revision,
                "problem_id": problem_id,
                "accepted_nodes": [
                    {
                        "node": _node_summary(node).model_dump(mode="json"),
                        "content": generated_section(node.body),
                        "relations": [edge.model_dump(mode="json") for edge in node.relations],
                    }
                    for node in accepted[:80]
                ],
                "instruction": (
                    "Use only accepted claim/proof nodes for theorem content. Preserve dependency "
                    "order and return manuscript mappings for durable graph recording."
                ),
            }

    def record_manuscript_result(
        self,
        *,
        problem_id: str,
        run_id: str,
        manuscript_result: Mapping[str, Any],
    ) -> GraphMergeResult:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes_list}
            now = self._now()
            outcome = str(manuscript_result.get("outcome") or "unknown")
            draft = manuscript_result.get("draft", {})
            draft_map = draft if isinstance(draft, Mapping) else {}
            claims = draft_map.get("claims", [])
            proposed: list[GraphNode] = []
            target = by_id.get(self.main_claim_id(problem_id))
            if target is not None:
                target.manuscript_mappings = list(
                    dict.fromkeys(
                        [
                            *target.manuscript_mappings,
                            *(
                                f"{target.ascend_id} -> manuscript claim {index}"
                                for index, _ in enumerate(
                                    claims if isinstance(claims, list) else [], start=1
                                )
                            ),
                        ]
                    )
                )
                target.updated_at = now
                target.last_modified_run = run_id
                proposed.append(target)
            artifact_specs = (
                ("paper.tex", "LaTeX manuscript source"),
                ("references.bib", "Verified bibliography"),
                ("paper.pdf", "Compiled manuscript PDF"),
                ("bibliography_audit.json", "Bibliography verification audit"),
            )
            for filename, title in artifact_specs:
                artifact_id = _deterministic_id(NodeType.ARTIFACT, problem_id, run_id, filename)
                proposed.append(
                    GraphNode(
                        ascend_id=artifact_id,
                        node_type=NodeType.ARTIFACT,
                        problem_id=problem_id,
                        title=title,
                        epistemic_status=(
                            EpistemicStatus.AUDIT_PASSED
                            if outcome == "compiled"
                            else EpistemicStatus.OPEN
                        ),
                        workflow_status=(
                            WorkflowStatus.COMPLETE
                            if outcome == "compiled"
                            else WorkflowStatus.BLOCKED
                        ),
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="manuscript-stage",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            title,
                            "## Artifact\n\n"
                            + f"`.ascend/runs/{run_id}/manuscript/{filename}`"
                            + "\n\n## Manuscript outcome\n\n"
                            + f"`{outcome}`",
                        ),
                        tags=["ascend/artifact", "ascend/manuscript"],
                        relations=[
                            GraphEdge(
                                source_id=artifact_id,
                                relation=RelationType.RELATED_TO,
                                target_id=self.main_claim_id(problem_id),
                            ),
                            GraphEdge(
                                source_id=artifact_id,
                                relation=RelationType.CREATED_DURING,
                                target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                            ),
                        ],
                        source_artifacts=[f".ascend/runs/{run_id}/manuscript/{filename}"],
                        metadata={"ascend_manuscript_outcome": outcome},
                    )
                )
            bibliography = manuscript_result.get("bibliography_audit")
            if isinstance(bibliography, Mapping):
                entries = bibliography.get("entries", [])
                if isinstance(entries, list):
                    source_nodes = [
                        node for node in by_id.values() if node.node_type is NodeType.SOURCE
                    ]
                    for entry in entries:
                        if not isinstance(entry, Mapping):
                            continue
                        key = str(entry.get("citation_key") or "")
                        for source in source_nodes:
                            if key and (
                                key == source.metadata.get("ascend_source_id")
                                or key.casefold() in source.title.casefold()
                            ):
                                source.manuscript_mappings = list(
                                    dict.fromkeys(
                                        [
                                            *source.manuscript_mappings,
                                            f"{source.ascend_id} -> {key}",
                                        ]
                                    )
                                )
                                source.metadata["ascend_bibtex_key"] = key
                                source.updated_at = now
                                source.last_modified_run = run_id
                                proposed.append(source)
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed,
                run_id=run_id,
                author="manuscript-stage",
                reason=f"Record manuscript mappings and artifact nodes for outcome {outcome}.",
                operation_id=f"manuscript-result:{run_id}",
                source_artifacts=[f".ascend/runs/{run_id}/manuscript/result.json"],
            )

    def formalization_context(self, problem_id: str) -> dict[str, object]:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=False)
            selected = [
                node
                for node in nodes
                if node.problem_id == problem_id
                and node.node_type
                in {NodeType.DEFINITION, NodeType.CLAIM, NodeType.PROOF, NodeType.FORMALIZATION}
                and node.epistemic_status
                in {
                    EpistemicStatus.PROVED_INFORMALLY,
                    EpistemicStatus.AUDIT_PASSED,
                    EpistemicStatus.LEAN_VERIFIED,
                }
            ]
            return {
                "graph_revision": state.revision,
                "problem_id": problem_id,
                "statement_nodes": [
                    {
                        "node": _node_summary(node).model_dump(mode="json"),
                        "content": generated_section(node.body),
                        "content_hash": node.content_hash,
                    }
                    for node in selected[:60]
                ],
            }

    def record_lean_result(
        self,
        *,
        problem_id: str,
        run_id: str,
        lean_result: Mapping[str, Any],
        lean_toolchain: str,
        mathlib_revision: str,
        source_file_hash: str | None,
        axiom_report_hash: str | None,
    ) -> GraphMergeResult:
        """Attach formalization to one exact statement version and build record."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes_list}
            claim_id = self.main_claim_id(problem_id)
            claim = by_id.get(claim_id)
            if claim is None:
                raise GraphValidationError("main claim is missing before Lean graph integration")
            now = self._now()
            outcome = str(lean_result.get("outcome") or "LEAN_FAILED")
            verification = lean_result.get("verification")
            verification_map = verification if isinstance(verification, Mapping) else {}
            alignment = lean_result.get("alignment")
            alignment_map = alignment if isinstance(alignment, Mapping) else {}
            statement = lean_result.get("statement_draft")
            statement_map = statement if isinstance(statement, Mapping) else {}
            verified = (
                outcome in {"LEAN_VERIFIED", "LEAN_VERIFIED_WITH_APPROVED_AXIOMS"}
                and bool(verification_map.get("passed", False))
                and str(verification_map.get("statement_hash_expected") or "")
                == str(verification_map.get("statement_hash_actual") or "")
                and str(alignment_map.get("status") or "") == "aligned"
            )
            formalization_id = _deterministic_id(
                NodeType.FORMALIZATION,
                problem_id,
                claim_id,
                str(claim.statement_version),
                run_id,
            )
            theorem_name = str(statement_map.get("theorem_name") or "unknown")
            statement_digest = str(
                lean_result.get("approved_statement_hash")
                or verification_map.get("statement_hash_expected")
                or ""
            )
            formalization = GraphNode(
                ascend_id=formalization_id,
                node_type=NodeType.FORMALIZATION,
                problem_id=problem_id,
                title=f"Lean formalization of {claim.title}",
                epistemic_status=(
                    EpistemicStatus.LEAN_VERIFIED
                    if verified
                    else EpistemicStatus.CANDIDATE
                    if statement_map
                    else EpistemicStatus.OPEN
                ),
                workflow_status=(WorkflowStatus.COMPLETE if verified else WorkflowStatus.BLOCKED),
                created_in_run=run_id,
                last_modified_run=run_id,
                author_role="deterministic-lean-verifier" if verified else "lean-stage",
                created_at=now,
                updated_at=now,
                body=new_generated_body(
                    f"Lean formalization of {claim.title}",
                    "## Claim linkage\n\n"
                    + f"- Claim: {wikilink_for(claim)}\n"
                    + f"- Statement version: `{claim.statement_version}`\n"
                    + f"- Statement hash: `{statement_digest or 'unknown'}`\n"
                    + "\n## Lean declaration\n\n"
                    + f"`{theorem_name}`\n\n"
                    + "## Build result\n\n"
                    + f"`{outcome}`\n\n"
                    + "## Axiom report\n\n"
                    + (
                        "\n".join(f"- `{item}`" for item in verification_map.get("used_axioms", []))
                        or "_No axioms reported._"
                    ),
                ),
                tags=[
                    "ascend/formalization",
                    "ascend/lean-verified" if verified else "ascend/lean-open",
                ],
                relations=[
                    GraphEdge(
                        source_id=formalization_id,
                        relation=RelationType.FORMALIZES,
                        target_id=claim_id,
                    ),
                    GraphEdge(
                        source_id=formalization_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                    ),
                ],
                source_artifacts=[
                    f".ascend/runs/{run_id}/lean/challenge.lean",
                    f".ascend/runs/{run_id}/lean/Main.lean",
                    f".ascend/runs/{run_id}/lean/build.log",
                    f".ascend/runs/{run_id}/lean/axioms.txt",
                ],
                metadata={
                    "ascend_claim_id": claim_id,
                    "ascend_statement_version": claim.statement_version,
                    "ascend_statement_hash": statement_digest,
                    "ascend_lean_declaration": theorem_name,
                    "ascend_source_file_hash": source_file_hash,
                    "ascend_lean_version": lean_toolchain,
                    "ascend_mathlib_revision": mathlib_revision,
                    "ascend_build_result": outcome,
                    "ascend_axiom_report_hash": axiom_report_hash,
                    "ascend_deterministic_verification_passed": verified,
                },
            )
            if verified:
                claim.epistemic_status = EpistemicStatus.LEAN_VERIFIED
                claim.workflow_status = WorkflowStatus.COMPLETE
                claim.invalidation_reasons = []
                claim.updated_at = now
                claim.last_modified_run = run_id
                claim.author_role = "deterministic-lean-verifier"
                claim.metadata.update(
                    {
                        "ascend_lean_statement_version": claim.statement_version,
                        "ascend_lean_statement_hash": statement_digest,
                        "ascend_lean_formalization_id": formalization_id,
                    }
                )
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=[claim, formalization],
                run_id=run_id,
                author="deterministic-lean-verifier" if verified else "lean-stage",
                reason=f"Attach Lean outcome {outcome} to exact claim statement version.",
                operation_id=f"lean-result:{run_id}",
                source_artifacts=[f".ascend/runs/{run_id}/lean/result.json"],
            )

    def record_run_status(
        self,
        *,
        problem_id: str,
        run_id: str,
        scientific_status: str,
        strongest_result: str,
        unresolved_obligations: Sequence[str],
        complete: bool,
    ) -> GraphMergeResult:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.ascend_id: node for node in nodes_list}
            run_node_id = _deterministic_id(NodeType.RUN, problem_id, run_id)
            run_node = by_id.get(run_node_id)
            if run_node is None:
                raise GraphValidationError(f"run node is missing: {run_node_id}")
            now = self._now()
            run_node.workflow_status = (
                WorkflowStatus.COMPLETE if complete else WorkflowStatus.IN_PROGRESS
            )
            run_node.epistemic_status = (
                EpistemicStatus.AUDIT_PASSED
                if scientific_status
                in {
                    "RESEARCH_ACCEPTED_FOR_MANUSCRIPT",
                    "LEAN_VERIFIED",
                    "LEAN_VERIFIED_WITH_APPROVED_AXIOMS",
                }
                else EpistemicStatus.OPEN
            )
            run_node.updated_at = now
            run_node.last_modified_run = run_id
            run_node.body = replace_generated_section(
                run_node.body,
                run_node.title,
                "## Run summary\n\n"
                + f"Scientific status: `{scientific_status}`\n\n"
                + "## Strongest result\n\n"
                + (strongest_result or "_No complete result established._")
                + "\n\n## Unresolved obligations\n\n"
                + ("\n".join(f"- {item}" for item in unresolved_obligations) or "_None._")
                + "\n\n## Run artifacts\n\n"
                + f"- `.ascend/runs/{run_id}/`",
            )
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=[run_node],
                run_id=run_id,
                author="ascend-workflow",
                reason=f"Record run status {scientific_status} in persistent graph memory.",
                operation_id=f"run-status:{run_id}:{scientific_status}:{int(complete)}",
                source_artifacts=[f".ascend/runs/{run_id}/state.json"],
            )


__all__ = [
    "GRAPH_DIRECTORIES",
    "GRAPH_SCHEMA_VERSION",
    "GRAPH_VAULT_RELATIVE",
    "GraphConflictError",
    "GraphNotInitializedError",
    "GraphValidationError",
    "KnowledgeGraph",
    "KnowledgeGraphError",
]
