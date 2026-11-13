"""Explicit unit dictionary (raw `Единица измерения` values verified in the audit, A3).

Quantities may be summed only within one ``dimension`` AND at compatible product
granularity (same normalized product). ``factor_to_base`` converts to ``base_unit``.
"""
from __future__ import annotations

# raw unit -> (code, dimension, base_unit, factor_to_base)
UNITS: dict[str, tuple[str, str, str, int]] = {
    "тонна": ("t", "mass", "kg", 1000),
    "килограмм": ("kg", "mass", "kg", 1),
    "литр": ("l", "volume", "l", 1),
    "куб.метр": ("m3", "volume", "l", 1000),
    "кв.метр": ("m2", "area", "m2", 1),
    "метр": ("m", "length", "m", 1),
    "штука": ("pcs", "count", "pcs", 1),
}
