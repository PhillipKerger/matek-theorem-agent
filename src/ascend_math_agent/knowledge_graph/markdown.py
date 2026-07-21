"""Obsidian-compatible Markdown/frontmatter parsing for knowledge nodes.

The vault intentionally uses a conservative flat YAML subset.  Values emitted by
ASCEND are JSON scalars or YAML lists of JSON strings, which remain valid YAML and
round-trip without a runtime YAML dependency.  Human-authored nested mappings are
rejected with a focused diagnostic instead of being silently flattened.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from .models import (
    ClaimType,
    EpistemicStatus,
    GraphEdge,
    GraphNode,
    NodeType,
    RelationType,
    WorkflowStatus,
)

GENERATED_START = "<!-- ASCEND:GENERATED:START -->"
GENERATED_END = "<!-- ASCEND:GENERATED:END -->"

_KEY = re.compile(r"\A[A-Za-z_][A-Za-z0-9_-]*\Z")
_WIKILINK = re.compile(r"\A\[\[([^]|]+)(?:\|[^]]+)?\]\]\Z")
_NODE_ID_IN_LINK = re.compile(r"\b([A-Z]{3}-[A-Z0-9]{8,64})\b")
_SECTION = re.compile(r"(?m)^##\s+(.+?)\s*$")


class GraphMarkdownError(ValueError):
    """Raised when a managed graph note cannot be parsed safely."""


def _decode_scalar(raw: str) -> str | int | bool | None:
    value = raw.strip()
    if not value:
        return ""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        if value in {"null", "~"}:
            return None
        if value.casefold() == "true":
            return True
        if value.casefold() == "false":
            return False
        if value.startswith(("{", "[", "&", "*", "!", ">", "|")):
            raise GraphMarkdownError(
                "frontmatter must remain flat; nested, anchored, tagged, or block values "
                "are not supported"
            ) from None
        return value
    if isinstance(decoded, (str, int, bool)) or decoded is None:
        return decoded
    raise GraphMarkdownError("frontmatter scalar must not contain a nested array or object")


def parse_flat_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Parse flat frontmatter and return it with the Markdown body."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise GraphMarkdownError("managed note must begin with YAML frontmatter")
    closing = normalized.find("\n---\n", 4)
    if closing < 0:
        raise GraphMarkdownError("managed note has no closing frontmatter delimiter")
    raw_frontmatter = normalized[4:closing]
    body = normalized[closing + 5 :]
    result: dict[str, object] = {}
    lines = raw_frontmatter.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            raise GraphMarkdownError(f"unexpected indented frontmatter line: {line!r}")
        key, separator, raw_value = line.partition(":")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            raise GraphMarkdownError(f"invalid flat frontmatter property: {line!r}")
        if key in result:
            raise GraphMarkdownError(f"duplicate frontmatter property: {key}")
        if raw_value.strip():
            result[key] = _decode_scalar(raw_value)
            continue
        items: list[str] = []
        while index < len(lines) and lines[index].startswith("  - "):
            item = _decode_scalar(lines[index][4:])
            if not isinstance(item, str):
                raise GraphMarkdownError(f"frontmatter list {key!r} must contain only strings")
            items.append(item)
            index += 1
        if index < len(lines) and lines[index].startswith((" ", "\t")):
            raise GraphMarkdownError(f"frontmatter property {key!r} uses unsupported nested YAML")
        result[key] = items
    return result, body


def _yaml_scalar(value: str | int | bool | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def format_flat_frontmatter(properties: Mapping[str, object]) -> str:
    """Render deterministic flat YAML accepted by Obsidian Properties."""

    lines = ["---"]
    for key, value in properties.items():
        if not _KEY.fullmatch(key):
            raise GraphMarkdownError(f"cannot render invalid frontmatter key: {key!r}")
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                if not isinstance(item, str):
                    raise GraphMarkdownError(f"frontmatter list {key!r} is not a string list")
                lines.append(f"  - {_yaml_scalar(item)}")
            continue
        if not (isinstance(value, (str, int, bool)) or value is None):
            raise GraphMarkdownError(f"frontmatter property {key!r} is not flat")
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def node_id_from_wikilink(value: str) -> str:
    match = _WIKILINK.fullmatch(value.strip())
    target = match.group(1) if match is not None else value.strip()
    node_match = _NODE_ID_IN_LINK.search(target)
    if node_match is None:
        raise GraphMarkdownError(f"relation target is not an ASCEND node link: {value!r}")
    return node_match.group(1)


def wikilink_for(node: GraphNode | str, *, title: str | None = None) -> str:
    label: str | None
    if isinstance(node, GraphNode):
        target = Path(node.path).stem if node.path is not None else node.ascend_id
        label = node.title if title is None else title
    else:
        target = node
        label = title
    return f"[[{target}|{label}]]" if label else f"[[{target}]]"


def _list_property(value: object, *, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise GraphMarkdownError(f"frontmatter property {key!r} must be a string list")
    return list(value)


def _string_property(
    properties: Mapping[str, object], key: str, *, required: bool = True
) -> str | None:
    value = properties.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise GraphMarkdownError(f"frontmatter property {key!r} must be a nonempty string")
    return value.strip()


def parse_node_note(path: Path, *, relative_path: str | None = None) -> GraphNode:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise GraphMarkdownError(f"graph note is not UTF-8: {path}") from exc
    properties, body = parse_flat_frontmatter(text)
    relations: list[GraphEdge] = []
    ascend_id = _string_property(properties, "ascend_id")
    assert ascend_id is not None
    for relation in RelationType:
        for raw_target in _list_property(properties.get(relation.value), key=relation.value):
            relations.append(
                GraphEdge(
                    source_id=ascend_id,
                    relation=relation,
                    target_id=node_id_from_wikilink(raw_target),
                )
            )
    known = {
        "ascend_id",
        "node_type",
        "problem_id",
        "title",
        "epistemic_status",
        "workflow_status",
        "claim_type",
        "statement_version",
        "created_in_run",
        "last_modified_run",
        "author_role",
        "created_at",
        "updated_at",
        "invalidation_reasons",
        "dependency_versions",
        "source_artifacts",
        "evidence",
        "manuscript_mappings",
        "tags",
        "ascend_tombstone",
        *(relation.value for relation in RelationType),
    }
    metadata: dict[str, str | int | bool | list[str] | None] = {}
    for key, value in properties.items():
        if key in known:
            continue
        if not (
            isinstance(value, (str, int, bool))
            or value is None
            or (isinstance(value, list) and all(isinstance(item, str) for item in value))
        ):
            raise GraphMarkdownError(f"unsupported frontmatter value for {key!r}")
        metadata[key] = value
    node_type = cast(str, _string_property(properties, "node_type"))
    problem_id = cast(str, _string_property(properties, "problem_id"))
    title = cast(str, _string_property(properties, "title"))
    epistemic_status = cast(str, _string_property(properties, "epistemic_status"))
    workflow_status = cast(str, _string_property(properties, "workflow_status"))
    created_in_run = cast(str, _string_property(properties, "created_in_run"))
    last_modified_run = cast(str, _string_property(properties, "last_modified_run"))
    author_role = cast(str, _string_property(properties, "author_role"))
    created_at = cast(str, _string_property(properties, "created_at"))
    updated_at = cast(str, _string_property(properties, "updated_at"))
    raw_statement_version = properties.get("statement_version", 1)
    if isinstance(raw_statement_version, bool) or not isinstance(raw_statement_version, int):
        raise GraphMarkdownError("frontmatter statement_version must be an integer")
    raw_claim_type = _string_property(properties, "claim_type", required=False)
    try:
        node = GraphNode(
            ascend_id=ascend_id,
            node_type=NodeType(node_type),
            problem_id=problem_id,
            title=title,
            epistemic_status=EpistemicStatus(epistemic_status),
            workflow_status=WorkflowStatus(workflow_status),
            claim_type=ClaimType(raw_claim_type) if raw_claim_type is not None else None,
            statement_version=raw_statement_version,
            created_in_run=created_in_run,
            last_modified_run=last_modified_run,
            author_role=author_role,
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
            body=body,
            tags=_list_property(properties.get("tags"), key="tags"),
            relations=relations,
            invalidation_reasons=_list_property(
                properties.get("invalidation_reasons"), key="invalidation_reasons"
            ),
            dependency_versions=_list_property(
                properties.get("dependency_versions"), key="dependency_versions"
            ),
            source_artifacts=_list_property(
                properties.get("source_artifacts"), key="source_artifacts"
            ),
            evidence=_list_property(properties.get("evidence"), key="evidence"),
            manuscript_mappings=_list_property(
                properties.get("manuscript_mappings"), key="manuscript_mappings"
            ),
            metadata=metadata,
            tombstone=bool(properties.get("ascend_tombstone", False)),
            path=relative_path or path.as_posix(),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    except (ValidationError, ValueError) as exc:
        raise GraphMarkdownError(f"invalid graph node {path}: {exc}") from exc
    return node


def _ordered_properties(node: GraphNode) -> dict[str, object]:
    properties: dict[str, object] = {
        "ascend_id": node.ascend_id,
        "node_type": node.node_type.value,
        "claim_type": node.claim_type.value if node.claim_type is not None else None,
        "problem_id": node.problem_id,
        "title": node.title,
        "epistemic_status": node.epistemic_status.value,
        "workflow_status": node.workflow_status.value,
        "statement_version": node.statement_version,
        "created_in_run": node.created_in_run,
        "last_modified_run": node.last_modified_run,
        "author_role": node.author_role,
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
    }
    by_relation: dict[RelationType, list[str]] = {}
    for edge in sorted(node.relations, key=lambda item: (item.relation.value, item.target_id)):
        by_relation.setdefault(edge.relation, []).append(wikilink_for(edge.target_id))
    for relation in RelationType:
        if relation in by_relation:
            properties[relation.value] = list(dict.fromkeys(by_relation[relation]))
    properties.update(
        {
            "invalidation_reasons": node.invalidation_reasons,
            "dependency_versions": node.dependency_versions,
            "source_artifacts": node.source_artifacts,
            "evidence": node.evidence,
            "manuscript_mappings": node.manuscript_mappings,
            "tags": node.tags,
            "ascend_tombstone": node.tombstone,
        }
    )
    for key in sorted(node.metadata):
        if key in properties or key in {relation.value for relation in RelationType}:
            raise GraphMarkdownError(f"node metadata collides with managed field {key!r}")
        properties[key] = node.metadata[key]
    return properties


def render_node_note(node: GraphNode) -> str:
    body = node.body.replace("\r\n", "\n").replace("\r", "\n")
    if GENERATED_START not in body or GENERATED_END not in body:
        body = new_generated_body(node.title, body)
    if not body.endswith("\n"):
        body += "\n"
    return format_flat_frontmatter(_ordered_properties(node)) + body


def new_generated_body(title: str, generated_content: str, human_content: str = "") -> str:
    human = human_content.strip()
    lines = [
        f"# {title}",
        "",
        GENERATED_START,
        generated_content.strip(),
        GENERATED_END,
        "",
        "## Human notes",
        "",
    ]
    if human:
        lines.append(human)
        lines.append("")
    return "\n".join(lines)


def replace_generated_section(existing_body: str, title: str, generated_content: str) -> str:
    start = existing_body.find(GENERATED_START)
    end = existing_body.find(GENERATED_END)
    if start < 0 or end < start:
        return new_generated_body(title, generated_content, existing_body)
    before = existing_body[: start + len(GENERATED_START)]
    after = existing_body[end:]
    updated = before.rstrip() + "\n" + generated_content.strip() + "\n" + after.lstrip()
    heading = re.compile(r"(?m)^#\s+.*$")
    if heading.search(updated):
        updated = heading.sub(f"# {title}", updated, count=1)
    else:
        updated = f"# {title}\n\n" + updated
    return updated if updated.endswith("\n") else updated + "\n"


def generated_section(body: str) -> str:
    start = body.find(GENERATED_START)
    end = body.find(GENERATED_END)
    if start < 0 or end < start:
        return body.strip()
    return body[start + len(GENERATED_START) : end].strip()


def exact_statement(body: str) -> str:
    """Extract a claim's exact-statement section for version invalidation."""

    generated = generated_section(body)
    matches = list(_SECTION.finditer(generated))
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != "exact statement":
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(generated)
        return generated[start:end].strip()
    return generated.strip()


def statement_hash(node: GraphNode) -> str:
    value = exact_statement(node.body) if node.node_type.value == "claim" else ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def machine_hash(node: GraphNode) -> str:
    """Hash machine-owned fields while allowing a human title/body/tag edit."""

    payload = {
        "ascend_id": node.ascend_id,
        "node_type": node.node_type.value,
        "problem_id": node.problem_id,
        "epistemic_status": node.epistemic_status.value,
        "workflow_status": node.workflow_status.value,
        "claim_type": node.claim_type.value if node.claim_type is not None else None,
        "statement_version": node.statement_version,
        "created_in_run": node.created_in_run,
        "last_modified_run": node.last_modified_run,
        "author_role": node.author_role,
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
        "relations": [
            edge.model_dump(mode="json")
            for edge in sorted(
                node.relations, key=lambda item: (item.relation.value, item.target_id)
            )
        ],
        "invalidation_reasons": node.invalidation_reasons,
        "dependency_versions": node.dependency_versions,
        "source_artifacts": node.source_artifacts,
        "evidence": node.evidence,
        "manuscript_mappings": node.manuscript_mappings,
        "metadata": {
            key: value for key, value in sorted(node.metadata.items()) if key.startswith("ascend_")
        },
        "tombstone": node.tombstone,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def note_hash(node: GraphNode) -> str:
    return hashlib.sha256(render_node_note(node).encode("utf-8")).hexdigest()


def edges_for(nodes: Iterable[GraphNode]) -> list[GraphEdge]:
    return [edge for node in nodes for edge in node.relations]


__all__ = [
    "GENERATED_END",
    "GENERATED_START",
    "GraphMarkdownError",
    "edges_for",
    "exact_statement",
    "format_flat_frontmatter",
    "generated_section",
    "machine_hash",
    "new_generated_body",
    "node_id_from_wikilink",
    "note_hash",
    "parse_flat_frontmatter",
    "parse_node_note",
    "render_node_note",
    "replace_generated_section",
    "statement_hash",
    "wikilink_for",
]
