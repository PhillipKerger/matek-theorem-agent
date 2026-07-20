"""Offline-first environment diagnostics for the CLI.

The ordinary doctor command performs only local executable, capability, and saved-login
checks.  It never sends a prompt to a model.  The opt-in ``deep`` path is deliberately
separate because it consumes Codex allowance, and the opt-in ``online`` path checks only
the advanced OpenAI API backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import AppConfig
from .redaction import redact_text, sanitized_environment
from .resources import read_resource_bytes

EXPECTED_FRAMEWORK_SHA256 = "bd724294a261f4bc2e5da2191813e40c1340bc6ee039c753cb5c60276e7a512c"


class CheckLevel(StrEnum):
    PASS = "pass"
    WARNING = "warning"
    FAILURE = "failure"


class DoctorGroup(StrEnum):
    ENVIRONMENT = "ASCEND environment"
    CODEX = "Codex backend (recommended and default)"
    API = "OpenAI API backend (advanced and optional)"
    RESEARCH_TOOLS = "Research tools"


class CodexAuthentication(StrEnum):
    CHATGPT = "chatgpt"
    API_KEY = "api_key"
    ACCESS_TOKEN = "access_token"
    AUTHENTICATED_UNKNOWN = "authenticated_unknown"
    NOT_AUTHENTICATED = "not_authenticated"
    ERROR = "error"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    level: CheckLevel
    detail: str
    remediation: str | None = None
    group: DoctorGroup = DoctorGroup.ENVIRONMENT


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]
    default_backend: str = "codex"

    @property
    def failures(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.level is CheckLevel.FAILURE)

    @property
    def warnings(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.level is CheckLevel.WARNING)

    def checks_for(self, group: DoctorGroup) -> tuple[DoctorCheck, ...]:
        """Return checks for a presentation group without duplicating diagnostics."""

        return tuple(check for check in self.checks if check.group is group)


CommandRunner = Callable[[Sequence[str], Path], tuple[int, str, str]]
OnlineProbe = Callable[[], str]
CodexDeepProbe = Callable[[str, Path], str]

_CODEX_LOGIN_REMEDIATION = (
    "Run: codex login; choose Sign in with ChatGPT; then rerun: ascend doctor"
)


def _run_version(argv: Sequence[str], cwd: Path) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=sanitized_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", redact_text(str(exc))
    return (
        completed.returncode,
        redact_text(completed.stdout.strip()),
        redact_text(completed.stderr.strip()),
    )


def _executable_check(
    *,
    name: str,
    executable: str,
    args: Sequence[str],
    root: Path,
    required: bool,
    remediation: str,
    group: DoctorGroup,
    which: Callable[[str], str | None],
    command_runner: CommandRunner,
) -> DoctorCheck:
    path = which(executable)
    if path is None:
        level = CheckLevel.FAILURE if required else CheckLevel.WARNING
        return DoctorCheck(name, level, f"{executable!r} was not found on PATH", remediation, group)
    code, stdout, stderr = command_runner((path, *args), root)
    if code != 0:
        level = CheckLevel.FAILURE if required else CheckLevel.WARNING
        detail = stderr or stdout or f"exited with status {code}"
        return DoctorCheck(name, level, detail, remediation, group)
    first_line = (stdout or stderr or path).splitlines()[0]
    return DoctorCheck(name, CheckLevel.PASS, first_line, group=group)


def _default_online_probe() -> str:
    # Imported only for an explicitly requested advanced API check.
    from openai import OpenAI

    page = OpenAI(timeout=10.0, max_retries=0).models.list()
    return f"OpenAI API reachable ({len(page.data)} models visible)"


def _default_codex_deep_probe(executable: str, project_root: Path) -> str:
    """Make one opt-in, minimal structured Codex call in a disposable directory."""

    del project_root
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    with tempfile.TemporaryDirectory(prefix="ascend-codex-doctor-") as temporary_name:
        temporary = Path(temporary_name).resolve()
        schema_path = temporary / "schema.json"
        output_path = temporary / "result.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        argv = (
            executable,
            "--ask-for-approval",
            "never",
            "--search",
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-C",
            str(temporary),
            "-",
        )
        try:
            completed = subprocess.run(
                argv,
                cwd=temporary,
                env=sanitized_environment(),
                input=(
                    "ASCEND doctor probe. Do not run shell commands or modify files. "
                    "Use web search to find the official Lean theorem prover website, then "
                    "return exactly the requested JSON object with ok set to true."
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(type(exc).__name__) from exc
        if completed.returncode != 0:
            raise RuntimeError(f"Codex exited with status {completed.returncode}")
        try:
            parsed: Any = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Codex did not produce valid structured output") from exc
        if parsed != {"ok": True}:
            raise RuntimeError("Codex returned an unexpected structured probe result")
        source_metadata = _codex_jsonl_has_source_urls(completed.stdout)
    if source_metadata:
        return "live structured-output probe succeeded; search source URL metadata is available"
    return (
        "live structured-output probe succeeded; this Codex version did not emit search source "
        "URLs, so ASCEND will use its deterministic source resolver"
    )


def _codex_jsonl_has_source_urls(text: str) -> bool:
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping):
            continue
        if value.get("type") == "url_citation" and isinstance(value.get("url"), str):
            return True
        item = value.get("item")
        if not isinstance(item, Mapping):
            continue
        action = item.get("action")
        sources = action.get("sources") if isinstance(action, Mapping) else item.get("sources")
        if not isinstance(sources, list):
            continue
        if any(
            isinstance(source, Mapping) and isinstance(source.get("url"), str) for source in sources
        ):
            return True
    return False


def _configured_backend(config: AppConfig) -> str:
    backend = getattr(config, "backend", None)
    provider = getattr(backend, "provider", "codex")
    return provider if provider in {"codex", "api"} else "codex"


def _configured_codex_executable(config: AppConfig) -> str:
    codex = getattr(config, "codex", None)
    executable = getattr(codex, "executable", None)
    if isinstance(executable, str) and executable.strip():
        return executable.strip()
    return config.lean.codex_command


def _codex_sessions_enabled(config: AppConfig) -> bool:
    codex = getattr(config, "codex", None)
    return bool(getattr(codex, "persist_sessions", False))


def _codex_skips_git_check(config: AppConfig) -> bool:
    codex = getattr(config, "codex", None)
    return bool(getattr(codex, "skip_git_repo_check", False))


def _has_flag(help_text: str, *spellings: str) -> bool:
    return any(
        re.search(rf"(?m)(?:^|[\s,]){re.escape(spelling)}(?:[\s,=<]|$)", help_text) is not None
        for spelling in spellings
    )


def _has_subcommand(help_text: str, command: str) -> bool:
    return re.search(rf"(?m)^\s*{re.escape(command)}(?:\s|$)", help_text) is not None


def _classify_codex_auth(code: int, stdout: str, stderr: str) -> CodexAuthentication:
    """Classify public ``codex login status`` output without exposing its contents."""

    normalized = f"{stdout}\n{stderr}".casefold()
    if code != 0:
        if any(
            marker in normalized
            for marker in ("not logged in", "not authenticated", "not signed in", "logged out")
        ):
            return CodexAuthentication.NOT_AUTHENTICATED
        return CodexAuthentication.ERROR
    if "chatgpt" in normalized:
        return CodexAuthentication.CHATGPT
    if "api key" in normalized or "api-key" in normalized:
        return CodexAuthentication.API_KEY
    if "access token" in normalized:
        return CodexAuthentication.ACCESS_TOKEN
    return CodexAuthentication.AUTHENTICATED_UNKNOWN


def _codex_checks(
    config: AppConfig,
    root: Path,
    *,
    deep: bool,
    which: Callable[[str], str | None],
    command_runner: CommandRunner,
    deep_probe: CodexDeepProbe | None,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    executable = _configured_codex_executable(config)
    # Codex is the model backend by default and also remains the existing
    # write-capable Lean formalizer when an API run keeps Lean enabled.
    required = _configured_backend(config) == "codex" or config.lean.enabled
    absent_level = CheckLevel.FAILURE if required else CheckLevel.WARNING
    path = which(executable)
    if path is None:
        checks.append(
            DoctorCheck(
                "Codex CLI",
                absent_level,
                f"{executable!r} was not found on PATH",
                "Install Codex using an official method, then run: codex login",
                DoctorGroup.CODEX,
            )
        )
        return checks

    code, stdout, stderr = command_runner((path, "--version"), root)
    if code != 0:
        checks.append(
            DoctorCheck(
                "Codex CLI",
                absent_level,
                f"version check exited with status {code}",
                "Reinstall or update Codex, then rerun: ascend doctor",
                DoctorGroup.CODEX,
            )
        )
        return checks
    version = (stdout or stderr or "installed version unavailable").splitlines()[0]
    checks.append(DoctorCheck("Codex CLI", CheckLevel.PASS, version, group=DoctorGroup.CODEX))

    git_repository = (root / ".git").is_dir() or (root / ".git").is_file()
    skip_git_check = _codex_skips_git_check(config)
    checks.append(
        DoctorCheck(
            "Codex workspace",
            CheckLevel.PASS
            if git_repository
            else (CheckLevel.WARNING if skip_git_check else absent_level),
            "Git repository detected"
            if git_repository
            else (
                "Git repository check explicitly disabled"
                if skip_git_check
                else "project root is not a Git repository"
            ),
            None
            if git_repository or skip_git_check
            else "Run ASCEND inside the intended Git repository, or explicitly set "
            "codex.skip_git_repo_check = true.",
            DoctorGroup.CODEX,
        )
    )

    root_code, root_stdout, root_stderr = command_runner((path, "--help"), root)
    exec_code, exec_stdout, exec_stderr = command_runner((path, "exec", "--help"), root)
    if root_code != 0 or exec_code != 0:
        checks.append(
            DoctorCheck(
                "Codex capabilities",
                absent_level,
                "could not inspect Codex help output",
                "Update Codex using an official installation method.",
                DoctorGroup.CODEX,
            )
        )
    else:
        root_help = f"{root_stdout}\n{root_stderr}"
        exec_help = f"{exec_stdout}\n{exec_stderr}"
        capabilities = (
            ("codex exec", _has_subcommand(root_help, "exec")),
            ("codex login", _has_subcommand(root_help, "login")),
            ("--json", _has_flag(exec_help, "--json")),
            (
                "--output-last-message",
                _has_flag(exec_help, "--output-last-message", "-o"),
            ),
            ("--output-schema", _has_flag(exec_help, "--output-schema")),
            ("--sandbox", _has_flag(exec_help, "--sandbox", "-s")),
            ("--ask-for-approval", _has_flag(root_help, "--ask-for-approval", "-a")),
            ("--cd", _has_flag(exec_help, "--cd", "-C")),
            ("--add-dir", _has_flag(exec_help, "--add-dir")),
            ("--ephemeral", _has_flag(exec_help, "--ephemeral")),
            ("--ignore-user-config", _has_flag(exec_help, "--ignore-user-config")),
            ("--ignore-rules", _has_flag(exec_help, "--ignore-rules")),
            ("--search", _has_flag(root_help, "--search")),
            ("--model", _has_flag(exec_help, "--model", "-m")),
            ("--config", _has_flag(exec_help, "--config", "-c")),
        )
        missing = [name for name, present in capabilities if not present]
        if skip_git_check and not _has_flag(exec_help, "--skip-git-repo-check"):
            missing.append("--skip-git-repo-check")
        if _codex_sessions_enabled(config):
            resume_code, resume_stdout, resume_stderr = command_runner(
                (path, "exec", "resume", "--help"), root
            )
            if resume_code != 0 or "resume" not in f"{resume_stdout}\n{resume_stderr}".casefold():
                missing.append("codex exec resume")
        checks.append(
            DoctorCheck(
                "Codex capabilities",
                absent_level if missing else CheckLevel.PASS,
                "missing required capability: " + ", ".join(missing)
                if missing
                else "noninteractive JSONL, schema, sandbox, search, model, and config flags found",
                "Update Codex using an official installation method." if missing else None,
                DoctorGroup.CODEX,
            )
        )

    auth_code, auth_stdout, auth_stderr = command_runner((path, "login", "status"), root)
    authentication = _classify_codex_auth(auth_code, auth_stdout, auth_stderr)
    auth_details = {
        CodexAuthentication.CHATGPT: "authenticated with ChatGPT",
        CodexAuthentication.API_KEY: "authenticated with an API key",
        CodexAuthentication.ACCESS_TOKEN: "authenticated with an access token",
        CodexAuthentication.AUTHENTICATED_UNKNOWN: "authenticated (method not reported)",
        CodexAuthentication.NOT_AUTHENTICATED: "Codex CLI is installed but is not signed in",
        CodexAuthentication.ERROR: "unable to determine Codex authentication status",
    }
    auth_ok = authentication in {
        CodexAuthentication.CHATGPT,
        CodexAuthentication.API_KEY,
        CodexAuthentication.ACCESS_TOKEN,
        CodexAuthentication.AUTHENTICATED_UNKNOWN,
    }
    checks.append(
        DoctorCheck(
            "Codex authentication",
            CheckLevel.PASS if auth_ok else absent_level,
            auth_details[authentication],
            None if auth_ok else _CODEX_LOGIN_REMEDIATION,
            DoctorGroup.CODEX,
        )
    )

    if deep:
        if not auth_ok:
            checks.append(
                DoctorCheck(
                    "Codex live probe",
                    absent_level,
                    "not attempted because Codex is not authenticated",
                    _CODEX_LOGIN_REMEDIATION,
                    DoctorGroup.CODEX,
                )
            )
        else:
            try:
                detail = (deep_probe or _default_codex_deep_probe)(path, root)
            except Exception as exc:
                checks.append(
                    DoctorCheck(
                        "Codex live probe",
                        absent_level,
                        f"failed ({type(exc).__name__})",
                        "Check Codex access/network, then rerun: ascend doctor --deep",
                        DoctorGroup.CODEX,
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        "Codex live probe", CheckLevel.PASS, detail, group=DoctorGroup.CODEX
                    )
                )
    return checks


def run_doctor_checks(
    config: AppConfig,
    project_root: Path,
    *,
    online: bool = False,
    deep: bool = False,
    environment: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    command_runner: CommandRunner = _run_version,
    online_probe: OnlineProbe | None = None,
    codex_deep_probe: CodexDeepProbe | None = None,
) -> DoctorReport:
    """Run diagnostics without model use unless ``deep=True`` is explicit.

    ``online=True`` retains the existing, separately opted-in API connectivity check.  It
    does not alter backend selection and never causes a Codex-to-API fallback.
    """

    root = project_root.resolve()
    env = os.environ if environment is None else environment
    checks: list[DoctorCheck] = []
    backend = _configured_backend(config)

    version = sys.version_info
    supported = version >= (3, 11)
    checks.append(
        DoctorCheck(
            "Python",
            CheckLevel.PASS if supported else CheckLevel.FAILURE,
            f"{version.major}.{version.minor}.{version.micro}",
            None if supported else "Install Python 3.11+, then run: python3.11 -m venv .venv",
            DoctorGroup.ENVIRONMENT,
        )
    )
    checks.append(DoctorCheck("Project root", CheckLevel.PASS, str(root)))
    checks.append(DoctorCheck("Configuration", CheckLevel.PASS, "resolved and validated"))
    checks.append(
        DoctorCheck(
            "Default model backend",
            CheckLevel.PASS,
            "Codex CLI" if backend == "codex" else "OpenAI Responses API (explicit selection)",
        )
    )

    checks.extend(
        _codex_checks(
            config,
            root,
            deep=deep,
            which=which,
            command_runner=command_runner,
            deep_probe=codex_deep_probe,
        )
    )

    has_key = bool(env.get("OPENAI_API_KEY", "").strip())
    api_required = backend == "api"
    checks.append(
        DoctorCheck(
            "OpenAI API authentication",
            CheckLevel.PASS
            if has_key
            else (CheckLevel.FAILURE if api_required else CheckLevel.WARNING),
            "OPENAI_API_KEY is configured"
            if has_key
            else "OPENAI_API_KEY is not configured; this is not required for Codex mode",
            None
            if has_key or not api_required
            else "Run: export OPENAI_API_KEY='your-api-key' (never put it in ascend.toml).",
            DoctorGroup.API,
        )
    )

    has_lean_project = (root / "lean-toolchain").is_file() and any(
        (root / name).is_file() for name in ("lakefile.toml", "lakefile.lean")
    )
    lean_required = config.lean.enabled
    checks.append(
        DoctorCheck(
            "Lean project",
            CheckLevel.PASS
            if has_lean_project
            else (CheckLevel.FAILURE if lean_required else CheckLevel.WARNING),
            "lean-toolchain and Lake project file found"
            if has_lean_project
            else "no complete Lean/Lake project markers found",
            None if has_lean_project else "Run: cd /path/to/your/lean-project (or pass --no-lean).",
            DoctorGroup.RESEARCH_TOOLS,
        )
    )

    checks.extend(
        [
            _executable_check(
                name="Git",
                executable="git",
                args=("--version",),
                root=root,
                required=True,
                remediation=("Ubuntu/WSL: sudo apt-get install git; macOS: brew install git"),
                group=DoctorGroup.RESEARCH_TOOLS,
                which=which,
                command_runner=command_runner,
            ),
            _executable_check(
                name="Lean",
                executable="lean",
                args=("--version",),
                root=root,
                required=lean_required,
                remediation=(
                    "Install elan from https://lean-lang.org/lean4/doc/setup.html "
                    "(or pass --no-lean)."
                ),
                group=DoctorGroup.RESEARCH_TOOLS,
                which=which,
                command_runner=command_runner,
            ),
            _executable_check(
                name="Lake",
                executable="lake",
                args=("--version",),
                root=root,
                required=lean_required,
                remediation="Run inside the project: lake update; then run: lake build",
                group=DoctorGroup.RESEARCH_TOOLS,
                which=which,
                command_runner=command_runner,
            ),
            _executable_check(
                name="LaTeX",
                executable=config.manuscript.latex_command[0],
                args=("--version",),
                root=root,
                required=config.manuscript.enabled,
                remediation=(
                    "Ubuntu/WSL: sudo apt-get install latexmk texlive-latex-extra; "
                    "macOS: brew install --cask mactex-no-gui"
                ),
                group=DoctorGroup.RESEARCH_TOOLS,
                which=which,
                command_runner=command_runner,
            ),
        ]
    )

    if config.lean.execution_backend == "docker":
        checks.append(
            _executable_check(
                name="Docker",
                executable="docker",
                args=("--version",),
                root=root,
                required=True,
                remediation=(
                    "Install Docker Desktop, or run: ascend run PROBLEM.md --sandbox native"
                ),
                group=DoctorGroup.RESEARCH_TOOLS,
                which=which,
                command_runner=command_runner,
            )
        )
        checks.append(
            _executable_check(
                name="Docker image",
                executable="docker",
                args=("image", "inspect", config.lean.docker_image),
                root=root,
                required=True,
                remediation=(
                    f"Build or load {config.lean.docker_image!r}; ASCEND never pulls images "
                    "implicitly. Alternatively, run with --sandbox native."
                ),
                group=DoctorGroup.RESEARCH_TOOLS,
                which=which,
                command_runner=command_runner,
            )
        )

    ascend_root = root / ".ascend"
    try:
        ascend_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="doctor-", dir=ascend_root):
            pass
    except OSError as exc:
        checks.append(
            DoctorCheck(
                "Workspace write access",
                CheckLevel.FAILURE,
                str(exc),
                f"Run: mkdir -p '{ascend_root}' && chmod u+rwx '{ascend_root}'",
                DoctorGroup.ENVIRONMENT,
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "Workspace write access",
                CheckLevel.PASS,
                str(ascend_root),
                group=DoctorGroup.ENVIRONMENT,
            )
        )

    actual_hash = hashlib.sha256(
        read_resource_bytes("prompts/research_prompt_framework.txt")
    ).hexdigest()
    framework_ok = actual_hash == EXPECTED_FRAMEWORK_SHA256
    checks.append(
        DoctorCheck(
            "Prompt framework integrity",
            CheckLevel.PASS if framework_ok else CheckLevel.FAILURE,
            actual_hash,
            None
            if framework_ok
            else "Reinstall ASCEND; use --framework only for an intentional custom framework.",
            DoctorGroup.ENVIRONMENT,
        )
    )

    if online:
        if not has_key:
            checks.append(
                DoctorCheck(
                    "OpenAI API connectivity",
                    CheckLevel.FAILURE if api_required else CheckLevel.WARNING,
                    "not attempted because OPENAI_API_KEY is not configured",
                    "Configure OPENAI_API_KEY only if you intend to use --backend api.",
                    DoctorGroup.API,
                )
            )
        else:
            try:
                detail = (online_probe or _default_online_probe)()
            except Exception as exc:  # third-party SDK exposes several error classes
                checks.append(
                    DoctorCheck(
                        "OpenAI API connectivity",
                        CheckLevel.FAILURE if api_required else CheckLevel.WARNING,
                        f"failed ({type(exc).__name__})",
                        "Check OPENAI_API_KEY and HTTPS, then rerun: ascend doctor --online.",
                        DoctorGroup.API,
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        "OpenAI API connectivity", CheckLevel.PASS, detail, group=DoctorGroup.API
                    )
                )

    return DoctorReport(tuple(checks), default_backend=backend)
