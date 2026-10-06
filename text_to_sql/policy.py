"""Access policy and the schema catalog it is checked against."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Functions a generated query may call. Anything else is rejected, both by the
# parser layer and (by real SQLite name) by the connection's authorizer.
ALLOWED_FUNCTIONS = frozenset("""
count sum avg min max total round coalesce ifnull nullif iif lower upper length substr
substring trim ltrim rtrim replace instr abs strftime date time datetime julianday typeof
group_concat like glob row_number rank dense_rank ntile lag lead first_value last_value
ceil ceiling floor sqrt pow power
""".split())

DANGEROUS_FUNCTIONS = frozenset("""
load_extension readfile writefile edit fts3_tokenizer randomblob zeroblob sqlite_compileoption_get
sqlite_source_id sqlite_version sqlite_log
""".split())


@dataclass(frozen=True)
class Policy:
    blocked_columns: frozenset = frozenset({("employees", "salary")})
    masked_columns: frozenset = frozenset({("customers", "email"), ("employees", "email")})
    max_rows: int = 100
    timeout_s: float = 2.0
    cross_join_limit: int = 1_000_000  # estimated rows from unindexed table scans
    mask_value: str = "***masked***"

    def is_sensitive(self, table: str, column: str) -> bool:
        key = (table.lower(), column.lower())
        return key in self.blocked_columns or key in self.masked_columns


@dataclass
class Catalog:
    """Tables, columns and row counts of the database (read via a trusted connection)."""
    tables: dict[str, list[str]] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)
    # (table, column) -> every distinct text value, for low-cardinality text columns
    values: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    @classmethod
    def from_connection(cls, con: sqlite3.Connection, policy: "Policy | None" = None,
                        max_distinct: int = 12) -> "Catalog":
        cat = cls()
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in names:
            cat.tables[t.lower()] = [r[1].lower() for r in con.execute(f'PRAGMA table_info("{t}")')]
            cat.row_counts[t.lower()] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for _cid, col, ctype, *_ in con.execute(f'PRAGMA table_info("{t}")').fetchall():
                if ctype.upper() != "TEXT" or (policy and policy.is_sensitive(t, col)):
                    continue
                vals = [r[0] for r in con.execute(
                    f'SELECT DISTINCT "{col}" FROM "{t}" WHERE "{col}" IS NOT NULL LIMIT {max_distinct + 1}')]
                if len(vals) <= max_distinct:
                    cat.values[(t.lower(), col.lower())] = set(vals)
        return cat
