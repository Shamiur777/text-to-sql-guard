"""Deterministic SQL guardrails: parse, validate, rewrite. No model is involved.

`validate_sql` returns a `GuardResult`. If `ok`, `result.sql` is the sanitised
statement to run (LIMIT enforced, masked columns replaced). Violations carry a
`code`; codes in GROUNDING_CODES mean "the SQL refers to something that does not
exist" (a hallucination signal), the rest are policy violations (a safety signal).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from .policy import ALLOWED_FUNCTIONS, DANGEROUS_FUNCTIONS, Catalog, Policy

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # 'unsupported syntax' warnings are expected for rejected input

GROUNDING_CODES = {"unknown_table", "unknown_column", "ambiguous_column", "unknown_function", "unknown_value"}

_FORBIDDEN = tuple(
    getattr(exp, n) for n in (
        "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command", "Pragma",
        "Attach", "Detach", "Merge", "Transaction", "Commit", "Rollback", "Set", "Use",
        "Copy", "Into", "TruncateTable", "Analyze", "Describe", "Show",
    ) if hasattr(exp, n)
)


@dataclass
class Violation:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class GuardResult:
    ok: bool
    sql: str | None = None
    violations: list[Violation] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)       # base tables referenced
    masked: list[str] = field(default_factory=list)       # "table.column" that were masked
    omitted: list[str] = field(default_factory=list)      # blocked columns dropped from `*`
    limit_applied: int | None = None
    has_aggregate: bool = False
    has_group_by: bool = False
    refs_checked: int = 0
    ctes: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=dict)  # alias or name -> base table

    @property
    def codes(self) -> set[str]:
        return {v.code for v in self.violations}

    @property
    def grounding_failure(self) -> bool:
        return bool(self.codes & GROUNDING_CODES)

    @property
    def policy_violation(self) -> bool:
        return bool(self.codes - GROUNDING_CODES)

    @property
    def grounding_score(self) -> float:
        """Share of table/column/function references that resolved (1.0 = all grounded)."""
        total = self.refs_checked
        bad = sum(1 for v in self.violations if v.code in GROUNDING_CODES)
        return 1.0 if total == 0 else max(0.0, 1 - bad / total)


def _reject(code: str, msg: str) -> GuardResult:
    return GuardResult(ok=False, violations=[Violation(code, msg)])


def _lexical_checks(sql: str) -> GuardResult | None:
    """Reject comments, control characters and stacked statements before parsing."""
    if not sql or not sql.strip():
        return _reject("empty", "empty query")
    if any(ord(c) < 32 and c not in "\t\n\r" for c in sql):
        return _reject("control_char", "control characters are not allowed")
    try:
        tokens = sqlglot.tokenize(sql, read="sqlite")
    except SqlglotError as e:
        return _reject("parse_error", f"could not tokenize: {str(e)[:80]}")
    if any(t.comments for t in tokens):
        return _reject("comment", "SQL comments are not allowed")
    semis = [i for i, t in enumerate(tokens) if t.token_type == TokenType.SEMICOLON]
    if len(semis) > 1 or (semis and semis[0] != len(tokens) - 1):
        return _reject("multiple_statements", "only one statement is allowed")
    return None


def _outputs(node: exp.Expression) -> list[str] | None:
    """Output column names of a derived table, or None if it contains a wildcard."""
    alias = node.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return [c.name.lower() for c in alias.args["columns"]]
    sel = node
    while isinstance(sel, (exp.Subquery, exp.CTE)):
        sel = sel.this
    first = sel
    while isinstance(first, exp.SetOperation):
        first = first.this
    if not isinstance(first, exp.Select):
        return None
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
           for e in first.expressions):
        return None
    return [n.lower() for n in first.named_selects]


class _Validator:
    def __init__(self, root: exp.Expression, catalog: Catalog, policy: Policy):
        self.root, self.cat, self.policy = root, catalog, policy
        self.res = GuardResult(ok=True)
        self.refs = 0
        self.ctes = {c.alias.lower(): c for c in root.find_all(exp.CTE)}
        self._sources: dict[int, dict] = {}
        self._mask: list[exp.Column] = []
        self._base: dict[int, tuple[str, str]] = {}

    # -- scope helpers -------------------------------------------------
    def add(self, code: str, msg: str) -> None:
        if not any(v.code == code and v.message == msg for v in self.res.violations):
            self.res.violations.append(Violation(code, msg))

    def sources(self, select: exp.Select) -> dict:
        """alias -> ('table', name) | ('derived', outputs-or-None) for one SELECT's FROM/JOINs."""
        key = id(select)
        if key in self._sources:
            return self._sources[key]
        out: dict = {}
        nodes = []
        frm = select.args.get("from_") or select.args.get("from")
        if frm is not None:
            nodes.append(frm.this)
        nodes += [j.this for j in select.args.get("joins") or []]
        for n in nodes:
            alias = (n.alias_or_name or "").lower()
            if isinstance(n, exp.Table):
                name = n.name.lower()
                if name in self.ctes and not n.args.get("db"):
                    out[alias] = ("derived", _outputs(self.ctes[name]))
                else:
                    out[alias] = ("table", name)
            elif isinstance(n, exp.Subquery):
                out[alias] = ("derived", _outputs(n))
            else:
                out[alias] = ("derived", None)
        self._sources[key] = out
        return out

    # -- steps ---------------------------------------------------------
    def check_nodes(self) -> None:
        for node in self.root.walk():
            if isinstance(node, _FORBIDDEN):
                self.add("forbidden_statement", f"{type(node).__name__} is not allowed")
        for t in self.root.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                self.add("forbidden_function_table", "table-valued functions are not allowed")
        for f in self.root.find_all(exp.Anonymous):
            self.refs += 1
            name = str(f.name).lower()
            if name in DANGEROUS_FUNCTIONS:
                self.add("dangerous_function", f"function {name}() is not allowed")
            elif name not in ALLOWED_FUNCTIONS:
                self.add("unknown_function", f"function {name}() does not exist or is not allowed")

    def check_tables(self) -> None:
        seen: list[str] = []
        for t in self.root.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                continue
            name = t.name.lower()
            if name in self.ctes and not t.args.get("db"):
                continue
            self.refs += 1
            db = t.args.get("db")
            if db is not None and db.name.lower() != "main":
                self.add("forbidden_schema", f"schema {db.name} is not allowed")
            elif name.startswith("sqlite_"):
                self.add("forbidden_table", f"system table {name} is not allowed")
            elif name not in self.cat.tables:
                self.add("unknown_table", f"table {name} does not exist")
            elif name not in seen:
                seen.append(name)
            self.res.aliases[(t.alias_or_name or name).lower()] = name
        self.res.tables = seen

    def expand_stars(self) -> None:
        for sel in reversed(list(self.root.find_all(exp.Select))):
            stars = [e for e in sel.expressions
                     if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))]
            if not stars:
                continue
            srcs = self.sources(sel)
            new: list[exp.Expression] = []
            for e in sel.expressions:
                if e not in stars:
                    new.append(e)
                    continue
                wanted = [e.table.lower()] if isinstance(e, exp.Column) and e.table else list(srcs)
                for alias in wanted:
                    kind = srcs.get(alias)
                    if kind is None:
                        self.add("unknown_table", f"table or alias {alias} does not exist")
                        continue
                    cols = (self.cat.tables.get(kind[1], []) if kind[0] == "table" else kind[1])
                    if cols is None:  # wildcard over an already-sanitised derived table
                        new.append(exp.Column(this=exp.Star(), table=exp.to_identifier(alias) if alias else None))
                        continue
                    for c in cols:
                        if kind[0] == "table" and (kind[1], c) in self.policy.blocked_columns:
                            self.res.omitted.append(f"{kind[1]}.{c}")
                            continue
                        new.append(exp.Column(this=exp.to_identifier(c),
                                              table=exp.to_identifier(alias) if alias else None))
            sel.set("expressions", new)
            self._sources.pop(id(sel), None)

    def resolve_columns(self) -> None:
        for col in list(self.root.find_all(exp.Column)):
            if isinstance(col.this, exp.Star):
                continue
            name = col.name.lower()
            qual = col.table.lower() if col.table else ""
            self.refs += 1
            chain = []
            node = col.parent
            while node is not None:
                if isinstance(node, exp.Select):
                    chain.append(node)
                node = node.parent
            base = self._resolve(col, name, qual, chain)
            if base is None:
                continue
            table, column = base
            self._base[id(col)] = base
            if (table, column) in self.policy.blocked_columns:
                self.add("blocked_column", f"column {table}.{column} is restricted")
            elif (table, column) in self.policy.masked_columns:
                p = col.parent
                bare = isinstance(p, exp.Select) or (isinstance(p, exp.Alias) and isinstance(p.parent, exp.Select))
                if bare:
                    self._mask.append(col)
                else:
                    self.add("blocked_column",
                             f"column {table}.{column} is masked and may only be selected, not filtered or computed on")

    def _resolve(self, col, name, qual, chain):
        """Return (table, column) for a base-table column, None if derived/unresolvable."""
        for sel in chain:
            srcs = self.sources(sel)
            if qual:
                if qual not in srcs:
                    continue
                kind, val = srcs[qual]
                if kind == "table":
                    if name in self.cat.tables.get(val, []):
                        return val, name
                    self.add("unknown_column", f"column {qual}.{name} does not exist")
                    return None
                if val is not None and name not in val:
                    self.add("unknown_column", f"column {qual}.{name} does not exist")
                return None
            hits = []
            for alias, (kind, val) in srcs.items():
                if kind == "table" and name in self.cat.tables.get(val, []):
                    hits.append((alias, val))
                elif kind == "derived" and (val is None or name in val):
                    hits.append((alias, None))
            if len(hits) > 1 and not self._in_using(sel, name):
                tables = ", ".join(a for a, _ in hits)
                self.add("ambiguous_column", f"column {name} is ambiguous between {tables}")
                return None
            if hits:
                return (hits[0][1], name) if hits[0][1] else None
            if name in {e.alias.lower() for e in sel.expressions if isinstance(e, exp.Alias)}:
                return None
        if qual:
            self.add("unknown_table", f"table or alias {qual} does not exist")
        else:
            self.add("unknown_column", f"column {name} does not exist")
        return None

    @staticmethod
    def _in_using(sel, name) -> bool:
        for j in sel.args.get("joins") or []:
            if any(i.name.lower() == name for i in (j.args.get("using") or [])):
                return True
        return False

    def check_values(self) -> None:
        """A string compared to a low-cardinality text column must be a value that exists there."""
        def check(col, lits):
            base = self._base.get(id(col))
            known = self.cat.values.get(base) if base else None
            if known is None:
                return
            self.refs += len(lits)
            for lit in lits:
                if lit.this not in known:
                    self.add("unknown_value",
                             f"{base[0]}.{base[1]} never contains '{lit.this}' (known: {', '.join(sorted(known)[:8])})")
        for node in self.root.find_all(exp.EQ, exp.NEQ):
            a, b = node.this, node.expression
            for col, lit in ((a, b), (b, a)):
                if isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and lit.is_string:
                    check(col, [lit])
        for node in self.root.find_all(exp.In):
            if isinstance(node.this, exp.Column):
                check(node.this, [e for e in node.expressions if isinstance(e, exp.Literal) and e.is_string])

    def apply_masks(self) -> None:
        for col in self._mask:
            label = col.alias_or_name
            lit = exp.Literal.string(self.policy.mask_value)
            if isinstance(col.parent, exp.Alias):
                col.replace(lit)
            else:
                col.replace(exp.Alias(this=lit, alias=exp.to_identifier(label)))
            self.res.masked.append(label)

    def enforce_limit(self) -> None:
        cap = self.policy.max_rows
        limit = self.root.args.get("limit")
        value = None
        if limit is not None and isinstance(limit.expression, exp.Literal) and not limit.expression.is_string:
            try:
                value = int(limit.expression.this)
            except ValueError:
                value = None
        if value is None or value > cap:
            self.root.set("limit", exp.Limit(expression=exp.Literal.number(cap)))
            self.res.limit_applied = cap
        else:
            self.res.limit_applied = value


def validate_sql(sql: str, catalog: Catalog, policy: Policy | None = None) -> GuardResult:
    policy = policy or Policy()
    early = _lexical_checks(sql)
    if early:
        return early
    try:
        stmts = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None and not isinstance(s, exp.Semicolon)]
    except SqlglotError as e:
        return _reject("parse_error", f"could not parse: {str(e)[:100]}")
    if len(stmts) != 1:
        return _reject("multiple_statements" if stmts else "empty", "exactly one statement is required")
    root = stmts[0]
    if not isinstance(root, (exp.Select, exp.SetOperation)):
        return _reject("not_select", f"only SELECT is allowed, got {type(root).__name__}")

    v = _Validator(root, catalog, policy)
    v.check_nodes()
    v.check_tables()
    if v.res.violations:                      # no point resolving columns of a bad query
        v.res.refs_checked = v.refs
        v.res.ok = False
        return v.res
    v.expand_stars()
    v.resolve_columns()
    v.check_values()
    v.res.refs_checked = v.refs
    if v.res.violations:
        v.res.ok = False
        return v.res
    v.apply_masks()
    v.enforce_limit()
    v.res.has_aggregate = any(
        not a.find_ancestor(exp.Window) for a in root.find_all(exp.AggFunc))
    v.res.has_group_by = root.find(exp.Group) is not None
    v.res.ctes = set(v.ctes)
    v.res.sql = root.sql(dialect="sqlite")
    return v.res
