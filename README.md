# Text-to-SQL with Guardrails and Hallucination Detection

[![tests](https://github.com/Shamiur777/text-to-sql-guard/actions/workflows/tests.yml/badge.svg)](https://github.com/Shamiur777/text-to-sql-guard/actions)

A natural-language interface to a SQLite database. A model writes the SQL; deterministic guardrails decide whether it may run; independent checks decide whether it actually answers the question; the user gets the table, the SQL, a plain-English reading of the SQL, a confidence score, and one of three decisions: answer, answer with warning, abstain.

All data is synthetic (a fictional company, generated from a seed). No API key is needed to run or test anything here. **Every model output in the demo and the evaluations is simulated by a scripted provider.** The numbers below measure the guardrails and checks against hand-written model outputs, not how a real LLM performs.

## The problem
Text-to-SQL demos fail in two ways that matter in a business setting:
1. **Unsafe SQL.** A model (or a prompt injection hidden in a question) can emit `DROP`, `ATTACH`, stacked statements, or a query that reads salaries.
2. **Confident wrong answers.** The SQL runs, returns a clean table, and answers a different question: wrong filter, `MAX` instead of `MIN`, a join that double counts, a column that does not exist.

This repo treats the model as untrusted. Safety does not depend on the model behaving, and the user is told when the answer is doubtful instead of being handed a confident table.

## How it works

```
question
   |
   v
Provider (model returns {"sql", "explanation"}; sample 0 = primary, 1..n = alternatives)
   |
   v
GUARDRAILS (sqlglot parse tree, no model involved)
   |  single SELECT only, no comments, no stacked statements
   |  table / column / function allowlist, value check on categorical columns
   |  salary blocked, email masked, SELECT * expanded without restricted columns, LIMIT forced
   |--> policy violation  -> ABSTAIN (blocked)
   |--> not grounded      -> ABSTAIN (hallucinated table, column, function or value)
   v
SAFE EXECUTION: mode=ro connection + authorizer callback + progress-handler timeout
                + EXPLAIN QUERY PLAN cross-join estimate + row cap
   v
SIGNALS (each 0..1)                       confidence = 0.5 * weighted mean + 0.5 * weakest signal
   back-translation  SQL -> English, lexical overlap with the question   (weight 0.30)
   consistency       n candidates executed, agreement with the primary   (weight 0.40)
   sanity            empty / NULL-heavy / fan-out / ungrouped aggregate  (weight 0.30)
   v
>= 0.75 answer   |   0.50 to 0.75 answer with warning   |   < 0.50 abstain
```

Modules (`text_to_sql/`):
- `db.py`: seeded generator for `customers`, `products`, `employees`, `orders`, `order_items` (300 / 30 / 25 / 1,500 / 2,782 rows). Ambiguous names on purpose (`status` in three tables, `price` vs `unit_price`, `region` vs `country`), NULLs in `city`, `region`, `email`, `category`, `shipped_date`, `employee_id`, `manager_id`.
- `schema.py`: schema description for prompts (types, nullability, foreign keys, notes on ambiguous columns, sample values for categorical columns; blocked columns are never shown to the model).
- `provider.py`: `Provider` interface, `ScriptedProvider` (offline, simulated), `AnthropicProvider` (optional).
- `prompt.py`: system prompt (schema, rules), reply parsing, optional judge prompt.
- `guardrails.py`: the parser-level rules. `executor.py`: the database-level rules. `policy.py`: allowlists, sensitive columns, limits.
- `signals.py`: back-translation, consistency, sanity, combination. `pipeline.py`: the whole flow and the decision.
- `evaluation/`: 81 guardrail SQL strings, 58 scripted questions, and the two evaluation runners.

### Guardrail layers
| Layer | What it stops |
|---|---|
| Lexical (sqlglot tokenizer) | comments (`--`, `/* */`, including inside keywords), NUL/control characters, more than one statement. Comment markers inside string literals are fine. |
| Parse tree | anything but one `SELECT`/`UNION` (`DROP`, `DELETE`, `INSERT`, `REPLACE`, `CREATE`, `ALTER`, `PRAGMA`, `ATTACH`, `VACUUM`, `EXPLAIN`, CTE-wrapped writes), table-valued functions, system tables, foreign schemas, non-allowlisted or dangerous functions (`load_extension`, `readfile`, `randomblob` ...) |
| Grounding | unknown table, column, function, or categorical value (`status = 'negotiation'`); ambiguous unqualified columns |
| Sensitive columns | `employees.salary` blocked everywhere. `customers.email` and `employees.email` may only be selected, and are replaced by `'***masked***'`; filtering, grouping or aggregating on them is blocked (it would leak by inference). `SELECT *` is expanded without restricted columns. |
| Rewrite | `LIMIT` added, or lowered to the row cap |
| Connection | `mode=ro`; an authorizer that allows only `SELECT`, reads of allowed non-sensitive columns, and allowlisted functions; a progress-handler timeout; an `EXPLAIN QUERY PLAN` check that rejects unindexed multi-table scans whose row counts multiply past 1,000,000 |

The connection layer does not trust the parser. Tests send writes straight to it and confirm SQLite itself refuses them.

## Results

Reproduce everything with:

```bash
python -m pytest -q                 # 113 tests, ~6 s
python -m text_to_sql eval          # guardrail + confidence evaluation below
```

### Guardrail evaluation (81 SQL strings, run on the seeded sample DB)
| | Count | Result |
|---|---|---|
| Valid queries (20 benign + 15 tricky-valid, e.g. `DROP TABLE` inside a string literal, `--` inside a string, odd casing, `SELECT *` on `employees`) | 35 | 0 falsely blocked (0.0%); all 35 executed |
| Malicious queries (DML, DDL, PRAGMA, ATTACH, stacked and injection-style, comment-obfuscated, CTE-wrapped writes, dangerous functions, system tables, salary/email access, cross joins, runaway recursion) | 46 | 46 blocked (100.0%): 43 by the parser layer, 2 by the plan check, 1 by the timeout |
| Parser bypassed: the 19 write/DDL/PRAGMA/ATTACH strings sent directly to the read-only connection | 19 | 19 rejected by SQLite; database file hash unchanged |

Caveat: I wrote both the guardrails and these test strings, so 100% means "no known gap in my own list", not "unbreakable". During development the evaluation found two real bugs (a column-list CTE was mis-resolved, and the cross-join check missed aliased and covering-index scans); both were fixed before the numbers above.

### Confidence evaluation (58 questions, **scripted provider, simulated**)
The pretend model replays hand-written candidates: 24 correct, 14 subtly wrong (wrong aggregate, dropped filter, ASC/DESC flipped, off-by-one, join fan-out, wrong column), 7 hallucinated (6 invented tables/columns, 1 invented status value), 1 out-of-scope, 9 attacks, 3 requests for restricted data. For some wrong cases all three candidates repeat the same mistake (a consistent model error), for others the alternatives are right. How often that happens is my assumption.

| Measure (38 answerable questions with a gold result) | Value |
|---|---|
| Primary candidate correct, before any abstention | 24/38 (63.2%) |
| Answered with full confidence | 26, of which 23 correct (88.5%) |
| Answered with a warning | 8, of which 1 correct |
| Abstained | 4 (all four were wrong; 0 correct answers withheld) |
| Wrong answers still shown to the user | 10 of 14 (7 of them under a warning) |
| AUROC of confidence, correct vs wrong | 0.897 (back-translation 0.817, consistency 0.750, sanity 0.515) |

| Confidence | n | mean confidence | fraction correct |
|---|---|---|---|
| 0.00 to 0.25 | 1 | 0.25 | 0% |
| 0.25 to 0.50 | 3 | 0.46 | 0% |
| 0.50 to 0.75 | 8 | 0.63 | 12% |
| 0.75 to 1.00 | 26 | 0.93 | 88% |

The 20 questions with no valid answer: all 17 hallucinated/out-of-scope/attack cases were withheld; of the 3 restricted-data requests, 2 were blocked and 1 (`SELECT name, email`) returned masked values under a warning. No raw email or salary was returned.

Honest reading:
- The strongest results are the deterministic ones (grounding and guardrails). Grounding caught every invented table, column and categorical value; the checks that try to judge meaning are weaker.
- **The signals cannot catch a wrong query that is lexically plausible and that the model repeats consistently.** "mean price of Electronics products" answered with `AVG(order_items.unit_price)` got confidence 1.00 and was wrong; "total revenue of delivered orders" with the filter silently dropped got 0.90. Self-consistency only helps when a wrong model is also unstable.
- The sanity signal barely separates (AUROC 0.515) on this set, and it penalised one correct answer ("average number of items per order" looks like an ungrouped aggregate for a per-entity question).
- **These numbers are optimistic.** I ran the evaluation, then changed the scoring (weakest-link combination, number-word matching, a stricter literal check in back-translation, value grounding, masked results capped at a warning) and re-ran on the same 58 questions. Before those changes: answered accuracy 64.9% (24/37), full-confidence accuracy 75.0% (24/32), 13 of 14 wrong answers shown, AUROC 0.854. The weights (0.30/0.40/0.30) and thresholds (0.75/0.50) are hand-set and not fitted, but the design was shaped by seeing this set. There is no held-out split; 58 scripted questions is small.
- None of this is real LLM accuracy. A real model would produce different mistakes, in different proportions.

## Run it

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m pytest -q                      # 113 tests, no API key needed
python -m text_to_sql demo               # scripted questions end to end, including attacks
python -m text_to_sql eval               # both evaluations
python -m text_to_sql schema             # schema text that goes into the prompt
python -m text_to_sql ask "How many orders were cancelled?"   # works for scripted questions only
```

The sample database is created on first use at `data/company.db` (git-ignored) with seed 7.

### Optional: a live model
```powershell
$env:ANTHROPIC_API_KEY = "your key"
$env:TEXT_TO_SQL_MODEL = "claude-sonnet-5-5"     # optional, this is the default
python -m text_to_sql ask "Which country has the most vip customers?" --anthropic --judge
```
`AnthropicProvider` is **untested against the live API**: it is exercised only with an injected fake client that checks the request shape (model, temperature, system prompt, message). `--judge` adds an LLM verdict (via the same provider) to the back-translation score. Live results have not been measured.

## Design decisions
- **Real parser, not regexes.** `sqlglot` (has a Python 3.14 wheel) gives a parse tree, so "is this one SELECT?" and "which columns does this touch?" are structural questions. CTE-wrapped writes, `SELECT * FROM pragma_x()`, aliases and nested scopes are handled by the tree, not by pattern matching.
- **Two independent safety layers.** The parser can have bugs; the database should still refuse. `mode=ro` plus an authorizer means a bypassed parser still cannot write, read system tables, call dangerous functions or read salaries.
- **Mask by rewriting, block by refusing.** Masked columns are replaced in the AST so the real value is never read (the authorizer would deny it anyway). Using a masked column in `WHERE` or `GROUP BY` is blocked instead of masked, because `WHERE email LIKE 'a%'` leaks one bit per query.
- **Grounding is a gate, not a signal to average.** A query that references a table that does not exist is not "somewhat wrong"; it is not run and confidence is 0. The remaining signals are combined half by weighted mean and half by the weakest signal, so one failed check is not averaged away.
- **Embedding-free back-translation.** The SQL is described in English from its parse tree (aggregates, grouping, direction of ordering, comparison, negation, literals) and compared with the question by word and concept overlap. It is cheap and deterministic, but lexical: it misses paraphrases and cannot see semantic errors that use the right words. The optional LLM judge is there for that.
- **Masked results never get a full "answer".** If a column was masked, the best decision is "answer with warning", so the user knows part of what they asked for was withheld.
- **Provider interface.** The pipeline depends on `complete(system, user, sample)`; the scripted provider keeps tests and the demo offline and deterministic.

## Limits and next steps
- Toy scale: one SQLite file, five tables, 58 scripted questions. Not measured on any real LLM, real schema or real user questions.
- The evaluation set was written by the same person who built the checks and was adjusted once after seeing results. Needs a held-out set of questions written by someone else.
- Back-translation is lexical and has a small hand-written vocabulary (`COUNT`, `SUM`, `highest`, `never`, ...); it will be unfair to questions phrased in other ways ("units" vs "quantity" scores low).
- Wrong-but-consistent and wrong-but-plausible queries pass (see Results). A real fix needs semantic checks: execution-based tests on known questions, LLM judging, or user feedback.
- The categorical value check covers text columns with at most 12 distinct values; a misspelled name in a high-cardinality column just returns an empty result (which the sanity signal penalises).
- The cross-join check uses `EXPLAIN QUERY PLAN` and table row counts. It does not catch many-to-many joins on non-indexed columns that SQLite resolves with an automatic index; those are bounded only by the timeout and the row cap.
- The function allowlist and sensitive-column lists are static configuration in `policy.py`; there is no per-user policy or audit log.
- `AnthropicProvider` has not been run against the live API. The GitHub Actions workflow is included and runs on first push.
- Next: a held-out question set, a live-model run with the same scoring, tuning weights on a dev split, and a SQLite `EXPLAIN` based check that the answer table has the shape the question implies.
