"""Exact numeric helpers.

DuckDB 2.0.0 returns DOUBLE for any division involving DECIMAL and for avg(DECIMAL)
(verified, docs/data_model.md §4). Ratios that are reported to users are therefore formatted
in Python from their exact numerator and denominator.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext


def exact_ratio(numerator: Decimal | int, denominator: Decimal | int, places: int = 2) -> Decimal:
    """numerator / denominator rounded half-up to ``places`` decimals, exactly."""
    num, den = Decimal(numerator), Decimal(denominator)
    if den == 0:
        raise ZeroDivisionError("denominator is zero")
    with localcontext() as ctx:
        ctx.prec = 80
        return (num / den).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def percent(part: int | Decimal, whole: int | Decimal, places: int = 2) -> Decimal:
    return exact_ratio(Decimal(part) * 100, whole, places)
