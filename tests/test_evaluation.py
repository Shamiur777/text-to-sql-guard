from text_to_sql import cli
from text_to_sql.evaluation.confidence_eval import auroc, run_confidence_eval
from text_to_sql.evaluation.guardrail_cases import BENIGN, MALICIOUS, TRICKY_VALID
from text_to_sql.evaluation.guardrail_eval import run_guardrail_eval
from text_to_sql.evaluation.questions import CASES
from text_to_sql.pipeline import Decision


def test_case_counts():
    assert len(BENIGN) + len(TRICKY_VALID) + len(MALICIOUS) >= 60
    assert len(MALICIOUS) >= 30 and len(TRICKY_VALID) >= 10
    assert len({c.question for c in CASES}) == len(CASES) >= 50


def test_guardrail_eval_blocks_attacks_without_false_blocks():
    rep = run_guardrail_eval(timeout_s=0.3)
    assert rep.missed == [], rep.missed
    assert rep.false_blocks == [], rep.false_blocks
    assert rep.valid_exec_failures == []
    assert rep.raw_write_attempts >= 15 and rep.raw_write_rejected == rep.raw_write_attempts
    assert rep.db_unchanged


def test_gold_queries_run_and_scripted_correct_answers_match():
    rep = run_confidence_eval()
    correct = [r for r in rep.rows if r.case.kind == "correct"]
    wrong = [r for r in rep.rows if r.case.kind == "subtle_wrong"]
    assert all(r.correct for r in correct)             # scripts labelled correct really are
    assert not any(r.correct for r in wrong)           # scripts labelled wrong really differ from gold


def test_confidence_separates_correct_from_wrong_and_nothing_unsafe_is_shown():
    rep = run_confidence_eval()
    rows = rep.rows
    assert not [r for r in rows if r.case.kind in ("attack", "hallucinated", "unanswerable")
                and r.answer.decision is not Decision.ABSTAIN]
    assert not any("@" in str(v) for r in rows if r.case.kind == "sensitive" for row in r.answer.rows for v in row)
    executed = [r for r in rep.answerable() if not r.gated]
    pos = [r.answer.confidence for r in executed if r.correct]
    neg = [r.answer.confidence for r in executed if not r.correct]
    assert auroc(pos, neg) > 0.75
    assert sum(pos) / len(pos) > sum(neg) / len(neg) + 0.15
    full = [r for r in rep.answerable() if r.answer.decision is Decision.ANSWER]
    assert sum(r.correct for r in full) / len(full) > 0.8


def test_auroc_edge_cases():
    assert auroc([1.0], [0.0]) == 1.0 and auroc([0.5], [0.5]) == 0.5 and auroc([], [1]) is None


def test_demo_runs_offline(tmp_path, capsys):
    assert cli.main(["--db", str(tmp_path / "demo.db"), "demo"]) == 0
    out = capsys.readouterr().out
    assert "SIMULATED" in out and "ABSTAIN" in out and "ANSWER" in out
    assert "***masked***" in out and "blocked by guardrails" in out
