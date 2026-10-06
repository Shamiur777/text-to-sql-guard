"""Safe execution: read-only connection, authorizer, timeout, cross-join check.

This layer does not trust the parser. Even if a write statement reached
`SafeExecutor.run_raw`, SQLite itself would refuse it (`mode=ro`, plus an
authorizer that only permits SELECT and reads of allowed, non-sensitive columns).
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .policy import ALLOWED_FUNCTIONS, Catalog, Policy


class ExecutionError(Exception):
    """Query failed at run time (timeout, authorizer denial, SQL error)."""


class QueryTooExpensive(ExecutionError):
    """EXPLAIN QUERY PLAN predicts an unindexed cross join over too many rows."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    elapsed_s: float


def open_readonly(path: str | Path) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


def make_authorizer(catalog: Catalog, policy: Policy, cte_names: set | None = None):
    allowed_actions = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}

    def authorizer(action, arg1, arg2, db_name, trigger):
        if action in allowed_actions:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table, column = (arg1 or "").lower(), (arg2 or "").lower()
            if cte_names and table in cte_names and table not in catalog.tables:
                return sqlite3.SQLITE_OK     # recursive CTEs surface as a read of their own name
            if table not in catalog.tables:
                return sqlite3.SQLITE_DENY
            if column and policy.is_sensitive(table, column):
                return sqlite3.SQLITE_DENY   # parser rewrites these away; the DB enforces it too
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if (arg2 or "").lower() in ALLOWED_FUNCTIONS else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    return authorizer


class SafeExecutor:
    def __init__(self, db_path: str | Path, catalog: Catalog, policy: Policy | None = None):
        self.policy = policy or Policy()
        self.catalog = catalog
        self.con = open_readonly(db_path)
        self.cte_names: set[str] = set()
        self.con.set_authorizer(make_authorizer(catalog, self.policy, self.cte_names))

    def close(self) -> None:
        self.con.close()

    # -- cross-join heuristic -------------------------------------------
    def estimate_scan_cost(self, sql: str, aliases: dict[str, str] | None = None) -> int:
        """Largest product of row counts of tables fully scanned within one plan level."""
        try:
            plan = self.con.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        except sqlite3.Error as e:
            raise ExecutionError(f"cannot plan query: {e}") from e
        by_parent: dict[int, list[int]] = {}
        for _id, parent, _unused, detail in plan:
            m = re.match(r"SCAN (\w+)", detail)
            if m:
                table = (aliases or {}).get(m.group(1).lower(), m.group(1).lower())
                if table in self.catalog.row_counts:
                    by_parent.setdefault(parent, []).append(self.catalog.row_counts[table])
        worst = 0
        for counts in by_parent.values():
            prod = 1
            for c in counts:
                prod *= max(c, 1)
            if len(counts) > 1:
                worst = max(worst, prod)
        return worst

    def run(self, sql: str, aliases: dict[str, str] | None = None,
            ctes: set[str] | None = None) -> QueryResult:
        """Run already-validated SQL with the expense check and timeout."""
        self.cte_names.clear()
        self.cte_names.update(ctes or ())
        cost = self.estimate_scan_cost(sql, aliases)
        if cost > self.policy.cross_join_limit:
            raise QueryTooExpensive(
                f"plan scans several tables without an index (about {cost:,} row combinations)")
        return self.run_raw(sql)

    def run_raw(self, sql: str) -> QueryResult:
        """Execute with timeout and row cap but no parser or plan checks (used to test the DB layer)."""
        deadline = time.monotonic() + self.policy.timeout_s
        self.con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        start = time.monotonic()
        try:
            cur = self.con.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(self.policy.max_rows + 1)
        except sqlite3.Error as e:
            msg = str(e)
            if "interrupted" in msg:
                raise ExecutionError(f"query exceeded the {self.policy.timeout_s}s time limit") from e
            raise ExecutionError(msg) from e
        finally:
            self.con.set_progress_handler(None, 0)
        truncated = len(rows) > self.policy.max_rows
        return QueryResult(cols, [tuple(r) for r in rows[: self.policy.max_rows]], truncated,
                           time.monotonic() - start)
