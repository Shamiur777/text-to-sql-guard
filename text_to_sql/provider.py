"""LLM provider interface, a scripted offline provider, and an optional Anthropic provider."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol


class Provider(Protocol):
    def complete(self, system: str, user: str, *, sample: int = 0) -> str:
        """Return the model text. `sample` > 0 asks for an alternative candidate."""


@dataclass
class Candidate:
    sql: str
    explanation: str = ""


class ScriptedProvider:
    """Deterministic stand-in for a model. SIMULATED: it replays canned outputs per question.

    `script` maps a question to a list of candidates; sample i returns candidate
    i (wrapping). `judge` optionally maps a question to a 0..1 verdict used when
    the pipeline asks for an LLM judge. Unknown questions get an explicit refusal-style
    output that is not valid SQL, so the pipeline abstains.
    """

    def __init__(self, script: dict[str, list[Candidate]], judge: dict[str, float] | None = None):
        self.script = script
        self.judge = judge or {}
        self.calls = 0

    def complete(self, system: str, user: str, *, sample: int = 0) -> str:
        self.calls += 1
        m = re.search(r"^Question: (.*)$", user, re.M)
        question = m.group(1).strip() if m else ""
        if "TASK: judge" in system:
            return json.dumps({"score": self.judge[question]}) if question in self.judge else "{}"
        cands = self.script.get(question)
        if not cands:
            return json.dumps({"sql": "", "explanation": "I do not know how to answer that."})
        c = cands[sample % len(cands)]
        return json.dumps({"sql": c.sql, "explanation": c.explanation})


class AnthropicProvider:
    """Calls the Anthropic Messages API. Lazy import; key from ANTHROPIC_API_KEY.

    Not exercised against the live API in this repo, only with an injected fake client.
    """

    def __init__(self, client=None, model: str | None = None, max_tokens: int = 700):
        self.model = model or os.environ.get("TEXT_TO_SQL_MODEL", "claude-sonnet-5-5")
        self.max_tokens = max_tokens
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic  # lazy: not needed for tests or the offline demo
            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, system: str, user: str, *, sample: int = 0) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0.0 if sample == 0 else 0.8,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(b, "text", "") for b in resp.content)
