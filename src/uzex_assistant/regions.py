"""Canonical region dictionary.

Source of the two spellings that occur in the data (verified in docs/data_audit.md, A9):
- deals.csv `Регион`, participants.csv `Регион`, brokers.csv `Регион`: Uzbek Latin (``uz_latn``)
- contracts.csv `Регион`: Russian (``ru``)
``uz_cyrl`` does not occur in the dataset; it is kept only for matching user questions later.
Raw strings are kept byte-exact (note U+2018 in ``Farg‘ona``).
"""
from __future__ import annotations

# key: (uz_latn as in deals/participants, ru as in contracts, uz_cyrl for query matching)
REGIONS: dict[str, tuple[str, str, str]] = {
    "tashkent_city": ("Toshkent shahri", "г. Ташкент", "Тошкент шаҳри"),
    "tashkent_region": ("Toshkent viloyati", "Ташкентская область", "Тошкент вилояти"),
    "andijan": ("Andijon viloyati", "Андижанская область", "Андижон вилояти"),
    "bukhara": ("Buxoro viloyati", "Бухарская область", "Бухоро вилояти"),
    "fergana": ("Farg‘ona viloyati", "Ферганская область", "Фарғона вилояти"),
    "jizzakh": ("Jizzax viloyati", "Джизакская область", "Жиззах вилояти"),
    "khorezm": ("Xorazm viloyati", "Хорезмская область", "Хоразм вилояти"),
    "namangan": ("Namangan viloyati", "Наманганская область", "Наманган вилояти"),
    "navoi": ("Navoiy viloyati", "Навоийская область", "Навоий вилояти"),
    "kashkadarya": ("Qashqadaryo viloyati", "Кашкадарьинская область", "Қашқадарё вилояти"),
    "samarkand": ("Samarqand viloyati", "Самаркандская область", "Самарқанд вилояти"),
    "syrdarya": ("Sirdaryo viloyati", "Сырдарьинская область", "Сирдарё вилояти"),
    "surkhandarya": ("Surxondaryo viloyati", "Сурхандарьинская область", "Сурхондарё вилояти"),
    "karakalpakstan": ("Qoraqalpog‘iston Respublikasi", "Республика Каракалпакстан", "Қорақалпоғистон Республикаси"),
}


def raw_to_key() -> dict[str, str]:
    """Map every raw spelling (uz_latn and ru) that occurs in the data to its key."""
    out: dict[str, str] = {}
    for key, (latn, ru, _cyrl) in REGIONS.items():
        out[latn] = key
        out[ru] = key
    return out
