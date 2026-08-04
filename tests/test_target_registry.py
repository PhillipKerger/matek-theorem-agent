from __future__ import annotations

import json
from pathlib import Path

import pytest

from matek_theorem_agent.knowledge_graph.targets import (
    TargetBinding,
    TargetBindingDisposition,
    TargetRegistry,
    TargetRegistryError,
    bind_frozen_target,
    load_target_registry,
    source_problem_sha256,
    write_target_registry,
)

SOURCE_HASH = "a" * 64


def bind(
    registry: TargetRegistry,
    *,
    statement: str = "For every n, cost(n) <= k * OPT(n) + beta.",
    contract: dict[str, str] | None = None,
    run_id: str = "run-one",
    migrate: bool = False,
    reason: str | None = None,
) -> tuple[TargetRegistry, TargetBinding]:
    return bind_frozen_target(
        registry,
        normalized_source_sha256=SOURCE_HASH,
        target_node_id="CLM-TARGET001",
        title="Exact target",
        exact_statement=statement,
        claim_contract=contract
        or {
            "quantifiers": "for every n",
            "conclusion": "cost(n) <= k * OPT(n) + beta",
        },
        compiled_prompt=f"Prompt for {run_id}: {statement}",
        run_id=run_id,
        allow_material_migration=migrate,
        migration_reason=reason,
    )


def test_unchanged_source_reuses_exact_frozen_target_across_compiler_paraphrases() -> None:
    registry, first_binding = bind(TargetRegistry())
    first = first_binding.target

    registry, second_binding = bind(
        registry,
        statement="For all n, the algorithm cost is at most k OPT plus beta.",
        run_id="run-two",
    )
    second = second_binding.target

    assert second_binding.disposition is TargetBindingDisposition.REUSED
    assert second.exact_statement == first.exact_statement
    assert second.canonical_contract_json == first.canonical_contract_json
    assert second.compiled_prompt == first.compiled_prompt
    assert second.statement_version == 1
    assert second.compatibility[-1].classification == "cosmetic_paraphrase"


def test_contract_only_change_requires_explicit_migration() -> None:
    registry, _ = bind(TargetRegistry())

    with pytest.raises(TargetRegistryError, match="explicit target migration"):
        bind(
            registry,
            contract={
                "quantifiers": "for every n",
                "conclusion": "cost(n) <= k * OPT(n)",
            },
            run_id="run-two",
        )


def test_explicit_material_migration_versions_target_and_records_compatibility() -> None:
    registry, first_binding = bind(TargetRegistry())
    registry, migrated_binding = bind(
        registry,
        statement="For every n, cost(n) <= (k+1) * OPT(n).",
        contract={
            "quantifiers": "for every n",
            "conclusion": "cost(n) <= (k+1) * OPT(n)",
        },
        run_id="run-two",
        migrate=True,
        reason="The user explicitly selected a corrected approximation target.",
    )
    migrated = migrated_binding.target

    assert migrated_binding.disposition is TargetBindingDisposition.MIGRATED
    assert migrated.statement_version == 2
    assert migrated.established_run_id == first_binding.target.established_run_id
    assert migrated.last_migration_run_id == "run-two"
    assert migrated.migrations[-1].reason.startswith("The user explicitly")
    assert migrated.compatibility[-1].classification == "explicit_migration"


def test_registry_round_trip_detects_tampering(tmp_path: Path) -> None:
    registry, _ = bind(TargetRegistry())
    path = tmp_path / "target-registry.json"
    write_target_registry(path, registry)

    assert load_target_registry(path) == registry
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["targets"][SOURCE_HASH]["title"] = "Tampered title"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TargetRegistryError, match="integrity digest"):
        load_target_registry(path)


def test_source_problem_hash_is_computed_from_normalized_bytes() -> None:
    assert source_problem_sha256(b"exact normalized source\n") == (
        "42f94e8ab5c6e7c9e19884d38ec18b798aa68cb17b893503f99840c7f9b3e003"
    )
