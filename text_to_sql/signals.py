"""Independent validity signals. Each returns a score in [0, 1] plus human-readable notes.

1. back-translation: describe the SQL in plain English (deterministically, from its
   parse tree) and measure lexical overlap with the question. Embedding-free.
2. consistency: several candidate queries are executed; agreement raises confidence.
3. result sanity: shape checks on the returned table (empty, NULLs, cardinality ...).
Schema grounding lives in guardrails.py (every table/column must exist).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .executor import QueryResult
from .guardrails import GuardResult
from .policy import Catalog

STOP = set("""a an the of in on at to for from by with and or is are was were be been do does did has have had
what which who whom whose how show list give find get tell me us please all any there their its it that this
those these as than then when where i we you can could would should many much name names every
""".split())
# 'many', 'name' and 'every' are handled via concepts or are too generic to require evidence.

CONCEPT_WORDS = {
    "COUNT": "many number count",
    "SUM": "total sum combined overall revenue sales spent spend spending",
    "AVG": "average mean avg typical",
    "HI": "highest most largest biggest maximum max latest newest top best greatest expensive",
    "LO": "lowest least smallest minimum min earliest oldest cheapest fewest bottom",
    "GROUP": "each per every by",
    "NEG": "not no without never except excluding exclude",
    "DISTINCT": "distinct unique different",
    "GT": "more over above greater exceeding exceed after since",
    "LT": "less fewer under below before cheaper lower",
}
WORD_TO_CONCEPT = {w: c for c, ws in CONCEPT_WORDS.items() for w in ws.split()}
# which SQL features satisfy a question concept
SATISFIED_BY = {
    "COUNT": {"COUNT"}, "SUM": {"SUM"}, "AVG": {"AVG"}, "HI": {"MAX", "DESC"}, "LO": {"MIN", "ASC"},
    "GROUP": {"GROUP", "DISTINCT"}, "NEG": {"NEG", "NULLCHK"}, "DISTINCT": {"DISTINCT", "GROUP"},
    "GT": {"GT"}, "LT": {"LT"},
}
# SQL features that must be justified by the question (precision side)
JUSTIFIED_BY = {
    "COUNT": {"COUNT", "HI", "LO"}, "SUM": {"SUM"}, "AVG": {"AVG"}, "MAX": {"HI"}, "MIN": {"LO"},
    "NEG": {"NEG"}, "GT": {"GT", "HI"}, "LT": {"LT", "LO"}, "DISTINCT": {"DISTINCT", "GROUP"},
}
SYNONYMS = {"revenue": "amount", "sales": "amount", "spent": "amount", "spend": "amount",
            "bought": "order", "purchase": "order", "purchased": "order", "placed": "order",
            "client": "customer", "staff": "employee", "worker": "employee", "item": "product",
            "price": "price", "cost": "price", "pay": "price", "paid": "price"}


def stem(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    return SYNONYMS.get(w, w)


NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten".split())}


def _tokens(text: str) -> list[str]:
    return [NUMBER_WORDS.get(t, t) for t in re.findall(r"[a-z0-9]+", text.lower())]


def question_profile(question: str) -> tuple[set[str], set[str]]:
    """Return (concepts, content-word stems) of a question."""
    toks = _tokens(question)
    concepts = {WORD_TO_CONCEPT[t] for t in toks if t in WORD_TO_CONCEPT}
    if "COUNT" in concepts:
        concepts.discard("SUM")      # "total number of orders" is a count, not a sum
    words = {stem(t) for t in toks if t not in STOP and t not in WORD_TO_CONCEPT}
    if "SUM" in concepts and ("revenue" in toks or "sales" in toks or "spent" in toks or "spend" in toks):
        words.add("amount")
    return concepts, words


@dataclass
class SqlDescription:
    sentence: str
    words: set[str] = field(default_factory=set)       # table/column/literal stems
    concepts: set[str] = field(default_factory=set)    # COUNT, SUM, GROUP, DESC ...
    literals: set[str] = field(default_factory=set)    # literal tokens that need support from the question


def describe_sql(sql: str) -> SqlDescription:
    """Deterministic English reading of a SELECT, plus the evidence used for the overlap score."""
    tree = sqlglot.parse_one(sql, read="sqlite")
    d = SqlDescription(sentence="")
    tables = [t.name for t in tree.find_all(exp.Table)]
    for t in tables:
        d.words |= {stem(p) for p in t.split("_")}
    for c in tree.find_all(exp.Column):
        if not isinstance(c.this, exp.Star):
            d.words |= {stem(p) for p in c.name.split("_")}
    for lit in tree.find_all(exp.Literal):
        for tok in _tokens(lit.this):
            if tok in ("d", "m", "y", "s", "q"):
                continue
            d.words.add(stem(tok))
            if tok.isdigit() or lit.is_string:
                d.literals.add(stem(tok))
    agg = {exp.Count: "COUNT", exp.Sum: "SUM", exp.Avg: "AVG", exp.Min: "MIN", exp.Max: "MAX"}
    parts = []
    for cls, name in agg.items():
        if tree.find(cls):
            d.concepts.add(name)
            parts.append({"COUNT": "counts rows", "SUM": "adds up", "AVG": "averages",
                          "MIN": "finds the minimum of", "MAX": "finds the maximum of"}[name])
    if tree.find(exp.Group):
        d.concepts.add("GROUP"); parts.append("for each group")
    if tree.find(exp.Distinct):
        d.concepts.add("DISTINCT"); parts.append("distinct")
    order = tree.find(exp.Order)
    if order:
        desc = any(o.args.get("desc") for o in order.expressions)
        d.concepts.add("DESC" if desc else "ASC")
        parts.append("highest first" if desc else "lowest first")
    limit = tree.find(exp.Limit)
    if limit is not None and isinstance(limit.expression, exp.Literal) and order:
        parts.append(f"top {limit.expression.this}")
        d.concepts.add("TOP")
    if tree.find(exp.GT) or tree.find(exp.GTE):
        d.concepts.add("GT"); parts.append("greater than")
    if tree.find(exp.LT) or tree.find(exp.LTE):
        d.concepts.add("LT"); parts.append("less than")
    if tree.find(exp.Not) or tree.find(exp.NEQ):
        d.concepts.add("NEG"); parts.append("excluding")
    for isn in tree.find_all(exp.Is):
        if isinstance(isn.expression, exp.Null):
            d.concepts.add("NULLCHK"); parts.append("missing values")
    cols = sorted({c.name for c in tree.find_all(exp.Column) if not isinstance(c.this, exp.Star)})
    d.sentence = ("Query " + ", ".join(parts) if parts else "Query lists rows") + \
        f" from {', '.join(sorted(set(tables)))} using columns {', '.join(cols) or '*'}"
    if d.literals:
        d.sentence += f" and values {', '.join(sorted(d.literals))}"
    return d


def backtranslation_score(question: str, sql: str) -> tuple[float, str]:
    """Lexical overlap between the question and an English reading of the SQL."""
    try:
        d = describe_sql(sql)
    except Exception:  # unparseable SQL cannot be back-translated
        return 0.0, "SQL could not be described"
    q_concepts, q_words = question_profile(question)
    word_cov = len(q_words & d.words) / len(q_words) if q_words else 1.0
    covered = [c for c in q_concepts if SATISFIED_BY[c] & d.concepts]
    concept_cov = len(covered) / len(q_concepts) if q_concepts else 1.0
    checks = [(f, JUSTIFIED_BY[f] & q_concepts) for f in d.concepts if f in JUSTIFIED_BY]
    checks += [(lit, lit in q_words or lit in {stem(t) for t in _tokens(question)}) for lit in d.literals]
    precision = sum(1 for _, ok in checks if ok) / len(checks) if checks else 1.0
    score = (0.45 * word_cov + 0.30 * concept_cov + 0.25 * precision) * (0.5 + 0.5 * precision)
    note = (f"word coverage {word_cov:.2f}, concept coverage {concept_cov:.2f}, "
            f"precision {precision:.2f}")
    return round(score, 3), note


def judge_score_note(score: float | None) -> str:
    return "no judge available" if score is None else f"judge verdict {score:.2f}"


# ---------------------------------------------------------------- consistency
def canonical(rows: list[tuple]) -> list[tuple]:
    def norm(v):
        return round(v, 2) if isinstance(v, float) else v
    return sorted((tuple(norm(v) for v in r) for r in rows), key=lambda r: [str(x) for x in r])


def consistency_score(results: list[QueryResult | None]) -> tuple[float | None, str]:
    """Agreement of every candidate with the primary (first) result. Failed candidates disagree."""
    if len(results) < 2:
        return None, "single candidate, consistency not measured"
    primary = results[0]
    if primary is None:
        return 0.0, "primary candidate did not execute"
    base = canonical(primary.rows)
    agree = sum(1 for r in results[1:] if r is not None and canonical(r.rows) == base)
    failed = sum(1 for r in results[1:] if r is None)
    note = f"{agree}/{len(results) - 1} alternative candidates match the primary result"
    if failed:
        note += f" ({failed} failed to run or were blocked)"
    return round(agree / (len(results) - 1), 3), note


# ---------------------------------------------------------------- sanity
PER_ENTITY = re.compile(r"\b(each|per|every|which|who)\b|\btop \d+", re.I)
SCALAR = re.compile(r"\b(how many|total|average|mean|number of|count|sum)\b", re.I)


def sanity_score(question: str, guard: GuardResult, result: QueryResult, catalog: Catalog) -> tuple[float, list[str]]:
    notes: list[str] = []
    penalty = 0.0
    n = len(result.rows)
    ncols = len(result.columns)
    if n == 0:
        penalty += 0.5; notes.append("result is empty")
    if result.truncated:
        penalty += 0.1; notes.append("result was truncated at the row cap")
    if n:
        null_cells = sum(1 for r in result.rows for v in r if v is None)
        ratio = null_cells / (n * ncols)
        if ratio > 0.5:
            penalty += 0.4; notes.append(f"{ratio:.0%} of result cells are NULL")
        elif ratio > 0.2:
            penalty += 0.15; notes.append(f"{ratio:.0%} of result cells are NULL")
    if n == 1 and ncols == 1 and result.rows[0][0] is None:
        penalty += 0.4; notes.append("single aggregate returned NULL")
    per_entity = bool(PER_ENTITY.search(question))
    if guard.has_aggregate and not guard.has_group_by and n == 1 and per_entity:
        penalty += 0.5; notes.append("per-entity question answered with one ungrouped aggregate row")
    if (not guard.has_aggregate) and guard.tables and n > 1:
        biggest = max(catalog.row_counts.get(t, 0) for t in guard.tables)
        if n > biggest and not result.truncated:
            penalty += 0.4; notes.append("more rows than any source table: probable join fan-out")
    top = re.search(r"\btop (\d+)\b", question, re.I)
    if top and n and n != int(top.group(1)) and not result.truncated:
        penalty += 0.3; notes.append(f"question asks for top {top.group(1)} but {n} rows returned")
    if SCALAR.search(question) and not per_entity and n > 5:
        penalty += 0.3; notes.append("scalar question returned many rows")
    return round(max(0.0, 1.0 - penalty), 3), notes


# ---------------------------------------------------------------- combination
WEIGHTS = {"backtranslation": 0.30, "consistency": 0.40, "sanity": 0.30}


def combine(signals: dict[str, float | None]) -> float:
    """Half weighted mean, half weakest signal: one failed check should not be averaged away."""
    avail = {k: v for k, v in signals.items() if v is not None and k in WEIGHTS}
    if not avail:
        return 0.0
    total = sum(WEIGHTS[k] for k in avail)
    mean = sum(WEIGHTS[k] * v for k, v in avail.items()) / total
    return round(0.5 * mean + 0.5 * min(avail.values()), 3)
