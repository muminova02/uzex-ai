"""Reproducible dataset audit (Prompt 1), part 2: expected facts + the open A10 checks.

Independent of the import pipeline on purpose: reads the raw CSVs as text with DuckDB's
CSV reader (all_varchar) and does its own conversions, so it can be used as a reference
for the importer's tests.

Usage:  .venv/bin/python -I scripts/audit_checks.py uzex_spot_data [> docs/audit_checks_output.txt]
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from uzex_assistant.regions import REGIONS, raw_to_key  # noqa: E402

AS_OF = "2026-10-30"
EXPECTED = {
    "deals": 500000, "contracts": 6616, "participants": 14392, "brokers": 120, "usd_rates": 1765,
    "deals_2022": 84299, "deals_2023": 93985, "deals_2024": 106647, "deals_2025": 115763, "deals_2026": 99306,
    "form_spot": 456724, "form_forward": 36431, "form_futures": 6845, "usd_deals": 6590,
}

d = Path(sys.argv[1])
con = duckdb.connect()
con.execute(f"create view d as select * from read_csv('{d}/deals_*.csv', all_varchar=true, header=true, filename=true)")
for t in ("contracts", "participants", "brokers", "usd_rates"):
    con.execute(f"create view {t} as select * from read_csv('{d}/{t}.csv', all_varchar=true, header=true)")
con.execute("create table region_map(raw varchar, key varchar)")
con.executemany("insert into region_map values (?, ?)", list(raw_to_key().items()))

NUM = "replace(replace(replace({c}, ' ', ''), chr(160), ''), chr(8239), '')"
# typed helper view (reference conversions)
con.execute(f"""
create view dt as select
  "Номер сделки" deal_id, strptime("Дата сделки", '%d.%m.%Y')::DATE deal_date,
  "Количество"::BIGINT qty, "Лот"::BIGINT lot, {NUM.format(c='"Сумма сделки"')}::DECIMAL(18,2) amount,
  {NUM.format(c='"Стартовая цена"')}::DECIMAL(18,2) start_price,
  "Поставлено"::BIGINT delivered, "Состояние сделки" status, "Форма контракта" form, "Тип торгов" trade_type,
  "Номер контракта" contract_id, "Регион" region, "ИНН продавца" seller, "ИНН покупателя" buyer,
  strptime("Дата оплаты", '%d.%m.%Y')::DATE pay_date, strptime("Дата поставки", '%d.%m.%Y')::DATE del_date,
  "Наименование товара" name
from d""")
con.execute("""
create view ct as select "Номер контракта" contract_id, "Валюта" currency, "Регион" region,
  "Срок оплаты"::INT pay_term, "Срок поставки"::INT del_term,
  strptime("Дата регистрации", '%d.%m.%Y')::DATE reg, strptime("Действует до", '%d.%m.%Y')::DATE valid_to,
  "Тип торгов" trade_type, "Форма контракта" form, "Наименование продукта" product,
  "Марка товара" brand, "Размер товара" size, "Сорт товара" grade, "Тип товара" ptype, "Класс товара" pclass
from contracts""")
con.execute("create view j as select dt.*, ct.currency, ct.region c_region, ct.pay_term, ct.del_term, ct.reg, ct.valid_to, "
            "ct.trade_type c_trade_type, ct.form c_form, ct.product, ct.brand, ct.size, ct.grade, ct.ptype, ct.pclass "
            "from dt join ct using (contract_id)")


def one(sql: str):
    return con.execute(sql).fetchone()


def show(title: str, sql: str, limit: int = 30) -> None:
    rows = con.execute(sql).fetchall()
    print(f"\n### {title}")
    print("SQL:", " ".join(sql.split()))
    for r in rows[:limit]:
        print("   ", r)
    if len(rows) > limit:
        print(f"    ... {len(rows) - limit} more rows")


print("# audit_checks.py output")
print("duckdb", duckdb.__version__, "| data dir", d)
print("\n## Source fingerprints (SHA-256)")
for f in sorted(d.iterdir()):
    if f.is_file():
        print(f"    {hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}  {f.stat().st_size} bytes")

print("\n## Expected facts (computed vs expected)")
computed = {
    "deals": one("select count(*) from d")[0],
    "contracts": one("select count(*) from contracts")[0],
    "participants": one("select count(*) from participants")[0],
    "brokers": one("select count(*) from brokers")[0],
    "usd_rates": one("select count(*) from usd_rates")[0],
    **{f"deals_{y}": one(f"select count(*) from dt where year(deal_date) = {y}")[0] for y in range(2022, 2027)},
    "form_spot": one("select count(*) from dt where form = 'Спот'")[0],
    "form_forward": one("select count(*) from dt where form = 'Форвард'")[0],
    "form_futures": one("select count(*) from dt where form = 'Фьючерс'")[0],
    "usd_deals": one("select count(*) from j where currency = 'Доллар США'")[0],
}
fails = 0
for k, exp in EXPECTED.items():
    ok = computed[k] == exp
    fails += not ok
    print(f"    {k:14s} computed={computed[k]:>7} expected={exp:>7} {'OK' if ok else 'MISMATCH'}")
print("    cancelled_nonpayment computed =", one("select count(*) from dt where status = 'Аннулирован(Не оплачен)'")[0])

show("Unique keys (rows, distinct)", """
select 'deals.Номер сделки', count(*), count(distinct "Номер сделки") from d union all
select 'contracts.Номер контракта', count(*), count(distinct "Номер контракта") from contracts union all
select 'participants.ИНН', count(*), count(distinct "ИНН") from participants union all
select 'brokers.Код брокера', count(*), count(distinct "Код брокера") from brokers union all
select 'brokers.ИНН', count(*), count(distinct "ИНН") from brokers union all
select 'usd_rates.Дата', count(*), count(distinct "Дата") from usd_rates""")
show("Identifier formats", """
select 'deal id 6 digits', count(*) filter (where not regexp_full_match("Номер сделки", '[0-9]{6}')) from d union all
select 'contract id 6 digits', count(*) filter (where not regexp_full_match("Номер контракта", '[0-9]{6}')) from contracts union all
select 'participant INN 9 digits', count(*) filter (where not regexp_full_match("ИНН", '[0-9]{9}')) from participants union all
select 'broker code BKnnn', count(*) filter (where not regexp_full_match("Код брокера", 'BK[0-9]{3}')) from brokers union all
select 'broker INN 9 digits', count(*) filter (where not regexp_full_match("ИНН", '[0-9]{9}')) from brokers union all
select 'TN VED 6 digits', count(*) filter (where not regexp_full_match("Код ТН ВЭД", '[0-9]{6}')) from contracts""")
show("Broker INNs that are also participant INNs", 'select count(*) from brokers b join participants p on b."ИНН" = p."ИНН"')
show("Date format DD.MM.YYYY violations (non-empty values)", """
select 'deals', count(*) filter (where not regexp_full_match("Дата сделки", '\\d{2}\\.\\d{2}\\.\\d{4}')
  or ("Дата оплаты" is not null and not regexp_full_match("Дата оплаты", '\\d{2}\\.\\d{2}\\.\\d{4}'))
  or ("Дата поставки" is not null and not regexp_full_match("Дата поставки", '\\d{2}\\.\\d{2}\\.\\d{4}'))) from d union all
select 'deal time HH:MM:SS', count(*) filter (where not regexp_full_match("Время сделки", '\\d{2}:\\d{2}:\\d{2}')) from d""")
show("Deal date range; deal file year = deal date year", """
select min(deal_date), max(deal_date),
  (select count(*) from d where split_part("Дата сделки", '.', 3) <> regexp_extract(filename, 'deals_(\\d{4})', 1)) from dt""")
show("Deal number increases with deal date+time (README claim): pairs out of order", """
select count(*) from (select "Номер сделки"::BIGINT n, strptime("Дата сделки" || ' ' || "Время сделки", '%d.%m.%Y %H:%M:%S') ts,
  lag(ts) over (order by n) prev_ts from d) where ts < prev_ts""")
show("Thousands-separator characters (rows containing)", """
select 'Сумма сделки', count(*) filter (where contains("Сумма сделки", ' ')), count(*) filter (where contains("Сумма сделки", chr(160))), count(*) filter (where contains("Сумма сделки", chr(8239))) from d union all
select 'Стартовая цена', count(*) filter (where contains("Стартовая цена", ' ')), count(*) filter (where contains("Стартовая цена", chr(160))), count(*) filter (where contains("Стартовая цена", chr(8239))) from d union all
select 'contracts.Базисная цена', count(*) filter (where contains("Базисная цена", ' ')), count(*) filter (where contains("Базисная цена", chr(160))), count(*) filter (where contains("Базисная цена", chr(8239))) from contracts union all
select 'usd_rates.Курс', count(*) filter (where contains("Курс", ' ')), 0, 0 from usd_rates""")
show("Separator grouping: values with separators that are not 3-digit groups", """
select count(*) filter (where contains("Сумма сделки", ' ') and not regexp_full_match("Сумма сделки", '\\d{1,3}( \\d{3})+(\\.\\d+)?')),
       count(*) filter (where contains("Стартовая цена", ' ') and not regexp_full_match("Стартовая цена", '\\d{1,3}( \\d{3})+(\\.\\d+)?')) from d""")
show("Max integer digits / decimal digits per numeric column", """
select 'Сумма сделки', max(length(split_part(replace("Сумма сделки",' ',''), '.', 1))), max(length(split_part("Сумма сделки", '.', 2))) from d union all
select 'Стартовая цена', max(length(split_part(replace("Стартовая цена",' ',''), '.', 1))), max(length(split_part("Стартовая цена", '.', 2))) from d union all
select 'Базисная цена', max(length(split_part(replace("Базисная цена",' ',''), '.', 1))), max(length(split_part("Базисная цена", '.', 2))) from contracts union all
select 'Курс', max(length(split_part("Курс", '.', 1))), max(length(split_part("Курс", '.', 2))) from usd_rates union all
select 'Количество', max(length("Количество")), 0 from d""")
show("Numeric ranges", """
select 'qty (lots)', min(qty)::VARCHAR, max(qty)::VARCHAR, count(*) filter (where qty <= 0) from dt union all
select 'amount', min(amount)::VARCHAR, max(amount)::VARCHAR, count(*) filter (where amount <= 0) from dt union all
select 'physical qty', min(qty*lot)::VARCHAR, max(qty*lot)::VARCHAR, 0 from dt union all
select 'Курс', min("Курс"::DECIMAL(12,2))::VARCHAR, max("Курс"::DECIMAL(12,2))::VARCHAR, count(*) filter (where "Курс"::DECIMAL(12,2) <= 0) from usd_rates""")
show("Amount ranges by currency", "select currency, min(amount), max(amount), count(*) from j group by 1")
show("Max USD amount x rate (precision bound for amount_uzs)", """
select max(j.amount * r."Курс"::DECIMAL(12,2)) from j join usd_rates r on strptime(r."Дата", '%d.%m.%Y')::DATE = j.deal_date where j.currency = 'Доллар США'""")
show("Unit price (amount / physical qty) range by unit and currency (DOUBLE, for outlier review only)", """
select currency, "Единица измерения", min(amount / (qty*lot))::DECIMAL(18,2), median(amount / (qty*lot))::DECIMAL(18,2),
  max(amount / (qty*lot))::DECIMAL(18,2) from j join d on d."Номер сделки" = j.deal_id group by all order by all""")
show("Unit price vs start price by trade type (count of deals: below / equal / above start price)", """
select trade_type, count(*) filter (where amount < start_price * qty * lot), count(*) filter (where amount = start_price * qty * lot),
  count(*) filter (where amount > start_price * qty * lot) from dt group by 1 order by 1""")
show("Сумма залога / Курс decimals / VAT flag / role values", """
select 'Сумма залога', list(distinct "Сумма залога" order by "Сумма залога") from contracts union all
select 'Плательщик НДС', list(distinct "Плательщик НДС") from participants union all
select 'Валюта', list(distinct "Валюта") from contracts union all
select 'Вид деятельности', list(distinct "Вид деятельности") from participants""")
show("USD rate dates: gaps in the daily calendar", """
select count(*) from (select strptime("Дата", '%d.%m.%Y')::DATE dd, lead(strptime("Дата", '%d.%m.%Y')::DATE) over (order by strptime("Дата", '%d.%m.%Y')) nx from usd_rates) where nx - dd <> 1""")

print("\n## Consistency between deals and contracts (repeated fields)")
show("Repeated-field mismatches", """
select count(*) filter (where trade_type <> c_trade_type) trade_type_mismatch,
       count(*) filter (where form <> c_form) form_mismatch,
       count(*) filter (where (select key from region_map where raw = region) is distinct from (select key from region_map where raw = c_region)) warehouse_region_mismatch
from j""")
show("Unmapped region strings (deals / contracts / participants / brokers)", """
select 'deals', count(*) from d where "Регион" not in (select raw from region_map) union all
select 'contracts', count(*) from contracts where "Регион" not in (select raw from region_map) union all
select 'participants', count(*) from participants where "Регион" not in (select raw from region_map) union all
select 'brokers', count(*) from brokers where "Регион" not in (select raw from region_map)""")
show("Deal warehouse region vs seller's participant region (mismatch count)", """
select count(*) from dt join participants p on p."ИНН" = dt.seller where dt.region <> p."Регион" """)
show("Seller INN role in participants / buyer INN role", """
select 'seller', p."Роль", count(*) from dt join participants p on p."ИНН" = dt.seller group by all union all
select 'buyer', p."Роль", count(*) from dt join participants p on p."ИНН" = dt.buyer group by all""")
show("Deal name = contract product + labelled attributes (exact string rebuild)", """
select count(*) filter (where name = concat_ws(' ', product,
   case when brand is not null then 'Марка ' || brand end, case when size is not null then 'Размер ' || size end,
   case when grade is not null then 'Сорт ' || grade end, case when ptype is not null then 'Тип ' || ptype end,
   case when pclass is not null then 'Класс ' || pclass end)) exact,
 count(*) filter (where lower(name) = lower(concat_ws(' ', product,
   case when brand is not null then 'Марка ' || brand end, case when size is not null then 'Размер ' || size end,
   case when grade is not null then 'Сорт ' || grade end, case when ptype is not null then 'Тип ' || ptype end,
   case when pclass is not null then 'Класс ' || pclass end))) case_insensitive,
 count(*) total from j""")
show("Examples where deal name differs from rebuilt contract name (case-insensitive)", """
select distinct name, product, brand, size, grade, ptype, pclass from j where lower(name) <> lower(concat_ws(' ', product,
   case when brand is not null then 'Марка ' || brand end, case when size is not null then 'Размер ' || size end,
   case when grade is not null then 'Сорт ' || grade end, case when ptype is not null then 'Тип ' || ptype end,
   case when pclass is not null then 'Класс ' || pclass end)) limit 10""")
show("Deal date outside contract validity [Дата регистрации, Действует до]", """
select count(*) filter (where deal_date < reg) before_reg, count(*) filter (where deal_date > valid_to) after_valid,
  count(*) filter (where deal_date = reg) on_reg_day, count(*) filter (where deal_date = valid_to) on_last_day from j""")
show("Contracts with Действует до < Дата регистрации", "select count(*) from ct where valid_to < reg")

print("\n## Statuses, deadlines and delivery")
show("Status x contract form", "select status, form, count(*) from dt group by all order by all")
show("'Новый' deal dates", "select deal_date, count(*) from dt where status = 'Новый' group by 1")
show("Deals dated 2026-10-30 by status", "select status, count(*) from dt where deal_date = DATE '2026-10-30' group by 1 order by 1")
show("Payment lag (pay_date - deal_date) minus Срок оплаты, paid deals: min / max / count late (>0) / count on deadline (=0)", """
select min((pay_date - deal_date) - pay_term), max((pay_date - deal_date) - pay_term),
  count(*) filter (where (pay_date - deal_date) > pay_term), count(*) filter (where (pay_date - deal_date) = pay_term),
  count(*) filter (where pay_date < deal_date) from j where pay_date is not null""")
show("Delivery lag (del_date - pay_date) minus Срок поставки, delivered deals: min / max / late / on deadline / before payment", """
select min((del_date - pay_date) - del_term), max((del_date - pay_date) - del_term),
  count(*) filter (where (del_date - pay_date) > del_term), count(*) filter (where (del_date - pay_date) = del_term),
  count(*) filter (where del_date < pay_date) from j where del_date is not null""")
show("Unpaid, not cancelled: payment due date (deal_date + Срок оплаты) relative to as-of 2026-10-30", f"""
select status, count(*), count(*) filter (where deal_date + pay_term < DATE '{AS_OF}') due_before_asof,
  count(*) filter (where deal_date + pay_term = DATE '{AS_OF}') due_on_asof, min(deal_date + pay_term), max(deal_date + pay_term)
from j where pay_date is null and status <> 'Аннулирован(Не оплачен)' group by 1 order by 1""")
show("Cancelled deals: payment due date relative to as-of", f"""
select count(*), count(*) filter (where deal_date + pay_term >= DATE '{AS_OF}') due_not_before_asof, max(deal_date + pay_term) from j where status = 'Аннулирован(Не оплачен)'""")
show("'Оплачен' (paid, undelivered): delivery due date (pay_date + Срок поставки) relative to as-of", f"""
select count(*), count(*) filter (where pay_date + del_term < DATE '{AS_OF}') due_before_asof,
  count(*) filter (where pay_date + del_term = DATE '{AS_OF}') due_on_asof, min(pay_date + del_term), max(pay_date + del_term) from j where status = 'Оплачен'""")
show("Поставлено vs Количество by status", """
select status, count(*) filter (where delivered is null) null_, count(*) filter (where delivered = 0) zero,
  count(*) filter (where delivered > 0 and delivered < qty) partial, count(*) filter (where delivered = qty) full_,
  count(*) filter (where delivered > qty) over_ from dt group by 1 order by 1""")
show("Dates after as-of", f"select count(*) filter (where pay_date > DATE '{AS_OF}'), count(*) filter (where del_date > DATE '{AS_OF}') from dt")

print("\n## Product names")
show("Distinct raw product strings", """
select 'deal Наименование товара', count(distinct name) from dt union all
select 'contract Наименование продукта', count(distinct product) from ct union all
select 'contract product+attributes', count(distinct (product, brand, size, grade, ptype, pclass)) from ct""")
show("Product name variants: contract names that collapse under lower-case + hyphen->space + whitespace squeeze", """
with n as (select product, regexp_replace(lower(replace(product, '-', ' ')), '\\s+', ' ', 'g') norm from ct)
select norm, list(distinct product) from n group by norm having count(distinct product) > 1 order by norm""", limit=40)
show("Distinct normalized contract names", """
select count(distinct regexp_replace(lower(replace(product, '-', ' ')), '\\s+', ' ', 'g')) from ct""")
show("Сорт value variants", 'select "Сорт товара", count(*) from contracts where "Сорт товара" is not null group by 1 order by 1', limit=60)
show("Grades embedded in product names (\"сорт\"/\"класс\" inside Наименование продукта)", """
select product, count(*) from ct where regexp_matches(lower(product), 'сорт|класс') group by 1 order by 1""")
show("Classification: distinct groups / categories / industries / TN VED", """
select count(distinct "Группа товара"), count(distinct "Категория товара"), count(distinct "Отрасль промышленности"), count(distinct "Код ТН ВЭД") from contracts""")
show("Group values", 'select "Группа товара", count(*) from contracts group by 1')
show("Category values", 'select "Категория товара", count(*) from contracts group by 1 order by 2 desc', limit=40)
show("Same TN VED, different units across contracts", """
select "Код ТН ВЭД", list(distinct "Единица измерения"), list(distinct "Наименование продукта")[1:4] from contracts group by 1 having count(distinct "Единица измерения") > 1""")
show("Same normalized product name, different units", """
select regexp_replace(lower(replace("Наименование продукта", '-', ' ')), '\\s+', ' ', 'g') n, list(distinct "Единица измерения"), list(distinct "Лот")
from contracts group by 1 having count(distinct "Единица измерения") > 1""")

print("\n## Summary")
print("expected-fact mismatches:", fails)
sys.exit(1 if fails else 0)
