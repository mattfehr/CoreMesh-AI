"""Guardrailed SQL execution for CoreMesh text-to-SQL workflows."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import sqlparse
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlparse import tokens as sql_tokens

from src.config import settings

log = logging.getLogger(__name__)


class UnsafeSQLError(ValueError):
    """Raised when generated SQL violates the sandbox safety policy."""


@dataclass(frozen=True)
class SQLSandboxConfig:
    row_limit: int = 1_000
    enforce_limit: bool = True
    read_only_transaction_sql: str = "SET TRANSACTION READ ONLY"
    blocked_keywords: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "ALTER",
                "CALL",
                "COPY",
                "CREATE",
                "DELETE",
                "DROP",
                "GRANT",
                "INSERT",
                "MERGE",
                "REVOKE",
                "TRUNCATE",
                "UPDATE",
            }
        )
    )
    blocked_functions: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "lo_import",
                "lo_export",
                "nextval",
                "pg_advisory_lock",
                "pg_advisory_unlock",
                "pg_cancel_backend",
                "pg_read_file",
                "pg_sleep",
                "pg_terminate_backend",
                "setval",
            }
        )
    )


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    type: str
    nullable: bool
    primary_key: bool = False


@dataclass(frozen=True)
class ForeignKeySchema:
    constrained_columns: list[str]
    referred_table: str | None
    referred_columns: list[str]
    name: str | None = None


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: list[ColumnSchema]
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKeySchema] = field(default_factory=list)
    schema: str | None = None


@dataclass(frozen=True)
class DatabaseSchema:
    tables: list[TableSchema]


@dataclass(frozen=True)
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    elapsed_ms: float
    limit_applied: bool


@dataclass(frozen=True)
class SanitizedSQL:
    sql: str
    limit_applied: bool


class SQLSandbox:
    """Schema introspection, SQL safety checks, and read-only execution."""

    _ALLOWED_STATEMENT_TYPES = {"SELECT"}
    _FUNCTION_CALL_PATTERN = re.compile(
        r"\b([a-z_][a-z0-9_]*)\s*\(",
        re.IGNORECASE,
    )

    def __init__(
        self,
        engine: Engine | None = None,
        config: SQLSandboxConfig | None = None,
    ) -> None:
        self.engine = engine or create_engine(settings.postgres_dsn)
        self.config = config or SQLSandboxConfig()

    def introspect_schema(self, schema: str | None = None) -> DatabaseSchema:
        inspector = inspect(self.engine)
        tables: list[TableSchema] = []

        for table_name in inspector.get_table_names(schema=schema):
            primary_key = inspector.get_pk_constraint(table_name, schema=schema)
            primary_key_columns = list(primary_key.get("constrained_columns") or [])
            primary_key_set = set(primary_key_columns)

            columns = [
                ColumnSchema(
                    name=column["name"],
                    type=str(column["type"]),
                    nullable=bool(column.get("nullable", True)),
                    primary_key=column["name"] in primary_key_set,
                )
                for column in inspector.get_columns(table_name, schema=schema)
            ]

            foreign_keys = [
                ForeignKeySchema(
                    constrained_columns=list(fk.get("constrained_columns") or []),
                    referred_table=fk.get("referred_table"),
                    referred_columns=list(fk.get("referred_columns") or []),
                    name=fk.get("name"),
                )
                for fk in inspector.get_foreign_keys(table_name, schema=schema)
            ]

            tables.append(
                TableSchema(
                    name=table_name,
                    columns=columns,
                    primary_key=primary_key_columns,
                    foreign_keys=foreign_keys,
                    schema=schema,
                )
            )

        return DatabaseSchema(tables=tables)

    def sanitize_sql(self, sql: str) -> SanitizedSQL:
        stripped = sql.strip()
        if not stripped:
            self._block("SQL query is empty.", sql)

        statements = [statement for statement in sqlparse.parse(stripped) if str(statement).strip()]
        if len(statements) != 1:
            self._block("Only one SQL statement may be executed at a time.", sql)

        statement = statements[0]
        statement_type = statement.get_type().upper()
        if statement_type not in self._ALLOWED_STATEMENT_TYPES:
            self._block(
                f"Only read-only SELECT statements are allowed; got {statement_type}.",
                sql,
            )

        self._raise_for_blocked_keywords(statement, stripped)
        self._raise_for_blocked_functions(statement, stripped)

        if not self.config.enforce_limit or self._has_limit(statement):
            return SanitizedSQL(sql=stripped, limit_applied=False)

        return SanitizedSQL(
            sql=self._append_row_limit(stripped),
            limit_applied=True,
        )

    def execute(self, sql: str) -> QueryResult:
        sanitized = self.sanitize_sql(sql)
        started = time.perf_counter()
        connection = self.engine.connect()
        transaction = connection.begin()

        try:
            connection.execute(text(self.config.read_only_transaction_sql))
            result = connection.execute(text(sanitized.sql))
            columns = list(result.keys())
            rows = [dict(row._mapping) for row in result.fetchall()]
            elapsed_ms = (time.perf_counter() - started) * 1_000
            return QueryResult(
                sql=sanitized.sql,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                elapsed_ms=elapsed_ms,
                limit_applied=sanitized.limit_applied,
            )
        finally:
            transaction.rollback()
            connection.close()

    def _block(self, reason: str, sql: str) -> None:
        log.warning("Blocked unsafe SQL query: %s | sql=%s", reason, sql)
        raise UnsafeSQLError(reason)

    def _raise_for_blocked_keywords(self, statement: sqlparse.sql.Statement, sql: str) -> None:
        for token in statement.flatten():
            if token.is_whitespace or token.ttype in sql_tokens.Comment:
                continue
            if token.ttype not in sql_tokens.Keyword:
                continue

            keyword = token.normalized.upper().split()[0]
            if keyword in self.config.blocked_keywords:
                self._block(f"Blocked unsafe SQL keyword: {keyword}.", sql)

    def _raise_for_blocked_functions(self, statement: sqlparse.sql.Statement, sql: str) -> None:
        for match in self._FUNCTION_CALL_PATTERN.finditer(str(statement)):
            function_name = match.group(1).lower()
            if function_name in self.config.blocked_functions:
                self._block(f"Blocked unsafe SQL function: {function_name}.", sql)

    def _append_row_limit(self, sql: str) -> str:
        without_semicolon = sql.strip().rstrip(";").rstrip()
        without_trailing_comment = self._strip_trailing_comments(without_semicolon)
        return f"{without_trailing_comment} LIMIT {self.config.row_limit}"

    @staticmethod
    def _strip_trailing_comments(sql: str) -> str:
        statement = sqlparse.parse(sql)[0]
        tokens = list(statement.tokens)
        while tokens and (tokens[-1].is_whitespace or tokens[-1].ttype in sql_tokens.Comment):
            tokens.pop()
        return "".join(str(token) for token in tokens).rstrip()

    @staticmethod
    def _has_limit(statement: sqlparse.sql.Statement) -> bool:
        for token in statement.tokens:
            if token.ttype in sql_tokens.Keyword and token.normalized.upper() == "LIMIT":
                return True
        return False
