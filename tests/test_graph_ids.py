from __future__ import annotations

import pytest

from matek_theorem_agent.graph_ids import (
    MAX_ID_DESCRIPTION_LENGTH,
    MAX_NODE_ID_LENGTH,
    dedupe_descriptive_id,
    descriptive_node_id,
    is_descriptive_node_id,
    is_legacy_node_id,
    normalize_id_description,
    strip_collision_suffix,
    suggest_node_ids,
    unknown_id_message,
    validate_any_node_id,
)


def test_legacy_ids_still_validate_and_normalize_case() -> None:
    assert validate_any_node_id("clm-atsptgt1") == "CLM-ATSPTGT1"
    assert validate_any_node_id("  PRF-MATRPRF1 ") == "PRF-MATRPRF1"
    assert is_legacy_node_id("CLM-ATSPTGT1")
    assert not is_descriptive_node_id("CLM-ATSPTGT1")


def test_descriptive_ids_validate_and_normalize_whitespace() -> None:
    value = validate_any_node_id("claim:   For a convex body\nof volume v  with centroid c")
    assert value == "CLAIM: For a convex body of volume v with centroid c"
    assert is_descriptive_node_id(value)
    assert is_descriptive_node_id("PROOF ATTEMPT: Induction over the boundary cases")
    assert not is_legacy_node_id(value)


def test_descriptive_ids_reject_unknown_words_and_bad_shapes() -> None:
    with pytest.raises(ValueError):
        validate_any_node_id("LEMMA: Unknown type word")
    with pytest.raises(ValueError):
        validate_any_node_id("CLAIM: ")
    with pytest.raises(ValueError):
        validate_any_node_id("CLAIM: brackets [break] wikilinks")
    with pytest.raises(ValueError):
        validate_any_node_id("not an id at all ...")
    with pytest.raises(ValueError):
        validate_any_node_id("CLM-SHORT")


def test_descriptive_node_id_composes_and_caps_length() -> None:
    node_id = descriptive_node_id("proof attempt", "A" * 500)
    assert node_id.startswith("PROOF ATTEMPT: ")
    description = node_id.split(": ", 1)[1]
    assert len(description) <= MAX_ID_DESCRIPTION_LENGTH
    with pytest.raises(ValueError):
        descriptive_node_id("theorem", "not a known word")


def test_normalize_id_description_collapses_and_sanitizes() -> None:
    assert normalize_id_description("  a\t b\n\n c  ") == "a b c"
    assert normalize_id_description("interval [0, 1] | pipe") == "interval (0, 1) / pipe"


def test_collision_suffix_allocation_is_deterministic() -> None:
    taken = {"claim: x"}
    assert dedupe_descriptive_id("CLAIM: X", taken) == "CLAIM: X (2)"
    taken.add("claim: x (2)")
    assert dedupe_descriptive_id("CLAIM: X", taken) == "CLAIM: X (3)"
    assert dedupe_descriptive_id("CLAIM: Y", taken) == "CLAIM: Y"
    assert strip_collision_suffix("CLAIM: X (12)") == "CLAIM: X"
    assert strip_collision_suffix("CLAIM: X") == "CLAIM: X"


def test_overlong_descriptive_ids_are_rejected() -> None:
    overlong = "CLAIM: " + "x" * MAX_NODE_ID_LENGTH
    with pytest.raises(ValueError):
        validate_any_node_id(overlong)
    assert not is_descriptive_node_id(overlong)


def test_suggest_node_ids_finds_lexically_close_descriptive_ids() -> None:
    known = [
        "CLAIM: Halfspaces through the centroid keep at least a 1/e volume fraction",
        "APPROACH: Blaschke-Santalo symmetrization",
        "OBLIGATION: Close the induction step for arbitrary n",
    ]
    suggestions = suggest_node_ids(
        "CLAIM: Halfspaces thruogh the centroid keep at least a 1/e volume fraction",
        known,
    )
    assert suggestions[0] == known[0]


def test_suggest_node_ids_is_case_insensitive_and_deterministic() -> None:
    known = ["CLAIM: Every boundary object has property P", "CLM-ATSPTGT1"]
    first = suggest_node_ids("claim: every boundary object has property p.", known)
    second = suggest_node_ids("claim: every boundary object has property p.", known)
    assert first == second
    assert first[0] == known[0]


def test_suggest_node_ids_returns_nothing_for_unrelated_queries_or_empty_pools() -> None:
    assert suggest_node_ids("zzz", []) == []
    assert (
        suggest_node_ids(
            "CLAIM: Completely unrelated statement about topology",
            [
                "CLAIM: Every boundary object has property P",
            ],
        )
        == []
    )


def test_unknown_id_message_inlines_suggestions() -> None:
    message = unknown_id_message(
        "scientific report references unknown dependency node ID(s): ",
        ["CLAIM: Halfspaces thruogh the centroid keep volume"],
        ["CLAIM: Halfspaces through the centroid keep at least a 1/e volume fraction"],
    )
    assert message.startswith("scientific report references unknown dependency node ID(s): ")
    assert "did you mean" in message
    assert "CLAIM: Halfspaces through the centroid keep at least a 1/e volume fraction" in message


def test_unknown_id_message_omits_suggestions_when_nothing_is_close() -> None:
    message = unknown_id_message(
        "unknown: ",
        ["CLAIM: Something entirely different entirely"],
        ["CLAIM: Every boundary object has property P"],
    )
    assert message == "unknown: CLAIM: Something entirely different entirely"
