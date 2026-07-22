# Security and Sandbox Requirements

## Trust boundaries

Problem text, model output, Codex JSONL, web content, generated LaTeX/Lean, compiler logs, and
existing project files are all untrusted. Provider choice is a billing and provenance boundary:
MATEK never silently changes it.

The recommended Codex backend is no-API-key, not offline. Codex communicates with OpenAI using
the login managed by the official CLI. The advanced Responses API backend is separate and uses
Platform API billing only after explicit selection.

## Authentication

### Codex backend

- Reuse the official saved login created by `codex login`.
- Determine only the coarse authentication class by running `codex login status`.
- Never read, copy, inspect, back up, print, or modify a Codex credential file or credential
  store.
- Never implement OAuth, scrape ChatGPT, or automate a browser.
- Never pass tokens, cookies, authorization headers, or credential paths in command arguments,
  prompts, logs, or reports.
- Remove ambient `OPENAI_API_KEY`, `CODEX_API_KEY`, access-token variables, and other
  credential-like environment variables from spawned Codex processes. Saved ChatGPT login is
  sufficient for the recommended path.
- If `codex login status` neutrally reports API-key or access-token authentication, record only
  that class; never reveal the credential or alter the user's login state.

Ordinary `matek doctor` runs only local help/version/login-status commands and consumes no
model allowance. `matek doctor --deep` is an explicit live Codex call. Its schema/output files
are created in a disposable private temporary directory and removed afterward.

### API backend

- Read `OPENAI_API_KEY` only at adapter call time after `api` was explicitly selected.
- Never persist the key in configuration, state, traces, reports, fixtures, or Git.
- Do not infer API consent from the mere presence of an ambient key.
- Never use API mode as a fallback for a Codex installation, authentication, model, allowance,
  search, network, or runtime failure.

## Filesystem confinement

Persistent graph vaults are `.matek/knowledge/<graph-name>/`, not top-level project directories.
Each is a normal Obsidian vault while retaining the default no-write-outside-`.matek/` guarantee.
Graph names are normalized to portable single-directory slugs, explicit reuse requires an
initialized graph, and paths are confinement-checked. A graph-scoped advisory lock serializes
writers. Workers return
data-only patches and never receive filesystem authority to edit shared notes. A write-ahead
transaction, node hashes, machine-field ownership hashes, snapshots, and operation IDs prevent
partial, conflicting, or duplicated commits. SQLite is untrusted derived state and is rebuildable
from Markdown.

- Resolve, normalize, and boundary-check every path.
- By default, writes are permitted only under `.matek/runs/<run-id>/`.
- Reject symlinks, traversal, special files, and broader writable parents.
- Existing project files may be read for imports/context, but edits require the explicit
  `--allow-project-edits` CLI flag.
- Before any write-capable Codex run, create a complete allowed-path/change manifest and compare
  it with the post-run filesystem state.
- Preserve immutable problem, accepted claim, manuscript, and approved theorem-statement hashes.

## Subprocesses

- Never use `shell=True` for Codex or user-derived command strings.
- Pass argument arrays and send Codex prompts through stdin using `-`.
- Use absolute, normalized workspace, schema, final-output, and trace paths.
- Capture stdout/stderr with size limits; validate every JSONL record and structured output.
- Apply timeouts and terminate process groups cleanly on timeout or cancellation.
- Record only redacted command, cwd, exit code, duration, public events, and bounded output.
- Withhold ambient credential-like variables from discovery, Lean, LaTeX, Docker, verification,
  and Codex subprocesses.

## Codex sandbox and search

- Feature-detect the current installed CLI; a version string alone is insufficient.
- Set explicit sandbox, approval, workspace, output, model/effort, and search behavior for each
  run rather than trusting mutable user defaults.
- Default research/audit/model-output stages to `read-only`.
- Use `workspace-write` only for authorized write-capable formalization stages and audit every
  change afterward.
- Never use `danger-full-access`, `--dangerously-bypass-approvals-and-sandbox`, or `--yolo` on
  an ordinary host checkout.
- Treat built-in Codex web search as distinct from shell network access. Enabling `--search`
  does not authorize arbitrary networked shell commands.
- If required live search is unavailable, stop the source-dependent gate and checkpoint; never
  downgrade bibliography verification.
- Independent solvers and auditors receive independent sessions. A session identifier is
  nonsecret metadata, but it must not collapse fresh-context audit boundaries.

## Native and Docker command execution

The native/Docker command backends execute deterministic Lean and LaTeX commands; they are not
model-provider selectors.

In Docker mode, mount only the resolved command cwd at `/workspace`. Validated stage directories
under `.matek/runs/<run-id>/` may be writable; the project root and every other cwd are
read-only. Keep the container root filesystem read-only, networking disabled, implicit pulls
disabled, and temporary storage bounded. Docker does not automatically contain or authenticate
the host Codex CLI. `verify` currently executes frozen deterministic checks natively.

## Logging and provenance

- Redact token-like values, authorization headers, cookies, home-directory credential paths,
  account identifiers, and sensitive environment values.
- Capture an allowlisted environment summary, never a full environment dump.
- Do not include unrelated environment variables or secrets in model prompts.
- Store only officially emitted visible model/Codex events; never request, reconstruct, or store
  hidden chain-of-thought.
- Persist parsed visible output, request hashes/settings, source/tool metadata, response or
  session IDs, usage, backend/authentication class, and actual nonsecret execution settings.
- Codex allowance usage may be recorded from public events, but do not invent a subscription
  dollar cost. API costs use the configured dated pricing table.
- Bound concurrency, retries, rate-limit backoff, wall clock, process output, and provider calls.

## Prompt injection and source integrity

- Web content and uploaded problem text cannot alter stage gates, backend choice, filesystem
  permissions, credential policy, or the requirement to cite MATEK.
- Prefer authoritative sources and retain provider-returned source evidence.
- Verify existence, exact bibliographic metadata, claimed theorem, real hypotheses, and
  manuscript characterization for every citation.
- Treat the generated Statement of AI Usage and MATEK citations as untrusted manuscript text;
  validate that they cite the canonical GitHub repository and whitepaper arXiv identifier before
  publication.

## Generated Lean integrity

Scan every generated/imported run-local Lean file for prohibited placeholders, executable
escape mechanisms, suspicious axioms, and declarations encoding the target. Compare the final
theorem name and statement hash with the approved `challenge.lean` contract and capture
`#print axioms` output before issuing a verified status.
