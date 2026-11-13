# UZEX Data Assistant: Language Handling and Grounded Answers (Prompt 4)

Code: `src/uzex_assistant/lang.py` (language/script detection, Uzbek Latin→Cyrillic for templates), `render.py` (deterministic renderer and grounding check), and the question-level checks in `engine.py` (`_question_checks`, `_finish`). **No extra LLM call is used for phrasing.** The only LLM call per question is the planner.

## Language
- **Detection is deterministic and based on the question**, not the planner's guess:
  - Uzbek Cyrillic: the letters ў қ ғ ҳ, or Uzbek Cyrillic words.
  - Uzbek Latin: Uzbek words or `o'`/`g'` with any apostrophe (`'`, `‘`, `’`, `ʻ`, `` ` ``).
  - Russian: Cyrillic plus Russian words.
  - Otherwise the planner's language, with Russian as the default.
- **Answer script:** Russian, Uzbek Latin, or Uzbek Cyrillic. Uzbek Cyrillic is produced by transliterating the Uzbek Latin *template text* only.
- **Data values are inserted verbatim** in every language: company names (in whatever script they are stored), INN/STIR, codes, deal and contract numbers, product names, and original brand spellings (`АИ-92`, never the normalized `аи92`).

## Answer structure
The answer is plain text (Telegram formatting comes in Prompt 5), in this order:
1. **Direct result**
2. **Concise table or list**
3. **Conditions:**
   - the effective period, or "full coverage 03.01.2022–30.10.2026"
   - the resolved entities with their role
   - the filters, with codes localized and data values verbatim
   - the status policy of every metric, plus a note when the defaults were used
4. **Method:**
   - the formula for each metric; average prices are labelled **weighted** (Σ amount / Σ quantity) or **arithmetic mean**
   - **USD→UZS conversion is stated with the number of converted deals**, counted under the metric's own status policy (and, for truncated rankings, over the shown rows only)
   - quantities in different units or product variants are listed separately and never summed
   - rounding happens only at render time
   - "contract value is not profit"
5. **Sources:** files, source-row count, `query_id`, sample deal IDs, and the note "statuses as of end of 30.10.2026; later changes are unknown".

Partial-year comparisons say that the last period is incomplete and that both periods were aligned to the same span. Truncated rankings and lists say "shown N of M", and a tie at the cut-off is flagged.

## States (each has its own wording in ru and uz)

| Status | Wording |
|---|---|
| verified zero | "Проверено по базе: при этих условиях найдено 0 сделок" |
| `no_matching_rows` | 0 matching rows verified; or "X is not in the catalog, no other company/product substituted"; or "rows exist, but none match the metric's status rule" |
| `missing_field` | names the missing information (profit → прибыль/foyda…), lists what is available, says value is not profit |
| `out_of_period` | requested dates, coverage 03.01.2022–30.10.2026, statuses as of 30.10.2026, no forecasts |
| `ambiguous` | one clarification question plus up to 6 candidates (names with INN/STIR, regions, product variants with unit and deal count, options) |
| `unsupported` | instruction refused / contradictory plan / unsupported question type |
| `execution_error` | "system failure, not missing data", with the reason code; for rate limits, the provider's actual wait hint |

## Semantic checks on the question (deterministic)
- **"объём / hajm / ҳажм / volume"** without money cues (сумма, сум, so'm, USD, доллар, оборот…) and without quantity cues (тонн, литр, кг, miqdor, tonna…) gives `ambiguous` (`ambiguous_volume`) with two options: value in money, or physical quantity.
- **Currency** is never silently converted when the meaning would change. If the question mentions dollars, or the scope is USD-only (a currency=USD filter or export deals) and it doesn't ask for sums, `trade_value_native` is added, so the original USD amount appears **next to** the UZS equivalent (recorded in the evidence notes). In mixed scopes the documented UZS default applies, and the answer states it with the conversion count.
- **Planner defaults** (null → default for language, currency, alignment, status policy, period) are recorded in the evidence (`defaults_applied=`), and each one is stated in the answer.

## Grounding
`grounding_errors(text, result, catalog_strings)` extracts every number from the rendered text. That includes dates, numbers grouped with NBSP or ordinary spaces, and decimal commas. Each one must occur in the typed result (facts, table, evidence, clarification, plan) or in the catalog strings used for display. If a check fails, the minimal facts-only rendering is used (`fallback_used`). Every rendering in the tests and examples passes this check.
