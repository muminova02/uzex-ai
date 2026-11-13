"""Deterministic language/script detection and Uzbek Latin → Cyrillic transliteration of templates.

The answer language follows the question, not the LLM's guess: Uzbek (Latin or Cyrillic) or Russian.
Only template text is transliterated; data values (company names, codes, IDs) are never changed.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

APOS = "'‘’ʻʼ`´"
UZ_CYRL_LETTERS = set("ўқғҳЎҚҒҲ")
UZ_LATN_WORDS = {
    "nechta", "qancha", "bitim", "bitimlar", "bitimlari", "yil", "yilda", "yilgi", "bo'yicha", "sotuvchi", "sotuvchilar",
    "xaridor", "xaridorlar", "kim", "eng", "qaysi", "qanday", "summasi", "hajmi", "hajm", "narxi", "o'rtacha", "va",
    "uchun", "bilan", "bo'lgan", "tuzilgan", "ko'rsat", "haqida", "ma'lumot", "nima", "jami", "necha", "oyda", "yilning",
    "viloyati", "shahri", "kompaniya", "bormi", "ber", "qilingan", "bekor", "yetkazib", "to'langan", "taqqosla", "solishtir",
    "mahsulot", "tovar", "miqdori", "dollarda", "so'mda", "ta", "yirik", "katta",
}
UZ_LATN_STEMS = ("bitim", "necht", "qanch", "yildagi", "yilgi", "yilda", "sotuvchi", "xaridor", "bajaril", "yetkaz", "to'la",
                 "bekor", "qisman", "faqat", "ko'rsat", "shartnoma", "hudud", "viloyat", "o'rtacha", "summa", "narx", "miqdor",
                 "tovar", "mahsulot", "taqqosla", "solishtir", "eng ", "kompaniya", "qaysi", "nimalar", "haqida", "bo'yicha")
EN_WORDS = {"the", "how", "many", "much", "what", "which", "show", "deals", "deal", "total", "top", "sellers", "buyers", "price",
            "average", "in", "of", "and", "for", "is", "are", "was", "were", "with"}
UZ_CYRL_WORDS = {"нечта", "қанча", "битим", "битимлар", "йил", "йилда", "бўйича", "сотувчи", "харидор", "ким", "энг",
                 "қайси", "суммаси", "ҳажми", "нархи", "ўртача", "ва", "учун", "билан", "бўлган", "тузилган", "ҳақида",
                 "маълумот", "жами", "вилояти", "шаҳри", "бекор", "қилинган", "тўланган", "ета"}
RU_WORDS = {"сколько", "сделок", "сделки", "сделка", "год", "году", "года", "какой", "какая", "какие", "кто", "что",
            "покажи", "сумма", "сумму", "объём", "объем", "цена", "средняя", "продавец", "покупатель", "сравни", "был",
            "было", "были", "заключено", "по", "за", "в", "и", "на", "из", "топ", "список", "крупнейших", "компании"}


@dataclass(frozen=True)
class Lang:
    lang: str  # "uz" | "ru"
    script: str  # "latn" | "cyrl"
    confident: bool
    reason: str


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).casefold()
    for a in APOS:
        s = s.replace(a, "'")
    return s


def detect(question: str, plan_language: str | None = None) -> Lang:
    s = _norm(question or "")
    words = re.findall(r"[\w']+", s)
    cyr = sum(1 for ch in s if "а" <= ch <= "я" or ch in "ёўқғҳ")
    lat = sum(1 for ch in s if "a" <= ch <= "z")
    if any(ch in UZ_CYRL_LETTERS for ch in question or ""):
        return Lang("uz", "cyrl", True, "uzbek_cyrillic_letters")
    uz_c = sum(w in UZ_CYRL_WORDS for w in words)
    ru = sum(w in RU_WORDS for w in words)
    uz_l = sum(w in UZ_LATN_WORDS or re.search(r"(o'|g')", w) is not None for w in words)
    if cyr >= lat:
        if uz_c > ru:
            return Lang("uz", "cyrl", True, "uzbek_cyrillic_words")
        if ru or cyr:
            return Lang("ru", "cyrl", ru > 0, "russian_words" if ru else "cyrillic_script")
    if uz_l:
        return Lang("uz", "latn", True, "uzbek_latin_words")
    if any(w.startswith(UZ_LATN_STEMS) for w in words):
        return Lang("uz", "latn", True, "uzbek_latin_stems")
    # The assistant serves Uzbek and Russian only: Latin-script text that is not English is Uzbek.
    if lat > cyr and not any(w in EN_WORDS for w in words):
        return Lang("uz", "latn", True, "latin_script_not_english")
    if plan_language in ("uz", "ru"):
        return Lang(plan_language, "latn" if plan_language == "uz" else "cyrl", False, "planner_language")
    return Lang("ru", "cyrl", False, "default")


_L2C = [
    ("o'", "ў"), ("g'", "ғ"), ("sh", "ш"), ("ch", "ч"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("ye", "е"),
    ("a", "а"), ("b", "б"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "ҳ"), ("i", "и"), ("j", "ж"),
    ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "қ"), ("r", "р"), ("s", "с"),
    ("t", "т"), ("u", "у"), ("v", "в"), ("x", "х"), ("y", "й"), ("z", "з"), ("'", "ъ"),
]


def latn_to_cyrl(text: str) -> str:
    """Uzbek Latin → Cyrillic for template text only. Segments inside ⟦…⟧ are data and kept verbatim."""
    out = []
    for i, part in enumerate(re.split(r"(⟦.*?⟧)", text)):
        if part.startswith("⟦") and part.endswith("⟧"):
            out.append(part[1:-1])
            continue
        s = part.replace("‘", "'").replace("’", "'").replace("ʻ", "'")
        res, j = [], 0
        while j < len(s):
            ch = s[j]
            low = s[j:j + 2].lower()
            hit = next(((a, c) for a, c in _L2C if len(a) == 2 and low == a), None)
            if hit is None and ch.lower() in {a for a, _ in _L2C if len(a) == 1}:
                hit = next((a, c) for a, c in _L2C if a == ch.lower())
            if hit is None:
                res.append(ch)
                j += 1
                continue
            a, c = hit
            seg = s[j:j + len(a)]
            res.append(c.upper() if seg[:1].isupper() else c)
            j += len(a)
        out.append("".join(res))
    return "".join(out)
