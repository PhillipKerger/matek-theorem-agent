"""Strict JSON Schema generation for model-backed structured outputs."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from pydantic import BaseModel


class StrictSchemaError(ValueError):
    """A Pydantic output schema cannot be represented by strict structured outputs."""

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        super().__init__(f"strict structured-output schema is unsupported at {path}: {detail}")


def strict_json_schema(output_type: type[BaseModel]) -> dict[str, Any]:
    """Build a closed schema accepted by OpenAI/Codex strict structured outputs."""

    schema = json.loads(
        json.dumps(output_type.model_json_schema(mode="serialization"), ensure_ascii=False)
    )
    _make_schema_strict(schema, path="$", seen=set())
    return cast(dict[str, Any], schema)


def strict_schema_sha256(output_type: type[BaseModel]) -> str:
    """Return a stable digest for the exact schema used by Codex."""

    encoded = json.dumps(
        strict_json_schema(output_type),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _make_schema_strict(node: Any, *, path: str, seen: set[int]) -> None:
    if isinstance(node, list):
        for index, item in enumerate(node):
            _make_schema_strict(item, path=f"{path}[{index}]", seen=seen)
        return
    if not isinstance(node, dict):
        return
    identity = id(node)
    if identity in seen:
        return
    seen.add(identity)
    node.pop("default", None)

    is_object = node.get("type") == "object" or "properties" in node
    if is_object:
        properties = node.get("properties", {})
        if not isinstance(properties, dict):
            raise StrictSchemaError(path, "object properties must be an object")
        additional = node.get("additionalProperties")
        if additional is not None and additional is not False:
            raise StrictSchemaError(
                path,
                "open dictionaries and arbitrary-key maps must be modeled as typed records",
            )
        node["additionalProperties"] = False
        node["required"] = list(properties)

    for key, value in node.items():
        if key in {"examples", "const", "enum"}:
            continue
        _make_schema_strict(value, path=f"{path}.{key}", seen=seen)
