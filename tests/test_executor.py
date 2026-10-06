"""Connection-level safety: these tests deliberately skip the parser."""
import hashlib
import sqlite3
import time

import pytest

from text_to_sql.executor import ExecutionError, QueryTooExpensive, SafeExecutor, open_readonly
from text_to_sql.guardrails import validate_sql
from text_to_sql.policy import Policy


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE products SET price = 0",
    "INSERT INTO products (id, name, price, stock, status) VALUES (999,'x',1,1,'available')",
    "DROP TABLE customers",
    "CREATE TABLE evil (x INT)",
    "ALTER TABLE customers ADD COLUMN x TEXT",
    "PRAGMA writable_schema = 1",
    "ATTACH DATABASE ':memory:' AS m",
    "VACUUM",
])
def test_writes_fail_even_if_the_parser_is_bypassed(sql, executor, db_path):
    before = hashlib.sha256(open(db_path, "rb").read()).hexdigest()
    with pytest.raises(ExecutionError):
        executor.run_raw(sql)
    assert hashlib.sha256(open(db_path, "rb").read()).hexdigest() == before


def test_bare_readonly_connection_rejects_writes_on_its_own(db_path):
    """No authorizer at all: mode=ro alone refuses the write."""
    con = open_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("DELETE FROM orders")
    con.close()


def test_stacked_statements_fail_at_the_driver(executor):
    with pytest.raises(ExecutionError):
        executor.run_raw("SELECT 1; SELECT 2")


def test_authorizer_denies_system_tables_functions_and_sensitive_columns(executor):
    with pytest.raises(ExecutionError, match="not authorized|prohibited"):
        executor.run_raw("SELECT name FROM sqlite_master")
    with pytest.raises(ExecutionError, match="not authorized|prohibited"):
        executor.run_raw("SELECT load_extension('x')")
    with pytest.raises(ExecutionError, match="not authorized|prohibited"):
        executor.run_raw("SELECT salary FROM employees")
    with pytest.raises(ExecutionError, match="not authorized|prohibited"):
        executor.run_raw("SELECT email FROM customers")
    with pytest.raises(ExecutionError, match="not authorized|prohibited"):
        executor.run_raw("SELECT * FROM employees")      # star would read salary
    with pytest.raises(ExecutionError):
        executor.run_raw("SELECT * FROM pragma_table_info('customers')")


def test_authorizer_allows_normal_reads(executor):
    assert executor.run_raw("SELECT COUNT(*) FROM customers").rows == [(300,)]
    assert executor.run_raw("SELECT name, department FROM employees LIMIT 2").columns == ["name", "department"]


def test_timeout_interrupts_runaway_query(executor):
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
    g = validate_sql(sql, executor.catalog)
    assert g.ok
    start = time.monotonic()
    with pytest.raises(ExecutionError, match="time limit"):
        executor.run(g.sql, g.aliases, g.ctes)
    assert time.monotonic() - start < 5


def test_connection_usable_after_timeout(executor):
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
    executor.cte_names.add("c")
    with pytest.raises(ExecutionError, match="time limit"):
        executor.run_raw(sql)
    assert executor.run_raw("SELECT 1").rows == [(1,)]


def test_row_cap_and_truncation_flag(db_path, catalog):
    ex = SafeExecutor(db_path, catalog, Policy(max_rows=10))
    res = ex.run_raw("SELECT id FROM orders")
    assert len(res.rows) == 10 and res.truncated
    res = ex.run_raw("SELECT id FROM orders LIMIT 3")
    assert len(res.rows) == 3 and not res.truncated
    ex.close()


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders, order_items, customers",
    "SELECT COUNT(*) FROM orders o JOIN order_items i ON 1 = 1",
    "SELECT COUNT(*) FROM order_items a CROSS JOIN order_items b",
])
def test_cross_joins_are_rejected_by_plan_check(sql, executor):
    g = validate_sql(sql, executor.catalog)
    assert g.ok
    with pytest.raises(QueryTooExpensive):
        executor.run(g.sql, g.aliases, g.ctes)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders o JOIN order_items i ON i.order_id = o.id",
    "SELECT c.name, COUNT(*) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
    "SELECT COUNT(*) FROM customers c JOIN products p ON p.status = c.status",   # small tables
])
def test_indexed_joins_are_not_flagged(sql, executor):
    g = validate_sql(sql, executor.catalog)
    assert g.ok
    executor.run(g.sql, g.aliases, g.ctes)


def test_small_cross_join_is_allowed(executor):
    g = validate_sql("SELECT COUNT(*) FROM employees e, products p", executor.catalog)
    assert executor.run(g.sql, g.aliases, g.ctes).rows == [(25 * 30,)]
