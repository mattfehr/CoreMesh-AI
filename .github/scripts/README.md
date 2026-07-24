# Self-healing documentation tooling

## Directory role

This directory contains the repository-local implementation used by
`self-healing-docs.yml`. It compares two Git commits without checking either
revision out, extracts documentation-relevant structural changes, retrieves
candidate Markdown blocks, and asks the configured OpenAI model to assess,
repair, and independently validate stale content.

The CLI can edit files, but it never stages, commits, or pushes them. GitHub
Actions owns those final branch-mutation steps and uses the CLI's explicit
allowlist.

## Package map

| Module | Responsibility |
| --- | --- |
| `self_healing_docs/gitops.py` | Resolve revisions, read blobs, filter changed source, and enforce the Markdown worktree allowlist. |
| `self_healing_docs/structural.py` | Normalize Python ASTs, Tree-sitter Go syntax trees, and safely parsed Compose YAML into comparable contracts. |
| `self_healing_docs/markdown.py` | Split tracked Markdown into non-overlapping ATX blocks and apply body-only replacements without changing headings or surrounding bytes. |
| `self_healing_docs/retrieval.py` | Combine exact technical references with cosine similarity over batched embeddings. |
| `self_healing_docs/providers.py` | Define typed assessment, rewrite, and validation schemas and call the OpenAI Responses and Embeddings APIs. |
| `self_healing_docs/pipeline.py` | Apply confidence, eligibility, provider, filesystem, and mutation gates. |
| `self_healing_docs/reporting.py` | Write machine-readable and pull-request-readable artifacts. |
| `tests/` | Exercise parsers, retrieval, typed provider behavior, mutation safety, Git integration, and workflow policy without network calls. |

## Structural contracts

The analyzer intentionally ignores comments, formatting, Python function
bodies, Go function bodies, generated/dependency trees, and test-only source
files. It observes:

- Python public functions and methods, signatures, annotations and defaults,
  Pydantic/settings-style fields, FastAPI or Flask application metadata, and
  decorated route metadata.
- Go exported functions and methods, exported types, structs and interfaces,
  exported fields and interface methods, and HTTP route registrations.
- Compose services, images/builds, commands, ports, profiles, dependencies,
  health checks, and environment contracts.

Added or removed symbols and services are always advisory. Modified bounded
contracts may be eligible for an automatic repair, but eligibility alone never
authorizes a write.

## Decision flow

1. Read supported changed files from the requested base and head Git objects.
2. Normalize and compare structural units.
3. Split safe, tracked, human-authored Markdown into heading blocks.
4. Link exact identifier, route, and configuration references, then add the
   highest-scoring embedding matches.
5. Run typed staleness assessment, body-only rewrite, and independent
   correction validation passes.
6. Apply a block only when it is deterministically bounded, both confidence
   scores meet the threshold, every validation dimension passes, and mechanical
   Markdown checks preserve the file path, heading tree, newline style, and all
   bytes outside the selected body.

Multiple structural changes mapped to the same block are evaluated in one
repair. Ambiguous, complex, low-confidence, rejected, or unsafe cases appear as
human-review items and do not modify the worktree. Parsing, provider, credential,
or unexpected-filesystem failures produce diagnostics and a non-zero exit.

Repository content is treated as untrusted model input. The model receives no
tools and cannot choose a path. Selected structural deltas, Markdown bodies,
and small neighboring style samples are sent to OpenAI; embeddings and prompts
are not committed to the repository, and Responses requests set
<code>store=false</code>.

## CLI

From the repository root:

```powershell
python -m pip install --requirement .github/scripts/requirements.txt
$env:PYTHONPATH = ".github/scripts"
python -B -m self_healing_docs `
  --repo-root . `
  --base-sha <base-sha> `
  --head-sha <head-sha> `
  --output-dir .self-healing-docs
```

Dry-run is the default. Add `--apply` only while checked out at the exact head
revision with a clean worktree. Apply mode rewrites approved tracked `.md`
files; it still does not commit or push.

Configuration is environment-based:

| Variable | Default |
| --- | --- |
| `OPENAI_API_KEY` | Required when structural changes need provider calls. |
| `DOC_HEALING_MODEL` | `gpt-5.6-luna` |
| `DOC_HEALING_EMBEDDING_MODEL` | `text-embedding-3-small` |
| `DOC_HEALING_REASONING_EFFORT` | `low` |
| `DOC_HEALING_SIMILARITY_THRESHOLD` | `0.45` |
| `DOC_HEALING_CONFIDENCE_THRESHOLD` | `0.90` |
| `DOC_HEALING_TOP_K` | `5` |
| `DOC_HEALING_MAX_CANDIDATES` | `20` |
| `DOC_HEALING_MAX_SECTION_CHARS` | `16000` |

Each run writes:

- `report.json`: revisions, normalized changes, candidate link graph and
  scores, typed decisions, repairs, review items, failures, and non-secret
  configuration.
- `summary.md`: concise status for the Actions summary and marker-based PR
  comment.
- `changes.patch`: the exact approved Markdown diff in apply mode.
- `applied-paths.txt`: the sorted staging allowlist consumed by the workflow.

## Tests

Tests use fake embeddings and typed model decisions and never call OpenAI:

```powershell
$env:PYTHONPATH = ".github/scripts"
python -B -m pytest -q -p no:cacheprovider .github/scripts/tests
```

On a restricted Windows host, pass a writable `--basetemp` directory because
the integration tests create temporary Git repositories.
