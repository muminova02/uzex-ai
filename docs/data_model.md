# UZEX Data Assistant: Data Model and Import Pipeline (Prompt 2)

Code: `src/uzex_assistant/importer.py` (pipeline), `schema.py` (headers, statuses, policies, codes), `regions.py`, `units.py`, `numbers.py`, `config.py`.
Metric definitions and status policies: `docs/solution_spec.md` §4–5. Data facts: `docs/data_audit.md`.

## 1. Running it

```bash
.venv/bin/python -m uzex_assistant.importer ensure   # build only if missing or stale; otherwise reuse
.venv/bin/python -m uzex_assistant.importer build    # force a rebuild
.venv/bin/python -m uzex_assistant.importer status   # exit 0 = up to date, 3 = stale/missing
```

Settings (environment variables or a git-ignored `.env`): `UZEX_DATA_DIR` (default `./uzex_spot_data`), `UZEX_DB_PATH` (default `./data/uzex.duckdb`), `UZEX_AS_OF` (default `2026-10-30`, the authoritative snapshot date). Exit code 2 together with a problem list on stderr means the import failed.

## 2. Pipeline

1. **Discover and fingerprint.** The pipeline expects `deals_*.csv` plus the four reference files and fails if any is missing. It records a SHA-256 hash and the size of every file.
2. **Reuse check.** The DB's `import_meta.identity_json` holds `{schema_version, as_of, files{sha256,size}}`. If it equals the current identity, the DB is reused (about 0.2 s) and nothing is rebuilt at application startup. Any change to the data, the schema version or the as-of date triggers a rebuild.
3. **Header and row count.** Python's `csv` module (strict mode) checks every header byte for byte against `schema.py` and counts the data rows independently. Rows with the wrong number of fields are rejected.
4. **Raw load.** DuckDB `read_csv` runs with an explicit all-`VARCHAR` column list and `auto_detect=false, strict_mode=true, quote='"', escape='"'`. Each row gets `source_file` and `source_row` (1 = first data row). DuckDB's row count must equal the Python count.
5. **Validation, with no coercion.** Every raw value is matched against a strict pattern before any cast. Each kind of violation is reported with its count and up to 5 samples `(file, row, value)`:
   - whole numbers: `^(\d{1,3}([   ]\d{3})+|\d+)$`
   - money and FX: the same pattern plus `(\.\d{1,2})?`, so at most 2 decimals
   - dates: `^\d{2}\.\d{2}\.\d{4}$` **and** a valid calendar date
   - time: `HH:MM:SS`
   - identifiers: digits only, stored as text
   - enumerations (status, form, type, trade type, currency, unit, region, role, VAT flag): must come from the known lists
   - required columns: non-empty
6. **Typed tables** are filled with plain `CAST`. After validation every cast succeeds, so a missed case raises an error instead of producing NULL. Primary keys are enforced.
7. **Integrity checks**, stored in the `import_checks` table (37 on the real data). They must all pass, or the import fails:
   - typed row count = raw row count
   - 0 orphans for contracts, sellers, buyers, both brokers and the FX rate
   - deal fields repeated in the contract are equal (lot size, unit, seller, contract type, form, trade type)
   - lot count, lot size, amount and rate are > 0
   - delivered ≤ purchased
   - a delivery date only with a payment date
   - status agrees with payment date
   - `deal_facts` row count = deals row count, with distinct `deal_id`
   - every USD deal has a rate
8. **Atomic write.** The DB is built in `<db>.building`, checkpointed, then moved into place with `os.replace`. If anything fails, the temporary file is deleted and the existing DB is left untouched (tested).

The raw CSVs are only opened for reading. Tests check that the SHA-256 hashes are unchanged after an import. Building the real dataset takes about 2.3 s on the development Mac (M-series, 10 cores) and produces a 74 MB DB.

## 3. Tables and views

| Object | Rows (real data) | Content |
|---|---|---|
| `deals` | 500 000 | typed deal rows + `source_file`, `source_row` |
| `contracts` | 6 616 | typed contract cards, `currency` (UZS/USD), `warehouse_region_key` |
| `participants` | 14 392 | `inn`, `name`, `region`, `region_key`, `role_code`, `vat_payer` |
| `brokers` | 120 | `broker_code`, `name`, `inn`, `region_key` |
| `usd_rates` | 1 765 | `rate_date`, `uzs_per_usd` |
| `regions`, `region_spellings` | 14, 28 | canonical key ↔ Uzbek Latin / Russian / Uzbek Cyrillic |
| `units` | 7 | unit code, dimension, base unit, factor |
| `statuses`, `status_policies` | 8, 32 | status flags; named status policies (`registry`, `not_cancelled`, `paid`, `fulfilled`, `fulfilled_any`, `active`, `awaiting_payment_any`, `cancelled`) |
| `codes` | 12 | raw → code for trade type, contract type, form, currency, role |
| `import_meta`, `import_checks`, `source_files` | 1, 37, 9 | identity/provenance, check results, file fingerprints |
| **view `deal_facts`** | 500 000 | one row per deal with every join (below) |

`deal_facts` columns, grouped:
- **Identity and source:** `deal_id`, `contract_id`, `source_file`, `source_row`
- **Time:** `deal_date`, `deal_time`, `deal_ts`, `deal_year/month/quarter`
- **Classification:** raw values plus `*_code` for trade type, contract type, form and status
- **Product:** `deal_product_name`, `product_name`, `brand`, `size`, `grade`, `product_type`, `product_class`, `tnved_code`, `product_group`, `product_category`, `industry`
- **Money:** `currency`, `currency_name`, `raw_amount`, `usd_rate_on_deal_date`, `fx_rate_applied`, `is_converted_from_usd`, `amount_uzs`, `start_price`, `base_price`, `unit_price`, `unit_price_uzs`
- **Quantity:** `lot_size`, `lot_count`, `quantity_native`, `quantity_unit`, `quantity_unit_code`, `quantity_dimension`, `quantity_base`, `quantity_base_unit`, `delivered_lots`, `delivered_quantity`, `remaining_lots`, `remaining_quantity`
- **Deadlines:** `payment_date`, `delivery_date`, `payment_term_days`, `delivery_term_days`, `payment_due_date`, `delivery_due_date`, `is_paid`, `is_cancelled`, `is_fully_delivered`, `is_payment_overdue`, `is_delivery_overdue`, `is_delivery_late`
- **Parties:** `seller_inn/name/region/region_key`, `buyer_inn/name/region/region_key`, `warehouse_region`, `warehouse_region_key`, `warehouse_address`, `seller_broker_code/name`, `buyer_broker_code/name` (the two brokers are joined separately)
- **Contract validity:** `contract_registered_on`, `contract_valid_until`

All joins are LEFT JOINs on primary keys, so rows cannot multiply. Tests confirm 500 000 rows both before and after the joins.

## 4. Money and numeric types

**Verified DuckDB 2.0.0 behaviour** (pinned by `tests/fixture/test_numbers_and_engine.py`):

| Behaviour | Consequence |
|---|---|
| `DECIMAL / INT` and `DECIMAL / DECIMAL` return `DOUBLE`; `avg(DECIMAL)` returns `DOUBLE` | SQL division is never used for reported values |
| `CAST('1.239' AS DECIMAL(18,2))` gives `1.24` and rounds silently | the decimal scale is validated with a regex before casting |
| `strptime('1.3.2025','%d.%m.%Y')` succeeds | the DD.MM.YYYY format is also checked with a regex |
| `DECIMAL(18,2) × DECIMAL(12,2)` gives `DECIMAL(18,4)`, and overflow **raises** an error | the left operand is widened to `DECIMAL(38,2)` before multiplying, which gives `DECIMAL(38,4)` |
| `sum(DECIMAL(18,2))` gives `DECIMAL(38,2)`; `round()` on a DECIMAL rounds half away from zero | sums stay exact |

**Precision, justified from the data** (A10):

| Value | Max in data | Type | Headroom |
|---|---|---|---|
| `raw_amount` | 30 190 376 286.00 (11 integer digits, ≤ 2 decimals) | `DECIMAL(18,2)` | 16 integer digits |
| `start_price`, `base_price` | 9 integer digits, ≤ 2 decimals | `DECIMAL(18,2)` | |
| `uzs_per_usd` | 13 500.03 (5 integer digits, 2 decimals) | `DECIMAL(12,2)` | |
| `amount_uzs` = amount × rate | 22 244 196 225.72 | `DECIMAL(38,4)` | exact, since 2 + 2 decimals = 4 and nothing is rounded |
| sum of `amount_uzs` over all deals | < 500 000 × 3.1e10 = 1.6e16 | `DECIMAL(38,4)` | about 22 digits of headroom |
| `quantity_native` = lots × lot size | 1 724 262 | `BIGINT` | |

**Exact unit price.** `unit_price = round_half_up(raw_amount / quantity_native, 6)` is computed with integer arithmetic in SQL: `(2·cents·10⁴ + q) // (2·q)` on HUGEINT, scaled back to `DECIMAL(38,6)` by multiplying with `0.000001`. All operands are positive (checked at import), so this is correct half-up rounding. The largest intermediate value, 6.0e16, stays below the BIGINT limit. `unit_price_uzs` uses the same method on `amount_uzs`. The smoke test checks all 500 000 unit prices against Python `Decimal`, with 0 mismatches.

**Ratios in later stages** (weighted average price = Σ amount ÷ Σ quantity, shares, percentages) must be computed as exact numerator and denominator sums in SQL and formatted with `numbers.exact_ratio` / `numbers.percent` (Decimal, ROUND_HALF_UP). Rounding happens only when the answer is rendered.

**Currency** always comes from `contracts.Валюта`. `amount_uzs` equals `raw_amount` for UZS deals. For USD deals it is `raw_amount × uzs_per_usd` at the rate of the deal date, from the supplied `usd_rates.csv`. No FX data is fetched at runtime. `is_converted_from_usd` tells the renderer to mention the conversion. Native sums are only ever grouped by `currency` and never mixed.

## 5. Units

`units` table / `units.py`: тонна→kg ×1000, килограмм→kg ×1, литр→l ×1, куб.метр→l ×1000, кв.метр, метр, штука ×1. `quantity_base` is only comparable within one `quantity_dimension`, and only for one resolved product. Petrol is sold in both litres (volume) and tonnes (mass), and these are never added together (smoke test `test_incompatible_units_for_same_product_are_visible`).

## 6. Statuses and deadlines

- No status filter is built into tables or views. Policies are data (`status_policies`) and are chosen per metric (spec §4–5).
- `payment_due_date = deal_date + payment_term_days`; `delivery_due_date = payment_date + delivery_term_days` (calendar days; NULL while unpaid).
- `is_payment_overdue`: unpaid, not cancelled, `payment_due_date < as_of`. Real data: **0**, which matches the README meaning of `Ждёт оплату` / `Заключен`.
- `is_delivery_overdue`: paid, not fully delivered, `remaining_lots > 0`, `delivery_due_date < as_of`. Real data: **22 514** (120 `Оплачен` + 22 394 of the 22 648 `Исполнен(Частично)`, based on the remaining quantity).
- `is_delivery_late` (historical): `delivery_date > delivery_due_date`. Real data: **17 805**.
- `remaining_lots/quantity`: `lot_count − coalesce(delivered_lots, 0)` for non-cancelled deals; NULL for cancelled deals, where nothing is owed. An empty `Поставлено` stays NULL and is never turned into 0.
- A due date equal to the as-of date is **not** overdue. Both sides of this boundary are tested on fixtures.

## 7. Tests

`tests/fixture/` holds the synthetic, hand-computed dataset (`conftest.py` documents every expected value). `tests/smoke/` holds the real-dataset tests (marker `smoke`). These build into a fresh temporary DB and compare against a reference computed independently with Python `csv` + `Decimal`: totals per currency, total UZS, quantities per unit, every unit price, FX rate and source row, and the deadline flags.

```bash
.venv/bin/python -m pytest tests/fixture          # synthetic only
.venv/bin/python -m pytest tests/smoke -m smoke   # real dataset (skipped if the data is absent)
```
