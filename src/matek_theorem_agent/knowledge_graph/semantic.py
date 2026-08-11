"""Graph-only, title-based research memory and semantic agent contracts.

This module is intentionally small and self-contained.  Markdown notes are the only
durable authority; the SQLite database is a disposable search aid.  Models exchange
descriptive mathematical titles and prose, never graph IDs, revisions, patches, or
filesystem paths.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..workspace import atomic_write_text, ensure_path_confined
from .markdown import GraphMarkdownError, format_flat_frontmatter, parse_flat_frontmatter


class SemanticGraphError(RuntimeError):
    """Base class for graph-only storage failures."""


class SemanticGraphFilesystemError(SemanticGraphError):
    """An unrecoverable failure while reading or committing Markdown."""


class SemanticTitleError(SemanticGraphError):
    """A descriptive title is absent, ambiguous, or unsafe."""


class SemanticFindingType(StrEnum):
    DEFINITION = "definition"
    THEOREM = "theorem"
    LEMMA = "lemma"
    PARTIAL_PROGRESS = "partial_progress"
    FAILED_APPROACH = "failed_approach"
    COUNTEREXAMPLE = "counterexample"
    COMPUTATION = "computation"
    SOURCE = "source"
    TASK = "task"


class SemanticFindingStatus(StrEnum):
    INCOMPLETE = "incomplete"
    PROPOSED = "proposed"
    BLOCKED = "blocked"
    REFUTED = "refuted"


class SemanticNodeKind(StrEnum):
    PROBLEM = "problem"
    DEFINITION = "definition"
    CLAIM = "claim"
    PARTIAL_PROGRESS = "partial_progress"
    APPROACH = "approach"
    OBLIGATION = "obligation"
    COUNTEREXAMPLE = "counterexample"
    EXPERIMENT = "experiment"
    SOURCE = "source"
    TASK = "task"
    INCIDENT = "incident"


class _SemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


_FORBIDDEN_TITLE = re.compile(r"[\\/#\[\]|^\x00-\x1f]")
_WHITESPACE = re.compile(r"\s+")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def normalize_semantic_title(value: str) -> str:
    """Normalize one human-facing title without manufacturing an identifier."""

    title = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip().strip(".")
    if not title:
        raise ValueError("semantic titles must not be blank")
    if len(title) > 180:
        raise ValueError("semantic titles must be at most 180 characters")
    if _FORBIDDEN_TITLE.search(title) is not None or title in {".", ".."}:
        raise ValueError("semantic titles contain a filesystem or wiki-link metacharacter")
    return title


def _normalize_title_list(values: list[str]) -> list[str]:
    normalized = [normalize_semantic_title(value) for value in values]
    casefolded = [value.casefold() for value in normalized]
    if len(casefolded) != len(set(casefolded)):
        raise ValueError("semantic title lists must not contain duplicates")
    return normalized


class SemanticFinding(_SemanticModel):
    """Mathematical content returned by a worker, independent of persistence."""

    finding_type: SemanticFindingType
    title: str
    relates_to: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    status: SemanticFindingStatus
    statement: str | None = None
    what_was_established: str
    what_was_tried_and_did_not_work: str = ""
    next_mathematical_bottleneck: str = ""
    supporting_evidence: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def title_is_descriptive(cls, value: str) -> str:
        return normalize_semantic_title(value)

    @field_validator("relates_to", "depends_on")
    @classmethod
    def links_are_descriptive_titles(cls, values: list[str]) -> list[str]:
        return _normalize_title_list(values)

    @field_validator(
        "what_was_established",
        "what_was_tried_and_did_not_work",
        "next_mathematical_bottleneck",
    )
    @classmethod
    def prose_is_normalized(cls, value: str) -> str:
        return value.strip()

    @field_validator("statement")
    @classmethod
    def statement_is_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("supporting_evidence")
    @classmethod
    def evidence_is_normalized(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def recoverable_mathematics_is_present(self) -> SemanticFinding:
        if not any(
            (
                self.statement,
                self.what_was_established,
                self.what_was_tried_and_did_not_work,
                self.next_mathematical_bottleneck,
            )
        ):
            raise ValueError("a semantic finding must contain recoverable mathematical content")
        return self


class SemanticWorkerReport(_SemanticModel):
    """The complete model-visible worker response."""

    schema_version: Literal[3] = 3
    assignment_title: str
    findings: list[SemanticFinding] = Field(default_factory=list)
    overall_progress: str
    next_assignment: str = ""

    @field_validator("assignment_title")
    @classmethod
    def assignment_title_is_descriptive(cls, value: str) -> str:
        return normalize_semantic_title(value)

    @field_validator("overall_progress", "next_assignment")
    @classmethod
    def report_prose_is_normalized(cls, value: str) -> str:
        return value.strip()


class SemanticCoordinatorAssignment(_SemanticModel):
    """A mathematical task; application code supplies scheduler identity."""

    title: str
    approach_family: str
    task: str
    expected_output: str
    relates_to: list[str]
    stopping_condition: str

    @field_validator("title")
    @classmethod
    def title_is_descriptive(cls, value: str) -> str:
        return normalize_semantic_title(value)

    @field_validator("relates_to")
    @classmethod
    def targets_are_descriptive(cls, values: list[str]) -> list[str]:
        return _normalize_title_list(values)

    @field_validator("approach_family", "task", "expected_output", "stopping_condition")
    @classmethod
    def required_prose_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("semantic coordinator assignment fields must not be blank")
        return normalized


class SemanticCoordinatorDecision(_SemanticModel):
    """The model-visible coordinator response, containing no storage bookkeeping."""

    assignments: list[SemanticCoordinatorAssignment] = Field(default_factory=list)
    rationale: str
    retire_assignments: list[str] = Field(default_factory=list)
    candidate_reports: list[str] = Field(default_factory=list)
    requested_graph_titles: list[str] = Field(default_factory=list, max_length=32)
    candidate_packaging_recommended: bool = False
    declared_scientific_stop: str | None = None

    @field_validator("requested_graph_titles")
    @classmethod
    def requests_are_titles(cls, values: list[str]) -> list[str]:
        return _normalize_title_list(values)

    @field_validator("retire_assignments", "candidate_reports")
    @classmethod
    def descriptive_lists_are_normalized(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("rationale")
    @classmethod
    def rationale_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("coordinator rationale must not be blank")
        return normalized

    @model_validator(mode="after")
    def assignment_titles_are_unique(self) -> SemanticCoordinatorDecision:
        titles = [assignment.title.casefold() for assignment in self.assignments]
        if len(titles) != len(set(titles)):
            raise ValueError("semantic coordinator assignment titles must be unique")
        return self


_KIND_DIRECTORY: dict[SemanticNodeKind, str] = {
    SemanticNodeKind.PROBLEM: "Problems",
    SemanticNodeKind.DEFINITION: "Definitions",
    SemanticNodeKind.CLAIM: "Claims",
    SemanticNodeKind.PARTIAL_PROGRESS: "Partial Progress",
    SemanticNodeKind.APPROACH: "Approaches",
    SemanticNodeKind.OBLIGATION: "Obligations",
    SemanticNodeKind.COUNTEREXAMPLE: "Counterexamples",
    SemanticNodeKind.EXPERIMENT: "Experiments",
    SemanticNodeKind.SOURCE: "Sources",
    SemanticNodeKind.TASK: "Tasks",
    SemanticNodeKind.INCIDENT: "Incidents",
}

_FINDING_KIND: dict[SemanticFindingType, SemanticNodeKind] = {
    SemanticFindingType.DEFINITION: SemanticNodeKind.DEFINITION,
    SemanticFindingType.THEOREM: SemanticNodeKind.CLAIM,
    SemanticFindingType.LEMMA: SemanticNodeKind.CLAIM,
    SemanticFindingType.PARTIAL_PROGRESS: SemanticNodeKind.PARTIAL_PROGRESS,
    SemanticFindingType.FAILED_APPROACH: SemanticNodeKind.APPROACH,
    SemanticFindingType.COUNTEREXAMPLE: SemanticNodeKind.COUNTEREXAMPLE,
    SemanticFindingType.COMPUTATION: SemanticNodeKind.EXPERIMENT,
    SemanticFindingType.SOURCE: SemanticNodeKind.SOURCE,
    SemanticFindingType.TASK: SemanticNodeKind.TASK,
}


class SemanticGraphNode(_SemanticModel):
    """Parsed Markdown node.  ``uid`` is deliberately omitted from semantic views."""

    uid: str
    title: str
    kind: SemanticNodeKind
    status: str
    depends_on: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    provenance: list[str] = Field(default_factory=list)
    body: str
    path: Path


class SemanticGraphIncident(_SemanticModel):
    title: str
    failed_links: list[str]
    source_finding_title: str
    path: Path


class SemanticAdmissionResult(_SemanticModel):
    status: Literal["committed", "committed_with_incident", "already_recorded"]
    title: str
    path: Path
    incident_paths: list[Path] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    semantic_correction: str = ""


class SemanticGraphValidation(_SemanticModel):
    valid: bool
    node_count: int
    link_count: int
    dangling_links: list[str] = Field(default_factory=list)
    ambiguous_titles: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, *, field: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise SemanticGraphFilesystemError(f"{path}: frontmatter {field!r} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SemanticGraphFilesystemError(f"{path}: invalid {field!r} timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _node_text(
    *,
    uid: str,
    title: str,
    kind: SemanticNodeKind,
    status_value: str,
    depends_on: Sequence[str],
    created_at: datetime,
    updated_at: datetime,
    provenance: Sequence[str],
    body: str,
) -> str:
    properties: dict[str, object] = {
        "uid": uid,
        "kind": kind.value,
        "status": status_value,
        "depends_on": [f"[[{item}]]" for item in depends_on],
        "created_at": _iso(created_at),
        "updated_at": _iso(updated_at),
        "provenance": list(provenance),
    }
    return format_flat_frontmatter(properties) + f"\n# {title}\n\n{body.strip()}\n"


def _section(label: str, value: str | None) -> str:
    if value is None or not value.strip():
        return ""
    return f"## {label}\n\n{value.strip()}\n"


def _finding_body(finding: SemanticFinding, *, valid_relations: Sequence[str]) -> str:
    relation_text = "\n".join(f"- [[{title}]]" for title in valid_relations)
    evidence_text = "\n".join(f"- {item}" for item in finding.supporting_evidence)
    sections = [
        _section("Statement", finding.statement),
        _section("What was established", finding.what_was_established),
        _section("What was tried and did not work", finding.what_was_tried_and_did_not_work),
        _section("Next mathematical bottleneck", finding.next_mathematical_bottleneck),
        _section("Related graph notes", relation_text),
        _section("Supporting evidence", evidence_text),
    ]
    return "\n".join(section for section in sections if section).strip()


class SemanticGraphWriter:
    """Deterministic admission service over one Markdown graph.

    The writer always reparses Markdown before validation and admission.  The SQLite
    index can be deleted or corrupted without affecting correctness.
    """

    def __init__(
        self,
        project_root: Path,
        graph_name: str,
        *,
        clock: Callable[[], datetime] | None = None,
        index_refresher: Callable[[SemanticGraphWriter, Sequence[SemanticGraphNode]], None]
        | None = None,
    ) -> None:
        root = project_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise SemanticGraphFilesystemError(f"project root is not a directory: {project_root}")
        normalized_name = re.sub(r"[^a-z0-9]+", "-", graph_name.casefold()).strip("-")
        if not normalized_name:
            raise ValueError("graph name must contain a letter or digit")
        self.project_root = root
        self.graph_name = normalized_name
        self.collection_root = ensure_path_confined(root, root / ".matek" / "knowledge")
        self.graph_root = ensure_path_confined(root, self.collection_root / normalized_name)
        self.index_path = ensure_path_confined(root, self.graph_root / "graph-index.sqlite")
        self.transactions_root = ensure_path_confined(root, self.graph_root / ".transactions")
        self.lock_path = ensure_path_confined(root, self.transactions_root / "writer.lock")
        self._clock = clock or _utc_now
        self._index_refresher = index_refresher

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise SemanticGraphFilesystemError("graph clock must return a datetime")
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.transactions_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SemanticGraphFilesystemError(f"writer lock is not regular: {self.lock_path}")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._recover_staged_admissions_unlocked()
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _recover_staged_admissions_unlocked(self) -> None:
        """Finish a committed staging intent before exposing graph state to a reader."""

        for transaction in sorted(self.transactions_root.glob("admission-*")):
            if not transaction.is_dir() or transaction.is_symlink():
                continue
            manifest_path = transaction / "manifest.json"
            if not manifest_path.is_file():
                shutil.rmtree(transaction)
                continue
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                write_items = raw["writes"]
                removal_items = raw["removals"]
                if not isinstance(write_items, list) or not isinstance(removal_items, list):
                    raise ValueError("manifest lists are malformed")
                for item in write_items:
                    if not isinstance(item, dict):
                        raise ValueError("manifest write is malformed")
                    relative = Path(str(item["path"]))
                    expected = str(item["sha256"])
                    destination = ensure_path_confined(self.graph_root, self.graph_root / relative)
                    staged = ensure_path_confined(transaction, transaction / "files" / relative)
                    if (
                        destination.is_file()
                        and hashlib.sha256(destination.read_bytes()).hexdigest() == expected
                    ):
                        staged.unlink(missing_ok=True)
                        continue
                    if (
                        not staged.is_file()
                        or hashlib.sha256(staged.read_bytes()).hexdigest() != expected
                    ):
                        raise ValueError(f"staged write is missing or corrupt: {relative}")
                    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    os.replace(staged, destination)
                written_paths = {
                    str(item["path"])
                    for item in write_items
                    if isinstance(item, dict) and "path" in item
                }
                for item in removal_items:
                    relative_text = str(item)
                    if relative_text in written_paths:
                        continue
                    ensure_path_confined(
                        self.graph_root, self.graph_root / Path(relative_text)
                    ).unlink(missing_ok=True)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SemanticGraphFilesystemError(
                    f"cannot recover staged graph admission {transaction}: {exc}"
                ) from exc
            shutil.rmtree(transaction)

    @property
    def initialized(self) -> bool:
        required = {*_KIND_DIRECTORY.values(), "Dashboards"}
        return self.graph_root.is_dir() and all(
            (self.graph_root / directory).is_dir() for directory in required
        )

    def initialize(self) -> list[str]:
        """Create a new graph layout and refresh its disposable index."""

        if self.graph_root.is_dir() and not self.initialized:
            existing = [
                path
                for path in self.graph_root.iterdir()
                if path.name not in {".transactions", "graph-index.sqlite", "Home.md"}
            ]
            if existing:
                raise SemanticGraphFilesystemError(
                    f"{self.graph_root} is not a graph-only Markdown vault; "
                    "legacy graphs are not imported or repaired—start with a new graph name"
                )
        with self._locked():
            self.collection_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.graph_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            for directory in _KIND_DIRECTORY.values():
                ensure_path_confined(self.graph_root, self.graph_root / directory).mkdir(
                    parents=True, mode=0o700, exist_ok=True
                )
            ensure_path_confined(self.graph_root, self.graph_root / "Dashboards").mkdir(
                parents=True, mode=0o700, exist_ok=True
            )
            nodes = self._parse_nodes_unlocked()
            return self._refresh_index_nonfatal(nodes)

    def initialize_problem(
        self,
        *,
        title: str,
        statement: str,
        provenance: Sequence[str] = (),
    ) -> str:
        """Create the descriptive root problem note, or reuse the exact title."""

        normalized_title = normalize_semantic_title(title)
        normalized_statement = statement.strip()
        if not normalized_statement:
            raise ValueError("a graph problem requires a mathematical statement")
        self.initialize()
        with self._locked():
            nodes = self._parse_nodes_unlocked()
            existing = self._by_title(nodes).get(normalized_title.casefold())
            if existing is not None:
                if existing.kind is not SemanticNodeKind.PROBLEM:
                    raise SemanticTitleError(
                        f"problem title already names a {existing.kind.value}: {normalized_title!r}"
                    )
                return existing.title
            now = self._now()
            path = ensure_path_confined(
                self.graph_root,
                self.graph_root
                / _KIND_DIRECTORY[SemanticNodeKind.PROBLEM]
                / f"{normalized_title}.md",
            )
            text = _node_text(
                uid=str(uuid.uuid4()),
                title=normalized_title,
                kind=SemanticNodeKind.PROBLEM,
                status_value="open",
                depends_on=(),
                created_at=now,
                updated_at=now,
                provenance=list(dict.fromkeys(item.strip() for item in provenance if item.strip())),
                body=_section("Statement", normalized_statement),
            )
            self._commit_unlocked(writes={path: text})
            self._refresh_index_nonfatal(self._parse_nodes_unlocked())
            return normalized_title

    def _managed_markdown_paths(self) -> list[Path]:
        paths: list[Path] = []
        for directory in _KIND_DIRECTORY.values():
            root = self.graph_root / directory
            if root.is_dir():
                paths.extend(path for path in root.glob("*.md") if path.is_file())
        return sorted(paths, key=lambda path: path.relative_to(self.graph_root).as_posix())

    def _parse_node(self, path: Path) -> SemanticGraphNode:
        try:
            frontmatter, body = parse_flat_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, GraphMarkdownError) as exc:
            raise SemanticGraphFilesystemError(f"cannot parse graph note {path}: {exc}") from exc
        uid_value = frontmatter.get("uid")
        kind_value = frontmatter.get("kind")
        status_value = frontmatter.get("status")
        depends_raw = frontmatter.get("depends_on", [])
        provenance_raw = frontmatter.get("provenance", [])
        if not isinstance(uid_value, str):
            raise SemanticGraphFilesystemError(f"{path}: missing hidden uid")
        try:
            uuid.UUID(uid_value)
        except ValueError as exc:
            raise SemanticGraphFilesystemError(f"{path}: malformed hidden uid") from exc
        try:
            kind = SemanticNodeKind(str(kind_value))
        except ValueError as exc:
            raise SemanticGraphFilesystemError(
                f"{path}: unsupported node kind {kind_value!r}"
            ) from exc
        if not isinstance(status_value, str) or not status_value.strip():
            raise SemanticGraphFilesystemError(f"{path}: missing status")
        if not isinstance(depends_raw, list) or not all(
            isinstance(item, str) for item in depends_raw
        ):
            raise SemanticGraphFilesystemError(f"{path}: depends_on must be a title-link list")
        if not isinstance(provenance_raw, list) or not all(
            isinstance(item, str) for item in provenance_raw
        ):
            raise SemanticGraphFilesystemError(f"{path}: provenance must be a string list")
        heading = re.search(r"(?m)^#\s+(.+?)\s*$", body)
        title = normalize_semantic_title(heading.group(1) if heading is not None else path.stem)
        dependencies: list[str] = []
        for raw in depends_raw:
            match = re.fullmatch(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", raw.strip())
            dependencies.append(normalize_semantic_title(match.group(1) if match else raw))
        return SemanticGraphNode(
            uid=uid_value,
            title=title,
            kind=kind,
            status=status_value.strip(),
            depends_on=dependencies,
            created_at=_parse_datetime(
                frontmatter.get("created_at"), field="created_at", path=path
            ),
            updated_at=_parse_datetime(
                frontmatter.get("updated_at"), field="updated_at", path=path
            ),
            provenance=list(provenance_raw),
            body=body,
            path=path,
        )

    def _parse_nodes_unlocked(self) -> list[SemanticGraphNode]:
        nodes = [self._parse_node(path) for path in self._managed_markdown_paths()]
        by_title: dict[str, list[SemanticGraphNode]] = {}
        by_uid: dict[str, list[SemanticGraphNode]] = {}
        for node in nodes:
            by_title.setdefault(node.title.casefold(), []).append(node)
            by_uid.setdefault(node.uid, []).append(node)
        duplicate_titles = [items[0].title for items in by_title.values() if len(items) > 1]
        duplicate_uids = [uid for uid, items in by_uid.items() if len(items) > 1]
        if duplicate_titles:
            raise SemanticTitleError(
                "ambiguous descriptive graph title(s): " + ", ".join(sorted(duplicate_titles))
            )
        if duplicate_uids:
            raise SemanticGraphFilesystemError(
                "duplicate hidden graph identity: " + ", ".join(sorted(duplicate_uids))
            )
        return nodes

    def load_nodes(self) -> tuple[list[SemanticGraphNode], list[str]]:
        """Parse the authority and best-effort refresh the derived index."""

        if not self.initialized:
            self.initialize()
        with self._locked():
            nodes = self._parse_nodes_unlocked()
            warnings = self._refresh_index_if_stale_nonfatal(nodes)
            return nodes, warnings

    @staticmethod
    def _graph_checksum(nodes: Sequence[SemanticGraphNode]) -> str:
        digest = hashlib.sha256()
        for node in sorted(nodes, key=lambda item: item.path.as_posix()):
            digest.update(node.path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(node.path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _index_checksum(self) -> str | None:
        if not self.index_path.is_file():
            return None
        try:
            with sqlite3.connect(self.index_path) as connection:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'graph_checksum'"
                ).fetchone()
        except (OSError, sqlite3.Error):
            return None
        return str(row[0]) if row is not None else None

    def _rebuild_index(self, nodes: Sequence[SemanticGraphNode]) -> None:
        if self._index_refresher is not None:
            self._index_refresher(self, nodes)
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="graph-index-", suffix=".sqlite", dir=self.transactions_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with sqlite3.connect(temporary) as connection:
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE nodes (title TEXT PRIMARY KEY COLLATE NOCASE, "
                    "kind TEXT NOT NULL, "
                    "status TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE links (source_title TEXT NOT NULL, target_title TEXT NOT NULL)"
                )
                for node in nodes:
                    connection.execute(
                        "INSERT INTO nodes(title, kind, status, path, body) VALUES (?, ?, ?, ?, ?)",
                        (
                            node.title,
                            node.kind.value,
                            node.status,
                            node.path.relative_to(self.graph_root).as_posix(),
                            node.body,
                        ),
                    )
                    for target in node.depends_on:
                        connection.execute(
                            "INSERT INTO links(source_title, target_title) VALUES (?, ?)",
                            (node.title, target),
                        )
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('graph_checksum', ?)",
                    (self._graph_checksum(nodes),),
                )
                connection.commit()
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _refresh_index_nonfatal(self, nodes: Sequence[SemanticGraphNode]) -> list[str]:
        warnings: list[str] = []
        try:
            self._rebuild_index(nodes)
        except Exception as exc:
            warnings.append(
                "Derived graph index refresh failed; continuing from Markdown authority: "
                + str(exc)
            )
        try:
            self._write_navigation(nodes)
        except OSError as exc:
            warnings.append(
                "Derived graph navigation refresh failed; continuing from Markdown authority: "
                + str(exc)
            )
        return warnings

    def _write_navigation(self, nodes: Sequence[SemanticGraphNode]) -> None:
        """Regenerate small human views; these files are never parsed as authority."""

        active = [
            node
            for node in nodes
            if node.kind is SemanticNodeKind.TASK and node.status not in {"complete", "refuted"}
        ]
        recent = sorted(nodes, key=lambda node: node.updated_at, reverse=True)[:40]
        kinds = [kind for kind in SemanticNodeKind if kind is not SemanticNodeKind.INCIDENT]
        home_lines = ["# MATEK knowledge graph", "", "## Browse by kind", ""]
        for kind in kinds:
            matching = sorted(
                (node for node in nodes if node.kind is kind),
                key=lambda node: node.title.casefold(),
            )
            if matching:
                home_lines.extend(
                    [f"### {kind.value.replace('_', ' ').title()}", ""]
                    + [f"- [[{node.title}]] — `{node.status}`" for node in matching]
                    + [""]
                )
        active_text = "# Active Tasks\n\n" + (
            "\n".join(f"- [[{node.title}]] — `{node.status}`" for node in active)
            if active
            else "_No active tasks._"
        )
        recent_text = "# Recent Changes\n\n" + (
            "\n".join(f"- [[{node.title}]] — {node.updated_at.isoformat()}" for node in recent)
            if recent
            else "_No graph changes yet._"
        )
        atomic_write_text(
            self.graph_root / "Home.md",
            "\n".join(home_lines).rstrip() + "\n",
            confinement_root=self.graph_root,
            mode=0o600,
        )
        atomic_write_text(
            self.graph_root / "Dashboards" / "Active Tasks.md",
            active_text + "\n",
            confinement_root=self.graph_root,
            mode=0o600,
        )
        atomic_write_text(
            self.graph_root / "Dashboards" / "Recent Changes.md",
            recent_text + "\n",
            confinement_root=self.graph_root,
            mode=0o600,
        )

    def _refresh_index_if_stale_nonfatal(self, nodes: Sequence[SemanticGraphNode]) -> list[str]:
        try:
            stale = self._index_checksum() != self._graph_checksum(nodes)
        except OSError:
            stale = True
        return self._refresh_index_nonfatal(nodes) if stale else []

    @staticmethod
    def _by_title(nodes: Sequence[SemanticGraphNode]) -> dict[str, SemanticGraphNode]:
        return {node.title.casefold(): node for node in nodes}

    def resolve_title(self, title: str) -> SemanticGraphNode:
        normalized = normalize_semantic_title(title)
        nodes, _ = self.load_nodes()
        match = self._by_title(nodes).get(normalized.casefold())
        if match is None:
            raise SemanticTitleError(f"graph title does not resolve: {normalized!r}")
        return match

    @staticmethod
    def _disambiguated_title(
        requested: str,
        *,
        existing: Mapping[str, SemanticGraphNode],
        qualifier: str,
    ) -> str:
        if requested.casefold() not in existing:
            return requested
        normalized_qualifier = normalize_semantic_title(qualifier).casefold()
        candidate = normalize_semantic_title(f"{requested} ({normalized_qualifier})")
        if candidate.casefold() not in existing:
            return candidate
        suffix = 2
        while True:
            candidate = normalize_semantic_title(f"{requested} ({normalized_qualifier} {suffix})")
            if candidate.casefold() not in existing:
                return candidate
            suffix += 1

    def _commit_unlocked(
        self,
        *,
        writes: Mapping[Path, str],
        removals: Sequence[Path] = (),
    ) -> None:
        transaction = Path(tempfile.mkdtemp(prefix="admission-", dir=self.transactions_root))
        staged_root = transaction / "files"
        try:
            staged: list[tuple[Path, Path]] = []
            for destination, contents in sorted(
                writes.items(), key=lambda item: item[0].as_posix()
            ):
                confined = ensure_path_confined(self.graph_root, destination)
                relative = confined.relative_to(self.graph_root)
                staged_path = staged_root / relative
                staged_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                atomic_write_text(staged_path, contents, confinement_root=transaction, mode=0o600)
                staged.append((staged_path, confined))
            manifest = {
                "writes": [
                    {
                        "path": destination.relative_to(self.graph_root).as_posix(),
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                    for source, destination in staged
                ],
                "removals": [
                    ensure_path_confined(self.graph_root, path)
                    .relative_to(self.graph_root)
                    .as_posix()
                    for path in removals
                ],
            }
            atomic_write_text(
                transaction / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                confinement_root=transaction,
                mode=0o600,
            )
            for source, destination in staged:
                destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                os.replace(source, destination)
            written = {destination for _, destination in staged}
            for path in removals:
                confined = ensure_path_confined(self.graph_root, path)
                if confined not in written:
                    confined.unlink(missing_ok=True)
        except OSError as exc:
            raise SemanticGraphFilesystemError(
                f"graph admission could not commit staged Markdown: {exc}; "
                f"transaction retained at {transaction}"
            ) from exc
        else:
            shutil.rmtree(transaction)

    def _incident_title(self, *, source_title: str, failed_links: Sequence[str]) -> str:
        stamp = self._now().astimezone(UTC).strftime("%Y-%m-%d %H%M%S")
        first = failed_links[0] if failed_links else source_title
        compact = normalize_semantic_title(first)[:72].rstrip()
        return normalize_semantic_title(f"{stamp} dangling link — {compact}")

    def _incident_text(
        self,
        *,
        title: str,
        source_finding: SemanticFinding,
        failed_links: Sequence[str],
        now: datetime,
        provenance: Sequence[str],
    ) -> str:
        link_list = "\n".join(f"- `{item}`" for item in failed_links)
        body = (
            "## Local admission failure\n\n"
            f"The proposed finding **{source_finding.title}** referenced descriptive title(s) "
            "that were not present in the Markdown graph. Only those relations were quarantined; "
            "the recoverable mathematical content was retained.\n\n"
            "## Unresolved titles\n\n"
            f"{link_list}\n\n"
            "## Exact source finding\n\n"
            "```json\n" + source_finding.model_dump_json(indent=2) + "\n```"
        )
        return _node_text(
            uid=str(uuid.uuid4()),
            title=title,
            kind=SemanticNodeKind.INCIDENT,
            status_value="open",
            depends_on=[],
            created_at=now,
            updated_at=now,
            provenance=provenance,
            body=body,
        )

    def admit_finding(
        self,
        finding: SemanticFinding,
        *,
        provenance: Sequence[str] = (),
        disambiguation: str | None = None,
    ) -> SemanticAdmissionResult:
        """Record one finding and quarantine only its unresolved title relations."""

        if not self.initialized:
            self.initialize()
        normalized_provenance = list(
            dict.fromkeys(item.strip() for item in provenance if item.strip())
        )
        with self._locked():
            nodes = self._parse_nodes_unlocked()
            existing = self._by_title(nodes)
            operation_markers = set(normalized_provenance)
            if operation_markers:
                prior = next(
                    (
                        node
                        for node in nodes
                        if operation_markers.intersection(node.provenance)
                        and node.title.casefold() == finding.title.casefold()
                    ),
                    None,
                )
                if prior is not None:
                    return SemanticAdmissionResult(
                        status="already_recorded",
                        title=prior.title,
                        path=prior.path,
                        semantic_correction=f"Existing result retained: “{prior.title}”.",
                    )

            qualifier = disambiguation or _FINDING_KIND[finding.finding_type].value.replace(
                "_", " "
            )
            title = self._disambiguated_title(finding.title, existing=existing, qualifier=qualifier)
            requested_links = list(dict.fromkeys([*finding.relates_to, *finding.depends_on]))
            valid_links = [
                existing[item.casefold()].title
                for item in requested_links
                if item.casefold() in existing
            ]
            failed_links = [item for item in requested_links if item.casefold() not in existing]
            now = self._now()
            kind = _FINDING_KIND[finding.finding_type]
            path = ensure_path_confined(
                self.graph_root,
                self.graph_root / _KIND_DIRECTORY[kind] / f"{title}.md",
            )
            writes: dict[Path, str] = {
                path: _node_text(
                    uid=str(uuid.uuid4()),
                    title=title,
                    kind=kind,
                    status_value=finding.status.value,
                    depends_on=[
                        existing[item.casefold()].title
                        for item in finding.depends_on
                        if item.casefold() in existing
                    ],
                    created_at=now,
                    updated_at=now,
                    provenance=normalized_provenance,
                    body=_finding_body(finding, valid_relations=valid_links),
                )
            }
            incident_paths: list[Path] = []
            if failed_links:
                incident_title = self._incident_title(source_title=title, failed_links=failed_links)
                incident_title = self._disambiguated_title(
                    incident_title, existing=existing, qualifier="incident"
                )
                incident_path = ensure_path_confined(
                    self.graph_root,
                    self.graph_root
                    / _KIND_DIRECTORY[SemanticNodeKind.INCIDENT]
                    / f"{incident_title}.md",
                )
                writes[incident_path] = self._incident_text(
                    title=incident_title,
                    source_finding=finding,
                    failed_links=failed_links,
                    now=now,
                    provenance=normalized_provenance,
                )
                incident_paths.append(incident_path)
            self._commit_unlocked(writes=writes)
            committed_nodes = self._parse_nodes_unlocked()
            warnings = self._refresh_index_nonfatal(committed_nodes)
            if failed_links:
                warnings.append(
                    "Quarantined unresolved descriptive relation(s): " + ", ".join(failed_links)
                )
            return SemanticAdmissionResult(
                status="committed_with_incident" if failed_links else "committed",
                title=title,
                path=path,
                incident_paths=incident_paths,
                warnings=warnings,
                semantic_correction=(
                    f"Partial result recorded: “{title}”. The unresolved relation(s) "
                    f"{', '.join(failed_links)} were quarantined; refreshed graph context "
                    "is available and research should continue."
                    if failed_links
                    else f"Result recorded: “{title}”."
                ),
            )

    def admit_worker_report(
        self,
        report: SemanticWorkerReport,
        *,
        provenance: Sequence[str] = (),
    ) -> list[SemanticAdmissionResult]:
        """Persist every recoverable finding, including reports with no theorem."""

        results: list[SemanticAdmissionResult] = []
        for finding in report.findings:
            results.append(
                self.admit_finding(
                    finding,
                    provenance=[*provenance, f"assignment: {report.assignment_title}"],
                )
            )
        if report.findings:
            return results
        partial = SemanticFinding(
            finding_type=SemanticFindingType.PARTIAL_PROGRESS,
            title=f"Partial progress on {report.assignment_title}",
            status=SemanticFindingStatus.INCOMPLETE,
            what_was_established=report.overall_progress or "No theorem was established.",
            next_mathematical_bottleneck=report.next_assignment,
        )
        admitted = self.admit_finding(
            partial,
            provenance=[*provenance, f"assignment: {report.assignment_title}"],
        )
        admitted.semantic_correction = (
            f"Partial result recorded: “{admitted.title}”. No theorem admitted. "
            f"{report.overall_progress} Next: {report.next_assignment}".strip()
        )
        return [admitted]

    def update_status(
        self,
        title: str,
        status: str,
        *,
        provenance: Sequence[str] = (),
    ) -> str:
        """Update one note's workflow status without changing its title or identity."""

        normalized_title = normalize_semantic_title(title)
        normalized_status = status.strip()
        if not normalized_status:
            raise ValueError("semantic graph statuses must not be blank")
        with self._locked():
            nodes = self._parse_nodes_unlocked()
            node = self._by_title(nodes).get(normalized_title.casefold())
            if node is None:
                raise SemanticTitleError(f"graph title does not resolve: {normalized_title!r}")
            body = re.sub(
                r"(?m)^#\s+" + re.escape(node.title) + r"\s*$",
                "",
                node.body,
                count=1,
            ).strip()
            updated_provenance = list(
                dict.fromkeys(
                    [
                        *node.provenance,
                        *(item.strip() for item in provenance if item.strip()),
                    ]
                )
            )
            text = _node_text(
                uid=node.uid,
                title=node.title,
                kind=node.kind,
                status_value=normalized_status,
                depends_on=node.depends_on,
                created_at=node.created_at,
                updated_at=self._now(),
                provenance=updated_provenance,
                body=body,
            )
            self._commit_unlocked(writes={node.path: text})
            self._refresh_index_nonfatal(self._parse_nodes_unlocked())
            return node.title

    @staticmethod
    def _replace_wikilink_title(text: str, old_title: str, new_title: str) -> str:
        pattern = re.compile(
            r"\[\[" + re.escape(old_title) + r"(?P<suffix>(?:#[^\]|]+)?(?:\|[^\]]+)?)\]\]",
            re.IGNORECASE,
        )
        return pattern.sub(lambda match: f"[[{new_title}{match.group('suffix')}]]", text)

    def rename_node(
        self,
        old_title: str,
        new_title: str,
        *,
        disambiguation: str | None = None,
    ) -> str:
        """Rename a note and atomically update every inbound descriptive wiki link."""

        old_normalized = normalize_semantic_title(old_title)
        requested_new = normalize_semantic_title(new_title)
        with self._locked():
            nodes = self._parse_nodes_unlocked()
            existing = self._by_title(nodes)
            target = existing.get(old_normalized.casefold())
            if target is None:
                raise SemanticTitleError(f"graph title does not resolve: {old_normalized!r}")
            without_target = {
                title: node for title, node in existing.items() if node.uid != target.uid
            }
            final_title = self._disambiguated_title(
                requested_new,
                existing=without_target,
                qualifier=disambiguation or target.kind.value.replace("_", " "),
            )
            now = self._now()
            writes: dict[Path, str] = {}
            removals: list[Path] = []
            for node in nodes:
                raw = node.path.read_text(encoding="utf-8")
                updated_raw = self._replace_wikilink_title(raw, target.title, final_title)
                if node.uid == target.uid:
                    frontmatter, body = parse_flat_frontmatter(updated_raw)
                    body = re.sub(r"(?m)^#\s+.+?\s*$", f"# {final_title}", body, count=1)
                    frontmatter["updated_at"] = _iso(now)
                    updated_raw = format_flat_frontmatter(frontmatter) + body
                    destination = ensure_path_confined(
                        self.graph_root,
                        self.graph_root / _KIND_DIRECTORY[target.kind] / f"{final_title}.md",
                    )
                    writes[destination] = updated_raw
                    if destination != node.path:
                        removals.append(node.path)
                elif updated_raw != raw:
                    frontmatter, body = parse_flat_frontmatter(updated_raw)
                    frontmatter["updated_at"] = _iso(now)
                    writes[node.path] = format_flat_frontmatter(frontmatter) + body
            self._commit_unlocked(writes=writes, removals=removals)
            committed = self._parse_nodes_unlocked()
            self._refresh_index_nonfatal(committed)
            return final_title

    def validate(self) -> SemanticGraphValidation:
        """Validate all descriptive links directly from Markdown."""

        nodes, warnings = self.load_nodes()
        titles = {node.title.casefold() for node in nodes}
        dangling: list[str] = []
        link_count = 0
        for node in nodes:
            raw = node.path.read_text(encoding="utf-8")
            links = [normalize_semantic_title(match.group(1)) for match in _WIKILINK.finditer(raw)]
            link_count += len(links)
            dangling.extend(
                f"{node.title} -> {link}" for link in links if link.casefold() not in titles
            )
        return SemanticGraphValidation(
            valid=not dangling,
            node_count=len(nodes),
            link_count=link_count,
            dangling_links=sorted(set(dangling)),
            warnings=warnings,
        )

    def semantic_context(
        self,
        *,
        focus_titles: Sequence[str] = (),
        maximum_nodes: int = 48,
    ) -> dict[str, object]:
        """Return concise title-based mathematics with no hidden identity or storage data."""

        nodes, warnings = self.load_nodes()
        by_title = self._by_title(nodes)
        selected: list[SemanticGraphNode] = []
        pending = [normalize_semantic_title(title) for title in focus_titles]
        seen: set[str] = set()
        while pending and len(selected) < maximum_nodes:
            title = pending.pop(0)
            key = title.casefold()
            if key in seen:
                continue
            seen.add(key)
            node = by_title.get(key)
            if node is None:
                continue
            selected.append(node)
            pending.extend(node.depends_on)
        if not selected:
            selected = [node for node in nodes if node.kind is not SemanticNodeKind.INCIDENT][
                :maximum_nodes
            ]
        entries = []
        for node in selected:
            statement_match = re.search(r"(?ms)^## Statement\s*\n+(.+?)(?=^## |\Z)", node.body)
            established_match = re.search(
                r"(?ms)^## What was established\s*\n+(.+?)(?=^## |\Z)", node.body
            )
            entries.append(
                {
                    "title": node.title,
                    "link": f"[[{node.title}]]",
                    "kind": node.kind.value,
                    "status": node.status,
                    "statement": statement_match.group(1).strip()
                    if statement_match is not None
                    else "",
                    "immediate_dependencies": node.depends_on,
                    "supporting_evidence": established_match.group(1).strip()
                    if established_match is not None
                    else "",
                }
            )
        del warnings
        return {"nodes": entries}


def recover_semantic_finding(value: Mapping[str, Any]) -> SemanticFinding | None:
    """Recover useful mathematical prose when optional model fields are malformed.

    Required title/content failures remain unrecoverable.  Invalid optional title links are
    dropped here and will be visible in the raw worker artifact retained by the caller.
    """

    raw_title = value.get("title")
    raw_type = value.get("finding_type", SemanticFindingType.PARTIAL_PROGRESS.value)
    raw_status = value.get("status", SemanticFindingStatus.INCOMPLETE.value)
    established = value.get("what_was_established", value.get("statement", ""))
    if not isinstance(raw_title, str) or not isinstance(established, str):
        return None
    try:
        title = normalize_semantic_title(raw_title)
        finding_type = SemanticFindingType(str(raw_type))
        status_value = SemanticFindingStatus(str(raw_status))
    except ValueError:
        return None

    def safe_titles(field: str) -> list[str]:
        raw = value.get(field, [])
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            try:
                result.append(normalize_semantic_title(item))
            except ValueError:
                continue
        return list(dict.fromkeys(result))

    return SemanticFinding(
        finding_type=finding_type,
        title=title,
        relates_to=safe_titles("relates_to"),
        depends_on=safe_titles("depends_on"),
        status=status_value,
        statement=value.get("statement") if isinstance(value.get("statement"), str) else None,
        what_was_established=established,
        what_was_tried_and_did_not_work=(
            value.get("what_was_tried_and_did_not_work", "")
            if isinstance(value.get("what_was_tried_and_did_not_work", ""), str)
            else ""
        ),
        next_mathematical_bottleneck=(
            value.get("next_mathematical_bottleneck", "")
            if isinstance(value.get("next_mathematical_bottleneck", ""), str)
            else ""
        ),
        supporting_evidence=[
            item for item in value.get("supporting_evidence", []) if isinstance(item, str)
        ]
        if isinstance(value.get("supporting_evidence", []), list)
        else [],
    )


__all__ = [
    "SemanticAdmissionResult",
    "SemanticCoordinatorAssignment",
    "SemanticCoordinatorDecision",
    "SemanticFinding",
    "SemanticFindingStatus",
    "SemanticFindingType",
    "SemanticGraphError",
    "SemanticGraphFilesystemError",
    "SemanticGraphIncident",
    "SemanticGraphNode",
    "SemanticGraphValidation",
    "SemanticGraphWriter",
    "SemanticNodeKind",
    "SemanticTitleError",
    "SemanticWorkerReport",
    "normalize_semantic_title",
    "recover_semantic_finding",
]
