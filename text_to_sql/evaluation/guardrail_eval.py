"""Guardrail evaluation: block rate on malicious SQL, false-block rate on valid SQL."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..db import build_sample_db
from ..executor import ExecutionError, QueryTooExpensive, SafeExecutor, open_readonly
from ..guardrails import validate_sql
from ..policy import Catalog, Policy
from .guardrail_cases import BENIGN, MALICIOUS, TRICKY_VALID

WRITE_PREFIXES = ("DROP", "DELETE", "UPDATE", "INSERT", "REPLACE", "CREATE", "ALTER", "PRAGMA",
                  "ATTACH", "DETACH", "VACUUM", "BEGIN", "WITH X AS (SELECT 1) UPDATE")


@dataclass
class GuardrailReport:
    n_valid: int = 0
    n_malicious: int = 0
    false_blocks: list = field(default_factory=list)
    valid_executed: int = 0
    valid_exec_failures: list = field(default_factory=list)
    missed: list = field(default_factory=list)
    layers: dict = field(default_factory=dict)
    raw_write_attempts: int = 0
    raw_write_rejected: int = 0
    db_unchanged: bool = True

    @property
    def block_rate(self) -> float:
        return (self.n_malicious - len(self.missed)) / self.n_malicious

    @property
    def false_block_rate(self) -> float:
        return len(self.false_blocks) / self.n_valid


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_write(sql: str) -> bool:
    s = sql.strip().upper()
    return s.startswith(WRITE_PREFIXES) or "INSERT INTO" in s or "DELETE FROM" in s or " UPDATE " in s


def run_guardrail_eval(db_path: str | Path | None = None, timeout_s: float = 0.5) -> GuardrailReport:
    tmp = None
    if db_path is None:
        tmp = tempfile.TemporaryDirectory()
        db_path = build_sample_db(Path(tmp.name) / "eval.db")
    db_path = Path(db_path)
    policy = Policy(timeout_s=timeout_s)
    trusted = open_readonly(db_path)
    catalog = Catalog.from_connection(trusted, policy)
    trusted.close()
    ex = SafeExecutor(db_path, catalog, policy)
    before = _sha(db_path)
    rep = GuardrailReport(n_valid=len(BENIGN) + len(TRICKY_VALID), n_malicious=len(MALICIOUS))

    for sql, note in BENIGN + TRICKY_VALID:
        g = validate_sql(sql, catalog, policy)
        if not g.ok:
            rep.false_blocks.append((sql, note, [str(v) for v in g.violations]))
            continue
        try:
            ex.run(g.sql, g.aliases, g.ctes)
            rep.valid_executed += 1
        except ExecutionError as e:
            rep.valid_exec_failures.append((sql, note, str(e)))

    for sql, note in MALICIOUS:
        g = validate_sql(sql, catalog, policy)
        if not g.ok:
            layer = "parser"
        else:
            try:
                ex.run(g.sql, g.aliases, g.ctes)
                rep.missed.append((sql, note))
                continue
            except QueryTooExpensive:
                layer = "plan check"
            except ExecutionError as e:
                layer = "timeout" if "time limit" in str(e) else "database"
        rep.layers[layer] = rep.layers.get(layer, 0) + 1

    # Second layer on its own: skip the parser entirely and send write attempts to the connection.
    for sql, _ in MALICIOUS:
        if _is_write(sql):
            rep.raw_write_attempts += 1
            try:
                ex.run_raw(sql)
            except (ExecutionError, sqlite3.Error):
                rep.raw_write_rejected += 1
    rep.db_unchanged = _sha(db_path) == before
    ex.close()
    if tmp:
        tmp.cleanup()
    return rep


def format_guardrail_report(rep: GuardrailReport) -> str:
    out = [
        f"Valid queries:     {rep.n_valid} ({len(BENIGN)} benign, {len(TRICKY_VALID)} tricky-valid)",
        f"  falsely blocked: {len(rep.false_blocks)} ({rep.false_block_rate:.1%})",
        f"  executed fine:   {rep.valid_executed}/{rep.n_valid}",
        f"Malicious queries: {rep.n_malicious}",
        f"  blocked:         {rep.n_malicious - len(rep.missed)} ({rep.block_rate:.1%})",
        f"  blocked by layer: {dict(sorted(rep.layers.items()))}",
        f"Parser bypassed (raw SQL sent straight to the read-only connection):",
        f"  write/DDL/PRAGMA/ATTACH attempts rejected by SQLite itself: {rep.raw_write_rejected}/{rep.raw_write_attempts}",
        f"  database file hash unchanged after all attempts: {rep.db_unchanged}",
    ]
    for sql, note, why in rep.false_blocks:
        out.append(f"  FALSE BLOCK [{note}]: {sql!r} -> {why}")
    for sql, note, why in rep.valid_exec_failures:
        out.append(f"  EXEC FAILURE [{note}]: {sql!r} -> {why}")
    for sql, note in rep.missed:
        out.append(f"  MISSED [{note}]: {sql!r}")
    return "\n".join(out)
