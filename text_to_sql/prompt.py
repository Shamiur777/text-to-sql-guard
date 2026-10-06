"""Prompt construction and parsing of the model's reply."""
from __future__ import annotations

import json
import re

from .policy import Policy

SYSTEM_TEMPLATE = """You translate a user's question into one SQLite SELECT query.

DATABASE SCHEMA
{schema}

RULES
- Output a single SELECT statement. No INSERT, UPDATE, DELETE, DDL, PRAGMA or ATTACH. No comments.
- Use only the tables and columns listed above. If the question cannot be answered with them, return an empty "sql" and say why in "explanation".
- Columns marked "masked" can only be selected, never filtered or computed on. Salaries are not available.
- Qualify columns with table aliases when joining; several tables share column names such as id, name and status.
- Add ORDER BY when the question asks for top/highest/lowest. At most {max_rows} rows are returned.
- "Revenue" means SUM(orders.total_amount) over orders whose status is not 'cancelled', unless the question says otherwise.

Reply with JSON only: {{"sql": "<query>", "explanation": "<one or two sentences describing what the query computes>"}}"""

JUDGE_SYSTEM = """TASK: judge
You check whether a SQL query answers a question. Reply with JSON only: {"score": <0..1>, "reason": "<short>"}.
1 means the query computes exactly what the question asks, 0 means it does not."""


def build_system_prompt(schema_text: str, policy: Policy) -> str:
    return SYSTEM_TEMPLATE.format(schema=schema_text, max_rows=policy.max_rows)


def build_user_prompt(question: str) -> str:
    return f"Question: {question.strip()}"


def build_judge_prompt(question: str, sql: str) -> str:
    return f"Question: {question.strip()}\nSQL: {sql}"


def parse_model_reply(text: str) -> tuple[str, str]:
    """Extract (sql, explanation) from a JSON reply, tolerating code fences and prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    brace = re.search(r"\{.*\}", text, re.S)
    if brace:
        candidates.append(brace.group(0))
    for c in candidates:
        try:
            data = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return str(data.get("sql") or "").strip(), str(data.get("explanation") or "").strip()
    block = re.search(r"```sql\s*(.*?)```", text, re.S | re.I)
    if block:
        return block.group(1).strip(), ""
    return "", ""


def parse_judge_reply(text: str) -> float | None:
    try:
        score = float(json.loads(text).get("score"))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None
    return min(1.0, max(0.0, score))
