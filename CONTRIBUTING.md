# Contributing to MATEK

Thank you for helping improve MATEK. The project is an early-stage, security-sensitive research
orchestrator: changes should preserve artifacts and recovery paths while keeping scientific,
publication, and Lean promotion gates strict.

## Development setup

Use Python 3.11 or newer. From a clone of the repository:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

On Windows, use WSL2 and run the same commands inside the Linux distribution. The default test
suite is offline and does not require a model account, API key, Lean, or LaTeX.

## Understand the contracts first

Read the specifications affected by your change before editing implementation code:

- `PRODUCT_REQUIREMENTS.md` defines product behavior and trust boundaries.
- `DECISIONS.md` records choices that intentionally constrain the design.
- `WORKFLOW_SPEC.md` and `ARTIFACT_CONTRACT.md` define state transitions and durable files.
- `CLI_SPEC.md` defines user-facing commands and recovery behavior.
- `SECURITY.md` defines filesystem, subprocess, credential, and untrusted-input requirements.
- `ARCHITECTURE.md` defines graph storage, persistence, and human-edit ownership rules.

Do not modify `resources/prompts/research_prompt_framework.txt`. Its exact bytes are an integrity
boundary checked by the application and release tooling. Do not weaken a failed gate to make a
test pass, and do not add an automatic fallback from Codex to the separately billed API backend.

## Make and test changes

Prefer focused changes with tests for every affected state transition, failure mode, and trust
boundary. Dependency injection is used for model providers, subprocesses, clocks, and
filesystem-sensitive behavior so unit tests remain deterministic.

Run the focused offline quality suite while developing:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest -q
.venv/bin/python scripts/verify_project.py
.venv/bin/python -m build
```

Before opening a pull request or preparing a release, also run the comprehensive offline workflow
scenarios. This overrides the default marker filter and runs every offline test:

```bash
.venv/bin/pytest -q -o addopts=-ra
```

If a Pydantic type represented in `resources/schemas/` changes, regenerate and commit its schema:

```bash
.venv/bin/python scripts/generate_model_schemas.py
```

The integrity check rejects missing, stale, or unexpected packaged schemas and mismatched package
versions. It also verifies the immutable research prompt framework.

Live tests are opt-in because they may consume Codex allowance, use paid API funds, or require
local external tools. Never enable them in ordinary unit tests:

```bash
MATEK_CODEX_LIVE_TESTS=1 .venv/bin/pytest -q -m codex_live
```

## Pull requests

A pull request should explain the user-visible behavior, recovery implications, tests added, and
any artifact-contract or migration impact. Keep generated output, credentials, private research
problems, and `.matek/` run directories out of commits. Preserve compatibility with existing run
state unless the change includes a deliberate, tested migration.

Before requesting review, confirm that:

- all offline checks pass;
- new network behavior is absent from unit tests and explicitly authorized at runtime;
- writes remain confined to `.matek/` unless the existing explicit opt-in applies;
- provider attempts remain usage-accounted, even when their output is invalid;
- scientific, publication, and Lean statuses remain independent and fail closed at promotion; and
- documentation and the changelog describe user-visible changes.

Report security vulnerabilities through the private process in `SECURITY.md`, not a public issue.
