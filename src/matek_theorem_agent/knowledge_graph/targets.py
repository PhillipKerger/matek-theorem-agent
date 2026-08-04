"""Immutable target registry keyed by normalized source-problem content."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..scientific import exact_statement_fingerprint, normalize_exact_statement
from ..workspace import atomic_write_json

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class TargetRegistryError(RuntimeError):
    """A target was changed without an explicit, auditable migration."""


class TargetBindingDisposition(StrEnum):
    CREATED = "created"
    REUSED = "reused"
    MIGRATED = "migrated"


class _TargetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def canonical_contract_json(contract: object) -> str:
    return json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _validate_sha256(value: str) -> str:
    normalized = value.strip().casefold()
    if not _SHA256.fullmatch(normalized):
        raise ValueError("target registry digests must be lowercase SHA-256 values")
    return normalized


class TargetCompatibility(_TargetModel):
    incoming_statement_sha256: str
    canonical_statement_sha256: str
    classification: Literal["cosmetic_paraphrase", "explicit_migration"]
    run_id: str

    @field_validator("incoming_statement_sha256", "canonical_statement_sha256")
    @classmethod
    def hashes_are_valid(cls, value: str) -> str:
        return _validate_sha256(value)


class TargetMigration(_TargetModel):
    migration_id: str
    run_id: str
    reason: str
    prior_statement_sha256: str
    prior_contract_sha256: str
    new_statement_sha256: str
    new_contract_sha256: str

    @field_validator("migration_id", "run_id", "reason")
    @classmethod
    def text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("target migrations require identity, run, and reason")
        return normalized

    @field_validator(
        "prior_statement_sha256",
        "prior_contract_sha256",
        "new_statement_sha256",
        "new_contract_sha256",
    )
    @classmethod
    def hashes_are_valid(cls, value: str) -> str:
        return _validate_sha256(value)


class FrozenTarget(_TargetModel):
    normalized_source_sha256: str
    target_node_id: str
    title: str
    exact_statement: str
    canonical_contract_json: str
    compiled_prompt: str
    statement_sha256: str
    contract_sha256: str
    compiled_prompt_sha256: str
    statement_version: int = Field(default=1, ge=1)
    established_run_id: str
    last_migration_run_id: str | None = None
    compatibility: list[TargetCompatibility] = Field(default_factory=list)
    migrations: list[TargetMigration] = Field(default_factory=list)

    @field_validator(
        "normalized_source_sha256",
        "statement_sha256",
        "contract_sha256",
        "compiled_prompt_sha256",
    )
    @classmethod
    def hashes_are_valid(cls, value: str) -> str:
        return _validate_sha256(value)

    @field_validator("title", "exact_statement", "target_node_id", "established_run_id")
    @classmethod
    def required_text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("frozen targets require complete identity and theorem text")
        return normalized

    @model_validator(mode="after")
    def content_hashes_match_bytes(self) -> FrozenTarget:
        if self.statement_sha256 != _sha256_text(self.exact_statement):
            raise ValueError("frozen target statement hash does not match its bytes")
        if self.contract_sha256 != _sha256_text(self.canonical_contract_json):
            raise ValueError("frozen target contract hash does not match its bytes")
        if self.compiled_prompt_sha256 != _sha256_text(self.compiled_prompt):
            raise ValueError("frozen target prompt hash does not match its bytes")
        return self


class TargetRegistry(_TargetModel):
    schema_version: Literal[1] = 1
    targets: dict[str, FrozenTarget] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keys_match_source_hashes(self) -> TargetRegistry:
        for key, target in self.targets.items():
            if _validate_sha256(key) != target.normalized_source_sha256:
                raise ValueError("target registry keys must equal normalized source hashes")
        return self


class TargetBinding(_TargetModel):
    disposition: TargetBindingDisposition
    target: FrozenTarget
    incoming_statement_sha256: str
    incoming_contract_sha256: str

    @field_validator("incoming_statement_sha256", "incoming_contract_sha256")
    @classmethod
    def hashes_are_valid(cls, value: str) -> str:
        return _validate_sha256(value)


def _make_frozen_target(
    *,
    normalized_source_sha256: str,
    target_node_id: str,
    title: str,
    exact_statement: str,
    claim_contract: object,
    compiled_prompt: str,
    run_id: str,
    statement_version: int = 1,
    compatibility: list[TargetCompatibility] | None = None,
    migrations: list[TargetMigration] | None = None,
) -> FrozenTarget:
    statement = normalize_exact_statement(exact_statement)
    contract = canonical_contract_json(claim_contract)
    return FrozenTarget(
        normalized_source_sha256=_validate_sha256(normalized_source_sha256),
        target_node_id=target_node_id,
        title=title,
        exact_statement=statement,
        canonical_contract_json=contract,
        compiled_prompt=compiled_prompt,
        statement_sha256=_sha256_text(statement),
        contract_sha256=_sha256_text(contract),
        compiled_prompt_sha256=_sha256_text(compiled_prompt),
        statement_version=statement_version,
        established_run_id=run_id,
        last_migration_run_id=run_id if statement_version > 1 else None,
        compatibility=compatibility or [],
        migrations=migrations or [],
    )


def bind_frozen_target(
    registry: TargetRegistry,
    *,
    normalized_source_sha256: str,
    target_node_id: str,
    title: str,
    exact_statement: str,
    claim_contract: object,
    compiled_prompt: str,
    run_id: str,
    allow_material_migration: bool = False,
    migration_reason: str | None = None,
) -> tuple[TargetRegistry, TargetBinding]:
    """Create, reuse, or explicitly migrate the target for one unchanged source."""

    source_hash = _validate_sha256(normalized_source_sha256)
    incoming_statement = normalize_exact_statement(exact_statement)
    incoming_contract = canonical_contract_json(claim_contract)
    incoming_statement_hash = _sha256_text(incoming_statement)
    incoming_contract_hash = _sha256_text(incoming_contract)
    existing = registry.targets.get(source_hash)
    if existing is None:
        created = _make_frozen_target(
            normalized_source_sha256=source_hash,
            target_node_id=target_node_id,
            title=title,
            exact_statement=incoming_statement,
            claim_contract=claim_contract,
            compiled_prompt=compiled_prompt,
            run_id=run_id,
        )
        return (
            registry.model_copy(update={"targets": {**registry.targets, source_hash: created}}),
            TargetBinding(
                disposition=TargetBindingDisposition.CREATED,
                target=created,
                incoming_statement_sha256=incoming_statement_hash,
                incoming_contract_sha256=incoming_contract_hash,
            ),
        )

    contract_changed = existing.contract_sha256 != incoming_contract_hash
    statement_changed = existing.statement_sha256 != incoming_statement_hash
    reason = (migration_reason or "").strip()
    migration_requested = allow_material_migration and bool(reason)
    if contract_changed and not migration_requested:
        raise TargetRegistryError(
            "the canonical claim contract changed for an unchanged normalized problem; "
            "an explicit target migration and reason are required"
        )
    if migration_requested and (contract_changed or statement_changed):
        if not reason:  # pragma: no cover - implied by migration_requested
            raise TargetRegistryError("an explicit target migration requires a nonblank reason")
        migration_id = (
            "target-migration-"
            + hashlib.sha256(
                "\0".join(
                    [
                        source_hash,
                        existing.contract_sha256,
                        incoming_contract_hash,
                        run_id,
                        reason,
                    ]
                ).encode()
            ).hexdigest()[:20]
        )
        migration = TargetMigration(
            migration_id=migration_id,
            run_id=run_id,
            reason=reason,
            prior_statement_sha256=existing.statement_sha256,
            prior_contract_sha256=existing.contract_sha256,
            new_statement_sha256=incoming_statement_hash,
            new_contract_sha256=incoming_contract_hash,
        )
        compatibility = list(existing.compatibility)
        compatibility.append(
            TargetCompatibility(
                incoming_statement_sha256=existing.statement_sha256,
                canonical_statement_sha256=incoming_statement_hash,
                classification="explicit_migration",
                run_id=run_id,
            )
        )
        migrated = _make_frozen_target(
            normalized_source_sha256=source_hash,
            target_node_id=existing.target_node_id,
            title=title,
            exact_statement=incoming_statement,
            claim_contract=claim_contract,
            compiled_prompt=compiled_prompt,
            run_id=existing.established_run_id,
            statement_version=existing.statement_version + 1,
            compatibility=compatibility,
            migrations=[*existing.migrations, migration],
        ).model_copy(update={"last_migration_run_id": run_id})
        return (
            registry.model_copy(update={"targets": {**registry.targets, source_hash: migrated}}),
            TargetBinding(
                disposition=TargetBindingDisposition.MIGRATED,
                target=migrated,
                incoming_statement_sha256=incoming_statement_hash,
                incoming_contract_sha256=incoming_contract_hash,
            ),
        )

    compatibility = list(existing.compatibility)
    if statement_changed and not any(
        item.incoming_statement_sha256 == incoming_statement_hash for item in compatibility
    ):
        compatibility.append(
            TargetCompatibility(
                incoming_statement_sha256=incoming_statement_hash,
                canonical_statement_sha256=existing.statement_sha256,
                classification="cosmetic_paraphrase",
                run_id=run_id,
            )
        )
    reused = existing.model_copy(update={"compatibility": compatibility})
    return (
        registry.model_copy(update={"targets": {**registry.targets, source_hash: reused}}),
        TargetBinding(
            disposition=TargetBindingDisposition.REUSED,
            target=reused,
            incoming_statement_sha256=incoming_statement_hash,
            incoming_contract_sha256=incoming_contract_hash,
        ),
    )


def target_registry_sha256(registry: TargetRegistry) -> str:
    payload = json.dumps(
        registry.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def render_target_registry(registry: TargetRegistry) -> str:
    """Render the integrity-bound registry for an atomic graph transaction."""

    payload = registry.model_dump(mode="json")
    payload["integrity_sha256"] = target_registry_sha256(registry)
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_target_registry(path: Path, registry: TargetRegistry) -> Path:
    return atomic_write_json(
        path,
        json.loads(render_target_registry(registry)),
        confinement_root=path.parent,
    )


def load_target_registry(path: Path) -> TargetRegistry:
    if not path.is_file():
        return TargetRegistry()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TargetRegistryError(f"cannot load frozen target registry: {exc}") from exc
    if not isinstance(raw, dict):
        raise TargetRegistryError("frozen target registry must be one JSON object")
    expected = raw.pop("integrity_sha256", None)
    try:
        registry = TargetRegistry.model_validate(raw)
    except ValueError as exc:
        raise TargetRegistryError(f"frozen target registry is invalid: {exc}") from exc
    if expected != target_registry_sha256(registry):
        raise TargetRegistryError("frozen target registry integrity digest does not match")
    return registry


def source_problem_sha256(normalized_source_bytes: bytes) -> str:
    return hashlib.sha256(normalized_source_bytes).hexdigest()


def target_semantic_fingerprint(target: FrozenTarget) -> str:
    """Expose a stable identity for dependency invalidation and compatibility audits."""

    return hashlib.sha256(
        "\0".join(
            [
                exact_statement_fingerprint(target.exact_statement),
                target.contract_sha256,
            ]
        ).encode()
    ).hexdigest()


__all__ = [
    "FrozenTarget",
    "TargetBinding",
    "TargetBindingDisposition",
    "TargetCompatibility",
    "TargetMigration",
    "TargetRegistry",
    "TargetRegistryError",
    "bind_frozen_target",
    "canonical_contract_json",
    "load_target_registry",
    "render_target_registry",
    "source_problem_sha256",
    "target_registry_sha256",
    "target_semantic_fingerprint",
    "write_target_registry",
]
