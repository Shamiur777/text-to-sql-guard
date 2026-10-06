"""End-to-end pipeline: question -> SQL -> guardrails -> execution -> signals -> decision."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import signals
from .executor import ExecutionError, QueryResult, SafeExecutor, open_readonly
from .guardrails import GuardResult, validate_sql
from .policy import Catalog, Policy
from .prompt import (JUDGE_SYSTEM, build_judge_prompt, build_system_prompt, build_user_prompt,
                     parse_judge_reply, parse_model_reply)
from .provider import Provider
from .schema import describe_schema


class Decision(str, Enum):
    ANSWER = "answer"
    WARN = "answer with warning"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class Thresholds:
    """Hand-set, not fitted. Below `warn` the pipeline refuses to show an answer."""
    answer: float = 0.75
    warn: float = 0.50


@dataclass
class Answer:
    question: str
    decision: Decision
    confidence: float
    reason: str = ""
    sql: str | None = None             # sanitised SQL that was executed
    original_sql: str = ""             # what the model wrote
    explanation: str = ""              # the model's own explanation
    back_translation: str = ""         # deterministic English reading of the SQL
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    truncated: bool = False
    blocked: bool = False              # stopped by a safety guardrail
    signals: dict[str, float | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def shown_rows(self) -> list[tuple]:
        """Rows to present to a user: nothing when the pipeline abstained."""
        return [] if self.decision is Decision.ABSTAIN else self.rows


class Pipeline:
    def __init__(self, db_path: str | Path, provider: Provider, policy: Policy | None = None,
                 n_candidates: int = 3, thresholds: Thresholds | None = None, use_judge: bool = False):
        self.policy = policy or Policy()
        self.provider = provider
        self.n = max(1, n_candidates)
        self.thr = thresholds or Thresholds()
        self.use_judge = use_judge
        trusted = open_readonly(db_path)
        self.catalog = Catalog.from_connection(trusted, self.policy)
        trusted.close()
        self.executor = SafeExecutor(db_path, self.catalog, self.policy)
        self.system = build_system_prompt(describe_schema(db_path, self.policy), self.policy)

    def close(self) -> None:
        self.executor.close()

    # ------------------------------------------------------------------
    def _generate(self, question: str, sample: int) -> tuple[str, str]:
        return parse_model_reply(self.provider.complete(self.system, build_user_prompt(question), sample=sample))

    def _guard_and_run(self, sql: str) -> tuple[GuardResult, QueryResult | None, str]:
        guard = validate_sql(sql, self.catalog, self.policy)
        if not guard.ok:
            return guard, None, "; ".join(map(str, guard.violations))
        try:
            return guard, self.executor.run(guard.sql, guard.aliases, guard.ctes), ""
        except ExecutionError as e:
            return guard, None, str(e)

    def ask(self, question: str) -> Answer:
        sql, explanation = self._generate(question, 0)
        ans = Answer(question=question, decision=Decision.ABSTAIN, confidence=0.0,
                     original_sql=sql, explanation=explanation)
        if not sql:
            ans.reason = "the model produced no SQL" + (f" ({explanation})" if explanation else "")
            return ans

        guard, result, err = self._guard_and_run(sql)
        ans.signals["grounding"] = guard.grounding_score
        if not guard.ok:
            ans.blocked = guard.policy_violation
            kind = "blocked by guardrails" if guard.policy_violation else "not grounded in the schema"
            ans.reason = f"{kind}: {err}"
            return ans
        ans.sql = guard.sql
        if result is None:
            ans.reason = f"execution failed: {err}"
            return ans

        if guard.masked:
            ans.notes.append("masked columns: " + ", ".join(sorted(set(guard.masked))))
        if guard.omitted:
            ans.notes.append("restricted columns left out: " + ", ".join(sorted(set(guard.omitted))))
        ans.columns, ans.rows, ans.truncated = result.columns, result.rows, result.truncated

        bt, bt_note = signals.backtranslation_score(question, sql)
        ans.back_translation = signals.describe_sql(sql).sentence
        if self.use_judge:
            judge = parse_judge_reply(self.provider.complete(JUDGE_SYSTEM, build_judge_prompt(question, sql)))
            if judge is not None:
                bt = round((bt + judge) / 2, 3)
            bt_note += "; " + signals.judge_score_note(judge)
        ans.notes.append("back-translation: " + bt_note)

        results: list[QueryResult | None] = [result]
        for i in range(1, self.n):
            alt_sql, _ = self._generate(question, i)
            results.append(self._guard_and_run(alt_sql)[1] if alt_sql else None)
        cons, cons_note = signals.consistency_score(results)
        ans.notes.append("consistency: " + cons_note)

        san, san_notes = signals.sanity_score(question, guard, result, self.catalog)
        ans.notes += ["sanity: " + n for n in san_notes]

        ans.signals.update(backtranslation=bt, consistency=cons, sanity=san)
        ans.confidence = signals.combine(ans.signals)
        if ans.confidence >= self.thr.answer and not guard.masked:
            ans.decision = Decision.ANSWER
        elif ans.confidence >= self.thr.warn:
            ans.decision = Decision.WARN
            ans.reason = ("some columns were masked by policy" if guard.masked
                          else "moderate confidence: check the SQL before relying on this")
        else:
            ans.decision = Decision.ABSTAIN
            ans.reason = "confidence below the abstain threshold"
        return ans
