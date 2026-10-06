"""Seeded synthetic sample database: a small fictional company.

Column names are deliberately a little ambiguous (`status` and `name` exist in
several tables, `price` vs `unit_price`, `region` vs `country`) and a few
columns contain NULLs, because that is what makes text-to-SQL hard.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DEFAULT_DB = Path("data") / "company.db"

SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    city TEXT,
    country TEXT NOT NULL,
    region TEXT,
    signup_date TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    price REAL NOT NULL,
    stock INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    department TEXT NOT NULL,
    hire_date TEXT NOT NULL,
    salary INTEGER NOT NULL,
    manager_id INTEGER REFERENCES employees(id)
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    employee_id INTEGER REFERENCES employees(id),
    order_date TEXT NOT NULL,
    shipped_date TEXT,
    status TEXT NOT NULL,
    total_amount REAL NOT NULL
);
CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_items_product ON order_items(product_id);
"""

COUNTRIES = {
    "Germany": ("Europe", ["Berlin", "Munich", "Hamburg"], 0.18),
    "France": ("Europe", ["Paris", "Lyon"], 0.14),
    "United Kingdom": ("Europe", ["London", "Leeds"], 0.14),
    "United States": ("North America", ["Austin", "Denver", "Boston", "Seattle"], 0.26),
    "Canada": ("North America", ["Toronto", "Calgary"], 0.08),
    "Brazil": ("South America", ["Recife", "Curitiba"], 0.06),
    "Japan": ("Asia", ["Osaka", "Sendai"], 0.07),
    "Australia": ("Oceania", ["Perth", "Adelaide"], 0.07),
}
FIRST = ["Ava", "Liam", "Noor", "Mateo", "Hana", "Oskar", "Priya", "Jonas", "Lena", "Omar",
         "Sofia", "Tariq", "Ines", "Kenji", "Maya", "Felix", "Zara", "Ivan", "Elif", "Diego"]
LAST = ["Keller", "Moreau", "Ito", "Silva", "Hart", "Novak", "Reyes", "Okafor", "Lindqvist",
        "Marsh", "Haddad", "Brandt", "Costa", "Walsh", "Sato", "Duarte", "Fischer", "Quinn"]
CATEGORIES = {
    "Electronics": [("Wireless Mouse", 24), ("USB-C Hub", 45), ("Mechanical Keyboard", 89),
                    ("Webcam HD", 59), ("Noise Headphones", 129), ("Portable Charger", 35),
                    ("Monitor Stand", 49), ("Bluetooth Speaker", 69)],
    "Office": [("Desk Lamp", 32), ("Notebook Pack", 12), ("Standing Mat", 55), ("Label Printer", 99),
               ("Paper Shredder", 79), ("Whiteboard", 65), ("Ink Set", 28)],
    "Kitchen": [("Coffee Grinder", 42), ("Water Bottle", 18), ("Lunch Box", 22), ("Tea Kettle", 38),
                ("Cutting Board", 20), ("Blender", 74)],
    "Outdoor": [("Camping Lantern", 27), ("Trail Backpack", 85), ("Hiking Poles", 48),
                ("Sleeping Bag", 110), ("Rain Jacket", 95)],
    "Toys": [("Puzzle 1000", 19), ("Building Set", 52), ("Board Game", 34), ("Kite", 15)],
}
DEPARTMENTS = [("Sales", 0.4, 52000, 78000), ("Support", 0.25, 38000, 54000),
               ("Engineering", 0.2, 72000, 120000), ("Finance", 0.1, 56000, 90000),
               ("Operations", 0.05, 45000, 70000)]
ORDER_STATUSES = [("delivered", 0.62), ("shipped", 0.12), ("pending", 0.1),
                  ("cancelled", 0.08), ("returned", 0.08)]


def _weighted(rng: random.Random, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def _day(base: date, n: int) -> str:
    return (base + timedelta(days=n)).isoformat()


def build_sample_db(path: str | Path, seed: int = 7, n_customers: int = 300,
                    n_orders: int = 1500) -> Path:
    """Create (overwrite) the sample database at `path`. Same seed, same data."""
    rng = random.Random(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    countries = [(c, v[2]) for c, v in COUNTRIES.items()]
    for cid in range(1, n_customers + 1):
        country = _weighted(rng, countries)
        region, cities, _ = COUNTRIES[country]
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        email = f"{name.lower().replace(' ', '.')}{cid}@example.test" if rng.random() > 0.04 else None
        city = rng.choice(cities) if rng.random() > 0.06 else None
        region_val = region if rng.random() > 0.03 else None
        status = rng.choices(["active", "inactive", "vip"], weights=[0.78, 0.17, 0.05])[0]
        con.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)",
                    (cid, name, email, city, country, region_val,
                     _day(date(2022, 1, 1), rng.randrange(0, 900)), status))

    pid = 0
    for cat, items in CATEGORIES.items():
        for name, price in items:
            pid += 1
            category = cat if rng.random() > 0.05 else None
            status = "discontinued" if rng.random() < 0.1 else "available"
            con.execute("INSERT INTO products VALUES (?,?,?,?,?,?)",
                        (pid, name, category, float(price), rng.randrange(0, 400), status))
    n_products = pid

    eid = 0
    for dept, share, lo, hi in DEPARTMENTS:
        for _ in range(max(2, round(share * 24))):
            eid += 1
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            manager = None if eid <= 2 else rng.randint(1, 2)
            email = f"{name.lower().replace(' ', '.')}.{eid}@corp.test"
            con.execute("INSERT INTO employees VALUES (?,?,?,?,?,?,?)",
                        (eid, name, email, dept, _day(date(2018, 1, 1), rng.randrange(0, 2000)),
                         rng.randrange(lo, hi, 500), manager))
    sales_ids = [r[0] for r in con.execute("SELECT id FROM employees WHERE department='Sales'")]

    prices = dict(con.execute("SELECT id, price FROM products"))
    # skewed customer activity: a few customers order a lot
    cust_weights = [1 / (i ** 0.6) for i in range(1, n_customers + 1)]
    iid = 0
    for oid in range(1, n_orders + 1):
        cust = rng.choices(range(1, n_customers + 1), weights=cust_weights)[0]
        status = _weighted(rng, ORDER_STATUSES)
        odate = date(2023, 1, 1) + timedelta(days=rng.randrange(0, 640))
        shipped = None
        if status in ("delivered", "shipped", "returned"):
            shipped = (odate + timedelta(days=rng.randint(1, 6))).isoformat()
        emp = rng.choice(sales_ids) if rng.random() > 0.07 else None
        total = 0.0
        lines = []
        for pr in rng.sample(range(1, n_products + 1), rng.choices([1, 2, 3, 4], [0.45, 0.3, 0.17, 0.08])[0]):
            qty = rng.choices([1, 2, 3, 5], [0.6, 0.25, 0.1, 0.05])[0]
            unit = round(prices[pr] * rng.choice([1.0, 1.0, 0.9, 1.05]), 2)  # price drifts from list
            total += qty * unit
            lines.append((pr, qty, unit))
        con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
                    (oid, cust, emp, odate.isoformat(), shipped, status, round(total, 2)))
        for pr, qty, unit in lines:
            iid += 1
            con.execute("INSERT INTO order_items VALUES (?,?,?,?,?)", (iid, oid, pr, qty, unit))
    con.commit()
    con.close()
    return path


def ensure_sample_db(path: str | Path = DEFAULT_DB, seed: int = 7) -> Path:
    path = Path(path)
    return path if path.exists() else build_sample_db(path, seed)
