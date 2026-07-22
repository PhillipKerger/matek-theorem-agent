"""Deterministic machine and human reports built only from persisted artifacts."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import RunState
from .state import ArtifactIntegrityError
from .workspace import atomic_write_json, atomic_write_text, sha256_file


class ArtifactLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    bytes: int = Field(ge=0)


class ReportNarrative(BaseModel):
    """Optional model-assisted prose; authoritative statuses remain deterministic."""

    model_config = ConfigDict(extra="forbid")

    executive_summary: str
    methodology_summary: str
    limitations: list[str] = Field(default_factory=list)

    @field_validator("executive_summary", "methodology_summary")
    @classmethod
    def _required_prose(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("report narrative prose must not be blank")
        return normalized


class FinalReport(BaseModel):
    """Superset of ``resources/schemas/final_report.schema.json``."""

    model_config = ConfigDict(extra="allow")

    run_id: str
    scientific_status: str
    manuscript_status: str
    lean_status: str
    strongest_result: str
    unresolved_obligations: list[str]
    artifacts: dict[str, ArtifactLink]
    original_problem: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    backend: dict[str, Any] = Field(default_factory=dict)
    backend_history: list[dict[str, Any]] = Field(default_factory=list)
    configuration: dict[str, Any] = Field(default_factory=dict)
    problem_clarification: dict[str, Any] = Field(default_factory=dict)
    literature_status: str = "unknown"
    literature_resolution_summary: str | None = None
    prompt_validation_warnings: list[str] = Field(default_factory=list)
    lean_consent: dict[str, Any] = Field(default_factory=dict)
    knowledge_graph: dict[str, Any] = Field(default_factory=dict)
    reproducibility: list[str] = Field(default_factory=list)
    narrative: ReportNarrative | None = None


class ReportArtifacts(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    report: FinalReport
    report_json: Path
    report_markdown: Path
    verification_certificate: Path
    hashes: dict[str, str]


def _artifact_inventory(run_root: Path) -> dict[str, ArtifactLink]:
    inventory: dict[str, ArtifactLink] = {}
    for path in sorted(run_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(run_root).as_posix()
        if (
            relative.startswith("report/")
            or relative in {"state.json", "state.json.bak", "state.json.tmp"}
            or relative.endswith(".tmp")
        ):
            continue
        inventory[relative] = ArtifactLink(
            path=relative,
            sha256=sha256_file(path),
            bytes=path.stat().st_size,
        )
    return inventory


def assert_report_certificate_inventory(run_root: Path) -> None:
    """Fail if the final certificate is missing, malformed, stale, or incomplete."""

    certificate_path = run_root.resolve() / "report" / "verification_certificate.json"
    try:
        payload = json.loads(certificate_path.read_text(encoding="utf-8"))
        raw_inventory = payload["artifact_hashes"]
    except (OSError, UnicodeError, KeyError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError(f"invalid verification certificate: {exc}") from exc
    if not isinstance(raw_inventory, dict) or not all(
        isinstance(path, str) and isinstance(digest, str) for path, digest in raw_inventory.items()
    ):
        raise ArtifactIntegrityError(
            "verification certificate artifact_hashes must be a string mapping"
        )
    expected = {str(path): str(digest) for path, digest in raw_inventory.items()}
    actual = {relative: entry.sha256 for relative, entry in _artifact_inventory(run_root).items()}
    if expected != actual:
        missing = sorted(actual.keys() - expected.keys())
        absent = sorted(expected.keys() - actual.keys())
        changed = sorted(
            path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
        )
        details = []
        if missing:
            details.append("uncertified=" + ", ".join(missing))
        if absent:
            details.append("missing=" + ", ".join(absent))
        if changed:
            details.append("changed=" + ", ".join(changed))
        raise ArtifactIntegrityError(
            "verification certificate inventory mismatch: " + "; ".join(details)
        )


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return ""


def build_final_report(
    state: RunState,
    *,
    narrative: ReportNarrative | None = None,
) -> FinalReport:
    """Build a report model without mutating the workspace."""

    run_root = state.run_root.resolve()
    metadata = state.metadata
    scientific = str(metadata.get("research_status", state.scientific_status.value))
    manuscript = str(metadata.get("manuscript_status", "NOT_STARTED"))
    lean = str(metadata.get("lean_status", "NOT_STARTED"))
    strongest = str(metadata.get("strongest_result", "No complete result was established."))
    raw_obligations = metadata.get("unresolved_obligations", [])
    obligations = (
        [str(item) for item in raw_obligations] if isinstance(raw_obligations, list) else []
    )
    usage = metadata.get("usage", {})
    backend = metadata.get("backend", {})
    raw_backend_history = metadata.get("backend_history", [])
    backend_history = (
        [dict(item) for item in raw_backend_history if isinstance(item, dict)]
        if isinstance(raw_backend_history, list)
        else []
    )
    configuration = metadata.get("configuration_summary", {})
    clarification = metadata.get("problem_clarification", {})
    raw_knowledge_graph = metadata.get("knowledge_graph", {})
    knowledge_graph = dict(raw_knowledge_graph) if isinstance(raw_knowledge_graph, dict) else {}
    graph_name = knowledge_graph.get("name")
    graph_commands = (
        [
            f"matek graph validate --knowledge-graph {graph_name}",
            f"matek graph status --knowledge-graph {graph_name}",
        ]
        if isinstance(graph_name, str) and graph_name
        else ["matek graph validate", "matek graph status"]
    )
    return FinalReport(
        run_id=state.run_id,
        scientific_status=scientific,
        manuscript_status=manuscript,
        lean_status=lean,
        strongest_result=strongest,
        unresolved_obligations=obligations,
        artifacts=_artifact_inventory(run_root),
        original_problem=_safe_text(run_root / "input" / "problem.md"),
        usage=dict(usage) if isinstance(usage, dict) else {},
        backend=dict(backend) if isinstance(backend, dict) else {},
        backend_history=backend_history,
        configuration=dict(configuration) if isinstance(configuration, dict) else {},
        problem_clarification=(dict(clarification) if isinstance(clarification, dict) else {}),
        literature_status=str(metadata.get("literature_status", "unknown")),
        literature_resolution_summary=(
            str(metadata["literature_resolution_summary"])
            if metadata.get("literature_resolution_summary")
            else None
        ),
        prompt_validation_warnings=(
            [str(item) for item in metadata.get("prompt_validation_warnings", [])]
            if isinstance(metadata.get("prompt_validation_warnings", []), list)
            else []
        ),
        lean_consent=(
            dict(metadata["lean_consent"]) if isinstance(metadata.get("lean_consent"), dict) else {}
        ),
        knowledge_graph=knowledge_graph,
        reproducibility=[
            f"matek status {state.run_id}",
            f"matek verify {state.run_id}",
            f"matek resume {state.run_id}",
            *graph_commands,
        ],
        narrative=narrative,
    )


def _markdown_link(relative: str) -> str:
    # Every artifact is one directory above report/; PurePosixPath keeps reports portable.
    return str(PurePosixPath("..") / PurePosixPath(relative))


def render_report_markdown(report: FinalReport) -> str:
    lines = [
        f"# MATEK report — `{report.run_id}`",
        "",
        "## Outcome",
        "",
        "| Gate | Status |",
        "| --- | --- |",
        f"| Research | `{report.scientific_status}` |",
        f"| Manuscript | `{report.manuscript_status}` |",
        f"| Lean | `{report.lean_status}` |",
        "",
    ]
    if report.problem_clarification.get("required") is True:
        lines.extend(
            [
                "## Problem clarification required",
                "",
                "MATEK stopped before research because the supplied description did not "
                "uniquely identify one mathematical target.",
                "",
                str(
                    report.problem_clarification.get(
                        "reason",
                        "The intended problem and exact success criterion were ambiguous.",
                    )
                ),
                "",
                "Please revise the problem file and address:",
                "",
            ]
        )
        raw_questions = report.problem_clarification.get("questions", [])
        if isinstance(raw_questions, list) and raw_questions:
            lines.extend(f"- {question}" for question in raw_questions)
        else:
            lines.append("- State the exact mathematical target and intended conclusion.")
        lines.extend(
            [
                "",
                str(
                    report.problem_clarification.get(
                        "next_action",
                        "Revise the problem file, then start a new MATEK run.",
                    )
                ),
                "",
            ]
        )

    if report.lean_consent:
        lines.extend(
            [
                "## Lean verification decision",
                "",
                f"- Outcome: `{report.lean_consent.get('outcome', 'unknown')}`",
                f"- Proceeded: `{report.lean_consent.get('proceed', False)}`",
                "",
            ]
        )

    if report.knowledge_graph:
        graph_name = str(report.knowledge_graph.get("name", "unknown"))
        graph_vault = str(report.knowledge_graph.get("vault", f".matek/knowledge/{graph_name}"))
        graph_index = str(
            report.knowledge_graph.get("index", f".matek/knowledge/{graph_name}/graph-index.sqlite")
        )
        lines.extend(
            [
                "## Persistent knowledge graph",
                "",
                f"- Graph: `{graph_name}`",
                f"- Problem node: `{report.knowledge_graph.get('problem_id', 'unknown')}`",
                f"- Revision: `{report.knowledge_graph.get('revision', 'unknown')}`",
                f"- Obsidian vault: [open Home](../../../knowledge/{graph_name}/Home.md) "
                f"(project path: `{graph_vault}`)",
                f"- Rebuildable index: project path `{graph_index}`",
                "",
            ]
        )

    lines.extend(
        [
            "## Prior literature assessment",
            "",
            f"- Classification: `{report.literature_status}`",
        ]
    )
    if report.literature_resolution_summary:
        lines.append(f"- Assessment: {report.literature_resolution_summary}")
    if report.prompt_validation_warnings:
        lines.extend(["", "## Prompt validation warnings", ""])
        lines.extend(f"- {warning}" for warning in report.prompt_validation_warnings)
    lines.extend(
        [
            "",
            "## Strongest established result",
            "",
            report.strongest_result,
            "",
            "## Unresolved obligations",
            "",
        ]
    )
    if report.unresolved_obligations:
        lines.extend(f"- {obligation}" for obligation in report.unresolved_obligations)
    else:
        lines.append("None recorded.")

    provider = report.backend.get("provider", "unknown")
    authentication = report.backend.get("authentication_class", "unverified")
    authentication_description = {
        "chatgpt": "ChatGPT subscription",
        "api_key": "Codex API-key login",
        "access_token": "Codex access token",
        "authenticated_unknown": "authenticated (method unknown)",
        "platform_api_key": "OpenAI Platform API key",
        "not_configured": "not configured",
        "not_authenticated": "not authenticated",
        "unverified": "unverified",
        None: "unverified",
    }.get(authentication, str(authentication))
    if provider == "codex":
        backend_description = "Codex CLI using saved Codex authentication"
        if authentication == "chatgpt":
            backend_description = "Codex CLI using saved ChatGPT authentication"
    elif provider == "api":
        backend_description = "OpenAI Responses API using Platform API billing"
    else:
        backend_description = "Unknown (legacy run without provider provenance)"
    requested_model = report.backend.get("model_requested")
    if requested_model is None:
        requested_model = "Codex default" if provider == "codex" else "unobserved"
    requested_effort = report.backend.get("reasoning_effort_requested", "unobserved")
    search_setting = (
        report.backend.get("web_search_enabled", "unobserved")
        if report.backend.get("completed_calls", 0)
        else report.backend.get(
            "web_search_policy",
            report.backend.get("web_search_enabled", "unobserved"),
        )
    )
    lines.extend(
        [
            "",
            "## Model execution backend",
            "",
            f"- Backend: {backend_description}",
            f"- Authentication class: {authentication_description} (`{authentication}`)",
            f"- Backend version: `{report.backend.get('backend_version') or 'unobserved'}`",
            f"- Requested model: `{requested_model}`",
            f"- Requested reasoning effort: `{requested_effort}`",
            f"- Live web search: `{search_setting}`",
            f"- Automatic provider fallback: `{report.backend.get('automatic_fallback', False)}`",
        ]
    )
    if report.backend_history:
        lines.extend(["", "### Explicit provider migrations", ""])
        for migration in report.backend_history:
            lines.append(
                "- "
                f"`{migration.get('from', 'unknown')}` → `{migration.get('to', 'unknown')}` "
                f"at `{migration.get('changed_at', 'unknown time')}` — "
                f"{migration.get('reason', 'explicit provider migration')}"
            )
    lines.extend(["", "## Original problem", "", "~~~~text"])
    lines.append(report.original_problem.rstrip())
    lines.extend(["~~~~", "", "## Usage", "", "```json"])
    lines.extend([json.dumps(report.usage, indent=2, sort_keys=True), "```"])
    if provider == "codex":
        lines.extend(
            [
                "",
                "Codex token/call observations are shown when available. MATEK does not "
                "convert ChatGPT/Codex allowance or credits into an estimated dollar cost.",
            ]
        )
    if report.narrative is not None:
        lines.extend(
            [
                "",
                "## Optional model-assisted narrative",
                "",
                report.narrative.executive_summary,
                "",
                "### Methodology summary",
                "",
                report.narrative.methodology_summary,
                "",
                "### Limitations",
                "",
            ]
        )
        if report.narrative.limitations:
            lines.extend(f"- {item}" for item in report.narrative.limitations)
        else:
            lines.append("No additional limitations were supplied by the optional rewrite.")
        lines.extend(
            [
                "",
                "> This prose is model-assisted. The deterministic status table, artifact "
                "hashes, and verification certificate are authoritative.",
                "",
                "## Artifacts",
                "",
            ]
        )
    else:
        lines.extend(["", "## Artifacts", ""])
    if report.artifacts:
        for relative, entry in sorted(report.artifacts.items()):
            lines.append(
                f"- [`{relative}`]({_markdown_link(relative)}) — "
                f"`sha256:{entry.sha256}` ({entry.bytes} bytes)"
            )
    else:
        lines.append("No stage artifacts were recorded.")
    lines.extend(["", "## Reproduce", "", "```bash", *report.reproducibility, "```", ""])
    lines.append(
        "This report is generated from persisted artifacts. Model confidence is not a "
        "substitute for bibliography, compiler, statement-alignment, or Lean verification gates."
    )
    lines.append("")
    return "\n".join(lines)


def write_final_report(
    state: RunState,
    *,
    narrative: ReportNarrative | None = None,
) -> ReportArtifacts:
    """Atomically replace all contracted report files without making model calls."""

    run_root = state.run_root.resolve()
    report_dir = run_root / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = build_final_report(state, narrative=narrative)
    report_json = atomic_write_json(
        report_dir / "report.json",
        report.model_dump(mode="json"),
        confinement_root=run_root,
    )
    report_markdown = atomic_write_text(
        report_dir / "REPORT.md",
        render_report_markdown(report),
        confinement_root=run_root,
    )
    certificate_data = {
        "schema_version": 1,
        "run_id": state.run_id,
        "artifact_hashes": {
            relative: entry.sha256 for relative, entry in sorted(report.artifacts.items())
        },
        "bibliography_status": report.manuscript_status,
        "lean_status": report.lean_status,
        "deterministic_verification_passed": bool(
            state.metadata.get("deterministic_verification_passed", False)
        ),
        "approved_axioms": state.metadata.get("approved_axioms", []),
    }
    certificate = atomic_write_json(
        report_dir / "verification_certificate.json",
        certificate_data,
        confinement_root=run_root,
    )
    paths = {
        "report/REPORT.md": report_markdown,
        "report/report.json": report_json,
        "report/verification_certificate.json": certificate,
    }
    return ReportArtifacts(
        report=report,
        report_json=report_json,
        report_markdown=report_markdown,
        verification_certificate=certificate,
        hashes={name: sha256_file(path) for name, path in paths.items()},
    )


def load_final_report(run_root: Path) -> ReportArtifacts:
    """Load existing report files without changing a completed run."""

    report_dir = run_root.resolve() / "report"
    report_json = report_dir / "report.json"
    report_markdown = report_dir / "REPORT.md"
    certificate = report_dir / "verification_certificate.json"
    for path in (report_json, report_markdown, certificate):
        if not path.is_file():
            raise FileNotFoundError(path)
    report = FinalReport.model_validate_json(report_json.read_text(encoding="utf-8"))
    paths = {
        "report/REPORT.md": report_markdown,
        "report/report.json": report_json,
        "report/verification_certificate.json": certificate,
    }
    return ReportArtifacts(
        report=report,
        report_json=report_json,
        report_markdown=report_markdown,
        verification_certificate=certificate,
        hashes={name: sha256_file(path) for name, path in paths.items()},
    )
