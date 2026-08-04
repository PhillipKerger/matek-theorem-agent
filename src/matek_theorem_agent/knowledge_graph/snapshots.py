"""Content-addressed revision storage for the persistent knowledge graph.

Schema-v1 snapshots are immutable full JSON documents stored directly beneath
``snapshots/``.  Schema v2 keeps those files readable but writes new revisions as
small delta manifests over immutable node/edge blobs, with periodic full hash-map
checkpoints.  The store reconstructs both formats into the same logical snapshot
shape used by graph diffing and optimistic stale-base checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..workspace import atomic_write_bytes, ensure_path_confined, sha256_bytes, sha256_text
from .markdown import render_node_note
from .models import GraphEdge, GraphNode, GraphSnapshotVerification, validate_node_id

SNAPSHOT_SCHEMA_VERSION = 2
DEFAULT_SNAPSHOT_CHECKPOINT_INTERVAL = 64
MAX_SNAPSHOT_REPLAY_DEPTH = 4_096

_REVISION = re.compile(r"\A\d{8}-[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_MANIFEST_KIND: Literal["matek.graph.snapshot.delta"] = "matek.graph.snapshot.delta"
_CHECKPOINT_KIND: Literal["matek.graph.snapshot.checkpoint"] = "matek.graph.snapshot.checkpoint"


class SnapshotIntegrityError(ValueError):
    """A snapshot, manifest, checkpoint, or content blob failed validation."""


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _pretty_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _validate_revision(value: str) -> str:
    if not _REVISION.fullmatch(value):
        raise ValueError("snapshot revision must use NNNNNNNN- followed by 16 lowercase hex digits")
    return value


def _validate_sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("snapshot digest must be a lowercase SHA-256 value")
    return value


def _expected_revision(number: int, node_hashes: Mapping[str, str]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            dict(sorted(node_hashes.items())),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"{number:08d}-{digest[:16]}"


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SnapshotCheckpoint(_SnapshotModel):
    schema_version: Literal[2]
    artifact_type: Literal["matek.graph.snapshot.checkpoint"]
    revision: str
    node_blobs: dict[str, str]
    edge_blobs: list[str]

    @field_validator("revision")
    @classmethod
    def revision_is_valid(cls, value: str) -> str:
        return _validate_revision(value)

    @field_validator("node_blobs")
    @classmethod
    def node_blob_map_is_valid(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for node_id, digest in value.items():
            normalized[validate_node_id(node_id)] = _validate_sha256(digest)
        if len(normalized) != len(value):
            raise ValueError("checkpoint node IDs must be unique after normalization")
        return dict(sorted(normalized.items()))

    @field_validator("edge_blobs")
    @classmethod
    def edge_blob_list_is_valid(cls, value: list[str]) -> list[str]:
        normalized = sorted(_validate_sha256(item) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("checkpoint edge blob hashes must be unique")
        return normalized


class _SnapshotManifest(_SnapshotModel):
    schema_version: Literal[2]
    artifact_type: Literal["matek.graph.snapshot.delta"]
    revision: str
    revision_number: int = Field(ge=0)
    created_at: str
    previous_revision: str | None
    parent_integrity_root: str | None
    added_nodes: dict[str, str]
    updated_nodes: dict[str, str]
    removed_nodes: dict[str, str]
    added_edges: list[str]
    removed_edges: list[str]
    checkpoint_sha256: str | None
    content_root: str
    reconstruction_sha256: str
    integrity_root: str

    @field_validator("revision")
    @classmethod
    def revision_is_valid(cls, value: str) -> str:
        return _validate_revision(value)

    @field_validator("previous_revision")
    @classmethod
    def previous_revision_is_valid(cls, value: str | None) -> str | None:
        return None if value is None else _validate_revision(value)

    @field_validator("parent_integrity_root", "checkpoint_sha256")
    @classmethod
    def optional_hash_is_valid(cls, value: str | None) -> str | None:
        return None if value is None else _validate_sha256(value)

    @field_validator("content_root", "reconstruction_sha256", "integrity_root")
    @classmethod
    def integrity_root_is_valid(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("added_nodes", "updated_nodes", "removed_nodes")
    @classmethod
    def node_delta_map_is_valid(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for node_id, digest in value.items():
            normalized[validate_node_id(node_id)] = _validate_sha256(digest)
        if len(normalized) != len(value):
            raise ValueError("manifest node IDs must be unique after normalization")
        return dict(sorted(normalized.items()))

    @field_validator("added_edges", "removed_edges")
    @classmethod
    def edge_delta_hashes_are_valid(cls, value: list[str]) -> list[str]:
        normalized = sorted(_validate_sha256(item) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("edge delta hashes must be unique")
        return normalized

    @model_validator(mode="after")
    def delta_sets_do_not_overlap(self) -> _SnapshotManifest:
        node_sets = (set(self.added_nodes), set(self.updated_nodes), set(self.removed_nodes))
        if any(
            node_sets[left].intersection(node_sets[right])
            for left in range(3)
            for right in range(left + 1, 3)
        ):
            raise ValueError("node delta categories must not overlap")
        if set(self.added_edges).intersection(self.removed_edges):
            raise ValueError("edge delta categories must not overlap")
        if (self.previous_revision is None) != (self.parent_integrity_root is None):
            raise ValueError("previous revision and parent integrity root must be present together")
        if self.revision_number != int(self.revision[:8]):
            raise ValueError("manifest revision number does not match its revision identifier")
        if self.previous_revision is None and self.revision_number != 0:
            raise ValueError("only revision zero may omit a previous revision")
        if self.previous_revision is not None:
            previous_number = int(self.previous_revision[:8])
            if previous_number + 1 != self.revision_number:
                raise ValueError("manifest revisions must form a contiguous history")
        return self


@dataclass(frozen=True)
class _SnapshotIndex:
    revision: str
    created_at: str
    node_blobs: dict[str, str]
    edge_blobs: frozenset[str]
    checkpoint_revision: str | None
    legacy: bool = False


@dataclass(frozen=True)
class _MaterializedSnapshot:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    node_hashes: dict[str, str]


class SnapshotStore:
    """Read legacy snapshots and write/verify content-addressed schema-v2 revisions."""

    def __init__(
        self,
        snapshots_root: Path,
        *,
        checkpoint_interval: int = DEFAULT_SNAPSHOT_CHECKPOINT_INTERVAL,
    ) -> None:
        if checkpoint_interval < 1:
            raise ValueError("snapshot checkpoint interval must be positive")
        if checkpoint_interval > MAX_SNAPSHOT_REPLAY_DEPTH:
            raise ValueError("snapshot checkpoint interval exceeds the bounded replay safety limit")
        self.root = snapshots_root.expanduser().resolve(strict=False)
        self.manifests_root = self.root / "manifests"
        self.checkpoints_root = self.root / "checkpoints"
        self.node_blobs_root = self.root / "blobs" / "nodes"
        self.edge_blobs_root = self.root / "blobs" / "edges"
        self.checkpoint_interval = checkpoint_interval

    def _safe_path(self, requested: Path) -> Path:
        try:
            relative = requested.relative_to(self.root)
        except ValueError as exc:
            raise SnapshotIntegrityError(f"snapshot path escapes its store: {requested}") from exc
        current = self.root
        if current.is_symlink():
            raise SnapshotIntegrityError(f"snapshot root must not be a symlink: {current}")
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise SnapshotIntegrityError(f"snapshot path must not contain a symlink: {current}")
        try:
            return ensure_path_confined(self.root, requested)
        except ValueError as exc:
            raise SnapshotIntegrityError(str(exc)) from exc

    def _ensure_directory(self, requested: Path) -> Path:
        if requested == self.root:
            if requested.is_symlink():
                raise SnapshotIntegrityError(f"snapshot root must not be a symlink: {requested}")
            requested.mkdir(mode=0o700, parents=True, exist_ok=True)
            return requested
        target = self._safe_path(requested)
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_symlink() or not target.is_dir():
            raise SnapshotIntegrityError(f"snapshot directory is not a regular directory: {target}")
        return target

    def _read_regular_bytes(self, requested: Path) -> bytes:
        target = self._safe_path(requested)
        try:
            status = target.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise SnapshotIntegrityError(f"snapshot artifact is missing: {target}") from exc
        if not stat.S_ISREG(status.st_mode):
            raise SnapshotIntegrityError(f"snapshot artifact is not a regular file: {target}")
        try:
            return target.read_bytes()
        except OSError as exc:
            raise SnapshotIntegrityError(f"cannot read snapshot artifact {target}: {exc}") from exc

    def _write_immutable_bytes(self, requested: Path, contents: bytes) -> Path:
        self._ensure_directory(requested.parent)
        target = self._safe_path(requested)
        if target.exists():
            existing = self._read_regular_bytes(target)
            if existing != contents:
                raise SnapshotIntegrityError(
                    f"immutable snapshot artifact already exists with different bytes: {target}"
                )
            return target
        return atomic_write_bytes(target, contents, confinement_root=self.root, mode=0o600)

    def _artifact_exists(self, requested: Path) -> bool:
        target = self._safe_path(requested)
        try:
            status = target.stat(follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(status.st_mode):
            raise SnapshotIntegrityError(f"snapshot artifact is not a regular file: {target}")
        return True

    def _legacy_path(self, revision: str) -> Path:
        return self.root / f"{_validate_revision(revision)}.json"

    def _manifest_path(self, revision: str) -> Path:
        return self.manifests_root / f"{_validate_revision(revision)}.json"

    def _checkpoint_path(self, revision: str) -> Path:
        return self.checkpoints_root / f"{_validate_revision(revision)}.json"

    def _node_blob_path(self, digest: str) -> Path:
        return self.node_blobs_root / f"{_validate_sha256(digest)}.json"

    def _edge_blob_path(self, digest: str) -> Path:
        return self.edge_blobs_root / f"{_validate_sha256(digest)}.json"

    def has_legacy_revision(self, revision: str) -> bool:
        return self._artifact_exists(self._legacy_path(revision))

    def has_v2_revision(self, revision: str) -> bool:
        return self._artifact_exists(self._manifest_path(revision))

    def _revision_schema(self, revision: str) -> Literal[1, 2]:
        legacy_exists = self.has_legacy_revision(revision)
        manifest_exists = self.has_v2_revision(revision)
        if legacy_exists and manifest_exists:
            raise SnapshotIntegrityError(
                f"revision has both legacy and schema-v2 snapshot records: {revision}"
            )
        if legacy_exists:
            return 1
        if manifest_exists:
            return 2
        raise SnapshotIntegrityError(f"graph revision snapshot does not exist: {revision}")

    @staticmethod
    def _node_blob_bytes(node: GraphNode) -> bytes:
        return _canonical_json_bytes(node.model_dump(mode="json"))

    @staticmethod
    def _edge_blob_bytes(edge: GraphEdge) -> bytes:
        return _canonical_json_bytes(edge.model_dump(mode="json"))

    @staticmethod
    def _edge_key(edge: GraphEdge) -> tuple[str, str, str]:
        return edge.source_id, edge.relation.value, edge.target_id

    @classmethod
    def _unique_edges(cls, nodes: Sequence[GraphNode]) -> list[GraphEdge]:
        by_key = {cls._edge_key(edge): edge for node in nodes for edge in node.relations}
        return [by_key[key] for key in sorted(by_key)]

    def _validate_materialized(
        self,
        *,
        revision: str,
        nodes: Sequence[GraphNode],
        edges: Sequence[GraphEdge],
        expected_node_hashes: Mapping[str, str] | None = None,
    ) -> _MaterializedSnapshot:
        by_id: dict[str, GraphNode] = {}
        for node in nodes:
            if node.matek_id in by_id:
                raise SnapshotIntegrityError(f"snapshot contains duplicate node {node.matek_id}")
            by_id[node.matek_id] = node
        node_hashes: dict[str, str] = {}
        for node_id, node in sorted(by_id.items()):
            if node.content_hash is None:
                raise SnapshotIntegrityError(f"snapshot node {node_id} has no content hash")
            _validate_sha256(node.content_hash)
            rendered_hash = sha256_text(render_node_note(node, relation_targets=by_id))
            if rendered_hash != node.content_hash:
                raise SnapshotIntegrityError(
                    f"snapshot node {node_id} does not reproduce its recorded Markdown hash"
                )
            node_hashes[node_id] = node.content_hash
        if (
            expected_node_hashes is not None
            and dict(sorted(expected_node_hashes.items())) != node_hashes
        ):
            raise SnapshotIntegrityError(
                "snapshot node hash map does not match its materialized nodes"
            )

        edge_by_key: dict[tuple[str, str, str], GraphEdge] = {}
        for edge in edges:
            key = self._edge_key(edge)
            if key in edge_by_key:
                raise SnapshotIntegrityError(f"snapshot contains duplicate edge {key}")
            if edge.source_id not in by_id or edge.target_id not in by_id:
                raise SnapshotIntegrityError(f"snapshot edge references a missing node: {key}")
            edge_by_key[key] = edge
        embedded = {self._edge_key(edge) for node in nodes for edge in node.relations}
        if embedded != set(edge_by_key):
            raise SnapshotIntegrityError(
                "snapshot edge blobs do not match the relations embedded in node blobs"
            )

        revision_number = int(revision[:8])
        expected_revision = _expected_revision(revision_number, node_hashes)
        if expected_revision != revision:
            raise SnapshotIntegrityError(
                "snapshot revision identity mismatch: "
                f"expected {expected_revision}, found {revision}"
            )
        return _MaterializedSnapshot(
            nodes=[by_id[node_id] for node_id in sorted(by_id)],
            edges=[edge_by_key[key] for key in sorted(edge_by_key)],
            node_hashes=node_hashes,
        )

    def _load_legacy(self, revision: str) -> tuple[dict[str, Any], bytes, _MaterializedSnapshot]:
        raw = self._read_regular_bytes(self._legacy_path(revision))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotIntegrityError(
                f"legacy graph snapshot is invalid JSON: {revision}"
            ) from exc
        if not isinstance(payload, dict):
            raise SnapshotIntegrityError(f"legacy graph snapshot is not an object: {revision}")
        if payload.get("schema_version") != 1 or payload.get("revision") != revision:
            raise SnapshotIntegrityError(
                f"legacy graph snapshot has inconsistent identity: {revision}"
            )
        raw_nodes = payload.get("nodes")
        raw_edges = payload.get("edges")
        raw_hashes = payload.get("node_hashes")
        if (
            not isinstance(raw_nodes, list)
            or not isinstance(raw_edges, list)
            or not isinstance(raw_hashes, dict)
        ):
            raise SnapshotIntegrityError(f"legacy graph snapshot is incomplete: {revision}")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_hashes.items()
        ):
            raise SnapshotIntegrityError(
                f"legacy graph snapshot has an invalid node hash map: {revision}"
            )
        try:
            nodes = [GraphNode.model_validate(item) for item in raw_nodes]
            edges = [GraphEdge.model_validate(item) for item in raw_edges]
        except (ValidationError, ValueError, TypeError) as exc:
            raise SnapshotIntegrityError(
                f"legacy graph snapshot has invalid graph records: {revision}"
            ) from exc
        materialized = self._validate_materialized(
            revision=revision,
            nodes=nodes,
            edges=edges,
            expected_node_hashes={str(key): str(value) for key, value in raw_hashes.items()},
        )
        return payload, raw, materialized

    def _legacy_index(self, revision: str) -> _SnapshotIndex:
        payload, _, materialized = self._load_legacy(revision)
        node_blobs = {
            node.matek_id: sha256_bytes(self._node_blob_bytes(node)) for node in materialized.nodes
        }
        edge_blobs = frozenset(
            sha256_bytes(self._edge_blob_bytes(edge)) for edge in materialized.edges
        )
        return _SnapshotIndex(
            revision=revision,
            created_at=str(payload.get("created_at") or ""),
            node_blobs=node_blobs,
            edge_blobs=edge_blobs,
            checkpoint_revision=revision,
            legacy=True,
        )

    def _manifest_integrity_root(self, manifest: _SnapshotManifest) -> str:
        payload = manifest.model_dump(mode="json", exclude={"integrity_root"})
        return sha256_bytes(_canonical_json_bytes(payload))

    @staticmethod
    def _content_root(node_blobs: Mapping[str, str], edge_blobs: Iterable[str]) -> str:
        """Bind a revision to every immutable blob in its reconstructed state."""

        return sha256_bytes(
            _canonical_json_bytes(
                {
                    "node_blobs": dict(sorted(node_blobs.items())),
                    "edge_blobs": sorted(edge_blobs),
                }
            )
        )

    def _parse_manifest_bytes(self, revision: str, raw: bytes) -> _SnapshotManifest:
        try:
            manifest = _SnapshotManifest.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise SnapshotIntegrityError(
                f"snapshot manifest is invalid: {revision}: {exc}"
            ) from exc
        if manifest.revision != revision:
            raise SnapshotIntegrityError(f"snapshot manifest has inconsistent identity: {revision}")
        expected_root = self._manifest_integrity_root(manifest)
        if manifest.integrity_root != expected_root:
            raise SnapshotIntegrityError(f"snapshot manifest integrity root is invalid: {revision}")
        return manifest

    def _stored_integrity_root(self, revision: str) -> str:
        legacy_path = self._legacy_path(revision)
        manifest_path = self._manifest_path(revision)
        legacy_exists = self._artifact_exists(legacy_path)
        manifest_exists = self._artifact_exists(manifest_path)
        if legacy_exists and manifest_exists:
            raise SnapshotIntegrityError(
                f"revision has both legacy and schema-v2 snapshot records: {revision}"
            )
        if legacy_exists:
            return sha256_bytes(self._read_regular_bytes(legacy_path))
        if manifest_exists:
            raw = self._read_regular_bytes(manifest_path)
            return self._parse_manifest_bytes(revision, raw).integrity_root
        raise SnapshotIntegrityError(f"graph revision snapshot does not exist: {revision}")

    def _load_manifest(self, revision: str) -> _SnapshotManifest:
        raw = self._read_regular_bytes(self._manifest_path(revision))
        manifest = self._parse_manifest_bytes(revision, raw)
        if manifest.previous_revision is not None:
            parent_root = self._stored_integrity_root(manifest.previous_revision)
            if manifest.parent_integrity_root != parent_root:
                raise SnapshotIntegrityError(
                    f"snapshot manifest parent root is inconsistent: {revision}"
                )
        return manifest

    def _load_checkpoint(self, revision: str, expected_sha256: str) -> _SnapshotCheckpoint:
        raw = self._read_regular_bytes(self._checkpoint_path(revision))
        if sha256_bytes(raw) != expected_sha256:
            raise SnapshotIntegrityError(f"snapshot checkpoint digest is invalid: {revision}")
        try:
            checkpoint = _SnapshotCheckpoint.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise SnapshotIntegrityError(
                f"snapshot checkpoint is invalid: {revision}: {exc}"
            ) from exc
        if checkpoint.revision != revision:
            raise SnapshotIntegrityError(
                f"snapshot checkpoint has inconsistent identity: {revision}"
            )
        return checkpoint

    @staticmethod
    def _apply_delta(base: _SnapshotIndex, manifest: _SnapshotManifest) -> _SnapshotIndex:
        nodes = dict(base.node_blobs)
        edges = set(base.edge_blobs)
        for node_id, digest in manifest.removed_nodes.items():
            if node_id not in nodes:
                raise SnapshotIntegrityError(
                    f"snapshot delta removes unknown node {node_id}: {manifest.revision}"
                )
            if nodes[node_id] != digest:
                raise SnapshotIntegrityError(
                    f"snapshot delta records the wrong removed hash for node {node_id}"
                )
            del nodes[node_id]
        for node_id, digest in manifest.updated_nodes.items():
            if node_id not in nodes:
                raise SnapshotIntegrityError(
                    f"snapshot delta updates unknown node {node_id}: {manifest.revision}"
                )
            if nodes[node_id] == digest:
                raise SnapshotIntegrityError(
                    f"snapshot delta records unchanged node {node_id} as updated"
                )
            nodes[node_id] = digest
        for node_id, digest in manifest.added_nodes.items():
            if node_id in nodes:
                raise SnapshotIntegrityError(
                    f"snapshot delta adds existing node {node_id}: {manifest.revision}"
                )
            nodes[node_id] = digest
        for digest in manifest.removed_edges:
            if digest not in edges:
                raise SnapshotIntegrityError(
                    f"snapshot delta removes unknown edge blob {digest}: {manifest.revision}"
                )
            edges.remove(digest)
        for digest in manifest.added_edges:
            if digest in edges:
                raise SnapshotIntegrityError(
                    f"snapshot delta adds existing edge blob {digest}: {manifest.revision}"
                )
            edges.add(digest)
        return _SnapshotIndex(
            revision=manifest.revision,
            created_at=manifest.created_at,
            node_blobs=dict(sorted(nodes.items())),
            edge_blobs=frozenset(edges),
            checkpoint_revision=base.checkpoint_revision,
        )

    def _index_for_revision(
        self,
        revision: str,
        *,
        stack: tuple[str, ...] = (),
        memo: dict[str, _SnapshotIndex] | None = None,
        validate_checkpoint_delta: bool = True,
    ) -> _SnapshotIndex:
        if revision in stack:
            raise SnapshotIntegrityError("snapshot manifest history contains a cycle")
        if len(stack) >= MAX_SNAPSHOT_REPLAY_DEPTH:
            raise SnapshotIntegrityError(
                "snapshot manifest history exceeds the replay safety bound"
            )
        cached = memo.get(revision) if memo is not None else None
        if cached is not None:
            return cached
        if self._revision_schema(revision) == 1:
            result = self._legacy_index(revision)
            if memo is not None:
                memo[revision] = result
            return result
        manifest = self._load_manifest(revision)
        if manifest.checkpoint_sha256 is not None:
            checkpoint = self._load_checkpoint(revision, manifest.checkpoint_sha256)
            result = _SnapshotIndex(
                revision=revision,
                created_at=manifest.created_at,
                node_blobs=checkpoint.node_blobs,
                edge_blobs=frozenset(checkpoint.edge_blobs),
                checkpoint_revision=revision,
            )
            if validate_checkpoint_delta:
                if manifest.previous_revision is None:
                    base = _SnapshotIndex(
                        revision="",
                        created_at="",
                        node_blobs={},
                        edge_blobs=frozenset(),
                        checkpoint_revision=None,
                    )
                else:
                    base = self._index_for_revision(
                        manifest.previous_revision,
                        stack=(*stack, revision),
                        memo=memo,
                        validate_checkpoint_delta=False,
                    )
                expected = self._apply_delta(base, manifest)
                if (
                    expected.node_blobs != result.node_blobs
                    or expected.edge_blobs != result.edge_blobs
                ):
                    raise SnapshotIntegrityError(
                        f"snapshot checkpoint does not match its revision delta: {revision}"
                    )
        else:
            if manifest.previous_revision is None:
                raise SnapshotIntegrityError("revision zero must include a full checkpoint")
            base = self._index_for_revision(
                manifest.previous_revision,
                stack=(*stack, revision),
                memo=memo,
                validate_checkpoint_delta=False,
            )
            if base.legacy:
                raise SnapshotIntegrityError(
                    "the first schema-v2 revision after a legacy snapshot must be a checkpoint"
                )
            result = self._apply_delta(base, manifest)
        content_root = self._content_root(result.node_blobs, result.edge_blobs)
        if manifest.content_root != content_root:
            raise SnapshotIntegrityError(
                f"snapshot manifest content root is inconsistent: {revision}"
            )
        if memo is not None:
            memo[revision] = result
        return result

    def _load_node_blob(self, node_id: str, digest: str) -> GraphNode:
        raw = self._read_regular_bytes(self._node_blob_path(digest))
        if sha256_bytes(raw) != digest:
            raise SnapshotIntegrityError(f"node blob digest is invalid: {digest}")
        try:
            node = GraphNode.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise SnapshotIntegrityError(f"node blob is invalid: {digest}: {exc}") from exc
        if node.matek_id != node_id:
            raise SnapshotIntegrityError(
                f"node blob {digest} contains {node.matek_id}, expected {node_id}"
            )
        return node

    def _load_edge_blob(self, digest: str) -> GraphEdge:
        raw = self._read_regular_bytes(self._edge_blob_path(digest))
        if sha256_bytes(raw) != digest:
            raise SnapshotIntegrityError(f"edge blob digest is invalid: {digest}")
        try:
            return GraphEdge.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise SnapshotIntegrityError(f"edge blob is invalid: {digest}: {exc}") from exc

    def _materialize_index(self, index: _SnapshotIndex) -> _MaterializedSnapshot:
        if index.legacy:
            return self._load_legacy(index.revision)[2]
        nodes = [
            self._load_node_blob(node_id, digest)
            for node_id, digest in sorted(index.node_blobs.items())
        ]
        edges = [self._load_edge_blob(digest) for digest in sorted(index.edge_blobs)]
        return self._validate_materialized(revision=index.revision, nodes=nodes, edges=edges)

    @staticmethod
    def _full_snapshot_payload(
        *,
        revision: str,
        created_at: str,
        materialized: _MaterializedSnapshot,
    ) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "revision": revision,
            "created_at": created_at,
            "node_hashes": dict(sorted(materialized.node_hashes.items())),
            "nodes": [node.model_dump(mode="json") for node in materialized.nodes],
            "edges": [edge.model_dump(mode="json") for edge in materialized.edges],
        }

    def _verified_full_snapshot_payload(
        self,
        manifest: _SnapshotManifest,
        materialized: _MaterializedSnapshot,
    ) -> dict[str, Any]:
        payload = self._full_snapshot_payload(
            revision=manifest.revision,
            created_at=manifest.created_at,
            materialized=materialized,
        )
        if sha256_bytes(_pretty_json_bytes(payload)) != manifest.reconstruction_sha256:
            raise SnapshotIntegrityError(
                f"snapshot reconstruction digest is invalid: {manifest.revision}"
            )
        return payload

    def load_snapshot(self, revision: str) -> dict[str, Any]:
        """Reconstruct one revision as a full logical snapshot object."""

        _validate_revision(revision)
        if self._revision_schema(revision) == 1:
            return self._load_legacy(revision)[0]
        manifest = self._load_manifest(revision)
        index = self._index_for_revision(revision, memo={})
        materialized = self._materialize_index(index)
        return self._verified_full_snapshot_payload(manifest, materialized)

    def reconstruct_bytes(self, revision: str) -> bytes:
        """Return a deterministic byte-for-byte full-snapshot reconstruction."""

        if self._revision_schema(revision) == 1:
            _, raw, _ = self._load_legacy(revision)
            return raw
        return _pretty_json_bytes(self.load_snapshot(revision))

    def verify_revision(self, revision: str) -> GraphSnapshotVerification:
        """Verify one revision, its parent binding, checkpoint, and all live blobs."""

        _validate_revision(revision)
        if self._revision_schema(revision) == 1:
            payload, raw, materialized = self._load_legacy(revision)
            return GraphSnapshotVerification(
                revision=revision,
                schema_version=1,
                integrity_root=sha256_bytes(raw),
                node_count=len(materialized.nodes),
                edge_count=len(materialized.edges),
                checkpoint_revision=revision,
                legacy=True,
                artifact_path=f"snapshots/{revision}.json",
                created_at=str(payload.get("created_at") or ""),
            )
        manifest = self._load_manifest(revision)
        index = self._index_for_revision(revision, memo={})
        materialized = self._materialize_index(index)
        self._verified_full_snapshot_payload(manifest, materialized)
        return GraphSnapshotVerification(
            revision=revision,
            schema_version=2,
            integrity_root=manifest.integrity_root,
            node_count=len(materialized.nodes),
            edge_count=len(materialized.edges),
            checkpoint_revision=index.checkpoint_revision,
            legacy=False,
            artifact_path=f"snapshots/manifests/{revision}.json",
            created_at=manifest.created_at,
        )

    def _revision_names(self) -> list[str]:
        names: set[str] = set()
        if self.root.is_dir():
            for path in self.root.glob("*.json"):
                if _REVISION.fullmatch(path.stem):
                    names.add(path.stem)
        if self.manifests_root.is_dir() and not self.manifests_root.is_symlink():
            for path in self.manifests_root.glob("*.json"):
                if _REVISION.fullmatch(path.stem):
                    if path.stem in names:
                        raise SnapshotIntegrityError(
                            f"revision has both legacy and schema-v2 snapshot records: {path.stem}"
                        )
                    names.add(path.stem)
        return sorted(names, key=lambda item: (int(item[:8]), item))

    def verify_all(self) -> list[GraphSnapshotVerification]:
        """Verify every legacy and schema-v2 revision in deterministic order."""

        return [self.verify_revision(revision) for revision in self._revision_names()]

    def write_revision(
        self,
        *,
        revision: str,
        revision_number: int,
        created_at: str,
        nodes: Sequence[GraphNode],
        previous_revision: str | None,
    ) -> GraphSnapshotVerification:
        """Write one immutable schema-v2 revision, or verify an existing record idempotently."""

        _validate_revision(revision)
        if revision_number != int(revision[:8]):
            raise SnapshotIntegrityError(
                "snapshot revision number does not match revision identity"
            )
        if previous_revision is None and revision_number != 0:
            raise SnapshotIntegrityError("only revision zero may omit a previous revision")
        if previous_revision is not None:
            _validate_revision(previous_revision)
            if int(previous_revision[:8]) + 1 != revision_number:
                raise SnapshotIntegrityError("snapshot revisions must form a contiguous history")
        selected = sorted(nodes, key=lambda item: item.matek_id)
        edges = self._unique_edges(selected)
        materialized = self._validate_materialized(
            revision=revision,
            nodes=selected,
            edges=edges,
        )

        node_records: list[tuple[bytes, str]] = []
        current_nodes: dict[str, str] = {}
        for node in materialized.nodes:
            blob = self._node_blob_bytes(node)
            digest = sha256_bytes(blob)
            node_records.append((blob, digest))
            current_nodes[node.matek_id] = digest
        edge_records: list[tuple[bytes, str]] = []
        current_edges: set[str] = set()
        for edge in materialized.edges:
            blob = self._edge_blob_bytes(edge)
            digest = sha256_bytes(blob)
            edge_records.append((blob, digest))
            current_edges.add(digest)

        legacy_path = self._legacy_path(revision)
        manifest_path = self._manifest_path(revision)
        legacy_exists = self._artifact_exists(legacy_path)
        manifest_exists = self._artifact_exists(manifest_path)
        if legacy_exists and manifest_exists:
            raise SnapshotIntegrityError(
                f"revision has both legacy and schema-v2 snapshot records: {revision}"
            )
        if legacy_exists:
            # Legacy snapshots are permanently read-only. Never add a second record for
            # the same revision, even during interrupted-transaction recovery.
            stored = self._legacy_index(revision)
            if stored.node_blobs != current_nodes or stored.edge_blobs != frozenset(current_edges):
                raise SnapshotIntegrityError(
                    f"legacy snapshot does not match the recovered graph state: {revision}"
                )
            return self.verify_revision(revision)
        if manifest_exists:
            manifest = self._load_manifest(revision)
            stored = self._index_for_revision(revision, memo={})
            if (
                manifest.revision_number != revision_number
                or manifest.created_at != created_at
                or manifest.previous_revision != previous_revision
                or stored.node_blobs != current_nodes
                or stored.edge_blobs != frozenset(current_edges)
            ):
                raise SnapshotIntegrityError(
                    f"schema-v2 snapshot does not match the recovered graph state: {revision}"
                )
            return self.verify_revision(revision)

        self._ensure_directory(self.root)
        self._ensure_directory(self.manifests_root)
        self._ensure_directory(self.checkpoints_root)
        self._ensure_directory(self.node_blobs_root)
        self._ensure_directory(self.edge_blobs_root)

        for blob, digest in node_records:
            self._write_immutable_bytes(self._node_blob_path(digest), blob)
        for blob, digest in edge_records:
            self._write_immutable_bytes(self._edge_blob_path(digest), blob)

        if previous_revision is None:
            base = _SnapshotIndex(
                revision="",
                created_at="",
                node_blobs={},
                edge_blobs=frozenset(),
                checkpoint_revision=None,
            )
            parent_root = None
        else:
            _validate_revision(previous_revision)
            base = self._index_for_revision(previous_revision, memo={})
            parent_root = self._stored_integrity_root(previous_revision)

        added_nodes = {
            node_id: current_nodes[node_id]
            for node_id in sorted(current_nodes.keys() - base.node_blobs.keys())
        }
        removed_nodes = {
            node_id: base.node_blobs[node_id]
            for node_id in sorted(base.node_blobs.keys() - current_nodes.keys())
        }
        updated_nodes = {
            node_id: current_nodes[node_id]
            for node_id in sorted(current_nodes.keys() & base.node_blobs.keys())
            if current_nodes[node_id] != base.node_blobs[node_id]
        }
        added_edges = sorted(current_edges - set(base.edge_blobs))
        removed_edges = sorted(set(base.edge_blobs) - current_edges)

        checkpoint_required = (
            previous_revision is None
            or base.legacy
            or revision_number % self.checkpoint_interval == 0
        )
        checkpoint_sha256: str | None = None
        if checkpoint_required:
            checkpoint = _SnapshotCheckpoint(
                schema_version=2,
                artifact_type=_CHECKPOINT_KIND,
                revision=revision,
                node_blobs=dict(sorted(current_nodes.items())),
                edge_blobs=sorted(current_edges),
            )
            checkpoint_bytes = _pretty_json_bytes(checkpoint.model_dump(mode="json"))
            checkpoint_sha256 = sha256_bytes(checkpoint_bytes)
            self._write_immutable_bytes(self._checkpoint_path(revision), checkpoint_bytes)

        manifest_without_root = {
            "schema_version": 2,
            "artifact_type": _MANIFEST_KIND,
            "revision": revision,
            "revision_number": revision_number,
            "created_at": created_at,
            "previous_revision": previous_revision,
            "parent_integrity_root": parent_root,
            "added_nodes": added_nodes,
            "updated_nodes": updated_nodes,
            "removed_nodes": removed_nodes,
            "added_edges": added_edges,
            "removed_edges": removed_edges,
            "checkpoint_sha256": checkpoint_sha256,
            "content_root": self._content_root(current_nodes, current_edges),
            "reconstruction_sha256": sha256_bytes(
                _pretty_json_bytes(
                    self._full_snapshot_payload(
                        revision=revision,
                        created_at=created_at,
                        materialized=materialized,
                    )
                )
            ),
        }
        integrity_root = sha256_bytes(_canonical_json_bytes(manifest_without_root))
        manifest = _SnapshotManifest.model_validate(
            {**manifest_without_root, "integrity_root": integrity_root}
        )
        manifest_bytes = _pretty_json_bytes(manifest.model_dump(mode="json"))
        # Publishing the manifest last makes a revision visible only after every
        # referenced blob and optional checkpoint is durable.
        self._write_immutable_bytes(manifest_path, manifest_bytes)
        return self.verify_revision(revision)


__all__ = [
    "DEFAULT_SNAPSHOT_CHECKPOINT_INTERVAL",
    "SNAPSHOT_SCHEMA_VERSION",
    "SnapshotIntegrityError",
    "SnapshotStore",
]
