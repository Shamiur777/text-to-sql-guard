import json

import pytest

from text_to_sql.pipeline import Decision, Pipeline, Thresholds
from text_to_sql.policy import Policy
from text_to_sql.prompt import parse_judge_reply, parse_model_reply
from text_to_sql.provider import AnthropicProvider, Candidate, ScriptedProvider


def make(db_path, script, **kw):
    return Pipeline(db_path, ScriptedProvider(script), Policy(timeout_s=1.0), **kw)


GOOD = "SELECT COUNT(*) FROM customers WHERE country = 'Germany'"
Q = "How many customers are from Germany?"


def test_agreeing_correct_candidates_are_answered(db_path):
    p = make(db_path, {Q: [Candidate(GOOD, "Counts German customers."), Candidate(GOOD), Candidate(GOOD)]})
    a = p.ask(Q)
    assert a.decision is Decision.ANSWER and a.confidence > 0.9
    assert a.rows[0][0] > 0 and a.sql.endswith("LIMIT 100")
    assert set(a.signals) == {"grounding", "backtranslation", "consistency", "sanity"}
    assert a.explanation == "Counts German customers." and "counts rows" in a.back_translation


def test_disagreeing_candidates_lower_confidence(db_path):
    other = "SELECT COUNT(*) FROM customers WHERE country = 'France'"
    agree = make(db_path, {Q: [Candidate(GOOD)] * 3}).ask(Q)
    split = make(db_path, {Q: [Candidate(GOOD), Candidate(other), Candidate(GOOD)]}).ask(Q)
    none = make(db_path, {Q: [Candidate(GOOD), Candidate(other), Candidate("SELECT 1 FROM nope")]}).ask(Q)
    assert split.signals["consistency"] == 0.5 and none.signals["consistency"] == 0.0
    assert agree.confidence > split.confidence > none.confidence


def test_blocked_sql_abstains_and_is_flagged_as_blocked(db_path):
    q = "Delete everything"
    a = make(db_path, {q: [Candidate("DELETE FROM orders")]}).ask(q)
    assert a.decision is Decision.ABSTAIN and a.blocked and a.confidence == 0.0
    assert a.sql is None and a.rows == []
    assert "blocked by guardrails" in a.reason


def test_hallucinated_schema_abstains_without_running(db_path):
    q = "What is the average rating?"
    a = make(db_path, {q: [Candidate("SELECT AVG(rating) FROM reviews")]}).ask(q)
    assert a.decision is Decision.ABSTAIN and not a.blocked
    assert "not grounded" in a.reason and a.signals["grounding"] == 0.0 and a.rows == []


def test_model_that_declines_or_returns_garbage_abstains(db_path):
    p = make(db_path, {"q1": [Candidate("", "no idea")]})
    assert p.ask("q1").decision is Decision.ABSTAIN
    assert "no SQL" in p.ask("unknown question").reason


def test_low_confidence_abstains_and_thresholds_are_respected(db_path):
    wrong = "SELECT AVG(id) FROM customers WHERE country = 'France'"
    script = {Q: [Candidate(wrong), Candidate(GOOD), Candidate(GOOD)]}
    strict = make(db_path, script, thresholds=Thresholds(answer=0.99, warn=0.98)).ask(Q)
    lenient = make(db_path, script, thresholds=Thresholds(answer=0.0, warn=0.0)).ask(Q)
    assert strict.decision is Decision.ABSTAIN and "below the abstain threshold" in strict.reason
    assert strict.shown_rows == [] and strict.rows       # withheld from display, kept for evaluation
    assert lenient.decision is Decision.ANSWER


def test_middle_band_answers_with_warning(db_path):
    other = "SELECT COUNT(*) FROM customers WHERE country = 'France'"
    a = make(db_path, {Q: [Candidate(GOOD), Candidate(other), Candidate(GOOD)]},
             thresholds=Thresholds(answer=0.95, warn=0.3)).ask(Q)
    assert a.decision is Decision.WARN and a.shown_rows


def test_runtime_failure_abstains(db_path):
    q = "x"
    a = make(db_path, {q: [Candidate("SELECT COUNT(*) FROM orders, order_items, customers")]}).ask(q)
    assert a.decision is Decision.ABSTAIN and "execution failed" in a.reason


def test_masked_columns_cap_decision_at_warning(db_path):
    q = "emails?"
    sql = "SELECT name, email FROM customers LIMIT 3"
    a = make(db_path, {q: [Candidate(sql)] * 3}, thresholds=Thresholds(answer=0.0, warn=0.0)).ask(q)
    assert a.decision is Decision.WARN and "masked" in a.reason
    assert all(r[1] == "***masked***" for r in a.rows)
    assert not any("@" in str(v) for r in a.rows for v in r)


def test_single_candidate_mode_skips_consistency(db_path):
    a = make(db_path, {Q: [Candidate(GOOD)]}, n_candidates=1).ask(Q)
    assert a.signals["consistency"] is None and a.decision is Decision.ANSWER


def test_llm_judge_is_averaged_into_back_translation(db_path):
    script = {Q: [Candidate(GOOD)] * 3}
    plain = make(db_path, script).ask(Q)
    judged = Pipeline(db_path, ScriptedProvider(script, judge={Q: 0.0}), Policy(), use_judge=True).ask(Q)
    assert judged.signals["backtranslation"] == pytest.approx(plain.signals["backtranslation"] / 2, abs=0.01)
    assert any("judge verdict 0.00" in n for n in judged.notes)


def test_judge_missing_verdict_is_ignored(db_path):
    a = Pipeline(db_path, ScriptedProvider({Q: [Candidate(GOOD)] * 3}), Policy(), use_judge=True).ask(Q)
    assert any("no judge available" in n for n in a.notes)


# ---------------------------------------------------------------- prompt and provider plumbing
def test_prompt_contains_schema_and_rules_but_not_salary(db_path):
    p = make(db_path, {})
    assert "TABLE employees" in p.system and "salary" not in p.system
    assert "single SELECT" in p.system and "JSON only" in p.system


def test_parse_model_reply_variants():
    assert parse_model_reply('{"sql": "SELECT 1", "explanation": "one"}') == ("SELECT 1", "one")
    assert parse_model_reply('```json\n{"sql": "SELECT 2", "explanation": "two"}\n```') == ("SELECT 2", "two")
    assert parse_model_reply('Sure! {"sql": "SELECT 3"} hope that helps')[0] == "SELECT 3"
    assert parse_model_reply("```sql\nSELECT 4\n```")[0] == "SELECT 4"
    assert parse_model_reply("I cannot help") == ("", "")
    assert parse_judge_reply('{"score": 1.4}') == 1.0 and parse_judge_reply("nope") is None


class FakeClient:
    """Stands in for anthropic.Anthropic; records the request."""
    def __init__(self, text):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        block = type("B", (), {"text": self.text})()
        return type("R", (), {"content": [block]})()


def test_anthropic_provider_with_injected_client(db_path):
    client = FakeClient("")
    client.text = json.dumps({"sql": GOOD, "explanation": "German customers."})
    prov = AnthropicProvider(client=client, model="test-model")
    pipe = Pipeline(db_path, prov, Policy(), n_candidates=2)
    a = pipe.ask(Q)
    assert a.decision is Decision.ANSWER
    assert len(client.calls) == 2
    assert client.calls[0]["model"] == "test-model" and client.calls[0]["temperature"] == 0.0
    assert client.calls[1]["temperature"] > 0
    assert "TABLE customers" in client.calls[0]["system"]
    assert client.calls[0]["messages"][0]["content"] == f"Question: {Q}"


def test_anthropic_provider_default_model_comes_from_env(monkeypatch):
    monkeypatch.delenv("TEXT_TO_SQL_MODEL", raising=False)
    assert AnthropicProvider(client=object()).model == "claude-sonnet-5-5"
    monkeypatch.setenv("TEXT_TO_SQL_MODEL", "other")
    assert AnthropicProvider(client=object()).model == "other"
