import pytest

from text_to_sql.guardrails import validate_sql
from text_to_sql.policy import Policy


def check(sql, catalog, policy=None):
    return validate_sql(sql, catalog, policy)


def codes(sql, catalog):
    return check(sql, catalog).codes


# ---------------------------------------------------------------- statement type
@pytest.mark.parametrize("sql", [
    "SELECT * FROM customers",
    "select name from customers where country = 'France';",
    "WITH t AS (SELECT id FROM customers) SELECT id FROM t",
    "SELECT name FROM customers UNION SELECT name FROM employees",
])
def test_single_select_is_allowed(sql, catalog):
    assert check(sql, catalog).ok


@pytest.mark.parametrize("sql,code", [
    ("DROP TABLE customers", "not_select"),
    ("DELETE FROM orders", "not_select"),
    ("UPDATE products SET price = 0", "not_select"),
    ("INSERT INTO products (name) VALUES ('x')", "not_select"),
    ("CREATE TABLE t (x INT)", "not_select"),
    ("ALTER TABLE customers ADD COLUMN x INT", "not_select"),
    ("PRAGMA table_info(customers)", "not_select"),
    ("ATTACH DATABASE 'x.db' AS x", "not_select"),
    ("VACUUM", "not_select"),
    ("WITH x AS (SELECT 1) INSERT INTO products SELECT * FROM products", "not_select"),
    ("WITH x AS (SELECT 1) DELETE FROM orders", "not_select"),
])
def test_non_select_is_rejected(sql, code, catalog):
    assert code in codes(sql, catalog)


# ---------------------------------------------------------------- stacking and comments
@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE customers",
    "SELECT 1; SELECT 2",
    "SELECT name FROM customers WHERE name = '' OR 1=1; DROP TABLE customers --",
    "SELECT 1 /* x */; DELETE FROM orders",
])
def test_stacked_statements_rejected(sql, catalog):
    assert not check(sql, catalog).ok


def test_single_trailing_semicolon_is_fine_but_two_are_not(catalog):
    assert check("SELECT COUNT(*) FROM orders;", catalog).ok
    assert "multiple_statements" in codes("SELECT COUNT(*) FROM orders;;", catalog)


@pytest.mark.parametrize("sql", [
    "SELECT name FROM customers -- trailing",
    "SELECT name /* inline */ FROM customers",
    "SELECT name FROM customers /*! hidden */",
    "SEL/**/ECT 1",
])
def test_comments_rejected(sql, catalog):
    assert not check(sql, catalog).ok


def test_comment_markers_inside_strings_are_fine(catalog):
    assert check("SELECT '-- x' AS a, '/* y */' AS b FROM customers", catalog).ok
    assert check("SELECT 'a; DROP TABLE x' AS a FROM customers", catalog).ok


def test_nul_byte_and_empty_rejected(catalog):
    assert "control_char" in codes("SELECT 1\x00; DROP TABLE customers", catalog)
    assert "empty" in codes("   ", catalog)
    assert "parse_error" in codes("SELEC FROM WHERE", catalog) or not check("SELEC FROM WHERE", catalog).ok


# ---------------------------------------------------------------- tables, schemas, functions
def test_system_tables_and_foreign_schemas_rejected(catalog):
    assert "forbidden_table" in codes("SELECT * FROM sqlite_master", catalog)
    assert "forbidden_table" in codes("SELECT * FROM sqlite_schema", catalog)
    assert "forbidden_schema" in codes("SELECT * FROM other.customers", catalog)
    assert check("SELECT COUNT(*) FROM main.customers", catalog).ok


def test_table_valued_functions_rejected(catalog):
    assert not check("SELECT * FROM pragma_table_info('customers')", catalog).ok


@pytest.mark.parametrize("fn", ["load_extension('x')", "readfile('x')", "writefile('x','y')",
                                "randomblob(1000000000)", "zeroblob(1000000000)", "fts3_tokenizer('x')"])
def test_dangerous_functions_rejected(fn, catalog):
    assert "dangerous_function" in codes(f"SELECT {fn}", catalog)


def test_unknown_function_is_a_grounding_failure(catalog):
    r = check("SELECT magic_score(name) FROM customers", catalog)
    assert "unknown_function" in r.codes and r.grounding_failure and not r.policy_violation


# ---------------------------------------------------------------- grounding
def test_unknown_table_and_column(catalog):
    r = check("SELECT x FROM nope", catalog)
    assert "unknown_table" in r.codes and r.grounding_failure
    r = check("SELECT lifetime_value FROM customers", catalog)
    assert r.codes == {"unknown_column"} and r.grounding_failure
    assert "unknown_column" in codes("SELECT c.foo FROM customers c", catalog)
    assert "unknown_table" in codes("SELECT z.name FROM customers c", catalog)


def test_grounding_score_reflects_share_of_bad_references(catalog):
    good = check("SELECT name FROM customers", catalog)
    bad = check("SELECT name, phone FROM customers", catalog)
    assert good.grounding_score == 1.0
    assert 0 < bad.grounding_score < 1.0


def test_ambiguous_unqualified_column(catalog):
    r = check("SELECT id FROM customers c JOIN orders o ON o.customer_id = c.id", catalog)
    assert "ambiguous_column" in r.codes
    assert check("SELECT c.id FROM customers c JOIN orders o ON o.customer_id = c.id", catalog).ok
    assert check("SELECT status FROM customers", catalog).ok


def test_select_alias_may_be_used_in_order_by_and_group_by(catalog):
    assert check("SELECT country AS k, COUNT(*) AS n FROM customers GROUP BY k ORDER BY n DESC", catalog).ok


def test_correlated_subquery_resolves_outer_alias(catalog):
    sql = "SELECT name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)"
    assert check(sql, catalog).ok


def test_made_up_categorical_value_is_a_grounding_failure(catalog):
    r = check("SELECT COUNT(*) FROM orders WHERE status = 'negotiation'", catalog)
    assert r.codes == {"unknown_value"} and r.grounding_failure
    assert "unknown_value" in codes("SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'refunded')", catalog)
    assert check("SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'shipped')", catalog).ok
    # high-cardinality text columns are not value-checked
    assert check("SELECT COUNT(*) FROM customers WHERE name = 'Nobody Atall'", catalog).ok


# ---------------------------------------------------------------- sensitive columns
def test_blocked_column_everywhere(catalog):
    for sql in ["SELECT salary FROM employees",
                "SELECT e.salary FROM employees e",
                "SELECT AVG(salary) FROM employees",
                "SELECT name FROM employees ORDER BY salary",
                "SELECT name FROM employees WHERE salary > 1",
                "SELECT salary FROM (SELECT * FROM employees)",
                'SELECT "SALARY" FROM EMPLOYEES']:
        assert "blocked_column" in codes(sql, catalog) or "unknown_column" in codes(sql, catalog), sql
    assert "blocked_column" in codes("SELECT salary FROM employees", catalog)


def test_star_hides_blocked_and_masks_email(catalog):
    r = check("SELECT * FROM employees", catalog)
    assert r.ok and "salary" not in r.sql.lower()
    assert "employees.salary" in r.omitted
    assert "'***masked***' AS email" in r.sql
    r = check("SELECT e.* FROM employees e", catalog)
    assert r.ok and "salary" not in r.sql.lower()


def test_masked_column_is_replaced_in_projection(catalog):
    r = check("SELECT name, email FROM customers", catalog)
    assert r.ok and r.masked == ["email"]
    assert "customers.email" not in r.sql and "'***masked***' AS email" in r.sql


def test_masked_column_cannot_be_filtered_or_computed(catalog):
    for sql in ["SELECT name FROM customers WHERE email LIKE 'a%'",
                "SELECT COUNT(DISTINCT email) FROM customers",
                "SELECT name FROM customers ORDER BY email",
                "SELECT LOWER(email) FROM customers",
                "SELECT c.name FROM customers c JOIN employees e ON c.email = e.email"]:
        assert "blocked_column" in codes(sql, catalog), sql


def test_alias_does_not_dodge_the_mask(catalog):
    r = check("SELECT c.email AS contact FROM customers c", catalog)
    assert r.ok and "contact" in r.sql and "customers" in r.sql and "c.email" not in r.sql


# ---------------------------------------------------------------- limits
def test_limit_is_added_lowered_or_kept(catalog):
    assert check("SELECT name FROM customers", catalog).sql.endswith("LIMIT 100")
    assert check("SELECT name FROM customers LIMIT 100000", catalog).sql.endswith("LIMIT 100")
    assert check("SELECT name FROM customers LIMIT -1", catalog).sql.endswith("LIMIT 100")
    assert check("SELECT name FROM customers LIMIT 7", catalog).sql.endswith("LIMIT 7")
    r = check("SELECT name FROM customers UNION SELECT name FROM employees", catalog, Policy(max_rows=10))
    assert r.sql.endswith("LIMIT 10")


def test_aggregate_flags(catalog):
    r = check("SELECT country, COUNT(*) FROM customers GROUP BY country", catalog)
    assert r.has_aggregate and r.has_group_by
    r = check("SELECT COUNT(*) FROM customers", catalog)
    assert r.has_aggregate and not r.has_group_by
    r = check("SELECT name, ROW_NUMBER() OVER (ORDER BY name) FROM customers", catalog)
    assert not r.has_aggregate
