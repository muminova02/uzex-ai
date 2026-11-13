# UZEX Data Assistant: Query Engine (Prompt 3)

Code: `src/uzex_assistant/` modules `plan.py`, `planner.py`, `llm.py`, `resolver.py`, `normalize.py`, `products.py`, `compiler.py`, `sqlguard.py`, `executor.py`, `engine.py`, `cli.py`.

## Pipeline

question → **planner** (LLM, structured JSON) → typed `QueryPlan` (pydantic, strict) → **entity resolution** against the real catalogs → **compiler** (constant column names, bound `?` params) → **SQL guard** → **bounded read-only execution** (worker process) → `AnswerResult` with **evidence**.

The LLM only produces the plan. It never chooses table or column names: those come from closed enums. It never supplies IDs or numbers that end up in answers. Its self-reported `llm_confidence` is recorded and ignored.

## Query plan

The plan has the following fields:
- `language` and `intent` (count / aggregate / ranking / lookup / list / comparison / custom / unsupported)
- `entities`: each with kind, text, catalog_hint, role, and brand/grade/class/size
- `period` / `compare_periods` / `align_equivalent_periods`
- `metrics` (21), `filters` (34 fields), `group_by` (27 dimensions)
- `order`, `top_n`, `currency`, `status_policy`
- `missing_fields`, `unsupported_reason`, `clarification`
- `custom_sql` / `custom_params`

The LLM-facing JSON schema (`plan.llm_json_schema`) is strict: every property is required, optional values are nullable, and `additionalProperties` is false.

Metric defaults follow `docs/solution_spec.md` §5. For example, a count uses `registry` and a value uses `not_cancelled`. Every metric is computed with its own `FILTER (WHERE status_code IN …)`, so a question can mix policies, and each policy is recorded in the evidence.

When a metric needs a unit, currency or product variant, the engine groups by it automatically. Quantities and prices are therefore never summed across units, currencies or product variants. Petrol in litres and petrol in tonnes come back as separate rows.

## Entity resolution (`resolver.py`)

| Entity | Resolves when | Ambiguous when | Never |
|---|---|---|---|
| Company | exact INN; normalized name equal; one company contains all mention tokens | several companies match | accepted on similarity alone: a similar name (difflib ≥ 0.82) becomes only a "did you mean" candidate |
| Broker | code `BKnnn`, INN, normalized name | several match | |
| Region | Uzbek Latin / Cyrillic, Russian, English stems | "Ташкент"/"Toshkent" without "city"/"region" (city vs region) | |
| Product | one commodity (43, from TN VED + keyword rules, `products.py`) and at least one variant with the requested brand/grade/class/size/descriptor words | several commodities ("масло"), the text and the LLM hint disagree, or no variant has those attributes | variants merged |
| Deal / contract | the ID exists | | |

Normalization covers:
- NFKC
- case folding
- ё (transliterated to `yo` in skeletons)
- six apostrophe variants and quote/dash variants
- Uzbek/Russian Cyrillic→Latin skeletons
- removal of legal-form words (MChJ/МЧЖ/XK/AJ…)
- decimal commas inside numbers

Original names and codes are always kept for display. An unknown company or product gives `no_matching_rows` with reason `*_not_found`; it is never replaced with a different entity.

## SQL controls (`sqlguard.py` + `executor.py`)

Three independent layers:
1. **Guard.**
   - Token check: forbidden keywords (ATTACH, INSTALL, LOAD, PRAGMA, SET, COPY, DDL/DML, DESCRIBE, EXPLAIN, RECURSIVE, LATERAL, TABLESAMPLE…) and name prefixes (`read_*`, `duckdb_*`, `pg_*`, `information_schema`, `glob`…).
   - Exactly one statement.
   - The root must be a SELECT or a set operation.
   - Allowlists for **AST node types**, **functions**, **physical tables** and **columns** (columns read from the DB at startup).
   - Lexical CTE scoping: a CTE is visible to later CTEs and the main query, not to earlier ones. A CTE defined inside a subquery is not visible outside it. A CTE may not shadow a table.
   - Nested subqueries and UNION branches are checked recursively.
   - Only positional `?` placeholders, and their count must equal the number of parameters.
2. **Engine hardening.**
   - Read-only connection.
   - `enable_external_access=false`, so file and network reads fail even if layer 1 missed them.
   - Extension autoload and autoinstall off.
   - `lock_configuration=true`.
   - `memory_limit` and `threads` set.
3. **Process isolation.**
   - A pool of N worker processes.
   - A timeout **kills** the worker (SIGTERM, then SIGKILL) and starts a new one. Nothing is left running in an abandoned thread.
   - At most N concurrent queries, then `busy` after the queue timeout.
   - Rows are capped (`max_rows` + 1 to detect truncation), and so are cell lengths.

Compiled plans pass through the same guard; there is a test for this.

`custom_sql` (the free-form escape hatch) allows **at most one** repair, and only for parse errors, unknown columns or wrong parameter counts. The repaired plan must be identical except for `custom_sql`; otherwise the result is `repair_changed_meaning`. Security rejections are never sent back for repair.

**Budget.** The total request budget is 70 s (configurable). The LLM gets `min(llm_timeout, budget − 15 s)`, and each SQL query gets `min(sql_timeout, remaining budget)`.

## AnswerResult

`status` is one of `success | no_matching_rows | missing_field | out_of_period | ambiguous | unsupported | execution_error`, together with `reason_code`.
- A verified `COUNT = 0` is `success` with `verified_zero=true`.
- A SUM/ratio over 0 rows, or a NULL under the status policy, is `no_matching_rows`. A NULL is never turned into 0.
- A failure of the LLM, the guard or the database is `execution_error`, never "no data".

Each `Fact` has `name`, `value` (Decimal values as strings), `unit`, `group`, `query_index`, `row_index`, `columns` (the exact result columns, e.g. `weighted_avg_price__num`/`__den`), `rows_used` and `method`. Ratios such as weighted prices, means, shares and % changes are computed in Python with Decimal ROUND_HALF_UP from exact SQL sums.

`Evidence` contains:
- `query_id`
- every query with its SQL, params, tables, row count, truncation flag, elapsed time and error
- source files (SHA-256 prefix and row counts)
- source-row count
- requested and effective periods (clipped to coverage 2022-01-03..2026-10-30; partial-year comparisons are aligned to the same span from each requested start)
- filters with the SQL they produced
- the status codes of each policy
- currency/FX notes (the number of rows converted from USD)
- normalization trail per entity
- up to 5 sample deal IDs
- truncation details (shown, total_groups, top_n, tie_at_cutoff)
- `as_of` and the prompt version

## LLM provider (`llm.py`)

- **`anthropic`.** Uses the official SDK. **Never run live**, because there is no paid key. Request settings:
  - Messages API with structured outputs: `output_config.format` = json_schema, plus `effort` (default `low` for latency).
  - System prompt with `cache_control`.
  - Server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`), which can be switched off with `UZEX_LLM_FALLBACKS=false`.
  - An explicit timeout, `max_retries` ≤ 1, and explicit `api_key`/`base_url` read only from `UZEX_LLM_*`. Ambient `ANTHROPIC_*` variables are ignored on purpose.

  Responses are checked in this order: stop reason `refusal` → `LLMError("refusal")`, `max_tokens` → `truncated`, invalid JSON → `bad_output`, plan schema violation → `bad_output`.
- **`groq`.** Uses the official `groq` SDK 1.7.0.
  - Request: Chat Completions with `response_format` json_schema `strict: true` (the transport schema without `description` keys), `reasoning_effort` (low/medium/high only; other values are a configuration error), `include_reasoning=false`, `temperature=0`. No Anthropic options are sent.
  - Model: the model ID must be set explicitly, and a Claude default is rejected. `verify_model` checks for an exact ID in `models.list` and never substitutes another model.
  - 429 and 5xx: retried with the wait taken from `Retry-After` (else from `x-ratelimit-reset-*`), at most `UZEX_LLM_MAX_ATTEMPTS` (3) attempts, and never past the deadline.
  - `json_validate_failed`: one re-generation of the same request.
  - Credentials and endpoint: `api_key`/`base_url` are passed explicitly, so ambient `GROQ_*` variables are ignored.

  Live results: `docs/live_llm_results.md`.
- **`mock`.** A scripted provider for tests and for the offline CLI (`--mock-plan`). It is always labelled as a mock.
- **`none`** (the default). `ask` returns `execution_error / llm_not_configured`.

## CLI

```bash
.venv/bin/python -m uzex_assistant.cli examples                     # all typed example plans (no LLM)
.venv/bin/python -m uzex_assistant.cli plan examples/plans/07_ranking_top_sellers_uz.json --json
.venv/bin/python -m uzex_assistant.cli ask "Сколько сделок в 2025?"  # needs UZEX_LLM_* in .env
.venv/bin/python -m uzex_assistant.cli ask "..." --mock-plan examples/plans/01_count_2025.json
.venv/bin/python -m uzex_assistant.cli sql "select count(*) from deal_facts where deal_year = ?" --params '[2025]'
.venv/bin/python -m uzex_assistant.cli resolve participant "Pudexiz Plast"
```

## Tests

- `tests/fixture/test_sqlguard.py`: 46 rejected and 9 accepted queries, plus parameter checks.
- `tests/fixture/test_engine_mock.py`: mocked LLM; extended synthetic fixture in `engine_fixture.py`. Covers:
  - counts, verified zero, NULL vs 0
  - USD conversion, unit/variant separation, exact ratios
  - ranking truncation, comparisons and alignment, list, lookup, cross-table filters
  - all no-data states, 7 ambiguity cases, script normalization
  - LLM errors, custom SQL with exactly one meaning-preserving repair
  - injection text staying a bound value
  - executor kill/recovery, read-only, file access blocked, configuration locked, row cap, concurrency `busy`
  - adapter request shape and error mapping (fake SDK client)
- `tests/smoke/test_engine_real.py`: real DB. Checks the control values and the ranking, cross-table and comparison results against hand-written SQL over the raw tables.
- `tests/fixture/test_groq_adapter.py`: Groq adapter with a fake SDK client. Covers:
  - request shape (no Anthropic options) and the transport schema keeping every constraint
  - pydantic validation after parsing, and null defaults
  - 429 Retry-After and reset hints, bounded attempts, fail-fast when the wait exceeds the deadline
  - 5xx retry, one schema re-generation, error mapping
  - model availability without substitution, configuration errors, ambient variables ignored, key never in errors
- `tests/live/test_llm_live.py`: **real API** (groq or anthropic, from `.env`), gated by `UZEX_RUN_LIVE_LLM=1`. **Ran on Groq: 6/6 passed.** `scripts/live_eval.py` runs the 19-question bilingual live set; results are in `docs/live_llm_results.md`.
