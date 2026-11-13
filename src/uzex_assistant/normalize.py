"""Text normalization for matching (never for display: original names and codes are kept).

* ``norm`` – Unicode NFKC, casefold, ё→е, apostrophe/quote/dash variants unified, punctuation
  to spaces (decimal commas inside numbers are kept), whitespace squeezed.
* ``skeleton`` – ``norm`` + Uzbek/Russian Cyrillic → Latin transliteration, apostrophes and
  legal-form words removed. Used for company/broker/region matching across scripts.
* ``stem_match`` – conservative prefix match for Russian/Uzbek inflection (хлопка ~ хлопок).
"""
from __future__ import annotations

import re
import unicodedata

APOSTROPHES = "'‘’ʻʼ`´ʹ′"  # all become ASCII '
QUOTES = "\"«»“”„‟"
DASHES = "-‐‑‒–—―−"

_CYR2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ғ": "g'", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "қ": "q", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ў": "o'", "ф": "f", "х": "x", "ҳ": "h", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "sh", "ъ": "'", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Legal-form words removed for company matching only (Uzbek Latin/Cyrillic, Russian).
LEGAL_FORMS = {
    "mchj", "мчж", "ooo", "ооо", "aj", "аж", "xk", "хк", "ok", "ок", "qk", "қк", "oaj", "оаж", "ao", "ао",
    "ap", "ип", "yatt", "ятт", "xususiy", "korxona", "корхона", "mas'uliyati", "masuliyati", "cheklangan",
    "jamiyati", "масъулияти", "чекланган", "жамияти", "aksiyadorlik", "акциядорлик", "zao", "зао", "oao",
    "оао", "pao", "пао", "ooo.", "llc", "ltd", "jsc", "unitar", "korxonasi", "фермер", "fermer", "xo'jaligi",
    "dehqon", "uk", "ук", "mchj.", "qo'shma", "korxonasi",
}


def norm(text: str) -> str:  # noqa: D103
    s = unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "е")
    for ch in APOSTROPHES:
        s = s.replace(ch, "'")
    for ch in QUOTES:
        s = s.replace(ch, " ")
    for ch in DASHES:
        s = s.replace(ch, " ")
    s = re.sub(r"(?<=\d),(?=\d)", "\u0001", s)  # protect decimal commas (32,5)
    s = re.sub(r"[(){}\[\];:!?.,№#*+=<>|\\]", " ", s)
    s = s.replace("\u0001", ",")
    return re.sub(r"\s+", " ", s).strip()


def translit(text: str) -> str:
    return "".join(_CYR2LAT.get(ch, ch) for ch in text)


def skeleton(text: str, drop_legal: bool = True) -> str:
    # transliterate ё before norm() folds it to е (Uzbek Cyrillic ё = Latin "yo")
    s = translit(norm(unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "yo")))
    # o' / g' are letters in Uzbek Latin; drop apostrophes so o'/o and g'/g compare equal
    s = s.replace("'", "")
    toks = [t for t in s.split() if not (drop_legal and t in _LEGAL_SKELETONS)]
    return " ".join(toks)


def tokens(text: str) -> list[str]:
    return norm(text).split()


def stem_match(query_tok: str, catalog_tok: str) -> bool:
    """Prefix match tolerant to inflectional endings; short catalog tokens need near-exact match."""
    q, c = query_tok, catalog_tok
    if q == c:
        return True
    if len(c) <= 3:
        return q.startswith(c) and len(q) - len(c) <= 2
    cp = 0
    for a, b in zip(q, c):
        if a != b:
            break
        cp += 1
    return cp >= max(3, len(c) - 2) and cp >= min(len(q), len(c)) - 2


_LEGAL_SKELETONS = {translit(norm(x)).replace("'", "") for x in LEGAL_FORMS}
