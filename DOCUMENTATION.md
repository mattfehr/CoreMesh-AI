# CoreMesh documentation conventions

This guide defines how source-adjacent documentation stays useful as CoreMesh
evolves. It applies to every human-authored service, test, script, manifest,
container definition, SQL file, workflow, and meaningful directory.

## What every module should explain

A maintainer should be able to answer five questions without reverse
engineering a module:

1. What responsibility does this file or directory own?
2. Where does it sit in the CoreMesh request or feedback flow?
3. Which internal and external systems does it depend on?
4. What state, I/O, network traffic, process startup, or other side effect can
   it cause?
5. Which invariants, fallback rules, and failure modes must survive a change?

Comments preserve intent and constraints. They should not paraphrase syntax
that is already obvious.

## File headers

Every human-authored file begins with a language-appropriate header that states
its purpose, larger-system role, important dependencies, and side effects.

Python uses its module docstring as the header:

~~~python
"""Validate read-only SQL before it reaches PostgreSQL.

System role:
    Safety boundary between generated SQL and the metadata database.
Dependencies:
    SQLAlchemy for execution and sqlparse for lexical inspection.
Side effects:
    Creates database connections and executes only validated SELECT queries.
"""
~~~

Go uses a package comment followed by any file-specific context that is not
shared by the package. Exported identifiers also receive conventional Go doc
comments:

~~~go
// Package gateway owns CoreMesh edge admission and upstream resilience.
//
// It depends on Redis and performs network I/O to Redis and configured
// upstreams while serving requests.
package gateway
~~~

Comment-based formats use labeled lines at the top:

~~~text
# Module: local stateful-infrastructure stack.
# Role: supplies data services to the gateway and runtime.
# Dependencies: Docker Compose and the referenced images.
# Side effects: creates containers, volumes, networks, and host port bindings.
~~~

Markdown files are documentation themselves. Their title and opening paragraph
serve as the header and must identify scope and current status.

## Generated and structural-file exceptions

Do not hand-edit generated or opaque artifacts merely to add a header. Current
examples are <code>go.sum</code>, compiled/cache files, Chroma storage, and the
binary invoice fixture. Empty <code>.gitkeep</code> files are structural
markers. Their purpose and provenance belong in the nearest README.

Dependency manifests that support comments, such as <code>go.mod</code> and
<code>requirements.txt</code>, are human-maintained and do require headers.

## API docstrings and inline comments

Document public functions, classes, methods, protocols, and exported Go
identifiers. Also document private helpers when their contract is not obvious.
Include parameters, return shape, raised or returned errors, mutation, network
or storage I/O, concurrency expectations, and fallback behavior when relevant.

Inline comments are most valuable around:

- security and trust boundaries;
- stable hashing, ordering, scoring, and threshold choices;
- concurrency, locks, retries, timeouts, and cancellation;
- destructive or persistent operations;
- fail-open versus fail-closed decisions;
- compatibility shims and optional-dependency fallbacks;
- logic whose simpler-looking rewrite would be incorrect.

Avoid comments such as “increment count,” historical change logs, stale TODOs,
or claims that only restate the roadmap.

## Directory READMEs

Every meaningful subsystem directory should have a README that covers:

- responsibility and explicit non-responsibilities;
- a module or file map;
- inbound and outbound data flow;
- external dependencies and persistent side effects;
- configuration and defaults;
- failure and fallback behavior;
- how to run and test it;
- extension points and invariants.

Placeholder directories must say that they are placeholders. Never describe a
planned component in the present tense unless executable code exists.

## Change checklist

When behavior changes:

1. Update the affected module header and API docstrings in the same change.
2. Update the nearest directory README if a contract, dependency, side effect,
   configuration value, or file map changed.
3. Update [ARCHITECTURE.md](ARCHITECTURE.md) when a service boundary, request
   flow, state owner, or implementation status changed.
4. Add a comment only if it preserves reasoning that types, names, or tests
   cannot express.
5. Run the formatter and tests named by the subsystem README.

The self-healing workflow assists this checklist when trusted pull requests
change Python, Go, or Compose structure. It can update only one existing
Markdown body at a time after deterministic eligibility checks, high-confidence
typed assessment, and independent validation. Added or removed capabilities,
ambiguous mappings, complex edits, and low-confidence findings remain
human-review items.

Automation does not replace ownership: review its marker-based PR comment,
download <code>report.json</code> or <code>changes.patch</code> when needed,
and confirm that architectural or cross-directory changes received the broader
updates required above. The implementation, local dry-run command, provider
data boundary, and offline test command live in
[the self-healing package guide](.github/scripts/README.md).
