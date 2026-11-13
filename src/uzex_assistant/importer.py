"""Idempotent CSV -> DuckDB import pipeline (Prompt 2).

* Raw CSVs are only read, never modified.
* Stage 1 loads every file with DuckDB's CSV parser into all-VARCHAR raw tables with an
  explicit column list (header names are checked separately, byte-exact).
* Stage 2 validates every raw value against strict patterns (numbers, DD.MM.YYYY dates,
  enumerations, keys) and fails with samples (file, row, value) instead of coercing.
* Stage 3 converts with plain CAST (which would raise, not hide, any missed problem) into
  typed tables, then checks keys, joins and cross-field consistency.
* The database is built in a temporary file and atomically renamed, so a failed import never
  damages an existing prepared database.
* ``ensure_database`` reuses an existing DB when schema version, as-of date and the SHA-256
  fingerprints of all source files match the metadata stored in it.

CLI:  python -m uzex_assistant.importer {build,ensure,status} [--data-dir D] [--db-path P]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from . import schema as S
from .config import get_settings
from .regions import REGIONS, raw_to_key
from .products import COMMODITIES, commodity_of, compact, variant_of
from .units import UNITS

SEP_CHARS = "   "  # space, NBSP, narrow NBSP (thousands separators)
SEP = f"[{SEP_CHARS}]"
INT_RE = rf"^(\d{{1,3}}({SEP}\d{{3}})+|\d+)$"
MONEY_RE = rf"^(\d{{1,3}}({SEP}\d{{3}})+|\d+)(\.\d{{1,2}})?$"  # non-negative, at most 2 decimals
DATE_RE = r"^\d{2}\.\d{2}\.\d{4}$"
TIME_RE = r"^\d{2}:\d{2}:\d{2}$"
DIGITS_RE = r"^\d+$"
DEPOSIT_RE = r"^\d{1,3} ?%$"


class ImportFailure(Exception):
    """Raised with a readable list of every problem found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Import failed:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass
class SourceFile:
    table: str  # raw target: deals | contracts | participants | brokers | usd_rates
    path: Path
    columns: list[str]
    sha256: str = ""
    size: int = 0
    csv_rows: int = -1  # data rows counted independently with Python's csv module


@dataclass
class BuildReport:
    db_path: Path
    rebuilt: bool
    identity: dict
    seconds: float = 0.0
    counts: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)


# --------------------------------------------------------------------------- sources

def discover_sources(data_dir: Path) -> list[SourceFile]:
    data_dir = Path(data_dir)
    problems: list[str] = []
    if not data_dir.is_dir():
        raise ImportFailure([f"data directory not found: {data_dir}"])
    deal_files = sorted(data_dir.glob(S.DEAL_FILE_GLOB))
    if not deal_files:
        problems.append(f"no {S.DEAL_FILE_GLOB} files in {data_dir}")
    sources = [SourceFile("deals", p, S.DEAL_COLUMNS) for p in deal_files]
    for table, (fname, cols) in S.REFERENCE_FILES.items():
        p = data_dir / fname
        if not p.is_file():
            problems.append(f"missing file: {p}")
        sources.append(SourceFile(table, p, cols))
    if problems:
        raise ImportFailure(problems)
    return sources


def fingerprint(sources: list[SourceFile]) -> None:
    for s in sources:
        h = hashlib.sha256()
        with open(s.path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        s.sha256, s.size = h.hexdigest(), s.path.stat().st_size


def identity_of(sources: list[SourceFile], as_of: dt.date) -> dict:
    return {
        "schema_version": S.SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "files": {s.path.name: {"sha256": s.sha256, "size": s.size} for s in sources},
    }


def check_headers_and_count(sources: list[SourceFile]) -> list[str]:
    """Byte-exact header check and an independent row count with Python's csv module."""
    problems = []
    for s in sources:
        with open(s.path, encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh, delimiter=",", quotechar='"', strict=True)
            try:
                header = next(reader)
            except StopIteration:
                problems.append(f"{s.path.name}: empty file")
                continue
            except (csv.Error, UnicodeDecodeError) as e:
                problems.append(f"{s.path.name}: unreadable header: {e}")
                continue
            if header != s.columns:
                problems.append(f"{s.path.name}: header mismatch: expected {s.columns}, got {header}")
                continue
            n = 0
            try:
                for row in reader:
                    if len(row) != len(s.columns):
                        problems.append(f"{s.path.name}: data row {n + 1} has {len(row)} fields, expected {len(s.columns)}")
                        break
                    n += 1
            except (csv.Error, UnicodeDecodeError) as e:
                problems.append(f"{s.path.name}: CSV error after data row {n}: {e}")
            s.csv_rows = n
    return problems


# --------------------------------------------------------------------------- metadata

def read_identity(db_path: Path) -> dict | None:
    if not Path(db_path).is_file():
        return None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            row = con.execute("select identity_json from import_meta").fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row else None
    except (duckdb.Error, json.JSONDecodeError):
        return None


def ensure_database(data_dir: Path | None = None, db_path: Path | None = None,
                    as_of: dt.date | None = None, force: bool = False) -> BuildReport:
    """Return a prepared DB; rebuild only if missing, stale (fingerprint/schema/as-of) or forced."""
    st = get_settings()
    data_dir, db_path, as_of = Path(data_dir or st.data_dir), Path(db_path or st.db_path), as_of or st.as_of
    sources = discover_sources(data_dir)
    fingerprint(sources)
    wanted = identity_of(sources, as_of)
    if not force and read_identity(db_path) == wanted:
        return BuildReport(db_path, rebuilt=False, identity=wanted)
    return build_database(data_dir, db_path, as_of, _sources=sources)


# --------------------------------------------------------------------------- build

def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _read_csv_sql(src: SourceFile) -> str:
    cols = "{" + ", ".join(f"{_q(c)}: 'VARCHAR'" for c in src.columns) + "}"
    return (f"read_csv({_q(str(src.path))}, columns={cols}, header=true, auto_detect=false, delim=',', "
            f"quote='\"', escape='\"', strict_mode=true, nullstr='', encoding='utf-8')")


def _clean(col: str) -> str:
    return f"regexp_replace({col}, '{SEP}', '', 'g')"


def _date(col: str) -> str:
    return f"CAST(strptime({col}, '%d.%m.%Y') AS DATE)"


class _Validator:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        self.problems: list[str] = []
        self.checks: list[tuple[str, int, int, bool]] = []

    def bad(self, table: str, label: str, where: str, show: str) -> None:
        """Record a problem if any row of ``table`` satisfies ``where``; show up to 5 samples."""
        n = self.con.execute(f"select count(*) from {table} where {where}").fetchone()[0]
        if n:
            sample = self.con.execute(
                f"select source_file, source_row, {show} from {table} where {where} order by source_file, source_row limit 5"
            ).fetchall()
            self.problems.append(f"{table}: {label}: {n} row(s); samples (file, row, value): {sample}")

    def column_pattern(self, table: str, col: str, regex: str, nullable: bool, label: str) -> None:
        c = _ident(col)
        cond = f"({c} is not null and not regexp_matches({c}, {_q(regex)}))"
        if not nullable:
            cond = f"({c} is null or {cond})"
        self.bad(table, f"{col}: {label}", cond, c)

    def column_in(self, table: str, col: str, allowed: list[str], nullable: bool = False) -> None:
        c = _ident(col)
        lst = ", ".join(_q(a) for a in allowed)
        cond = f"{c} not in ({lst})" + ("" if nullable else f" or {c} is null")
        self.bad(table, f"{col}: unknown value", cond, c)

    def not_null(self, table: str, cols: list[str]) -> None:
        for col in cols:
            self.bad(table, f"{col}: empty value", f"{_ident(col)} is null", _ident(col))

    def valid_dates(self, table: str, col: str, nullable: bool) -> None:
        self.column_pattern(table, col, DATE_RE, nullable, "not DD.MM.YYYY")
        c = _ident(col)
        self.bad(table, f"{col}: impossible calendar date",
                 f"regexp_matches({c}, {_q(DATE_RE)}) and try_strptime({c}, '%d.%m.%Y') is null", c)

    def expect(self, name: str, actual: int, expected: int) -> None:
        ok = actual == expected
        self.checks.append((name, actual, expected, ok))
        if not ok:
            self.problems.append(f"check failed: {name}: got {actual}, expected {expected}")

    def raise_if_problems(self) -> None:
        if self.problems:
            raise ImportFailure(self.problems)


def build_database(data_dir: Path, db_path: Path, as_of: dt.date, _sources: list[SourceFile] | None = None) -> BuildReport:
    t0 = time.monotonic()
    sources = _sources or discover_sources(data_dir)
    if not _sources:
        fingerprint(sources)
    identity = identity_of(sources, as_of)
    problems = check_headers_and_count(sources)
    if problems:
        raise ImportFailure(problems)

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(db_path.name + ".building")
    for p in (tmp, Path(str(tmp) + ".wal")):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        report = _build(con, sources, identity, as_of)
        con.execute("checkpoint")
        con.close()
        os.replace(tmp, db_path)
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    report.db_path, report.seconds = db_path, round(time.monotonic() - t0, 2)
    # verify the stored identity round-trips (what ensure_database will compare against)
    if read_identity(db_path) != identity:
        raise ImportFailure(["stored import metadata could not be read back"])
    return report


def _build(con: duckdb.DuckDBPyConnection, sources: list[SourceFile], identity: dict, as_of: dt.date) -> BuildReport:
    v = _Validator(con)

    # ---------------- stage 1: raw text tables
    for table in ("deals", "contracts", "participants", "brokers", "usd_rates"):
        src = next(s for s in sources if s.table == table)
        cols = ", ".join(f"{_ident(c)} VARCHAR" for c in src.columns)
        con.execute(f"create table raw_{table} (source_file VARCHAR, source_row BIGINT, {cols})")
    for src in sources:
        try:
            con.execute(f"insert into raw_{src.table} select {_q(src.path.name)}, row_number() over (), * "
                        f"from {_read_csv_sql(src)}")
        except duckdb.Error as e:
            raise ImportFailure([f"{src.path.name}: CSV parse error: {e}"]) from e
        n = con.execute(f"select count(*) from raw_{src.table} where source_file = ?", [src.path.name]).fetchone()[0]
        v.expect(f"rows in {src.path.name} (DuckDB vs Python csv)", n, src.csv_rows)
    v.raise_if_problems()

    # ---------------- stage 2: value validation (no coercion)
    D, C, P, B, R = "raw_deals", "raw_contracts", "raw_participants", "raw_brokers", "raw_usd_rates"
    region_raw = list(raw_to_key())
    v.not_null(D, [c for c in S.DEAL_COLUMNS if c not in ("Дата оплаты", "Дата поставки", "Поставлено")])
    for col in ("Номер сделки", "Номер контракта", "ИНН продавца", "ИНН покупателя"):
        v.column_pattern(D, col, DIGITS_RE, False, "not digits")
    v.valid_dates(D, "Дата сделки", False)
    v.valid_dates(D, "Дата оплаты", True)
    v.valid_dates(D, "Дата поставки", True)
    v.column_pattern(D, "Время сделки", TIME_RE, False, "not HH:MM:SS")
    v.bad(D, "Время сделки: impossible time", "try_strptime(\"Время сделки\", '%H:%M:%S') is null", '"Время сделки"')
    for col in ("Количество", "Лот"):
        v.column_pattern(D, col, INT_RE, False, "not a whole number")
    v.column_pattern(D, "Поставлено", INT_RE, True, "not a whole number")
    for col in ("Сумма сделки", "Стартовая цена"):
        v.column_pattern(D, col, MONEY_RE, False, "not a number with at most 2 decimals")
    v.column_in(D, "Тип торгов", list(S.TRADE_TYPES))
    v.column_in(D, "Тип контракта", list(S.CONTRACT_TYPES))
    v.column_in(D, "Форма контракта", list(S.CONTRACT_FORMS))
    v.column_in(D, "Состояние сделки", list(S.STATUSES))
    v.column_in(D, "Единица измерения", list(UNITS))
    v.column_in(D, "Регион", region_raw)

    v.not_null(C, [c for c in S.CONTRACT_COLUMNS if c not in
                   ("Марка товара", "Размер товара", "Сорт товара", "Тип товара", "Класс товара")])
    for col in ("Номер контракта", "Код ТН ВЭД", "ИНН продавца"):
        v.column_pattern(C, col, DIGITS_RE, False, "not digits")
    for col in ("Лот", "Срок оплаты", "Срок поставки"):
        v.column_pattern(C, col, INT_RE, False, "not a whole number")
    v.column_pattern(C, "Базисная цена", MONEY_RE, False, "not a number with at most 2 decimals")
    v.column_pattern(C, "Сумма залога", DEPOSIT_RE, False, "not '<n> %'")
    v.valid_dates(C, "Дата регистрации", False)
    v.valid_dates(C, "Действует до", False)
    v.column_in(C, "Валюта", list(S.CURRENCIES))
    v.column_in(C, "Единица измерения", list(UNITS))
    v.column_in(C, "Регион", region_raw)
    v.column_in(C, "Тип торгов", list(S.TRADE_TYPES))
    v.column_in(C, "Тип контракта", list(S.CONTRACT_TYPES))
    v.column_in(C, "Форма контракта", list(S.CONTRACT_FORMS))

    v.not_null(P, S.PARTICIPANT_COLUMNS)
    v.column_pattern(P, "ИНН", DIGITS_RE, False, "not digits")
    v.column_in(P, "Регион", region_raw)
    v.column_in(P, "Роль", list(S.ROLES))
    v.column_in(P, "Плательщик НДС", list(S.VAT_FLAGS))
    v.not_null(B, S.BROKER_COLUMNS)
    v.column_pattern(B, "ИНН", DIGITS_RE, False, "not digits")
    v.column_in(B, "Регион", region_raw)
    v.valid_dates(R, "Дата", False)
    v.column_pattern(R, "Курс", MONEY_RE, False, "not a number with at most 2 decimals")
    v.raise_if_problems()

    # ---------------- stage 3: dimensions and typed tables
    con.execute("create table regions (region_key VARCHAR PRIMARY KEY, name_uz_latn VARCHAR, name_ru VARCHAR, name_uz_cyrl VARCHAR)")
    con.executemany("insert into regions values (?, ?, ?, ?)", [(k, *names) for k, names in REGIONS.items()])
    con.execute("create table region_spellings (raw VARCHAR PRIMARY KEY, region_key VARCHAR)")
    con.executemany("insert into region_spellings values (?, ?)", list(raw_to_key().items()))
    con.execute("create table units (unit VARCHAR PRIMARY KEY, unit_code VARCHAR, dimension VARCHAR, base_unit VARCHAR, factor_to_base INTEGER)")
    con.executemany("insert into units values (?, ?, ?, ?, ?)", [(u, *rest) for u, rest in UNITS.items()])
    con.execute("create table statuses (status VARCHAR PRIMARY KEY, status_code VARCHAR UNIQUE, is_paid BOOLEAN, is_cancelled BOOLEAN, is_fully_delivered BOOLEAN)")
    con.executemany("insert into statuses values (?, ?, ?, ?, ?)", [(s, *rest) for s, rest in S.STATUSES.items()])
    con.execute("create table status_policies (policy VARCHAR, status_code VARCHAR, PRIMARY KEY (policy, status_code))")
    con.executemany("insert into status_policies values (?, ?)",
                    [(p, c) for p, codes in S.STATUS_POLICIES.items() for c in codes])
    con.execute("create table codes (domain VARCHAR, raw VARCHAR, code VARCHAR, PRIMARY KEY (domain, raw))")
    for domain, mapping in (("trade_type", S.TRADE_TYPES), ("contract_type", S.CONTRACT_TYPES),
                            ("contract_form", S.CONTRACT_FORMS), ("currency", S.CURRENCIES), ("role", S.ROLES)):
        con.executemany("insert into codes values (?, ?, ?)", [(domain, r, c) for r, c in mapping.items()])

    try:
        _create_typed_tables(con)
    except duckdb.ConstraintException as e:  # duplicate primary keys
        raise ImportFailure([f"duplicate key while loading typed tables: {e}"]) from e

    _create_product_catalog(con, v)

    # ---------------- stage 4: integrity and consistency checks
    raw_counts = {t: con.execute(f"select count(*) from raw_{t}").fetchone()[0]
                  for t in ("deals", "contracts", "participants", "brokers", "usd_rates")}
    for t, n in raw_counts.items():
        v.expect(f"typed {t} rows = raw rows", con.execute(f"select count(*) from {t}").fetchone()[0], n)
    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    v.expect("deals without contract", one("select count(*) from deals d anti join contracts c using (contract_id)"), 0)
    v.expect("deals with unknown seller", one("select count(*) from deals d anti join participants p on p.inn = d.seller_inn"), 0)
    v.expect("deals with unknown buyer", one("select count(*) from deals d anti join participants p on p.inn = d.buyer_inn"), 0)
    v.expect("deals with unknown seller broker", one("select count(*) from deals d anti join brokers b on b.broker_code = d.seller_broker_code"), 0)
    v.expect("deals with unknown buyer broker", one("select count(*) from deals d anti join brokers b on b.broker_code = d.buyer_broker_code"), 0)
    v.expect("deals without USD rate on deal date", one("select count(*) from deals d anti join usd_rates r on r.rate_date = d.deal_date"), 0)
    for col in ("lot_size", "quantity_unit", "seller_inn", "contract_type", "contract_form", "trade_type"):
        v.expect(f"deals.{col} <> contracts.{col}",
                 one(f"select count(*) from deals d join contracts c using (contract_id) where d.{col} <> c.{col}"), 0)
    v.expect("deals with lot_count <= 0", one("select count(*) from deals where lot_count <= 0"), 0)
    v.expect("deals with lot_size <= 0", one("select count(*) from deals where lot_size <= 0"), 0)
    v.expect("deals with raw_amount <= 0", one("select count(*) from deals where raw_amount <= 0"), 0)
    v.expect("usd rates <= 0", one("select count(*) from usd_rates where uzs_per_usd <= 0"), 0)
    v.expect("delivered_lots > lot_count", one("select count(*) from deals where delivered_lots > lot_count"), 0)
    v.expect("delivery_date without payment_date", one("select count(*) from deals where delivery_date is not null and payment_date is null"), 0)
    v.expect("paid-status deals without payment_date", one(
        "select count(*) from deals d join statuses s using (status) where s.is_paid and d.payment_date is null"), 0)
    v.expect("unpaid-status deals with payment_date", one(
        "select count(*) from deals d join statuses s using (status) where not s.is_paid and d.payment_date is not null"), 0)
    v.raise_if_problems()

    _create_views(con, as_of)
    v.expect("deal_facts rows = deals rows (joins do not multiply)",
             one("select count(*) from deal_facts"), raw_counts["deals"])
    v.expect("deal_facts distinct deal_id", one("select count(distinct deal_id) from deal_facts"), raw_counts["deals"])
    v.expect("deals without product variant", one("select count(*) from deal_facts where product_variant_key is null"), 0)
    v.expect("USD deals without applied FX rate", one(
        "select count(*) from deal_facts where currency = 'USD' and fx_rate_applied is null"), 0)
    v.raise_if_problems()

    con.execute("create table import_meta (schema_version VARCHAR, as_of DATE, built_at_utc VARCHAR, "
                "duckdb_version VARCHAR, identity_json VARCHAR)")
    built_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")  # text: no pytz needed to read it
    con.execute("insert into import_meta values (?, ?, ?, ?, ?)",
                [S.SCHEMA_VERSION, as_of, built_at, duckdb.__version__, json.dumps(identity, sort_keys=True)])
    con.execute("create table import_checks (check_name VARCHAR, actual BIGINT, expected BIGINT, ok BOOLEAN)")
    con.executemany("insert into import_checks values (?, ?, ?, ?)", v.checks)
    con.execute("create table source_files (file_name VARCHAR PRIMARY KEY, target_table VARCHAR, sha256 VARCHAR, size_bytes BIGINT, data_rows BIGINT)")
    con.executemany("insert into source_files values (?, ?, ?, ?, ?)",
                    [(s.path.name, s.table, s.sha256, s.size, s.csv_rows) for s in sources])
    for t in ("deals", "contracts", "participants", "brokers", "usd_rates"):
        con.execute(f"drop table raw_{t}")
    counts = {t: one(f"select count(*) from {t}") for t in ("deals", "contracts", "participants", "brokers", "usd_rates", "deal_facts")}
    return BuildReport(Path(), rebuilt=True, identity=identity, counts=counts, checks=v.checks)


def _create_typed_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
    create table usd_rates as
    select {_date('"Дата"')} as rate_date, CAST("Курс" AS DECIMAL(12,2)) as uzs_per_usd,
           source_file, source_row
    from raw_usd_rates""")
    con.execute("alter table usd_rates add primary key (rate_date)")

    con.execute(f"""
    create table brokers as
    select "Код брокера" as broker_code, "Наименование" as name, "ИНН" as inn, "Регион" as region,
           rs.region_key, source_file, source_row
    from raw_brokers b join region_spellings rs on rs.raw = b."Регион" """)
    con.execute("alter table brokers add primary key (broker_code)")

    con.execute("""
    create table participants as
    select "ИНН" as inn, "Наименование" as name, "Регион" as region, rs.region_key,
           "Вид деятельности" as activity, "Роль" as role, rc.code as role_code,
           CASE "Плательщик НДС" WHEN 'Да' THEN true WHEN 'Нет' THEN false END as vat_payer,
           source_file, source_row
    from raw_participants p join region_spellings rs on rs.raw = p."Регион"
    join codes rc on rc.domain = 'role' and rc.raw = p."Роль" """)
    con.execute("alter table participants add primary key (inn)")

    con.execute(f"""
    create table contracts as
    select "Номер контракта" as contract_id, "Наименование продукта" as product_name,
           "Марка товара" as brand, "Размер товара" as size, "Сорт товара" as grade,
           "Тип товара" as product_type, "Класс товара" as product_class, "Код ТН ВЭД" as tnved_code,
           "Группа товара" as product_group, "Категория товара" as product_category,
           "Отрасль промышленности" as industry, "Единица измерения" as quantity_unit,
           CAST({_clean('"Лот"')} AS INTEGER) as lot_size, "Валюта" as currency_name, cur.code as currency,
           CAST({_clean('"Базисная цена"')} AS DECIMAL(18,2)) as base_price,
           CAST(trim(replace("Сумма залога", '%', '')) AS INTEGER) as deposit_pct,
           "Базис поставки" as delivery_basis, "Регион" as warehouse_region, rs.region_key as warehouse_region_key,
           "Адрес склада" as warehouse_address, "Тип торгов" as trade_type, "Тип контракта" as contract_type,
           "Форма контракта" as contract_form,
           CAST({_clean('"Срок оплаты"')} AS INTEGER) as payment_term_days,
           CAST({_clean('"Срок поставки"')} AS INTEGER) as delivery_term_days,
           "Упаковка" as packaging, "ИНН продавца" as seller_inn,
           {_date('"Дата регистрации"')} as registered_on, {_date('"Действует до"')} as valid_until,
           source_file, source_row
    from raw_contracts c join region_spellings rs on rs.raw = c."Регион"
    join codes cur on cur.domain = 'currency' and cur.raw = c."Валюта" """)
    con.execute("alter table contracts add primary key (contract_id)")

    con.execute(f"""
    create table deals as
    select "Номер сделки" as deal_id, {_date('"Дата сделки"')} as deal_date,
           CAST("Время сделки" AS TIME) as deal_time, "Тип торгов" as trade_type,
           CAST({_clean('"Количество"')} AS BIGINT) as lot_count,
           CAST({_clean('"Сумма сделки"')} AS DECIMAL(18,2)) as raw_amount,
           CAST({_clean('"Стартовая цена"')} AS DECIMAL(18,2)) as start_price,
           "Номер контракта" as contract_id, "Наименование товара" as deal_product_name,
           CAST({_clean('"Лот"')} AS INTEGER) as lot_size, "Единица измерения" as quantity_unit,
           "Регион" as warehouse_region, rs.region_key as warehouse_region_key,
           "Тип контракта" as contract_type, "Форма контракта" as contract_form, "Состояние сделки" as status,
           "ИНН продавца" as seller_inn, "ИНН покупателя" as buyer_inn,
           "Брокер продавца" as seller_broker_code, "Брокер покупателя" as buyer_broker_code,
           {_date('"Дата оплаты"')} as payment_date, {_date('"Дата поставки"')} as delivery_date,
           CAST({_clean('"Поставлено"')} AS BIGINT) as delivered_lots,
           source_file, source_row
    from raw_deals d join region_spellings rs on rs.raw = d."Регион" """)
    con.execute("alter table deals add primary key (deal_id)")


def _create_product_catalog(con: duckdb.DuckDBPyConnection, v: "_Validator") -> None:
    """Commodity/variant per contract (src/uzex_assistant/products.py). Every contract must map."""
    con.execute("create table commodities (commodity_key VARCHAR PRIMARY KEY, name_ru VARCHAR, tnved_code VARCHAR, aliases VARCHAR)")
    con.executemany("insert into commodities values (?, ?, ?, ?)",
                    [(k, ru, code, json.dumps(al, ensure_ascii=False)) for k, (code, _kw, ru, al) in COMMODITIES.items()])
    rows = con.execute("select contract_id, tnved_code, product_name, brand, size, grade, product_type, product_class, "
                       "quantity_unit from contracts order by contract_id").fetchall()
    brands: dict[str, set[str]] = {}
    for r in rows:
        ck = commodity_of(r[1], r[2])
        if ck and r[3]:
            brands.setdefault(ck, set()).add(compact(r[3]))
    out, unmapped = [], []
    for r in rows:
        var = variant_of(r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], brands.get(commodity_of(r[1], r[2]) or "", set()))
        if var is None:
            unmapped.append((r[0], r[1], r[2]))
            continue
        out.append((r[0], var.commodity_key, COMMODITIES[var.commodity_key][2], var.key, var.descriptor,
                    var.brand, var.grade, var.product_class, var.size))
    if unmapped:
        v.problems.append(f"contracts without a commodity mapping: {len(unmapped)}; samples: {unmapped[:5]}")
        v.raise_if_problems()
    con.execute("create table product_catalog (contract_id VARCHAR PRIMARY KEY, commodity_key VARCHAR, commodity_name_ru VARCHAR, "
                "variant_key VARCHAR, variant_descriptor VARCHAR, brand_norm VARCHAR, grade_norm VARCHAR, class_norm VARCHAR, size_norm VARCHAR)")
    con.executemany("insert into product_catalog values (?, ?, ?, ?, ?, ?, ?, ?, ?)", out)


def _create_views(con: duckdb.DuckDBPyConnection, as_of: dt.date) -> None:
    as_of_sql = f"DATE '{as_of.isoformat()}'"
    # Exact unit price: DuckDB division on DECIMAL yields DOUBLE, so it is computed with integer
    # arithmetic: round_half_up(amount_minor / quantity) at 6 decimals. All operands are > 0
    # (checked above), so (2n + d) // (2d) is half-up rounding. Max numerator for raw_amount:
    # 3.02e10 * 10^2 (cents) * 10^4 * 2 = 6.0e16 < 9.2e18 (BIGINT); HUGEINT is used anyway.
    con.execute(f"""
    create view deal_facts as
    with b as (select
      d.deal_id, d.deal_date, d.deal_time, d.deal_date + d.deal_time as deal_ts,
      year(d.deal_date) as deal_year, month(d.deal_date) as deal_month, quarter(d.deal_date) as deal_quarter,
      d.trade_type, tt.code as trade_type_code, d.contract_type, ct.code as contract_type_code,
      d.contract_form, cf.code as contract_form_code, d.status, st.status_code,
      d.contract_id, d.deal_product_name, c.product_name, c.brand, c.size, c.grade, c.product_type,
      c.product_class, c.tnved_code, c.product_group, c.product_category, c.industry,
      c.currency, c.currency_name, d.raw_amount,
      r.uzs_per_usd as usd_rate_on_deal_date,
      CASE WHEN c.currency = 'USD' THEN r.uzs_per_usd END as fx_rate_applied,
      (c.currency = 'USD') as is_converted_from_usd,
      CAST(CASE WHEN c.currency = 'USD' THEN CAST(d.raw_amount AS DECIMAL(38,2)) * r.uzs_per_usd
                ELSE d.raw_amount END AS DECIMAL(38,4)) as amount_uzs,
      d.start_price, c.base_price, c.deposit_pct,
      d.lot_size, d.lot_count, d.lot_count * d.lot_size as quantity_native,
      d.quantity_unit, u.unit_code as quantity_unit_code, u.dimension as quantity_dimension,
      d.lot_count * d.lot_size * u.factor_to_base as quantity_base, u.base_unit as quantity_base_unit,
      CAST(CAST((2 * CAST(d.raw_amount * 100 AS HUGEINT) * 10000 + d.lot_count * d.lot_size)
                // (2 * d.lot_count * d.lot_size) AS DECIMAL(38,0)) * 0.000001 AS DECIMAL(38,6)) as unit_price,
      d.delivered_lots, d.delivered_lots * d.lot_size as delivered_quantity,
      CASE WHEN NOT st.is_cancelled THEN d.lot_count - coalesce(d.delivered_lots, 0) END as remaining_lots,
      CASE WHEN NOT st.is_cancelled THEN (d.lot_count - coalesce(d.delivered_lots, 0)) * d.lot_size END as remaining_quantity,
      d.payment_date, d.delivery_date, c.payment_term_days, c.delivery_term_days,
      d.deal_date + c.payment_term_days as payment_due_date,
      d.payment_date + c.delivery_term_days as delivery_due_date,
      st.is_paid, st.is_cancelled, st.is_fully_delivered,
      (NOT st.is_paid AND NOT st.is_cancelled AND d.deal_date + c.payment_term_days < {as_of_sql}) as is_payment_overdue,
      (st.is_paid AND NOT st.is_fully_delivered AND d.lot_count - coalesce(d.delivered_lots, 0) > 0
         AND d.payment_date + c.delivery_term_days < {as_of_sql}) as is_delivery_overdue,
      (d.delivery_date > d.payment_date + c.delivery_term_days) as is_delivery_late,
      d.seller_inn, sp.name as seller_name, sp.region as seller_region, sp.region_key as seller_region_key,
      d.buyer_inn, bp.name as buyer_name, bp.region as buyer_region, bp.region_key as buyer_region_key,
      d.warehouse_region, d.warehouse_region_key, c.warehouse_address,
      d.seller_broker_code, sb.name as seller_broker_name, d.buyer_broker_code, bb.name as buyer_broker_name,
      c.registered_on as contract_registered_on, c.valid_until as contract_valid_until,
      c.delivery_basis, c.packaging,
      pc.commodity_key, pc.commodity_name_ru, pc.variant_key as product_variant_key, pc.variant_descriptor,
      pc.brand_norm, pc.grade_norm, pc.class_norm, pc.size_norm,
      sp.activity as seller_activity, sp.vat_payer as seller_vat_payer,
      bp.activity as buyer_activity, bp.vat_payer as buyer_vat_payer,
      sb.region_key as seller_broker_region_key, bb.region_key as buyer_broker_region_key,
      d.source_file, d.source_row
    from deals d
    left join product_catalog pc on pc.contract_id = d.contract_id
    left join contracts c on c.contract_id = d.contract_id
    left join statuses st on st.status = d.status
    left join units u on u.unit = d.quantity_unit
    left join codes tt on tt.domain = 'trade_type' and tt.raw = d.trade_type
    left join codes ct on ct.domain = 'contract_type' and ct.raw = d.contract_type
    left join codes cf on cf.domain = 'contract_form' and cf.raw = d.contract_form
    left join participants sp on sp.inn = d.seller_inn
    left join participants bp on bp.inn = d.buyer_inn
    left join brokers sb on sb.broker_code = d.seller_broker_code
    left join brokers bb on bb.broker_code = d.buyer_broker_code
    left join usd_rates r on r.rate_date = d.deal_date)
    select *,
      -- same exact half-up method on amount_uzs (scale 4 -> integer count of 10^-4 UZS)
      CAST(CAST((2 * CAST(amount_uzs * 10000 AS HUGEINT) * 100 + quantity_native) // (2 * quantity_native)
           AS DECIMAL(38,0)) * 0.000001 AS DECIMAL(38,6)) as unit_price_uzs
    from b
    """)


# --------------------------------------------------------------------------- read-only access

class StaleDatabase(RuntimeError):
    pass


def require_database(data_dir: Path | None = None, db_path: Path | None = None, as_of: dt.date | None = None) -> dict:
    """Runtime check (web, bot): the prepared DB must exist and match the sources. Never imports unless
    UZEX_RUNTIME_IMPORT=true. If the source CSVs are not mounted, only the DB's own identity is checked."""
    st = get_settings()
    data_dir, db_path, as_of = Path(data_dir or st.data_dir), Path(db_path or st.db_path), as_of or st.as_of
    if st.runtime_import:
        return ensure_database(data_dir, db_path, as_of).identity
    have = read_identity(db_path)
    if have is None:
        raise StaleDatabase(f"prepared database missing or unreadable: {db_path}. Run the one-time import: "
                            "python -m uzex_assistant.importer ensure")
    if data_dir.is_dir() and any(data_dir.glob("*.csv")):
        sources = discover_sources(data_dir)
        fingerprint(sources)
        if identity_of(sources, as_of) != have:
            raise StaleDatabase("prepared database does not match the source files / as-of / schema version. "
                                "The runtime does not re-import; run: python -m uzex_assistant.importer ensure")
    return have


def connect_readonly(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    path = Path(db_path or get_settings().db_path)
    if not path.is_file():
        raise FileNotFoundError(f"prepared database not found: {path} (run: python -m uzex_assistant.importer ensure)")
    return duckdb.connect(str(path), read_only=True)


# --------------------------------------------------------------------------- CLI

def _freeze_block(st, db_path: Path, wanted: dict) -> str | None:
    """Import CLI only: after the freeze, (re)building is refused unless it reproduces the frozen sources."""
    if st.allow_after_freeze:
        return None
    ff = Path(st.freeze_path or Path(db_path).parent / "FREEZE.json")
    frozen = json.loads(ff.read_text(encoding="utf-8")).get("db_identity") if ff.is_file() else None
    if frozen is not None:
        if frozen == wanted:
            return None  # e.g. disaster recovery: rebuilding the identical frozen database is allowed
        return ("refused: the source files differ from the frozen submission (FREEZE.json). "
                "Data changes after the freeze need organizer approval (override: UZEX_ALLOW_AFTER_FREEZE=true).")
    if dt.datetime.now(dt.timezone.utc) >= st.freeze_at:
        return (f"refused: after the submission freeze ({st.freeze_at.isoformat()}) no new ingestion is done "
                "automatically. Restore the frozen data/db backup, or get organizer approval and set "
                "UZEX_ALLOW_AFTER_FREEZE=true for a one-off manual import.")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m uzex_assistant.importer")
    ap.add_argument("command", choices=["build", "ensure", "status"])
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--db-path", type=Path)
    a = ap.parse_args(argv)
    st = get_settings()
    data_dir, db_path = a.data_dir or st.data_dir, a.db_path or st.db_path
    try:
        if a.command == "status":
            sources = discover_sources(data_dir)
            fingerprint(sources)
            wanted, have = identity_of(sources, st.as_of), read_identity(db_path)
            print(f"db: {db_path} exists={Path(db_path).is_file()} up_to_date={wanted == have}")
            return 0 if wanted == have else 3
        sources = discover_sources(data_dir)
        fingerprint(sources)
        wanted = identity_of(sources, st.as_of)
        needs = a.command == "build" or read_identity(db_path) != wanted
        blocked = _freeze_block(st, db_path, wanted) if needs else None
        if blocked:
            print(blocked, file=sys.stderr)
            return 4
        rep = ensure_database(data_dir, db_path, st.as_of, force=(a.command == "build"))
    except ImportFailure as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"db: {rep.db_path} rebuilt={rep.rebuilt} seconds={rep.seconds}")
    for k, n in rep.counts.items():
        print(f"  {k}: {n}")
    failed = [c for c in rep.checks if not c[3]]
    if rep.rebuilt:
        print(f"  checks: {len(rep.checks)} run, {len(failed)} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
