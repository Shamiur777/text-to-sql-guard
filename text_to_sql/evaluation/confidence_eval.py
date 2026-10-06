"""Does the confidence score separate correct answers from wrong ones?

SIMULATED evaluation: the model is a ScriptedProvider replaying hand-written candidates
(see questions.py). It says how well the checks catch the failure modes I scripted,
not how a real LLM would score.
"""
from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..db import build_sample_db
from ..pipeline import Answer, Decision, Pipeline
from ..policy import Policy
from ..provider import ScriptedProvider
from ..signals import canonical
from .questions import CASES, Case

BINS = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)]


@dataclass
class Row:
    case: Case
    answer: Answer
    correct: bool            # the displayed table equals the gold result
    gated: bool              # stopped before signals could be computed (blocked / ungrounded / failed)


@dataclass
class ConfidenceReport:
    rows: list[Row] = field(default_factory=list)

    def answerable(self):
        return [r for r in self.rows if r.case.gold is not None]

    def shown(self, rows):
        return [r for r in rows if r.answer.decision is not Decision.ABSTAIN]


def build_script(cases=CASES) -> ScriptedProvider:
    return ScriptedProvider({c.question: c.candidates for c in cases})


def run_confidence_eval(db_path: str | Path | None = None, cases=CASES) -> ConfidenceReport:
    tmp = None
    if db_path is None:
        tmp = tempfile.TemporaryDirectory()
        db_path = build_sample_db(Path(tmp.name) / "eval.db")
    pipe = Pipeline(db_path, build_script(cases), Policy(timeout_s=1.0))
    rep = ConfidenceReport()
    for case in cases:
        ans = pipe.ask(case.question)
        correct = False
        if case.gold is not None and ans.sql is not None and ans.rows is not None:
            gold = pipe.executor.run_raw(case.gold)
            correct = canonical(gold.rows) == canonical(ans.rows) and ans.sql is not None
        gated = "consistency" not in ans.signals
        rep.rows.append(Row(case, ans, correct, gated))
    pipe.close()
    if tmp:
        tmp.cleanup()
    return rep


def auroc(pos: list[float], neg: list[float]) -> float | None:
    """P(random correct answer has higher confidence than random wrong one); ties count half."""
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def format_confidence_report(rep: ConfidenceReport) -> str:
    out: list[str] = []
    rows = rep.rows
    out.append("SIMULATED evaluation: scripted provider, not a real LLM.")
    out.append(f"Questions: {len(rows)}  " + str(dict(Counter(r.case.kind for r in rows))))

    out.append("\nDecisions by kind")
    kinds = ["correct", "subtle_wrong", "hallucinated", "unanswerable", "attack", "sensitive"]
    out.append(f"  {'kind':<14}{'n':>3}{'answer':>8}{'warn':>6}{'abstain':>9}   blocked/ungrounded")
    for k in kinds:
        rs = [r for r in rows if r.case.kind == k]
        dec = Counter(r.answer.decision for r in rs)
        gated = sum(1 for r in rs if r.gated)
        out.append(f"  {k:<14}{len(rs):>3}{dec[Decision.ANSWER]:>8}{dec[Decision.WARN]:>6}{dec[Decision.ABSTAIN]:>9}   {gated}")

    ans_rows = rep.answerable()
    shown = rep.shown(ans_rows)
    abst = [r for r in ans_rows if r.answer.decision is Decision.ABSTAIN]
    strict = [r for r in ans_rows if r.answer.decision is Decision.ANSWER]
    primary_ok = sum(1 for r in ans_rows if r.correct)
    out.append(f"\nAnswerable questions (have a gold result): {len(ans_rows)}")
    out.append(f"  primary candidate correct, before any abstention: {primary_ok}/{len(ans_rows)} ({primary_ok/len(ans_rows):.1%})")
    out.append(f"  answered (answer or warning): {len(shown)}, correct {sum(r.correct for r in shown)}"
               f" -> accuracy of answered {sum(r.correct for r in shown)/max(len(shown),1):.1%}")
    out.append(f"  answered with full confidence only: {len(strict)}, correct {sum(r.correct for r in strict)}"
               f" -> {sum(r.correct for r in strict)/max(len(strict),1):.1%}")
    warn = [r for r in ans_rows if r.answer.decision is Decision.WARN]
    out.append(f"  answered with a warning: {len(warn)}, correct {sum(r.correct for r in warn)}")
    out.append(f"  abstained: {len(abst)}, of which the primary was actually correct (needless abstentions): "
               f"{sum(r.correct for r in abst)}; wrong answers withheld: {sum(not r.correct for r in abst)}")
    n_wrong = sum(not r.correct for r in ans_rows)
    out.append(f"  wrong answers shown to the user: {sum(not r.correct for r in shown)} of {n_wrong} wrong primaries")

    unans = [r for r in rows if r.case.gold is None and r.case.kind != "sensitive"]
    shown_bad = [r for r in unans if r.answer.decision is not Decision.ABSTAIN]
    out.append(f"\nQuestions with no valid answer (hallucinated, unanswerable, attacks): {len(unans)}")
    out.append(f"  withheld: {len(unans) - len(shown_bad)}, shown anyway: {len(shown_bad)}")
    for r in shown_bad:
        out.append(f"    SHOWN: {r.case.question!r} -> {r.answer.decision.value}, confidence {r.answer.confidence}")
    leaks = [r for r in rows if r.case.kind == "sensitive"
             and any(isinstance(v, str) and "@" in v for row in r.answer.rows for v in row)]
    sens = [r for r in rows if r.case.kind == "sensitive"]
    out.append(f"Sensitive-data requests: {len(sens)}; blocked {sum(r.gated for r in sens)}, "
               f"masked and flagged {sum(not r.gated for r in sens)}, raw emails returned: {len(leaks)}")

    executed = [r for r in ans_rows if not r.gated]
    out.append(f"\nCalibration (answerable questions that reached the signals: {len(executed)})")
    out.append(f"  {'confidence bin':<16}{'n':>3}{'mean conf':>11}{'fraction correct':>18}")
    for lo, hi in BINS:
        b = [r for r in executed if lo <= r.answer.confidence < hi]
        if b:
            mc = sum(r.answer.confidence for r in b) / len(b)
            out.append(f"  [{lo:.2f}, {min(hi,1.0):.2f}){'':<4}{len(b):>3}{mc:>11.2f}{sum(r.correct for r in b)/len(b):>18.0%}")
        else:
            out.append(f"  [{lo:.2f}, {min(hi,1.0):.2f}){'':<4}{0:>3}{'-':>11}{'-':>18}")
    pos = [r.answer.confidence for r in executed if r.correct]
    neg = [r.answer.confidence for r in executed if not r.correct]
    a = auroc(pos, neg)
    out.append(f"  mean confidence: correct {sum(pos)/max(len(pos),1):.2f} (n={len(pos)}), wrong {sum(neg)/max(len(neg),1):.2f} (n={len(neg)})")
    out.append(f"  AUROC (correct vs wrong): {a:.3f}" if a is not None else "  AUROC: n/a")
    for sig in ("backtranslation", "consistency", "sanity"):
        p = [r.answer.signals[sig] for r in executed if r.correct and r.answer.signals.get(sig) is not None]
        n = [r.answer.signals[sig] for r in executed if not r.correct and r.answer.signals.get(sig) is not None]
        sa = auroc(p, n)
        out.append(f"  signal {sig:<16} mean correct {sum(p)/max(len(p),1):.2f}, wrong {sum(n)/max(len(n),1):.2f}, AUROC {sa:.3f}" if sa is not None else f"  signal {sig}: n/a")

    cw = [r for r in shown if not r.correct]
    out.append(f"\nWrong answers that were still shown ({len(cw)}):")
    for r in cw:
        out.append(f"  conf {r.answer.confidence:.2f} [{r.case.kind}] {r.case.question}")
    nw = [r for r in abst if r.correct]
    out.append(f"Correct answers that were withheld ({len(nw)}):")
    for r in nw:
        out.append(f"  conf {r.answer.confidence:.2f} {r.case.question}  <- {r.answer.notes}")
    return "\n".join(out)
