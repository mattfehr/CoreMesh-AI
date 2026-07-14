# Guarded SQL engine

<code>SQLSandbox</code> is a defense-in-depth boundary for generated
text-to-SQL. It exposes schema introspection, validation, bounded execution, and
materialized result contracts to trusted Python callers and the SQL specialist.

## Validation policy

The default policy accepts exactly one statement whose parsed type is SELECT.
It rejects mutation/admin keywords and a denylist of PostgreSQL functions with
file, lock, sequence, sleep, or backend-control effects. A limit of 1000 is
appended when no top-level limit is present.

Validation is lexical and allow/deny-list based. It does not prove semantic
safety. Production must also use a database identity with read-only grants,
statement timeout, resource limits, and access only to approved schemas.

## Execution

The sandbox opens a SQLAlchemy connection and transaction, executes
<code>SET TRANSACTION READ ONLY</code>, runs sanitized SQL, materializes all
bounded rows, and always rolls back/closes. Schema introspection reads visible
tables, columns, primary keys, and foreign keys.

Tests inject SQLite and override the read-only setup statement where necessary;
default runtime configuration targets PostgreSQL. Blocked SQL is logged, so
avoid including secrets in generated queries or production logs.
