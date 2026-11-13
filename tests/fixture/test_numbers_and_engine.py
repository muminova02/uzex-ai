"""Exact-number helpers and the DuckDB type behaviour the design relies on (DuckDB 2.0.0)."""
from decimal import Decimal

import duckdb
import pytest

from uzex_assistant.numbers import exact_ratio, percent


def test_exact_ratio_half_up():
    assert exact_ratio(Decimal("203456000.00"), 22000) == Decimal("9248.00")
    assert exact_ratio(Decimal("27000.50"), 3, 6) == Decimal("9000.166667")
    assert exact_ratio(5, 2, 0) == Decimal("3")
    assert percent(33719, 500000) == Decimal("6.74")
    with pytest.raises(ZeroDivisionError):
        exact_ratio(1, 0)


@pytest.fixture(scope="module")
def con():
    return duckdb.connect()


def test_decimal_division_returns_double(con):
    # Design assumption: never trust SQL division for reported ratios.
    assert con.execute("select typeof(1.00::DECIMAL(18,2) / 3::BIGINT)").fetchone()[0] == "DOUBLE"
    assert con.execute("select typeof(avg(1.00::DECIMAL(18,2)))").fetchone()[0] == "DOUBLE"


def test_decimal_cast_rounds_silently(con):
    # Why the importer validates the scale with a regex before casting.
    assert con.execute("select CAST('1.239' AS DECIMAL(18,2))").fetchone()[0] == Decimal("1.24")


def test_strptime_accepts_single_digit_day(con):
    # Why the importer checks DD.MM.YYYY with a regex as well.
    assert con.execute("select try_strptime('1.3.2025', '%d.%m.%Y') is not null").fetchone()[0]


def test_multiplication_precision(con):
    assert con.execute("select typeof(1.00::DECIMAL(18,2) * 1.00::DECIMAL(12,2))").fetchone()[0] == "DECIMAL(18,4)"
    assert con.execute("select typeof(1.00::DECIMAL(38,2) * 1.00::DECIMAL(12,2))").fetchone()[0] == "DECIMAL(38,4)"
    with pytest.raises(duckdb.OutOfRangeException):
        con.execute("select 99999999999999.99::DECIMAL(18,2) * 99999.99::DECIMAL(12,2)").fetchone()


def test_sum_of_decimal_stays_decimal(con):
    assert con.execute("select typeof(sum(x)) from (values (1.00::DECIMAL(18,2))) t(x)").fetchone()[0] == "DECIMAL(38,2)"
