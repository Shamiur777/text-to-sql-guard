"""Schema description for prompts: tables, columns, notes and sample values."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .policy import Policy

# Short human notes. They clarify the deliberately ambiguous column names.
NOTES = {
    ("customers", "status"): "account state: active / inactive / vip",
    ("customers", "region"): "continent-level region; country is the country name",
    ("customers", "signup_date"): "ISO date text YYYY-MM-DD",
    ("products", "price"): "current list price",
    ("products", "status"): "available / discontinued",
    ("orders", "status"): "delivered / shipped / pending / cancelled / returned",
    ("orders", "total_amount"): "order value at time of sale; includes cancelled orders",
    ("orders", "order_date"): "ISO date text YYYY-MM-DD",
    ("orders", "shipped_date"): "NULL until the order ships",
    ("orders", "employee_id"): "sales employee who handled the order; may be NULL",
    ("order_items", "unit_price"): "price actually charged per unit (may differ from products.price)",
    ("employees", "manager_id"): "id of the manager (NULL for top managers)",
    ("employees", "email"): "masked in results",
}

_SAMPLE_COLUMNS = {"status", "category", "country", "region", "department"}


def describe_schema(db_path: str | Path, policy: Policy | None = None, with_samples: bool = True) -> str:
    """Return a compact text schema. Blocked columns are not shown to the model at all."""
    policy = policy or Policy()
    con = sqlite3.connect(db_path)
    lines: list[str] = []
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        lines.append(f"TABLE {t} ({n} rows)")
        for _cid, col, ctype, notnull, _d, pk in con.execute(f'PRAGMA table_info("{t}")'):
            if (t, col) in policy.blocked_columns:
                continue
            flags = ["PRIMARY KEY"] if pk else []
            if not notnull and not pk:
                flags.append("nullable")
            extra = []
            if (t, col) in NOTES:
                extra.append(NOTES[(t, col)])
            if with_samples and col in _SAMPLE_COLUMNS:
                vals = [r[0] for r in con.execute(
                    f'SELECT DISTINCT "{col}" FROM "{t}" WHERE "{col}" IS NOT NULL ORDER BY 1 LIMIT 8')]
                extra.append("values: " + ", ".join(map(str, vals)))
            tail = f"  -- {'; '.join(extra)}" if extra else ""
            lines.append(f"  {col} {ctype}{' ' + ', '.join(flags) if flags else ''}{tail}")
        for _i, _s, ref_table, frm, to, *_ in con.execute(f'PRAGMA foreign_key_list("{t}")'):
            lines.append(f"  FOREIGN KEY {frm} -> {ref_table}.{to}")
        lines.append("")
    con.close()
    return "\n".join(lines).rstrip()
