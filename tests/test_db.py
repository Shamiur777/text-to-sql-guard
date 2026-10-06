import hashlib
import sqlite3

from text_to_sql.db import build_sample_db
from text_to_sql.schema import describe_schema


def _digest(path):
    con = sqlite3.connect(path)
    rows = [con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            for t in ("customers", "products", "employees", "orders", "order_items")]
    con.close()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def test_same_seed_same_data(tmp_path):
    a = build_sample_db(tmp_path / "a.db", seed=3, n_customers=40, n_orders=100)
    b = build_sample_db(tmp_path / "b.db", seed=3, n_customers=40, n_orders=100)
    c = build_sample_db(tmp_path / "c.db", seed=4, n_customers=40, n_orders=100)
    assert _digest(a) == _digest(b)
    assert _digest(a) != _digest(c)


def test_sample_data_has_nulls_and_ambiguous_names(db_path):
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM customers WHERE city IS NULL").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM orders WHERE shipped_date IS NULL").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM employees WHERE manager_id IS NULL").fetchone()[0] > 0
    tables_with_status = [t for t in ("customers", "orders", "products")
                          if "status" in [r[1] for r in con.execute(f"PRAGMA table_info({t})")]]
    assert len(tables_with_status) == 3
    # totals are consistent with line items
    bad = con.execute("""SELECT COUNT(*) FROM orders o WHERE ABS(o.total_amount -
        (SELECT SUM(quantity * unit_price) FROM order_items WHERE order_id = o.id)) > 0.05""").fetchone()[0]
    assert bad == 0


def test_schema_description_hides_blocked_columns_and_lists_values(db_path, policy):
    text = describe_schema(db_path, policy)
    assert "TABLE orders" in text and "FOREIGN KEY customer_id -> customers.id" in text
    assert "salary" not in text
    assert "values: cancelled, delivered, pending, returned, shipped" in text
    assert "masked in results" in text
