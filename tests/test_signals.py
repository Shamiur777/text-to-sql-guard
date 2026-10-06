from text_to_sql.executor import QueryResult
from text_to_sql.guardrails import validate_sql
from text_to_sql.signals import (backtranslation_score, combine, consistency_score, describe_sql,
                                 question_profile, sanity_score)


def qr(rows, cols=("a",), truncated=False):
    return QueryResult(list(cols), rows, truncated, 0.0)


# ---------------------------------------------------------------- back-translation
def test_describe_sql_names_aggregates_filters_and_order():
    d = describe_sql("SELECT country, COUNT(*) FROM customers WHERE status = 'vip' "
                     "GROUP BY country ORDER BY 2 DESC LIMIT 3")
    assert {"COUNT", "GROUP", "DESC", "TOP"} <= d.concepts
    assert "vip" in d.literals and "customer" in d.words
    assert "counts rows" in d.sentence and "top 3" in d.sentence


def test_question_profile_extracts_concepts():
    c, w = question_profile("How many customers are from Germany?")
    assert c == {"COUNT"} and w == {"customer", "germany"}
    c, _ = question_profile("What is the total number of orders?")
    assert c == {"COUNT"}                       # 'total number' is a count, not a sum


def test_matching_sql_scores_high_and_mismatches_score_lower():
    q = "How many customers are from Germany?"
    good, _ = backtranslation_score(q, "SELECT COUNT(*) FROM customers WHERE country = 'Germany'")
    wrong_value, _ = backtranslation_score(q, "SELECT COUNT(*) FROM customers WHERE country = 'France'")
    wrong_agg, _ = backtranslation_score(q, "SELECT AVG(id) FROM customers WHERE country = 'Germany'")
    assert good > 0.9 and wrong_value < good - 0.2 and wrong_agg < good - 0.2


def test_direction_of_ordering_matters():
    q = "Which department has the largest number of employees?"
    desc, _ = backtranslation_score(q, "SELECT department, COUNT(*) c FROM employees GROUP BY department ORDER BY c DESC LIMIT 1")
    asc, _ = backtranslation_score(q, "SELECT department, COUNT(*) c FROM employees GROUP BY department ORDER BY c ASC LIMIT 1")
    assert desc > asc


def test_missing_negation_is_noticed():
    q = "How many customers have never made an order?"
    good, _ = backtranslation_score(q, "SELECT COUNT(*) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id WHERE o.id IS NULL")
    bad, _ = backtranslation_score(q, "SELECT COUNT(DISTINCT c.id) FROM customers c JOIN orders o ON o.customer_id = c.id")
    assert good > bad + 0.2


def test_number_words_match_limit_literals():
    s, _ = backtranslation_score("List the five most expensive products.",
                                 "SELECT name, price FROM products ORDER BY price DESC LIMIT 5")
    assert s > 0.8


def test_unparseable_sql_scores_zero():
    assert backtranslation_score("anything", "SELECT FROM WHERE (")[0] == 0.0


# ---------------------------------------------------------------- consistency
def test_consistency_full_partial_and_none():
    a = qr([(1,), (2,)])
    same_other_order = qr([(2,), (1,)])
    different = qr([(3,)])
    assert consistency_score([a, same_other_order, qr([(1,), (2,)])])[0] == 1.0
    assert consistency_score([a, same_other_order, different])[0] == 0.5
    assert consistency_score([a, different, different])[0] == 0.0


def test_consistency_failed_candidates_count_as_disagreement():
    a = qr([(1,)])
    score, note = consistency_score([a, None, qr([(1,)])])
    assert score == 0.5 and "failed" in note
    assert consistency_score([None, a])[0] == 0.0


def test_consistency_ignores_float_noise_and_needs_two_candidates():
    assert consistency_score([qr([(1.001,)]), qr([(1.0004,)])])[0] == 1.0
    assert consistency_score([qr([(1,)])])[0] is None


# ---------------------------------------------------------------- sanity
def sanity(question, sql, rows, catalog, cols=("a",), truncated=False):
    g = validate_sql(sql, catalog)
    assert g.ok, g.violations
    return sanity_score(question, g, qr(rows, cols, truncated), catalog)


def test_empty_result_penalised(catalog):
    s, notes = sanity("Which customers live in Atlantis?", "SELECT name FROM customers WHERE city = 'x'", [], catalog)
    assert s <= 0.5 and "empty" in notes[0]


def test_ungrouped_aggregate_for_per_entity_question(catalog):
    s, notes = sanity("Show the average order total per status", "SELECT AVG(total_amount) FROM orders", [(10.0,)], catalog)
    assert s <= 0.5 and any("ungrouped" in n for n in notes)
    s, _ = sanity("Show the average order total per status",
                  "SELECT status, AVG(total_amount) FROM orders GROUP BY status", [("a", 1.0), ("b", 2.0)], catalog, ("s", "v"))
    assert s == 1.0


def test_null_heavy_results_and_null_scalars(catalog):
    s, notes = sanity("What are the cities?", "SELECT city FROM customers", [(None,)] * 8 + [("x",)] * 2, catalog)
    assert s < 1.0 and any("NULL" in n for n in notes)
    s, notes = sanity("What is the total stock?", "SELECT SUM(stock) FROM products WHERE 1=0", [(None,)], catalog)
    assert s < 1.0


def test_fan_out_detected_when_rows_exceed_largest_source_table(catalog):
    rows = [(i,) for i in range(100)]
    s, notes = sanity("List products", "SELECT name FROM products", rows, catalog)
    assert any("fan-out" in n for n in notes)


def test_top_n_cardinality(catalog):
    s, notes = sanity("Show the top 3 products", "SELECT name FROM products", [("a",), ("b",)], catalog)
    assert any("top 3" in n for n in notes)


def test_healthy_result_scores_one(catalog):
    s, notes = sanity("How many customers are there?", "SELECT COUNT(*) FROM customers", [(300,)], catalog)
    assert s == 1.0 and notes == []


# ---------------------------------------------------------------- combination
def test_combine_is_pulled_down_by_the_weakest_signal():
    strong = combine({"backtranslation": 1.0, "consistency": 1.0, "sanity": 1.0})
    one_bad = combine({"backtranslation": 1.0, "consistency": 0.0, "sanity": 1.0})
    assert strong == 1.0 and one_bad < 0.5
    assert combine({"backtranslation": 0.8, "consistency": None, "sanity": 0.8}) == 0.8
    assert combine({}) == 0.0
