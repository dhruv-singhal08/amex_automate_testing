# Synthetic User Platform

A reproducible harness for running constrained synthetic users against products and validating where simulated users get stuck.

## Setup

Requirements: Python 3.11+ and `uv`.

```powershell
uv sync
Copy-Item .env.example .env
uv run python -m synthetic_user.cli init-db
```

Set the provider credentials in `.env`. The project accepts the documented `LLM_*` names and the shorter `GEMINI_API_KEY` / `GEMINI_MODEL` names.

## Commands

```powershell
uv run python -m synthetic_user.cli init-db
uv run python -m synthetic_user.cli list-personas
uv run python -m synthetic_user.cli generate --persona junior_analyst --product insight_profile --n 6 --out runs/questions.json
uv run python -m synthetic_user.cli run --persona junior_analyst --product insight_profile --adapter insight --questions runs/questions.json --repeats 2 --out runs/latest.json
uv run python -m synthetic_user.cli validate --run-id <run-id>
uv run python -m synthetic_user.cli metrics --run-id <run-id>
uv run python -m synthetic_user.cli export --run-id <run-id> --format csv
uv run python -m synthetic_user.cli export --run-id <run-id> --format json
```

`mock` is available as a local adapter for tests. Generated questions and run records are JSON files; exported conversation summaries can be CSV or JSON.

## UI

Two independent Streamlit apps — the product under test and the harness that tests it are deliberately separate applications, not tabs of one app, the same way they'd be separate services in reality:

```powershell
uv run streamlit run insight_app.py --server.port 8501   # the Insight product itself
uv run streamlit run ui/Home.py --server.port 8502        # the synthetic user test harness
```

- **`insight_app.py`** — a chat window onto the mock Insight billing product (`mock_product.engine.InsightEngine`). Every message is a real call to the configured LLM to generate SQL, then a real query against `mock_product/data/billing.duckdb`. Follow-up questions ("what about last month?", "just Engineering") are resolved against the conversation so far. If a turn fails (bad SQL, refused request, provider rate limit, etc.) the underlying error is shown alongside the reply, not hidden behind a generic message.
- **`ui/Home.py`** — the Synthetic User Test Harness: the same setup → generate → run → validate → metrics → export flow as the CLI. Pick a persona and product, generate questions, run the conversation grid, inspect every conversation turn by turn (including the persona's own reasoning and doubt), validate against an optional capability spec, and export CSV/JSON.

The Test Harness offers three adapters:

- **mock** — instant, no LLM calls, for exercising the harness itself.
- **insight** — calls `InsightEngine` directly, in-process. Fast, no browser required.
- **insight (watch in browser)** — `InsightBrowserAdapter` also types each generated question into a real, visible browser tab running `insight_app.py`, so you can watch the conversation happen live in the product's own UI, the way the old demo's Playwright driver worked. The browser is for visibility only — it is never scraped for the result. The recorded response still comes from a direct backend call (the same one `insight` uses), so the harness's data never depends on parsing Streamlit's DOM. Requires `insight_app.py` to already be running at the given URL, and is slower than `insight` since each turn waits on a real page render.

Both processes use the same LLM provider quota, so running large harness grids and the standalone product chat at the same time can trip a free-tier rate limit on either one — that shows up as an explicit error in the UI, not a silent failure.

The UI is a thin layer over `synthetic_user` — it calls `generate_questions`, `run_grid`, `validate`, and `compute_metrics` directly, so behavior always matches the CLI.

## Mock product

`mock_product.engine.InsightEngine` is a small multi-turn NL-to-SQL engine: it turns a question (plus the conversation so far, for follow-ups) into a single DuckDB `SELECT` against `mock_product/data/billing.duckdb`, validates it's read-only, executes it, and returns a short natural-language summary alongside the raw rows. `InsightAdapter` wraps it to the `Adapter` protocol, holding one transcript per session so multi-turn follow-ups resolve correctly.

## Reference-guided evaluation

The base flow tells you whether a simulated user *got stuck*. It does not tell you whether the product was *right* — a confident wrong answer that satisfies a novice persona scores as a resolution. Reference-guided evaluation adds that missing axis.

Each question is generated together with a **reference**: a description, in business language, of what a correct answer needs — which tables and columns, which filters, what aggregation and grain, and which of those are genuinely mandatory. A judge then compares the product's actual query against that reference.

```powershell
uv run python -m synthetic_user.cli generate --persona junior_analyst --product insight_profile --n 10 --reference --fewshot-mode mixed
uv run python -m synthetic_user.cli run --persona junior_analyst --product insight_profile --adapter insight --questions runs/questions.json --reference runs/references.json
uv run python -m synthetic_user.cli evaluate --run-id <run-id>
uv run python -m synthetic_user.cli metrics --run-id <run-id>
```

It is opt-in throughout. Without `--reference` the original flow is byte-for-byte unchanged, and the reference metrics are *skipped with a stated reason* rather than reported as zeros — a zero would read as "the product got it wrong".

### What it can and cannot verify

**It can** check that the product went to the right tables and columns, applied the filters the question implied, aggregated at the right grain over the right period, and did the right *kind* of thing — answering, refusing, or asking for clarification.

**It cannot verify that a number is correct.** Evaluation never executes SQL, against the mock DuckDB or anything else, because the real product environment does not permit execution. Everything is static analysis: SQL is parsed with `sqlglot`, never run. A query can therefore be judged `correct` in structure and still return a wrong figure because of a subtlety no parser can see. Treat `correct_rate` as "asked the data the right question", not "produced the right answer".

Two further limits worth stating plainly:

- **References are generated, so some are wrong.** The judge can mark one `reference_suspect`, and that verdict is excluded from the correctness denominator rather than charged to the product. `reference_invalid_rate` and `reference_suspect_rate` are harness-health metrics: if they rise, the references need attention, not the product.
- **The judge is unfalsifiable until calibrated.** `export-calibration` writes a stratified review sheet; `import-calibration` stores your labels and reports agreement, Cohen's kappa, a confusion matrix, and every disagreement. Until labels exist **for the current judge prompt version**, metrics print: `Judge not calibrated — correctness metrics are provisional.` Labels do not carry across prompt versions, because changing the prompt changes what was measured.

```powershell
uv run python -m synthetic_user.cli export-calibration --run-id <run-id> --n 60
uv run python -m synthetic_user.cli import-calibration --file runs/calibration_<run-id>.csv
```

### Leak rules

The schema, the few-shot bank and the glossary are **privileged**. They reach exactly three places: the question generator, the reference validator, and the judge. They never reach the persona agent, the adapter, or any message sent to the product.

Of the reference itself, only `expectation` — plain business language — may go near a persona, where it replaces the generated intent. Tables, columns, filters, aggregation, grain and the rest never do. The asymmetry is the same one the capability spec already relies on: a user who knew which columns the answer needed would not ask the questions worth testing.

This is enforced by a test, not by convention. `tests/test_persona_agent.py` runs a full conversation and asserts that no schema identifier, no few-shot SQL and no reference field other than `expectation` appears in any persona prompt or any adapter message. It is mutation-tested — deliberately leaking artifacts into the transcript makes it fail — so it cannot quietly stop checking.

The judge does **not** see the few-shots. It must decide from the reference and the query, not pattern-match against examples it has already been shown.

## Tracing (optional)

Every LLM call passes through one function — `LLMClient.complete()` — so the platform can emit a full trace of a run to [Langfuse](https://langfuse.com) for local inspection.

It is **off by default and entirely optional**: nothing is installed unless you ask for it, and the code is inert unless both the package and the keys are present.

```powershell
uv sync --extra tracing
```

Then set in `.env`:

```text
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

What you get, grouped the way the harness actually nests:

```
session = run
└── trace = one conversation, named by the question
    ├── span  "turn 0"
    │   ├── generation  llm.product   (the NL-to-SQL call)
    │   └── generation  llm.persona   (the turn decision, +1 more if the JSON retry fires)
    ├── span  "turn 1" ...
    └── generation  llm.persona       (closing summary)
trace = "generate N questions"        (before the run, so it has no session)
trace = "validate"                    (per conversation, after the run)
```

Each generation records the full prompt, the response, the model, token usage, and — importantly — provider failures such as 429s, which are otherwise invisible. The normally-silent JSON-parse retries in `generator.py` and `persona_agent.py` show up as extra generations under the same span.

**Tracing never affects a run.** Failures are swallowed and logged at debug level; `tests/test_tracing.py` asserts that a tracer which raises on every call leaves `complete()`'s behavior unchanged.

**To remove it completely:** delete `synthetic_user/core/tracing.py` and `tests/test_tracing.py`, drop the `tracing` extra from `pyproject.toml`, and delete the call sites — they are all marked with a `# tracing` comment:

```powershell
Select-String -Path synthetic_user\*\*.py -Pattern '# tracing'
```

## Design decisions

1. The persona agent makes all in-character decisions. The moderator is plain code that enforces turn, loop, adapter-error, and budget limits; the persona cannot override those limits.

2. The product profile is what users are told. The capability spec is internal and never reaches a persona prompt. That asymmetry is deliberate: a user who knew the technical limits would not ask the questions that matter most.

3. Seeds make the harness reproducible. Model providers do not guarantee deterministic output, which is why every question runs multiple times.

## Metrics

Product metrics and harness-health metrics are presented separately. Hesitation and cleanup are harness-health signals: low hesitation can mean the personas are too weak and the run is untrustworthy, not that the product is good.
