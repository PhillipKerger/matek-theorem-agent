"""Redacted event and usage JSONL logs for one run workspace."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .budget import UsageRecord
from .redaction import RedactionResult, SecretRedactor
from .workspace import atomic_write_json, ensure_path_confined

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class JournalCorruptionError(RuntimeError):
    """Raised when an audit journal cannot be trusted in full."""


class ModelCallJournalError(JournalCorruptionError):
    pass


class ModelRequestAudit(BaseModel):
    """Persistable request fields; raw instructions and input are never stored."""

    model_config = ConfigDict(extra="forbid", strict=True)

    model: str
    reasoning_mode: str
    reasoning_effort: str
    web_search: bool
    maximum_web_search_calls: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    instructions_sha256: str
    input_text_sha256: str

    @field_validator("instructions_sha256", "input_text_sha256")
    @classmethod
    def _hash_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("request content hashes must be lowercase SHA-256")
        return value


class ModelCallIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: str
    stage: str
    cache_namespace: str
    output_schema: str
    request: ModelRequestAudit

    @field_validator("request_key")
    @classmethod
    def _key_is_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("model-call request key must be lowercase SHA-256")
        return value


class ModelCallRecord(BaseModel):
    """One complete, redacted, replayable paid-response checkpoint."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    created_at: datetime
    request_key: str
    stage: str
    cache_namespace: str
    output_schema: str
    request: ModelRequestAudit
    response_id: str
    status: str
    usage: UsageRecord
    tool_metadata: list[dict[str, Any]] = Field(default_factory=list)
    parsed: Any

    @field_validator("request_key")
    @classmethod
    def _key_is_sha256(cls, value: str) -> str:
        return ModelCallIdentity._key_is_sha256(value)

    @field_validator("stage", "cache_namespace", "output_schema", "response_id", "status")
    @classmethod
    def _text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model-call audit fields must not be blank")
        return value.strip()


class _UsageJournalEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    run_id: str
    usage: UsageRecord
    stage: str | None = None


def load_usage_journal_strict(path: Path) -> list[UsageRecord]:
    """Load every usage row or reject the entire journal.

    Budget recovery must never silently skip malformed, torn, or duplicate paid-call
    entries, because doing so could undercount spend on resume.
    """

    if not path.is_file():
        return []
    records: list[UsageRecord] = []
    response_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                raise JournalCorruptionError(
                    f"usage journal {path} contains a blank row at line {line_number}"
                )
            try:
                entry = _UsageJournalEntry.model_validate_json(line)
            except (ValidationError, ValueError) as exc:
                raise JournalCorruptionError(
                    f"usage journal {path} is invalid at line {line_number}"
                ) from exc
            response_id = entry.usage.response_id
            if response_id is not None:
                if not response_id.strip():
                    raise JournalCorruptionError(
                        f"usage journal {path} has a blank response ID at line {line_number}"
                    )
                if response_id in response_ids:
                    raise JournalCorruptionError(
                        f"usage journal {path} repeats response ID {response_id!r}"
                    )
                response_ids.add(response_id)
            records.append(entry.usage)
    return records


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


class JsonlLogger:
    """Append one redacted JSON object per durable line."""

    def __init__(
        self,
        path: Path,
        *,
        confinement_root: Path | None = None,
        redactor: SecretRedactor | None = None,
        redaction_log: Path | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = (
            ensure_path_confined(confinement_root, path)
            if confinement_root is not None
            else path.expanduser().resolve(strict=False)
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self._redactor = redactor or SecretRedactor()
        self._redaction_log = (
            ensure_path_confined(confinement_root, redaction_log)
            if confinement_root is not None and redaction_log is not None
            else redaction_log
        )
        self._clock = clock
        self._lock = threading.Lock()

    @staticmethod
    def _encoded_line(record: Any) -> bytes:
        return (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=_json_default,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _append(path: Path, encoded: bytes) -> None:
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written == 0:  # pragma: no cover - defensive OS failure handling
                    raise OSError("zero-byte write while appending JSONL log")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write(self, record: Mapping[str, Any] | BaseModel) -> RedactionResult[Any]:
        raw: Any = record.model_dump(mode="python") if isinstance(record, BaseModel) else record
        result = self._redactor.redact_data_result(raw)
        encoded = self._encoded_line(result.value)
        with self._lock:
            self._append(self.path, encoded)
            if result.changed and self._redaction_log is not None:
                notice = {
                    "timestamp": _timestamp(self._clock()),
                    "target": self.path.name,
                    "replacements": result.replacements,
                    "categories": list(result.categories),
                }
                self._redaction_log.parent.mkdir(parents=True, exist_ok=True)
                self._append(self._redaction_log, self._encoded_line(notice))
        return result


class ModelCallStore:
    """Atomic request-keyed response cache and per-call audit store."""

    def __init__(
        self,
        path: Path,
        *,
        confinement_root: Path,
        redactor: SecretRedactor,
        redaction_log: Path,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = ensure_path_confined(confinement_root, path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._confinement_root = confinement_root
        self._redactor = redactor
        self._redaction_log = ensure_path_confined(confinement_root, redaction_log)
        self._clock = clock
        self._lock = threading.Lock()

    def identity(self, normalized_request: Mapping[str, Any]) -> ModelCallIdentity:
        """Redact and identify a normalized request without retaining its prompt."""

        redacted = self._redactor.redact_data_result(dict(normalized_request)).value
        if not isinstance(redacted, Mapping):  # pragma: no cover - dict remains a mapping
            raise ModelCallJournalError("normalized model request did not remain a mapping")
        settings = redacted.get("settings")
        if not isinstance(settings, Mapping):
            raise ModelCallJournalError("normalized model request has invalid settings")
        instructions = redacted.get("instructions")
        input_text = redacted.get("input_text")
        output_schema = redacted.get("output_schema")
        stage = redacted.get("stage")
        cache_namespace = redacted.get("cache_namespace")
        if (
            not isinstance(instructions, str)
            or not isinstance(input_text, str)
            or not isinstance(output_schema, str)
            or not isinstance(stage, str)
            or not stage.strip()
            or not isinstance(cache_namespace, str)
            or not cache_namespace.strip()
        ):
            raise ModelCallJournalError("normalized model request has invalid text fields")

        canonical = json.dumps(
            redacted,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
        request_key = sha256(canonical).hexdigest()
        try:
            request_audit = ModelRequestAudit.model_validate(
                {
                    "model": settings.get("model"),
                    "reasoning_mode": settings.get("reasoning_mode"),
                    "reasoning_effort": settings.get("reasoning_effort"),
                    "web_search": settings.get("web_search"),
                    "maximum_web_search_calls": settings.get("maximum_web_search_calls"),
                    "max_output_tokens": settings.get("max_output_tokens"),
                    "instructions_sha256": sha256(instructions.encode("utf-8")).hexdigest(),
                    "input_text_sha256": sha256(input_text.encode("utf-8")).hexdigest(),
                }
            )
        except ValidationError as exc:
            raise ModelCallJournalError(
                "normalized model request has invalid audit fields"
            ) from exc
        return ModelCallIdentity(
            request_key=request_key,
            stage=stage.strip(),
            cache_namespace=cache_namespace.strip(),
            output_schema=output_schema,
            request=request_audit,
        )

    def _record_path(self, request_key: str) -> Path:
        ModelCallIdentity._key_is_sha256(request_key)
        return ensure_path_confined(self._confinement_root, self.path / f"{request_key}.json")

    def load(self, identity: ModelCallIdentity) -> ModelCallRecord | None:
        """Load and fully validate a replay checkpoint, failing closed on corruption."""

        path = self._record_path(identity.request_key)
        if not path.is_file():
            return None
        try:
            record = ModelCallRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise ModelCallJournalError(f"model-call checkpoint is invalid: {path}") from exc
        if (
            record.request_key != identity.request_key
            or record.stage != identity.stage
            or record.cache_namespace != identity.cache_namespace
            or record.output_schema != identity.output_schema
            or record.request != identity.request
        ):
            raise ModelCallJournalError(
                f"model-call checkpoint identity does not match its request key: {path}"
            )
        return record

    def load_by_request_key(
        self,
        request_key: str,
        *,
        expected_stage: str,
        expected_cache_namespace: str,
    ) -> ModelCallRecord | None:
        """Load a checkpoint when a durable caller already owns its exact key.

        This is intentionally scoped by stage and cache generation. It supports
        crash recovery of a scheduler request map without making unrelated or
        archived model calls transferable budget credit.
        """

        path = self._record_path(request_key)
        if not path.is_file():
            return None
        try:
            record = ModelCallRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise ModelCallJournalError(f"model-call checkpoint is invalid: {path}") from exc
        if (
            record.request_key != request_key
            or record.stage != expected_stage
            or record.cache_namespace != expected_cache_namespace
        ):
            raise ModelCallJournalError(
                f"model-call checkpoint does not match the owning scheduler scope: {path}"
            )
        return record

    def persist(
        self,
        identity: ModelCallIdentity,
        *,
        stage: str,
        response_id: str,
        status: str,
        usage: UsageRecord,
        tool_metadata: list[dict[str, Any]],
        parsed: Any,
    ) -> ModelCallRecord:
        """Durably checkpoint a redacted completed result before returning it."""

        if not response_id.strip():
            raise ModelCallJournalError("a completed model response has no response ID")
        if stage.strip() != identity.stage:
            raise ModelCallJournalError("model-call persistence stage does not match request key")
        raw = {
            "schema_version": 1,
            "created_at": _timestamp(self._clock()),
            "request_key": identity.request_key,
            "stage": stage,
            "cache_namespace": identity.cache_namespace,
            "output_schema": identity.output_schema,
            "request": identity.request,
            "response_id": response_id,
            "status": status,
            "usage": usage,
            "tool_metadata": tool_metadata,
            "parsed": parsed,
        }
        redaction = self._redactor.redact_data_result(raw)
        try:
            candidate = ModelCallRecord.model_validate(redaction.value)
        except ValidationError as exc:
            raise ModelCallJournalError("redacted model-call checkpoint is invalid") from exc
        path = self._record_path(identity.request_key)
        with self._lock:
            existing = self.load(identity)
            if existing is not None:
                return existing
            atomic_write_json(
                path,
                candidate.model_dump(mode="json"),
                confinement_root=self._confinement_root,
                mode=0o600,
            )
            if redaction.changed:
                notice = {
                    "timestamp": _timestamp(self._clock()),
                    "target": str(path.relative_to(self._confinement_root)),
                    "replacements": redaction.replacements,
                    "categories": list(redaction.categories),
                }
                JsonlLogger._append(self._redaction_log, JsonlLogger._encoded_line(notice))
        return candidate


class RunLogger:
    """Write the three logs required by the artifact contract."""

    def __init__(
        self,
        run_root: Path,
        *,
        run_id: str | None = None,
        secrets: tuple[str, ...] = (),
        model_cache_namespace: str = "default",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.run_root = run_root.expanduser().resolve(strict=True)
        self.run_id = run_id or self.run_root.name
        if not model_cache_namespace.strip():
            raise ValueError("model cache namespace must not be blank")
        self.model_cache_namespace = model_cache_namespace.strip()
        self._clock = clock
        logs_root = ensure_path_confined(self.run_root, self.run_root / "logs")
        logs_root.mkdir(exist_ok=True)
        redaction_path = ensure_path_confined(logs_root, logs_root / "redaction.log")
        descriptor = os.open(redaction_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        redactor = SecretRedactor(secrets)
        self.events = JsonlLogger(
            logs_root / "events.jsonl",
            confinement_root=self.run_root,
            redactor=redactor,
            redaction_log=redaction_path,
            clock=clock,
        )
        self.usages = JsonlLogger(
            logs_root / "usage.jsonl",
            confinement_root=self.run_root,
            redactor=redactor,
            redaction_log=redaction_path,
            clock=clock,
        )
        self.model_calls = ModelCallStore(
            logs_root / "model_calls",
            confinement_root=self.run_root,
            redactor=redactor,
            redaction_log=redaction_path,
            clock=clock,
        )
        self.redaction_path = redaction_path
        self._usage_index_lock = threading.Lock()
        self._usage_response_ids: set[str] | None = None

    def event(
        self,
        event: str,
        *,
        level: LogLevel = "INFO",
        stage: str | Enum | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> RedactionResult[Any]:
        if not event.strip():
            raise ValueError("event name must not be blank")
        if level not in _LOG_LEVELS:
            raise ValueError(f"invalid log level: {level}")
        record: dict[str, Any] = {
            "timestamp": _timestamp(self._clock()),
            "run_id": self.run_id,
            "level": level,
            "event": event.strip(),
            "data": dict(data or {}),
        }
        if stage is not None:
            record["stage"] = stage.value if isinstance(stage, Enum) else stage
        return self.events.write(record)

    def usage(
        self,
        usage: UsageRecord | Mapping[str, Any],
        *,
        stage: str | Enum | None = None,
    ) -> RedactionResult[Any]:
        record: dict[str, Any] = {
            "timestamp": _timestamp(self._clock()),
            "run_id": self.run_id,
            "usage": (
                usage.model_dump(mode="python") if isinstance(usage, UsageRecord) else dict(usage)
            ),
        }
        if stage is not None:
            record["stage"] = stage.value if isinstance(stage, Enum) else stage
        with self._usage_index_lock:
            result = self.usages.write(record)
            response_id = record["usage"].get("response_id")
            if self._usage_response_ids is not None and isinstance(response_id, str):
                self._usage_response_ids.add(response_id)
            return result

    def usage_once(
        self,
        usage: UsageRecord,
        *,
        stage: str | Enum | None = None,
    ) -> bool:
        """Durably log a paid response exactly once by provider response ID."""

        response_id = usage.response_id
        if response_id is None or not response_id.strip():
            raise JournalCorruptionError("paid usage must have a nonblank response ID")
        with self._usage_index_lock:
            if self._usage_response_ids is None:
                existing = load_usage_journal_strict(self.usages.path)
                self._usage_response_ids = {
                    record.response_id for record in existing if record.response_id is not None
                }
            if response_id in self._usage_response_ids:
                return False
            record: dict[str, Any] = {
                "timestamp": _timestamp(self._clock()),
                "run_id": self.run_id,
                "usage": usage.model_dump(mode="python"),
            }
            if stage is not None:
                record["stage"] = stage.value if isinstance(stage, Enum) else stage
            self.usages.write(record)
            self._usage_response_ids.add(response_id)
            return True

    # Verb-prefixed aliases read naturally in injected application services.
    log_event = event
    log_usage = usage


EventLogger = RunLogger


__all__ = [
    "EventLogger",
    "JournalCorruptionError",
    "JsonlLogger",
    "LogLevel",
    "ModelCallIdentity",
    "ModelCallJournalError",
    "ModelCallRecord",
    "ModelCallStore",
    "ModelRequestAudit",
    "RunLogger",
    "load_usage_journal_strict",
]
