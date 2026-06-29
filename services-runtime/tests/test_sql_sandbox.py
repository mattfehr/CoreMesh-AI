import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.sql_engine.sandbox import SQLSandbox, SQLSandboxConfig, UnsafeSQLError  # noqa: E402


class EngineThatMustNotConnect:
    def connect(self):
        raise AssertionError("Unsafe SQL should be blocked before opening a connection.")


def test_delete_query_is_blocked_before_database_connection():
    sandbox = SQLSandbox(engine=EngineThatMustNotConnect())

    with pytest.raises(UnsafeSQLError, match="DELETE"):
        sandbox.execute("DELETE FROM users")


def test_writable_cte_is_blocked():
    sandbox = SQLSandbox(engine=EngineThatMustNotConnect())

    with pytest.raises(UnsafeSQLError, match="DELETE"):
        sandbox.sanitize_sql(
            "WITH doomed AS (DELETE FROM users RETURNING *) SELECT * FROM doomed"
        )


def test_limit_is_appended_outside_trailing_line_comment():
    sandbox = SQLSandbox(engine=EngineThatMustNotConnect())

    sanitized = sandbox.sanitize_sql("SELECT * FROM prompt_registry -- audit note")

    assert sanitized.sql == "SELECT * FROM prompt_registry LIMIT 1000"
    assert sanitized.limit_applied is True


@pytest.mark.parametrize(
    ("sql", "function_name"),
    [
        ("SELECT pg_sleep(10)", "pg_sleep"),
        ("SELECT nextval('users_id_seq')", "nextval"),
    ],
)
def test_blocked_functions_are_rejected(sql, function_name):
    sandbox = SQLSandbox(engine=EngineThatMustNotConnect())

    with pytest.raises(UnsafeSQLError, match=function_name):
        sandbox.sanitize_sql(sql)


def test_blocked_query_is_logged(caplog):
    sandbox = SQLSandbox(engine=EngineThatMustNotConnect())

    with caplog.at_level("WARNING"):
        with pytest.raises(UnsafeSQLError):
            sandbox.sanitize_sql("DELETE FROM users")

    assert "Blocked unsafe SQL query" in caplog.text
    assert "DELETE FROM users" in caplog.text


def test_execute_sets_read_only_and_always_rolls_back():
    row = MagicMock()
    row._mapping = {"n": 1}
    result_proxy = MagicMock()
    result_proxy.keys.return_value = ["n"]
    result_proxy.fetchall.return_value = [row]

    transaction = MagicMock()
    connection = MagicMock()
    connection.begin.return_value = transaction
    connection.execute.side_effect = [None, result_proxy]

    engine = MagicMock()
    engine.connect.return_value = connection

    sandbox = SQLSandbox(engine=engine)
    result = sandbox.execute("SELECT 1 AS n")

    read_only_call = connection.execute.call_args_list[0][0][0]
    query_call = connection.execute.call_args_list[1][0][0]
    assert read_only_call.text == "SET TRANSACTION READ ONLY"
    assert query_call.text == "SELECT 1 AS n LIMIT 1000"
    transaction.rollback.assert_called_once()
    connection.close.assert_called_once()
    assert result.columns == ["n"]
    assert result.rows == [{"n": 1}]
    assert result.row_count == 1
    assert result.limit_applied is True


def test_execute_rolls_back_even_when_query_fails():
    transaction = MagicMock()
    connection = MagicMock()
    connection.begin.return_value = transaction
    connection.execute.side_effect = [None, RuntimeError("query failed")]

    engine = MagicMock()
    engine.connect.return_value = connection

    sandbox = SQLSandbox(engine=engine)

    with pytest.raises(RuntimeError, match="query failed"):
        sandbox.execute("SELECT 1")

    transaction.rollback.assert_called_once()
    connection.close.assert_called_once()


def test_execute_runs_against_sqlite_with_rollback():
    engine = create_engine("sqlite:///:memory:")
    sandbox = SQLSandbox(
        engine=engine,
        config=SQLSandboxConfig(read_only_transaction_sql="BEGIN"),
    )

    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE prompt_registry (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO prompt_registry (id, name) VALUES (1, 'alpha')"))
        connection.commit()

    result = sandbox.execute("SELECT name FROM prompt_registry")

    assert result.rows == [{"name": "alpha"}]
    assert result.limit_applied is True

    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM prompt_registry")).scalar_one()
    assert count == 1


def test_sqlalchemy_introspection_returns_tables_columns_and_foreign_keys():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String(255), nullable=False),
    )
    Table(
        "orders",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, ForeignKey(users.c.id), nullable=False),
    )
    metadata.create_all(engine)

    schema = SQLSandbox(engine=engine).introspect_schema()
    tables = {table.name: table for table in schema.tables}

    assert set(tables) == {"orders", "users"}
    assert tables["users"].primary_key == ["id"]
    assert tables["users"].columns[0].primary_key is True
    assert tables["users"].columns[1].name == "email"
    assert tables["users"].columns[1].nullable is False
    assert tables["orders"].foreign_keys[0].constrained_columns == ["user_id"]
    assert tables["orders"].foreign_keys[0].referred_table == "users"
    assert tables["orders"].foreign_keys[0].referred_columns == ["id"]


def test_select_query_gets_default_limit_when_missing():
    sandbox = SQLSandbox(engine=EngineThatMustNotConnect())

    sanitized = sandbox.sanitize_sql("SELECT * FROM prompt_registry")

    assert sanitized.sql == "SELECT * FROM prompt_registry LIMIT 1000"
    assert sanitized.limit_applied is True


def test_existing_limit_is_preserved():
    sandbox = SQLSandbox(
        engine=EngineThatMustNotConnect(),
        config=SQLSandboxConfig(row_limit=1000),
    )

    sanitized = sandbox.sanitize_sql("SELECT * FROM prompt_registry LIMIT 25")

    assert sanitized.sql == "SELECT * FROM prompt_registry LIMIT 25"
    assert sanitized.limit_applied is False


def test_subquery_limit_does_not_count_as_outer_row_cap():
    sandbox = SQLSandbox(engine=EngineThatMustNotConnect())

    sanitized = sandbox.sanitize_sql(
        "SELECT * FROM (SELECT * FROM prompt_registry LIMIT 25) nested"
    )

    assert sanitized.sql.endswith("nested LIMIT 1000")
    assert sanitized.limit_applied is True
