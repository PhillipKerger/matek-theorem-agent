# Graph-only research state

This is the controlling specification for MATEK research memory. It supersedes every older
reference in this repository to canonical ledgers, graph patches, opaque graph identifiers,
legacy graph migration, or authoritative graph-state metadata.

## One authority

For new runs, `.matek/knowledge/<graph-name>/` contains descriptive Markdown notes. Those notes
and their wiki links are the complete durable research authority. A note filename and heading are
human-readable mathematical titles such as `Within-layer Yoneda vanishing.md`.

Small system-owned frontmatter may contain only `uid`, `kind`, `status`, `depends_on`, timestamps,
and provenance. `uid` is a hidden rename aid and is never sent to a model or displayed in normal
navigation. The system, not a research model, owns frontmatter and Markdown mutation.

`graph-index.sqlite` and dashboards are disposable derived views. Before resolution or admission,
MATEK reparses Markdown and refreshes a missing, corrupt, or stale index. A refresh failure is an
operational warning; title resolution and research continue from Markdown.

There is no canonical ledger, migration directory, graph patch, or second authoritative graph
state. Existing graph formats are not imported or repaired; users restart them as new graphs.

## Semantic model boundary

Workers report mathematical findings with descriptive fields: finding type, title, related
titles, status, statement, established content, failed attempts, next bottleneck, and evidence.
Coordinators assign mathematical tasks by descriptive title. Model contexts contain only titles,
statements, statuses, immediate dependencies, and selected evidence.

Scheduler identity, provenance, title resolution, link direction, filenames, admission, and
status promotion are application responsibilities outside model context.

Partial progress, failed approaches, and recoverable reports with no theorem are first-class graph
notes. They do not stop research.

## Admission and failure handling

`SemanticGraphWriter` serializes each graph independently. It reparses Markdown, resolves each
title exactly, disambiguates title collisions deterministically, stages all changed files, commits
them with atomic replacements, and refreshes derived views after commit.

A proposed dangling relation is omitted from the committed finding. The mathematical content is
still retained, and an `Incidents/` note records the human-readable missing title and exact source
finding. The coordinator receives refreshed semantic context and continues.

Only explicit resource/time/user policy, a declared scientific stop, or an unrecoverable Markdown
filesystem failure may stop research. A stale cache or one invalid proposed relation cannot.

## Verification

Mathematical trust is computed directly from the current Markdown nodes, statuses, provenance, and
relations. A model's self-declared status never verifies a result. Independent audits, citation
checks, and deterministic Lean checks remain separate gates.
