from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from ascend_math_agent.config import AppConfig
from ascend_math_agent.doctor import (
    CheckLevel,
    CodexAuthentication,
    DoctorGroup,
    _classify_codex_auth,
    _codex_jsonl_has_source_urls,
    _run_version,
    run_doctor_checks,
)


@pytest.mark.parametrize(
    ("jsonl", "expected"),
    [
        (
            '{"item":{"action":{"sources":[{"url":"https://lean-lang.org"}]}}}\n',
            True,
        ),
        ('{"type":"url_citation","url":"https://lean-lang.org"}\n', True),
        ('{"item":{"action":{"type":"search","query":"Lean"}}}\n', False),
        ("non-json diagnostic\n", False),
    ],
)
def test_codex_jsonl_source_url_capability_detection(jsonl: str, expected: bool) -> None:
    assert _codex_jsonl_has_source_urls(jsonl) is expected


_CODEX_ROOT_HELP = """Commands:
  exec   Run non-interactively
  login  Manage login
Options:
  -c, --config <key=value>
  -m, --model <MODEL>
  -a, --ask-for-approval <POLICY>
      --search
"""

_CODEX_EXEC_HELP = """Commands:
  resume  Resume a session
Options:
  -c, --config <key=value>
  -m, --model <MODEL>
  -s, --sandbox <MODE>
  -C, --cd <DIR>
      --add-dir <DIR>
      --ephemeral
      --ignore-user-config
      --ignore-rules
      --skip-git-repo-check
      --json
  -o, --output-last-message <FILE>
      --output-schema <FILE>
"""


@pytest.fixture(autouse=True)
def _git_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)


def _healthy_runner(
    argv: Sequence[str], cwd: Path, *, commands: list[tuple[str, ...]] | None = None
) -> tuple[int, str, str]:
    del cwd
    command = tuple(argv)
    if commands is not None:
        commands.append(command)
    arguments = command[1:]
    executable = Path(command[0]).name
    if arguments == ("--version",):
        return 0, f"{executable} 1.0", ""
    if executable == "codex" and arguments == ("--help",):
        return 0, _CODEX_ROOT_HELP, ""
    if executable == "codex" and arguments == ("exec", "--help"):
        return 0, _CODEX_EXEC_HELP, ""
    if executable == "codex" and arguments == ("exec", "resume", "--help"):
        return 0, "Resume a previous session", ""
    if executable == "codex" and arguments == ("login", "status"):
        return 0, "Logged in using ChatGPT", ""
    return 0, "available", ""


def test_doctor_is_offline_by_default(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
    (tmp_path / "lakefile.toml").write_text("name = 'fixture'\n", encoding="utf-8")
    probed = False

    def online_probe() -> str:
        nonlocal probed
        probed = True
        raise AssertionError("default doctor must not use the network")

    report = run_doctor_checks(
        AppConfig(),
        tmp_path,
        environment={},
        which=lambda command: f"/usr/bin/{command}",
        command_runner=_healthy_runner,
        online_probe=online_probe,
    )

    assert not probed
    assert not report.failures
    api_auth = next(check for check in report.checks if check.name == "OpenAI API authentication")
    assert api_auth.level is CheckLevel.WARNING
    assert "not required for Codex mode" in api_auth.detail
    assert report.default_backend == "codex"


def test_doctor_explains_missing_optional_lean_tools(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {"lean": {"enabled": False}, "manuscript": {"enabled": False}}
    )
    report = run_doctor_checks(
        config,
        tmp_path,
        environment={},
        which=lambda command: f"/usr/bin/{command}" if command in {"codex", "git"} else None,
        command_runner=_healthy_runner,
    )

    assert any(
        check.name == "Lean" and check.level is CheckLevel.WARNING for check in report.checks
    )
    assert any("--no-lean" in (check.remediation or "") for check in report.checks)


def test_doctor_checks_configured_docker_image_without_pulling(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
    (tmp_path / "lakefile.toml").write_text("name = 'fixture'\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def command_runner(argv: Sequence[str], cwd: Path) -> tuple[int, str, str]:
        commands.append(tuple(argv))
        return _healthy_runner(argv, cwd)

    config = AppConfig.model_validate(
        {
            "lean": {
                "execution_backend": "docker",
                "docker_image": "registry.example/ascend-lean:test",
            }
        }
    )
    report = run_doctor_checks(
        config,
        tmp_path,
        environment={},
        which=lambda command: f"/usr/bin/{command}",
        command_runner=command_runner,
    )

    assert not report.failures
    assert (
        "/usr/bin/docker",
        "image",
        "inspect",
        "registry.example/ascend-lean:test",
    ) in commands
    assert all("pull" not in command for command in commands)


def test_doctor_version_probe_withholds_and_redacts_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    observed_environment: dict[str, str] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        observed_environment.update(environment)
        return subprocess.CompletedProcess(
            args=["tool"],
            returncode=1,
            stdout="Authorization: Bearer sk-test-secret-value",
            stderr="api_key=sk-test-secret-value",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    code, stdout, stderr = _run_version(("tool", "--version"), tmp_path)

    assert code == 1
    assert "OPENAI_API_KEY" not in observed_environment
    assert "sk-test-secret-value" not in stdout + stderr
    assert "[REDACTED]" in stdout + stderr


@pytest.mark.parametrize(
    ("code", "output", "expected"),
    [
        (0, "Logged in using ChatGPT", CodexAuthentication.CHATGPT),
        (0, "Logged in using an API key", CodexAuthentication.API_KEY),
        (0, "Logged in using an access token", CodexAuthentication.ACCESS_TOKEN),
        (0, "Authenticated", CodexAuthentication.AUTHENTICATED_UNKNOWN),
        (1, "Not logged in", CodexAuthentication.NOT_AUTHENTICATED),
        (1, "unexpected failure", CodexAuthentication.ERROR),
    ],
)
def test_codex_auth_status_is_minimally_classified(
    code: int, output: str, expected: CodexAuthentication
) -> None:
    assert _classify_codex_auth(code, output, "") is expected


def test_doctor_groups_codex_and_optional_api_checks(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {"lean": {"enabled": False}, "manuscript": {"enabled": False}}
    )
    report = run_doctor_checks(
        config,
        tmp_path,
        environment={},
        which=lambda command: f"/usr/bin/{command}" if command in {"codex", "git"} else None,
        command_runner=_healthy_runner,
    )

    codex_names = {check.name for check in report.checks_for(DoctorGroup.CODEX)}
    api_names = {check.name for check in report.checks_for(DoctorGroup.API)}
    assert codex_names == {
        "Codex CLI",
        "Codex workspace",
        "Codex capabilities",
        "Codex authentication",
    }
    assert api_names == {"OpenAI API authentication"}
    assert not report.failures


def test_ordinary_doctor_never_runs_a_codex_model_probe(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    config = AppConfig.model_validate(
        {"lean": {"enabled": False}, "manuscript": {"enabled": False}}
    )

    run_doctor_checks(
        config,
        tmp_path,
        environment={},
        which=lambda command: f"/usr/bin/{command}" if command in {"codex", "git"} else None,
        command_runner=lambda argv, cwd: _healthy_runner(argv, cwd, commands=commands),
        codex_deep_probe=lambda executable, root: (_ for _ in ()).throw(
            AssertionError(f"unexpected live probe: {executable} {root}")
        ),
    )

    codex_commands = [command[1:] for command in commands if Path(command[0]).name == "codex"]
    assert codex_commands == [
        ("--version",),
        ("--help",),
        ("exec", "--help"),
        ("exec", "resume", "--help"),
        ("login", "status"),
    ]


def test_deep_doctor_uses_explicit_probe_seam(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {"lean": {"enabled": False}, "manuscript": {"enabled": False}}
    )
    probes: list[tuple[str, Path]] = []

    def probe(executable: str, root: Path) -> str:
        probes.append((executable, root))
        return "live structured-output probe succeeded"

    report = run_doctor_checks(
        config,
        tmp_path,
        deep=True,
        environment={},
        which=lambda command: f"/usr/bin/{command}" if command in {"codex", "git"} else None,
        command_runner=_healthy_runner,
        codex_deep_probe=probe,
    )

    assert probes == [("/usr/bin/codex", tmp_path.resolve())]
    deep_check = next(check for check in report.checks if check.name == "Codex live probe")
    assert deep_check.level is CheckLevel.PASS


def test_missing_required_codex_capability_fails_without_model_call(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {"lean": {"enabled": False}, "manuscript": {"enabled": False}}
    )

    def incomplete_runner(argv: Sequence[str], cwd: Path) -> tuple[int, str, str]:
        if tuple(argv[1:]) == ("exec", "--help"):
            return 0, _CODEX_EXEC_HELP.replace("--output-schema <FILE>", ""), ""
        return _healthy_runner(argv, cwd)

    report = run_doctor_checks(
        config,
        tmp_path,
        environment={},
        which=lambda command: f"/usr/bin/{command}" if command in {"codex", "git"} else None,
        command_runner=incomplete_runner,
    )

    capability = next(check for check in report.checks if check.name == "Codex capabilities")
    assert capability.level is CheckLevel.FAILURE
    assert "--output-schema" in capability.detail


def test_codex_workspace_requires_git_unless_explicitly_skipped(tmp_path: Path) -> None:
    (tmp_path / ".git").rmdir()
    base = {"lean": {"enabled": False}, "manuscript": {"enabled": False}}

    required_report = run_doctor_checks(
        AppConfig.model_validate(base),
        tmp_path,
        environment={},
        which=lambda command: f"/usr/bin/{command}" if command in {"codex", "git"} else None,
        command_runner=_healthy_runner,
    )
    required_check = next(
        check for check in required_report.checks if check.name == "Codex workspace"
    )
    assert required_check.level is CheckLevel.FAILURE

    skipped_report = run_doctor_checks(
        AppConfig.model_validate({**base, "codex": {"skip_git_repo_check": True}}),
        tmp_path,
        environment={},
        which=lambda command: f"/usr/bin/{command}" if command in {"codex", "git"} else None,
        command_runner=_healthy_runner,
    )
    skipped_check = next(
        check for check in skipped_report.checks if check.name == "Codex workspace"
    )
    assert skipped_check.level is CheckLevel.WARNING
    assert not skipped_report.failures


def test_explicit_api_backend_requires_key_but_codex_becomes_optional(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "config_version": 2,
            "backend": {"provider": "api"},
            "lean": {"enabled": False},
            "manuscript": {"enabled": False},
        }
    )
    report = run_doctor_checks(
        config,
        tmp_path,
        environment={},
        which=lambda command: "/usr/bin/git" if command == "git" else None,
        command_runner=_healthy_runner,
    )

    codex_check = next(check for check in report.checks if check.name == "Codex CLI")
    api_check = next(check for check in report.checks if check.name == "OpenAI API authentication")
    assert codex_check.level is CheckLevel.WARNING
    assert api_check.level is CheckLevel.FAILURE
    assert report.default_backend == "api"
