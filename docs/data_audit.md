# UZEX Data Assistant: Data Audit

Audit date: 2026-11-04 (Asia/Tashkent)
Dataset: `uzex_spot_data.zip`, SHA-256 `88182bf4dad3569eea5fac611e8a16db7a455110fdaa6495650e7d87e09ac258`
Tool: `scripts/profile.py` (pandas 3.0.5, Python 3.13.18), plus manual review of `README.md` and the CSV headers.
All CSVs were read as strings (`dtype=str`, `keep_default_na=False`), so nothing was coerced silently.

**Local re-verification (2026-11-04, macOS arm64, Python 3.12.13, `.venv`):**
- `scripts/profile.py` re-run with pandas 3.1.1 (3.0.5 not used locally): every count in A1–A8 reproduced exactly; the sample rows also matched.
- The original `uzex_spot_data.zip` is not present locally (the data is supplied unpacked in `uzex_spot_data/`), so the zip SHA-256 above could **not** be re-checked. Per-file SHA-256 fingerprints of the local copy are recorded in `docs/audit_checks_output.txt` and are used by the importer.
- New script `scripts/audit_checks.py` (DuckDB 2.0.0, raw text via `read_csv(all_varchar=true)`, independent of the import pipeline) computes the expected facts and closes the A10 checks; full output with the SQL of every check: `docs/audit_checks_output.txt`. Summary in A10–A13 below.

The document has two parts. Part A lists **verified findings**, each one reproduced from script output or read directly from the files. Part B lists **implications and plans** for the implementation; its status is in B7.

---

## Part A: Verified findings

### A1. Files and row counts

| File | Data rows | Notes |
|---|---|---|
| deals_2022.csv | 84 299 | |
| deals_2023.csv | 93 985 | |
| deals_2024.csv | 106 647 | |
| deals_2025.csv | 115 763 | |
| deals_2026.csv | 99 306 | |
| **deals total** | **500 000** | `Номер сделки` is unique across all 500 000 rows |
| contracts.csv | 6 616 | |
| participants.csv | 14 392 | |
| brokers.csv | 120 | |
| usd_rates.csv | 1 765 | one row per calendar day, 01.01.2022 – 31.10.2026 |

The per-year deal counts match the counts stated in README.md.

### A2. Deal headers

`deals_*.csv` (all five files share one header):
`Номер сделки, Дата сделки, Время сделки, Тип торгов, Количество, Сумма сделки, Стартовая цена, Номер контракта, Наименование товара, Лот, Единица измерения, Регион, Тип контракта, Форма контракта, Состояние сделки, ИНН продавца, ИНН покупателя, Брокер продавца, Брокер покупателя, Дата оплаты, Дата поставки, Поставлено`

`contracts.csv`:
`Номер контракта, Наименование продукта, Марка товара, Размер товара, Сорт товара, Тип товара, Класс товара, Код ТН ВЭД, Группа товара, Категория товара, Отрасль промышленности, Единица измерения, Лот, Валюта, Базисная цена, Сумма залога, Базис поставки, Регион, Адрес склада, Тип торгов, Тип контракта, Форма контракта, Срок оплаты, Срок поставки, Упаковка, ИНН продавца, Дата регистрации, Действует до`

`participants.csv`: `ИНН, Наименование, Регион, Вид деятельности, Роль, Плательщик НДС`
`brokers.csv`: `Код брокера, Наименование, ИНН, Регион`
`usd_rates.csv`: `Дата, Курс`

### A3. Categorical distributions (deals)

**Состояние сделки**

| Status | Count |
|---|---|
| Исполнен | 431 061 |
| Аннулирован(Не оплачен) | 33 719 |
| Исполнен(Частично) | 22 648 |
| Оплачен | 5 854 |
| Заключен | 3 954 |
| Ждёт оплату | 1 714 |
| Новый | 964 |
| Не подписан | 86 |

**Тип торгов:** Английский аукцион 254 021; Двусторонний аукцион 235 504; Обратный аукцион 10 475.
**Тип контракта:** Внутренний 493 410; Экспортный 6 590.
**Форма контракта:** Спот 456 724; Форвард 36 431; Фьючерс 6 845.
**Единица измерения:** тонна 261 629; килограмм 203 891; литр 12 524; штука 8 707; куб.метр 5 509; кв.метр 3 877; метр 3 863.
**Лот (deals):** 1 → 446 853; 100 → 33 823; 1000 → 17 416; 12 → 1 908.
**Регион (deals), 14 values:** Toshkent shahri 98 435; Namangan viloyati 58 493; Buxoro viloyati 41 124; Samarqand viloyati 40 118; Sirdaryo viloyati 38 970; Toshkent viloyati 35 914; Qashqadaryo viloyati 32 770; Xorazm viloyati 28 362; Andijon viloyati 24 713; Farg‘ona viloyati 24 211; Surxondaryo viloyati 21 723; Navoiy viloyati 18 904; Qoraqalpog‘iston Respublikasi 18 895; Jizzax viloyati 17 368.

### A4. Referential integrity (deals → reference tables)

| Check | Result |
|---|---|
| Deals whose `Номер контракта` is missing from contracts.csv | 0 |
| Deals where `Лот` ≠ contract `Лот` | 0 |
| Deals where `Единица измерения` ≠ contract unit | 0 |
| Deals where `ИНН продавца` ≠ contract seller INN | 0 |
| Deals where `Тип контракта` ≠ contract type | 0 |
| Seller INNs missing from participants.csv | 0 |
| Buyer INNs missing from participants.csv | 0 |
| Seller / buyer broker codes missing from brokers.csv | 0 / 0 |
| Deal dates with no USD rate | 0 |

### A5. Currency

- Contracts by `Валюта`: Узбекский сум 6 468; Доллар США 148.
- Deal type × contract currency, cross-tabulated:
  - Экспортный → Доллар США: 6 590; Узбекский сум: 0
  - Внутренний → Узбекский сум: 493 410; Доллар США: 0

### A6. Participants

- No duplicate INNs and no duplicate names.
- `Роль`: Покупатель 14 000; Продавец 392.
- No INN appears as both seller and buyer in the deals.
- Names mix Cyrillic and Latin scripts and contain nested quotes (for example `"""ҲАРАЙЎМ КИМЁ"" МЧЖ"` in the raw CSV).

### A7. Numeric formats

- `Сумма сделки` and `Стартовая цена` use a space as the thousands separator and `.` as the decimal point (for example `304 547 938.4`).
- After removing spaces and NBSP, every value in `Сумма сделки`, `Стартовая цена`, `Количество`, `Лот` and `Поставлено` matches `-?\d+(\.\d+)?`. No malformed values were found.
- Fractional digits in `Сумма сделки`: 2 digits in 118 033 rows, 1 digit in 35 385 rows, an integer in the rest. No value has more than 2 decimal places.
- `Поставлено` is empty in 46 291 rows. Every other numeric column has no empty values.

### A8. Status vs. date/delivery fields (share of rows where the field is empty)

| Status | Дата оплаты empty | Дата поставки empty | Поставлено empty |
|---|---|---|---|
| Исполнен | 0.00 | 0.00 | 0.00 |
| Исполнен(Частично) | 0.00 | 0.00 | 0.00 |
| Оплачен | 0.00 | 1.00 | 1.00 |
| Аннулирован(Не оплачен) | 1.00 | 1.00 | 1.00 |
| Заключен | 1.00 | 1.00 | 1.00 |
| Ждёт оплату | 1.00 | 1.00 | 1.00 |
| Не подписан | 1.00 | 1.00 | 1.00 |
| Новый | 1.00 | 1.00 | 1.00 |

Shares are rounded to 2 decimals. The empty counts add up exactly: 5 854 + 33 719 + 3 954 + 1 714 + 86 + 964 = 46 291, which equals the number of empty `Поставлено` values.

### A9. Naming and region inconsistencies

- **Region vocabularies differ between files.** deals.csv and participants.csv use Uzbek Latin (`Farg‘ona viloyati`, `Toshkent shahri`). contracts.csv uses Russian (`Ферганская область`, `г. Ташкент`, `Республика Каракалпакстан`). Each file has 14 distinct values.
- **Product names vary between sellers.** README.md states this, and a sample of contracts.csv confirms it: `Хлопок-сырец (спот)` vs `Хлопок сырец (спот)`; Сорт written as `3 сорт`, `первый сорт` or `третий сорт`; a grade inside the name (`Пшеница продовольственная 4-класс`) instead of the attribute column. One deal sample has `БЕНЗИН АВТОМОБИЛЬНЫЙ Марка АИ-92`, while a contract sample has `Бензин` with Марка `АИ-92`.
- The deals' `Наименование товара` combines the product name and its attributes in one string (`… Марка … Размер … Сорт … Тип … Класс …`).

### A10. Former open checks, now verified

Source: `scripts/audit_checks.py` → `docs/audit_checks_output.txt` (each section prints its SQL). Region comparisons use the 14-entry dictionary in `src/uzex_assistant/regions.py` (every raw region string in all four files maps; 0 unmapped).

| Check | Result |
|---|---|
| Distinct product strings | deals `Наименование товара` 697; contracts `Наименование продукта` 230; contract (product + 5 attributes) 695 |
| Product-name variants | lower-case + hyphen→space + whitespace squeeze collapses 230 contract names to 188; 36 groups merge (mostly ALL-CAPS vs normal case, `Хлопок-сырец`/`Хлопок сырец`, `1-класс`/`1 класс`) |
| `Сорт товара` spellings | 28 distinct raw values for at most ~6 grades (`1 сорт`, `1-сорта`, `I сорт`, `первый сорт`, `ПЕРВЫЙ СОРТ`, `в/с`, `высшего сорта` …) |
| Grade/class inside the product name | 80 contract product names contain `сорт`/`класс` (e.g. `ХЛОПОК СЫРЕЦ (СПОТ) II СОРТ I КЛАСС`, `Пшеница 4-класс`) |
| Deal name vs contract | deal `Наименование товара` = contract product + labelled attributes (`Марка … Размер … Сорт … Тип … Класс …`): 500 000 / 500 000 case-insensitively, 458 581 byte-exact (the rest differ only in letter case) |
| Same product, incompatible units | `Бензин` / `Бензин автомобильный`: **литр (lot 1000) and тонна** — volume vs mass, must never be summed together. `Дизельное топливо` / `Топливо дизельное`: тонна and килограмм (convertible). TN VED 271012 and 271019 each span two units |
| Deal warehouse region vs contract region | 0 mismatches after mapping; deal `Регион` also equals the seller participant's `Регион` in all 500 000 deals |
| Deal date within contract `Дата регистрации`–`Действует до` | 0 before, 0 after (4 995 on the registration day, 5 164 on the last valid day, i.e. the window is inclusive); 0 contracts with end < start |
| Payment vs `Срок оплаты` (paid deals) | `pay_date − deal_date − term` ∈ [−45, 0]: **no late payments**; 71 622 paid exactly on the deadline day; 0 paid before the deal date |
| Delivery vs `Срок поставки` (deals with a delivery date) | `del_date − pay_date − term` ∈ [−59, +15]: **17 805 late deliveries**, 31 367 on the deadline day, 0 before payment |
| Unpaid, not cancelled (`Новый`, `Не подписан`, `Заключен`, `Ждёт оплату`) | 0 with payment due date < 2026-10-30; 39 `Ждёт оплату` + 3 `Заключен` due exactly on 2026-10-30 (so "not expired" includes the due day) |
| `Аннулирован(Не оплачен)` | all 33 719 have payment due date < 2026-10-30 (latest 2026-10-29) |
| `Оплачен` (paid, not delivered) | 5 854; 120 with delivery due date < 2026-10-30 (overdue at snapshot), 19 due on 2026-10-30 |
| `Поставлено` vs `Количество` | `Исполнен`: = Количество in all 431 061; `Исполнен(Частично)`: 0 < Поставлено < Количество in all 22 648; never 0, never > Количество; NULL for all other statuses |
| `Новый` dates | all 964 dated 2026-10-30, and no deal on 2026-10-30 has another status |
| Status × form | `Заключен` only Форвард/Фьючерс; `Ждёт оплату` only Спот (as README says) |
| Dates after the snapshot | 0 payment or delivery dates after 2026-10-30 |
| `Количество` | 1 … 1 724 262 lots; **0 rows with 0** (unit-price division is safe) |
| `Сумма сделки` | UZS 64 671.00 … 30 190 376 286.00; USD 7 327.96 … 1 683 554.25; max 11 integer digits, max 2 decimals |
| `Курс` | 10 829.10 … 13 500.03 UZS/USD, 2 decimals, daily calendar with 0 gaps |
| Max USD amount × rate | 22 244 196 225.72 UZS |
| Unit-price ranges | reviewed per unit and currency in the output; no zero or negative prices |
| Start price vs result | Английский аукцион: price ≥ start in all rows; Обратный аукцион: price < start in all rows; Двусторонний: below start in 235 500, equal in 4 |
| `Сумма залога` | exactly `10 %`, `15 %`, `20 %` (integer percent + space + `%`) |
| `Код ТН ВЭД` | always 6 digits, no leading zero (40 distinct); still stored as text |
| Identifier formats | deal and contract numbers 6 digits; INNs 9 digits; broker codes `BKnnn`; broker INNs do not overlap participant INNs |
| Deal number order | deal number increases with deal date+time (0 inversions); deal file year = deal-date year for all rows |
| Classification | 2 groups, 8 categories, 10 industries |

### A11. Number formatting (new)

Thousands separators: ordinary space U+0020 in all 500 000 `Сумма сделки` and 496 335 `Стартовая цена` values; **0** occurrences of NBSP U+00A0, narrow NBSP U+202F or thin space U+2009 in any numeric column. All separated values form correct 3-digit groups. `Базисная цена` and `Курс` have no separators. The importer still accepts U+00A0/U+202F (tested on fixtures) and rejects anything that is not a valid grouping.

### A12. Questions this data cannot answer

There are no fields for profit, cost, bank details, contact persons, or deliveries/payments after 2026-10-30; post-snapshot states are unknown. Participant `Роль` is fixed per company (392 sellers, 14 000 buyers).

### A13. Not checked

- Whether two different raw product names denote the same physical product beyond case/hyphen/whitespace differences (for example `Бензин` vs `Бензин автомобильный`). This is a business-identity decision and is left to entity resolution (Prompt 3), with clarification questions.
- Company-name duplicates across scripts (Cyrillic vs Latin spelling of the same company): participant names and INNs are unique, but cross-script similarity was not analysed.

---

## Part B: Implications for implementation

1. **Parsing.** Load money columns as `DECIMAL(18,2)` after stripping spaces and NBSP. Load `Количество`, `Лот` and `Поставлено` as integers, keeping `Поставлено` NULL (never 0) when it is empty. Parse dates as `DD.MM.YYYY`.
2. **Derived columns**, computed in the database:
   `physical_quantity = Количество * Лот`; `delivered_quantity = Поставлено * Лот`;
   `unit_price = Сумма сделки / physical_quantity`. Rounding happens only at render time, and the unit and currency are always carried along.
3. **Currency.** Take currency from `contracts.Валюта`. UZS equivalent of USD deals = `Сумма сделки × Курс` on `Дата сделки`. Never add up mixed currencies without converting them first.
4. **Status semantics.** Annulled deals did not happen. Questions about volume or turnover need an explicit policy on including or excluding them, stated in the answer.
5. **Entity resolution.** Build one region dictionary that maps the Uzbek Latin and Russian forms (plus Uzbek Cyrillic and common aliases) to a canonical key. Normalize product names: case, hyphens, ordinal grade words (`первый` → `1`), attribute labels. Match company names across scripts and quote styles, falling back to INN.
6. **Joins.** Deal and contract integrity is 100%, so inner joins lose no rows.
7. **Status (2026-11-04).** B1–B4 and B6 are implemented in the Prompt 2 import pipeline (`src/uzex_assistant/importer.py`; rules in `docs/data_model.md`). B5 is only partly done: the region dictionary exists, while product and company resolution belong to Prompt 3. One refinement to B1: `DECIMAL(18,2)` is used for money because no value has more than 2 decimals (A10); the importer rejects values with more.
