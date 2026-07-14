"""Public guarded-SQL API.

System role:
    Re-exports schema/result contracts and the read-only execution boundary used
    by the SQL agent specialist.
Dependencies:
    Importing loads SQLAlchemy/sqlparse definitions and runtime settings but
    does not open a connection until a sandbox method is used.
Side effects:
    Sandbox calls introspect or query the configured database; importing this
    package has no database side effect.
"""

from src.sql_engine.sandbox import (
    ColumnSchema,
    DatabaseSchema,
    ForeignKeySchema,
    QueryResult,
    SQLSandbox,
    SQLSandboxConfig,
    TableSchema,
    UnsafeSQLError,
)

__all__ = [
    "ColumnSchema",
    "DatabaseSchema",
    "ForeignKeySchema",
    "QueryResult",
    "SQLSandbox",
    "SQLSandboxConfig",
    "TableSchema",
    "UnsafeSQLError",
]
