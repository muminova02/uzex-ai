# Accuracy audit (2026-11-05, before the 18:00 Asia/Tashkent submission freeze)

Scope: factual accuracy of the existing engine. No new product features beyond capability gaps the data could
already answer. Every number below comes from a saved result file in `eval/results/`.

## 1. Results at a glance

| Layer | What it measures | Set | Result |
|---|---|---|---|
| Offline, fixed plans, **pre-audit build** `a804df0075a9` | same set, old code | 60 q | 44/60 |
| Offline, fixed plans (MockProvider) | resolution, semantic checks, SQL, arithmetic, rendering — **not** language understanding | dev 40 q | 40/40 (after 3 engine fixes + 1 reference fix) |
| Offline, fixed plans | same | holdout 20 q, **first run** | **18/20** — both misses were a defect in my reference, not the engine |
| Offline, fixed plans | same | holdout 20 q, after reference fix (no engine change) | 20/20 |
| Offline, fixed plans | historical failures | regression 8 | 8/8 |
| **Live** (Groq `openai/gpt-oss-120b`, real planner, shared quota gate) | real uz/ru understanding end to end | 10 holdout q, sequential, once | **7 correct / 3 incorrect / 0 blocked** |

The offline 60/60 must not be read as accuracy on real questions: the plan is written by hand. The live 7/10 is
the only measurement of language understanding here, and 10 questions are far too few for a precise rate
(a 95 % interval for 7/10 is roughly 35–93 %). No 100 % claim is made anywhere.

## 2. Capability audit (schema → QueryPlan)

`deal_facts` columns were checked against every metric expression, dimension and filter of `plan.py`.

Already expressible: counts, sums in UZS and native currency, quantities by unit (never mixed), weighted /
mean / min / max unit price, average and largest deal value, distinct sellers/buyers/variants, cancellation
share, overdue payment/delivery, late delivery; filters on form, contract type, trade type, status, currency,
unit, category/group/industry/TN VED, activities, delivery basis, warehouse/seller/buyer region, year/month,
lot size/count, amounts, prices, terms, payment/delivery dates, all boolean flags; group-by every participant,
broker role, region role, commodity/variant, category, form/type/status, currency, unit, year/quarter/month;
product brand/grade/class/size through product entities; broker roles seller/buyer/any; comparisons of two
periods; deal/contract/company/broker lookups.

Gaps the data could answer but the plan could not express — **implemented**:

| Gap | Added |
|---|---|
| "How many days on average from deal to payment / from payment to delivery?" | metrics `avg_days_to_payment`, `avg_days_to_delivery` (mean over rows that have the dates; denominator = non-null count, not all rows) |
| "How many brokers worked for sellers / for buyers?" | metrics `distinct_seller_brokers`, `distinct_buyer_brokers` |
| Payment/delivery due dates, fully delivered flag | filters `payment_due_date`, `delivery_due_date`, `is_fully_delivered` |
| Weighted/min/max price of a product whose raw names split it into several variants | per-unit totals over all variants (Σamount/Σquantity per unit and currency), previously only for quantities |

### Questions the current agent cannot correctly express or answer

None of these was measured live; "behaviour" is what the code does with them.

| Example question | Why | Current behaviour |
|---|---|---|
| «Какая медианная цена сахара в 2025?» | no median metric | only via the model's `custom_sql` (guarded); if the model plans a mean instead → refused (`plan_mismatch`), never a mean labelled as median |
| «На сколько процентов цена сделки выше стартовой?» (auction premium) | `start_price`/`base_price` not in any metric | `custom_sql` or unsupported |
| «Средний залог (deposit %) по цементу» | `deposit_pct` not exposed | `custom_sql` or unsupported |
| «Сколько сделок заключено в понедельник / после 15:00?» | no weekday/time dimension | `custom_sql` or unsupported |
| «Какие контракты истекают в 2027?» | contract registration/validity dates not exposed | unsupported; 2027 is outside the data |
| «Сделки брокеров из Самаркандской области» | broker home region not exposed | unsupported (or wrongly read as warehouse region: unchecked) |
| «Что упаковано в мешки?», warehouse street address | `packaging`, `warehouse_address` not exposed | unsupported |
| Prices across units (litres + tonnes as one number), sums of quantities across different products | not meaningful | always split by unit; ranking by quantity across products is refused |
| Profit, margin, cost, phones, bank details, employees | not in the data | `missing_field` |
| Forecasts (2027) | outside the data | `out_of_period` |
| Paraphrases the cue checks do not recognise (e.g. «пятерых лидеров», Uzbek Cyrillic number words) | constraint checks are regex-based | a planner error there is **not** caught |
| Three or more periods to compare | comparison takes exactly two | unsupported |

## 3. Evaluation set v2

* `scripts/eval/cases_v2.py`: 30 meanings × (ru, uz) = 60 questions, fresh wording. **dev** = 20 meanings (40 q),
  **holdout** = 10 meanings (20 q, run offline once and sampled for the live round). `REGRESSION` = 8 historical
  failures, scored separately.
* Coverage: trade type, distinct buyers, seller/buyer/any broker roles, broker counts, buyer/seller/warehouse
  regions, brand (АИ-92, ЦЕМ I 42,5 Н), class spelled 6 ways (4 класс / IV класс / 4-КЛАСС / name-only), size
  (Ø12мм/Ø12ММ), payment and delivery timing, fulfilled status, paid-but-undelivered quantity, USD native value,
  quarter and year comparisons, top-N rankings, cancellation share, verified zero, explicit vs ambiguous Tashkent,
  missing fields.
* Expected results: `scripts/eval/reference_v2.py` → `eval/golden_v2.json`, pure Python over the raw CSVs
  (no engine import, no DuckDB, no prepared DB).
* Each case states the expected **metric, period / compare periods, entity kind/role/id, filters, top_n,
  unit/currency, status and clarification behaviour**; `scripts/eval/run_v2.py` checks all of them on the FINAL
  plan + evidence + facts, and the value at render precision. Runtime code never reads these files.

## 4. Per-fix report

**How "before" was measured.** The repository is not a git repo. The pre-audit code was taken from the
Prompt 7 Docker image `uzex-assistant:local` (build `a804df0075a9`; `diff -r` vs `src/` shows only the five
files this audit changed). The pre-L07 code is that tree with the original `apply_choice` loop restored verbatim
from this session's transcript. Runner: `UZEX_EVAL_SRC=<tree>/src scripts/eval/run_v2.py offline …`; tests:
`PYTHONPATH=<tree>/src pytest …`. All runs below are **offline (MockProvider, fixed plans, no LLM call)**
unless marked live.

| Run (offline) | pre-L07 tree | pre-audit (`a804df0075a9`) | current |
|---|---|---|---|
| 60-question set v2 | — | **44/60** | **60/60** |
| 8 regression cases | 0/8 | 1/8 | 8/8 |
| `tests/fixture/test_accuracy_audit.py` (31 tests) | — | 23 fail, 8 pass (the 8 are "must not refuse" guards) | 31 pass |
| `test_region_choice_settles_every_occurrence` (web chat path) | **fails** | passes | passes |

### Fix 0 — Tashkent clarification loop (known failure, reproduced first)
* **Failing question:** «Toshkentda 2024-yilda qancha bitim tuzilgan?» (live 2026-11-05 L07; R01).
* **Expected:** one clarification (city or region); choosing «Toshkent shahri» gives the final count, 21 247 deals.
* **Before:** after the choice the same clarification «Aniqlashtiring: Toshkent shahri yoki Toshkent viloyatimi?»
  came back, reproduced offline (R01 → `clarification_loop`) and through `/api/chat/choose`.
* **Root cause:** the live model put «Toshkentda» in the plan twice; `apply_choice` replaced only the first
  matching entity, so the second still resolved as ambiguous.
* **After:** the choice applies to every equal region mention and every region filter (scalar or list).
  R01 = 21 247 (= reference), web test passes. **Not re-run live** (live H09, a different Tashkent question,
  passed on the new build).
* The reproduction also exposed an **eval-runner gap**: it auto-answered the repeated question and scored the old
  code "correct". A repeated identical clarification now counts as a failure.

### Fix 1 — meanings the plan could not express
* **Failing questions:** «Через сколько дней в среднем оплачивали сделки 2025 года?» (A12), «…от оплаты до
  поставки в 2024 году?» (A13), «Сколько брокеров представляли продавцов в 2024 году?» (A03), «Сколько всего
  брокеров работало на стороне покупателей?» (H02).
* **Expected:** 6.81 days, 10.77 days, broker counts from `golden_v2.json`.
* **Before:** the plan failed validation (`metrics.0 Input should be 'deal_count', …`), so the user got an
  error: 8/60 questions.
* **Root cause:** no metric for date differences or distinct brokers, though the columns exist.
* **After:** `avg_days_to_payment`, `avg_days_to_delivery` (denominator = rows that have the dates),
  `distinct_seller_brokers`, `distinct_buyer_brokers`; filters `payment_due_date`, `delivery_due_date`,
  `is_fully_delivered`. All 8 correct offline; H02 also **correct live** (the model chose the new metric).

### Fix 2 — weighted price split into raw-name variants
* **Failing questions:** «Какая средневзвешенная цена бензина АИ-92 в 2025 году?» (A09), «Средневзвешенная цена
  карбамида за тонну в 2024 году?» (H04).
* **Expected:** one price per unit over all variants (АИ-92: 14 028 458,23 сум/т and 10 697,40 сум/л; urea
  4 762 397,37 сум/т).
* **Before:** only per-variant prices («Мочевина (карбамид) Марка Б … 4 776 808,77», «Карбамид Марка Б …
  4 754 867,22»), and no answer to the question asked.
* **Root cause:** raw product names split one product into variants. Per-unit totals existed only for quantities.
* **After:** per-unit totals (Σamount/Σquantity per unit and currency, own recorded query) for every
  unit-grouped metric. Correct offline; H04 **correct live**.

### Fix 3 — internal label shown to the user
* **Failing questions:** «Сколько тонн арматуры диаметром 12 мм купили в 2025 году?» (A11), «Сколько
  портландцемента оплачено, но ещё не поставлено?» (A15).
* **Expected:** per-variant rows plus one labelled total line.
* **Before:** «… 17 968 т; quantity__unit_total: 21 095 т» inside row 1 (the number was right, the text wrong).
* **Root cause:** total facts shared a row index with the first variant row and were rendered in it.
* **After:** totals are excluded from rows and rendered once. The grader now flags any `name__suffix` label in an answer.

### Fix 4 — plan silently drops or substitutes a constraint (semantic validation)
* **Failing questions (R02–R05 + 12 test cases):** «Сколько сделок было в 2023 году?» planned without a period;
  «Топ-5 продавцов…» planned with the default 10; «…у покупателей из Самаркандской области» planned as
  `seller_region`; «медианная цена сахара» planned as `mean_unit_price`; «Сколько покупателей…» as `deal_count`;
  «АИ-95» without a brand; «средневзвешенная» as a simple mean.
* **Expected:** never answer a different question; ask the user to rephrase (no SQL run).
* **Before:** answered: an all-period count, a top 10, seller-side numbers, a mean labelled as asked, and so on (R02–R05 all
  `success` with the wrong meaning).
* **Root cause:** nothing compared the plan with the question's explicit cues.
* **After:** `_lost_constraints` checks years, top-N (digits and uz/ru number words), buyer/seller side for
  participants, brokers, regions and rankings, weighted/median/average-price, distinct-count questions, and АИ-xx brands
  → `plan_mismatch`. Guard tests: 6 faithful plans are **not** refused; 0 false refusals on the 60-question set.

### Fix 5 — contradictory filters returned a "verified zero"
* **Failing plans:** status=paid AND status=fulfilled; lot_count>10 AND <5; currency = and ≠ USD; deal_year 2023
  with period 2025.
* **Expected:** "conditions contradict each other", never "0 deals (verified)".
* **Before:** `success`, verified zero (4 tests fail on the old tree).
* **Root cause:** only boolean-flag contradictions were checked.
* **After:** equal-field conflicts, = vs ≠, empty numeric ranges and year-vs-period conflicts → `contradictory_plan`.

### Fix 6 — unclear region role answered silently
* **Failing question:** «Сколько сделок у покупателей в Андижане в 2025 году?» (R06) with the region planned on the warehouse.
* **Expected:** a focused clarification (warehouse or buyers' region). Choosing «регион покупателя» gives
  7 895 (= reference A06) without a second LLM call.
* **Before:** answered 4 673, the warehouse count: **a wrong number shown as an answer**.
* **Root cause:** the warehouse default was applied although the question named the buyer side.
* **After:** `ambiguous_region_role` clarification applied to every region mention, chained to the final answer;
  not asked when the question says «склад/ombor» or has no side cue.

### Fix 7 — grounding accepted any number in the payload
* **Failing case:** an answer «Количество сделок: 7777.» where 7777 occurs only in LLM token usage (or 4242 only
  in a request id).
* **Expected:** flagged as ungrounded (the renderer falls back to raw facts).
* **Before:** accepted (test fails on the old tree).
* **Root cause:** `payload_numbers` dumped the whole result, including the plan, LLM metadata, timings and SQL.
* **After:** only facts, table, this answer's evidence, clarification/reason, row counts, top_n and inserted
  catalog strings count. All 457 tests still pass, so no legitimate answer lost grounding.

### Fix 8 — role word planned as a region (found **live**, H03)
* **Failing question:** «Назови три региона покупателей с наибольшей суммой покупок в 2025 году».
* **Expected:** top 3 buyer regions by value (Toshkent shahri, Toshkent viloyati, Farg‘ona viloyati with reference values).
* **Before (live):** «"покупателей" нет в справочнике регионов»: the model emitted the role word as a region entity.
* **Root cause:** generic role words were ignored only for participant/broker entities.
* **After:** ignored for region entities too. R07 offline gives the exact reference ranking. **Not re-run live.**

### Fix 9 — missing field refused as "not a data question" (found **live**, H10)
* **Failing question:** «Какие телефоны и банковские реквизиты у продавцов карбамида?».
* **Expected:** «таких данных нет в наборе (телефоны, банковские реквизиты)».
* **Before (live):** "I answer questions about deal data…" (`not_a_data_question`).
* **Root cause:** the model chose a refusal type, and the engine trusted it.
* **After:** deterministic missing-field cues (phone, bank details, contacts, profit, margin, cost, employees)
  reclassify a non-injection refusal. Never applied when override or destructive wording is present (test). R08
  offline correct. **Not re-run live.**

### Fix 10 — repeated condition line (found **live**, H09)
* **Failing question:** «2025-yilda Toshkentda nechta bitim bo'lgan?» after choosing «Toshkent shahri».
* **Expected:** the condition «ombor hududi: Toshkent shahri» shown once.
* **Before (live):** the answer (24 478) was correct, but the condition was printed twice.
* **Root cause:** the model repeated the region (the L07 pattern), and each resolved mention produced a line.
* **After:** identical condition lines are shown once. Test `test_repeated_region_mention_is_one_condition_line`;
  60/60 and 8/8 offline unchanged. Rendering only; no value changes.

### Reference and grader defects (not engine changes)
* **A10 / H05:** my reference read wheat class and cement brand only from the contract fields; 446 and 808 deals
  carry them only in the product name. Raw rows were checked: the engine was right, and the reference was fixed. The
  first holdout run (18/20) is kept as recorded.
* **Grader:** clarification loops and leaked internal labels were not detected at first (above); both are now checked.
* **Method text:** "average deal value" was described as "mean of unit prices"; corrected.

## 5. Live round (one bounded run)

* Quota checked first: ledger 3 172 tokens used in 24 h, one 157-token probe OK, need for 3 judges × 10 q ×
  1.5 safety = 107 775 tokens reserved. After the round: 35 496 used, 164 504 left, judging reserve still fits.
* Frozen for the round: build `dev-16fe3d1d8ade`, prompt `p4-2026-11-05.1-accuracy`, in-process engine with the
  production provider and the shared gate (same code as the restarted web service). 10 holdout questions
  (5 uz, 5 ru), sequential, never retried. File: `eval/results/v2_live_all_2026-11-05T111217+0000.json`.

| Item | Lang | Outcome | Latency | Note |
|---|---|---|---|---|
| H01 overdue payments 2026 | uz | incorrect (strict) | 1.7 s | value 0 = reference 0, but plan used deal_count + `is_payment_overdue` filter instead of `overdue_payment_count`: equivalent, yet a verified zero is weak evidence, so not counted |
| H02 distinct buyer brokers | ru | correct | 1.2 s | new metric chosen by the model |
| H03 top-3 buyer regions | ru | **incorrect** | 58.6 s | fix 9 (not re-run live) |
| H04 urea weighted price 2024 | uz | correct | 1.6 s | |
| H05 cement ЦЕМ I 42,5 Н 2025 | ru | correct | 58.2 s | |
| H06 Tashkent *region* warehouses | uz | correct | 1.8 s | no unnecessary clarification |
| H07 polyethylene avg deal value | ru | correct | 58.5 s | |
| H08 BK085 as seller broker 2024 vs 2025 | uz | correct | 1.6 s | |
| H09 "Toshkentda" → clarification → city | uz | correct | 58.4 s | the model again put Tashkent twice in the plan (the L07 pattern); one choice → 24 478 = reference, no 2nd LLM call. Choice applied in-process (`apply_choice` + `run_plan`, the same calls `/api/chat/choose` makes), not over HTTP |
| H10 phones/bank details | ru | **incorrect** | 1.1 s | fix 10 (not re-run live) |

* Supported-question accuracy (H01–H09): 7/9; incorrect factual answers: **0** (no wrong number was shown;
  H03 was a wrong "not found", H01 a correct value through an unexpected plan); unnecessary refusals: 1 (H03);
  unnecessary clarifications: 0; quota blocks: 0; all 10 used real LLM calls (~3 200 tokens each).
* Latency: 6 answers 1.1–1.8 s; 4 answers ≈ 58 s = shared-gate TPM waits (8 000 tokens/min ≈ 2.5 questions/min),
  all inside the 70 s deadline.
* **Tashkent live:** H09 is the first live completion of the clarification flow (question → clarification → choice →
  correct final answer) on a doubled region mention. The original L07 question was **not** re-run live, and
  the HTTP choice endpoint was exercised only in the mocked web test.
* Fixes 8 and 9 (and the cosmetic fix 10) were made **after** this round on build `dev-efa44a108a7a`; they are verified only by mocked
  tests. The live score stays 7/10 and is not re-claimed.

## 6. Remaining failures and risks

* The constraint checks are cue-based (regex on uz/ru wording); they can miss paraphrases and can refuse a
  correct plan when a cue is misleading. Measured false refusals on the 60-question set with faithful plans: 0.
  Uzbek Cyrillic is covered only partially.
* Equivalent plans (H01) are graded strictly; the grader does not prove semantic equivalence.
* Live sample is 10 questions; dev wording was written by the same author as the fixes.
* Quota: after the round the ledger shows 164 504 tokens left in the rolling 24 h window; 3 judges × 10 questions
  × 1.5 safety at the learned ≈3 150 tokens/question needs ≈141 700, a margin of only ≈23 000 tokens (≈7 questions).
  More judges, more questions, or other use of the same key does not fit. The TPM limit (8 000/min ≈ 2.5
  questions/min) makes every 3rd question in a burst wait ≈58 s, and a 4th concurrent one can exceed 70 s. The
  ledger sees only this machine's calls; the provider balance is not visible through the API.
* Web and bot run build `dev-2b4e053eaa42`. The live round ran on `dev-16fe3d1d8ade`; differences = fixes 8–10.
* Docker image / `BUILD_INFO.json` from Prompt 7 predates these changes: rebuild before deployment
  (`docs/runbook.md` §2). `FREEZE.json` has not been written.
* Freeze: after 2026-11-05 18:00 Asia/Tashkent no further code, prompt or data change without organizer
  authorization.

## 7. Files

`src/uzex_assistant/{plan,compiler,engine,render,planner}.py` (prompt version `p4-2026-11-05.1-accuracy`),
`tests/fixture/test_accuracy_audit.py` (new), `tests/fixture/test_engine_mock.py` (one assertion adapted),
`scripts/eval/{cases_v2,reference_v2,run_v2}.py`, `eval/golden_v2.json`, `eval/results/v2_*.json`.
Full suite: 458 passed, 1 skipped. Result files: `eval/results/v2_offline_*_{before_l07,before_audit,current}_*.json`.

## 8. Addendum 17:04–17:10 — switch to Claude Opus 5.5 (team decision, before the freeze)

* First live run: 0/10, every request rejected with HTTP 400 before inference (24 union-typed schema parameters).
  Fixed with `anthropic_schema()` (nullable → optional, 3 unions left) + test.
* Same 10 holdout questions, sequential, once: **9/10 strict** (Groq on the same questions: 7/10). H03 and H10,
  which Groq failed, are correct. H04 shows the correct value through `weighted_avg_price_uzs` instead of
  `weighted_avg_price`; the grader counts it as incorrect. Latency 2.2–4.3 s, plus a first call of 33.4 s.
* HTTP acceptance on the frozen release `007eca8cac4f`: 5/5 in 2.0–2.9 s, Tashkent clarification included.
* These holdout questions had already been seen once (by Groq), so this is a provider comparison, not a fresh
  sample. Offline results are unchanged (60/60, 8/8; 460 tests pass).
