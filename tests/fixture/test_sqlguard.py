"""SQL guard: rejected and accepted queries (allowlist, scopes, statements, functions)."""
import pytest

from uzex_assistant.sqlguard import SQLGuard, SQLRejected

SCHEMA = {
    "deal_facts": {"deal_id", "deal_date", "seller_inn", "buyer_inn", "amount_uzs", "status_code", "deal_year"},
    "participants": {"inn", "name", "region_key"},
    "brokers": {"broker_code", "name"},
}
G = SQLGuard(SCHEMA)

REJECT = [
    ("attach 'x.db' as y", "forbidden_keyword"),
    ("install httpfs", "forbidden_keyword"),
    ("load httpfs", "forbidden_keyword"),
    ("pragma version", "forbidden_keyword"),
    ("set threads = 1", "forbidden_keyword"),
    ("copy deal_facts to 'x.csv'", "forbidden_keyword"),
    ("insert into deal_facts values (1)", "forbidden_keyword"),
    ("create table x as select 1", "forbidden_keyword"),
    ("delete from deal_facts", "forbidden_keyword"),
    ("update deal_facts set deal_id = '1'", "forbidden_keyword"),
    ("drop table deal_facts", "forbidden_keyword"),
    ("describe deal_facts", "forbidden_keyword"),
    ("summarize deal_facts", "forbidden_keyword"),
    ("explain select 1", "forbidden_keyword"),
    ("call pragma_version()", "forbidden_keyword"),
    ("select 1; select 2", "statement_count"),
    ("select deal_id from deal_facts; drop table deal_facts", "statement_count"),
    ("select * from read_csv('a.csv')", "forbidden_name"),
    ("select * from read_parquet('a.parquet')", "forbidden_name"),
    ("select * from glob('*')", "table_function"),
    ("select * from 'file.csv'", "table_not_allowed"),
    ("select * from duckdb_tables()", "forbidden_name"),
    ("select * from duckdb_settings", "forbidden_name"),
    ("select * from information_schema.tables", "forbidden_name"),
    ("select * from pg_catalog.pg_class", "forbidden_name"),
    ("select * from main.deal_facts", "qualified_table"),
    ("select * from deals", "table_not_allowed"),
    ("select secret_col from deal_facts", "unknown_column"),
    ("select x.deal_id from deal_facts f", "unknown_table_alias"),
    ("select getenv('HOME')", "function_not_allowed"),
    ("select current_setting('threads')", "function_not_allowed"),
    ("select * from deal_facts tablesample 10%", "forbidden_keyword"),
    ("with recursive r as (select 1) select * from r", "forbidden_keyword"),
    ("with deal_facts as (select 1 as a) select * from deal_facts", "cte_name_not_allowed"),
    ("with b as (select * from a), a as (select deal_id from deal_facts) select * from b", "table_not_allowed"),
    ("select * from (with a as (select 1 as x) select x from a) s, a", "table_not_allowed"),
    ("select deal_id from deal_facts union select inn from secret_table", "table_not_allowed"),
    ("select * from deal_facts where deal_id in (select deal_id from read_csv('x'))", "forbidden_name"),
    ("with a as (select * from duckdb_settings()) select * from a", "forbidden_name"),
    ("select * from deal_facts where deal_id = $1", "named_placeholder"),
    ("select * from deal_facts where deal_id = $id", "named_placeholder"),
    ("select * from deal_facts, lateral (select 1)", "forbidden_keyword"),
    ("select list(deal_id) from deal_facts", "function_not_allowed"),
    ("select [1, 2]", "function_not_allowed"),
    ("", "empty"),
    ("select" + " 1," * 5000 + " 1", "too_long"),
]


@pytest.mark.parametrize("sql, reason", REJECT)
def test_rejected(sql, reason):
    with pytest.raises(SQLRejected) as e:
        G.validate(sql, [])
    assert e.value.reason == reason, (sql, e.value)


def test_placeholder_count_must_match():
    with pytest.raises(SQLRejected, match="param_count"):
        G.validate("select count(*) from deal_facts where deal_year = ?", [])
    with pytest.raises(SQLRejected, match="param_type"):
        G.validate("select count(*) from deal_facts where deal_year = ?", [object()])


ACCEPT = [
    ("select count(*) from deal_facts where deal_year = ?", [2025]),
    ("select count(*) filter (where status_code in (?, ?)) as n from deal_facts", ["paid", "fulfilled"]),
    ("with a as (select deal_id, seller_inn from deal_facts), b as (select * from a) select b.seller_inn, count(*) as n "
     "from b join participants p on p.inn = b.seller_inn group by 1 order by n desc limit 5", []),
    ("with a as (select deal_id from deal_facts) select * from deal_facts where deal_id in (select deal_id from a)", []),
    ("select seller_inn as x from deal_facts union all select buyer_inn from deal_facts", []),
    ("select f.deal_id from deal_facts f where exists (select 1 from participants p where p.inn = f.buyer_inn)", []),
    ("select deal_year, sum(amount_uzs) as v, count(*) over () as g from deal_facts group by deal_year order by v desc", []),
    ("select name from participants where lower(name) like ? limit 3", ["%plast%"]),
    ("select 1 -- a comment; not a second statement", []),
]


@pytest.mark.parametrize("sql, params", ACCEPT)
def test_accepted(sql, params):
    r = G.validate(sql, params)
    assert r.tables <= set(SCHEMA)


def test_string_literals_are_not_keywords():
    # keywords inside string values are data, not statements
    assert G.validate("select count(*) from deal_facts where status_code = 'attach; drop'", [])
