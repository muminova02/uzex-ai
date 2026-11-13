"""Product catalog: commodities and variants (verified against contracts.csv, docs/data_audit.md A10).

Commodity = one physical commodity. A TN VED code identifies it, and for the three TN VED codes
that cover several commodities in this data (230990, 252210, 271019) the product-name keyword
decides. These are the only "synonyms" applied, and every rule was checked against the full list
of 230 raw product names. Similar names are NOT merged beyond that.

Variant = commodity + descriptor (remaining name words, word order ignored) + brand + grade +
class + size + unit. Variants with different brand, grade, class, size, descriptor or unit are
never merged. They can be counted together within one commodity scope, but prices and quantities
are reported per variant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .normalize import norm

# key: (tnved, keyword stem required in normalized name or None, ru display, [aliases: ru/uz/en])
COMMODITIES: dict[str, tuple[str, str | None, str, list[str]]] = {
    "wheat": ("100199", None, "Пшеница", ["пшеница", "bug'doy", "bugdoy", "wheat"]),
    "rice": ("100630", None, "Рис", ["рис", "guruch", "rice"]),
    "flour": ("110100", None, "Мука", ["мука", "un", "flour"]),
    "sunflower_oil": ("151219", None, "Масло подсолнечное", ["масло подсолнечное", "kungaboqar yog'i", "sunflower oil"]),
    "cottonseed_oil": ("151229", None, "Масло хлопковое", ["масло хлопковое", "paxta yog'i", "cottonseed oil"]),
    "sugar": ("170199", None, "Сахар", ["сахар", "shakar", "sugar"]),
    "drinking_water": ("220190", None, "Вода питьевая", ["вода питьевая", "ichimlik suvi", "drinking water"]),
    "cotton_meal": ("230610", None, "Шрот хлопковый", ["шрот", "kunjara", "shrot", "cottonseed meal"]),
    "compound_feed": ("230990", "комбикорм", "Комбикорм", ["комбикорм", "omuxta yem", "compound feed"]),
    "feed_mix": ("230990", "кормосмес", "Кормосмесь", ["кормосмесь", "yem aralashmasi", "feed mix"]),
    "salt": ("250100", None, "Соль пищевая", ["соль", "tuz", "osh tuzi", "salt"]),
    "gypsum": ("252020", None, "Гипс", ["гипс", "gips", "gypsum"]),
    "quicklime": ("252210", "негашен", "Известь негашеная", ["известь негашеная", "so'ndirilmagan ohak", "quicklime"]),
    "building_lime": ("252210", "строител", "Известь строительная", ["известь строительная", "qurilish ohagi", "building lime"]),
    "cement": ("252329", None, "Портландцемент", ["цемент", "портландцемент", "sement", "cement"]),
    "gasoline": ("271012", None, "Бензин", ["бензин", "benzin", "gasoline", "petrol"]),
    "diesel": ("271019", "дизел", "Дизельное топливо", ["дизельное топливо", "дизель", "dizel", "dizel yoqilg'isi", "diesel"]),
    "fuel_oil": ("271019", "мазут", "Мазут", ["мазут", "mazut", "fuel oil"]),
    "lpg": ("271112", None, "Сжиженный газ", ["сжиженный газ", "пропан бутан", "suyultirilgan gaz", "suyultirilgan", "lpg"]),
    "bitumen": ("271320", None, "Битум", ["битум", "bitum", "bitumen"]),
    "argon": ("280421", None, "Аргон газообразный", ["аргон", "argon"]),
    "nitrogen": ("280430", None, "Азот", ["азот", "azot", "nitrogen"]),
    "sulfuric_acid": ("280700", None, "Кислота серная", ["серная кислота", "sulfat kislota", "sulfuric acid"]),
    "caustic_soda": ("281511", None, "Сода каустическая", ["сода каустическая", "kaustik soda", "caustic soda"]),
    "urea": ("310210", None, "Карбамид", ["карбамид", "мочевина", "karbamid", "mochevina", "urea"]),
    "ammonium_nitrate": ("310230", None, "Аммиачная селитра", ["аммиачная селитра", "селитра", "ammiakli selitra", "selitra", "ammonium nitrate"]),
    "enamel": ("320810", None, "Эмаль", ["эмаль", "emal", "enamel"]),
    "polyethylene": ("390120", None, "Полиэтилен", ["полиэтилен", "polietilen", "polyethylene"]),
    "polypropylene": ("390210", None, "Полипропилен", ["полипропилен", "polipropilen", "polypropylene"]),
    "pvc": ("390410", None, "Поливинилхлорид", ["поливинилхлорид", "пвх", "polivinilxlorid", "pvx", "pvc"]),
    "pe_pipe": ("391721", None, "Трубы полиэтиленовые", ["трубы полиэтиленовые", "труба пэ", "polietilen quvur", "quvur", "pe pipe"]),
    "lumber": ("440711", None, "Пиломатериалы", ["пиломатериал", "yog'och", "taxta", "lumber"]),
    "raw_cotton": ("520100", None, "Хлопок-сырец", ["хлопок сырец", "хлопок", "paxta xom ashyosi", "paxta", "raw cotton"]),
    "cotton_yarn": ("520512", None, "Пряжа хлопчатобумажная", ["пряжа", "ip kalava", "ip", "yarn"]),
    "brick": ("690410", None, "Кирпич", ["кирпич", "g'isht", "brick"]),
    "glass": ("700529", None, "Стекло", ["стекло", "shisha", "oyna", "glass"]),
    "steel_sheet": ("720853", None, "Лист стальной горячекатаный", ["лист стальной", "po'lat list", "steel sheet"]),
    "rebar": ("721420", None, "Арматура", ["арматура", "armatura", "rebar"]),
    "rebar_wire": ("721710", None, "Проволока арматурная", ["проволока", "sim", "wire"]),
    "copper_cathode": ("740311", None, "Катод медный", ["катод медный", "медь", "mis katod", "mis", "copper cathode"]),
    "aluminium_profile": ("760421", None, "Профиль алюминиевый", ["профиль алюминиевый", "alyuminiy profil", "aluminium profile"]),
    "bearing": ("848210", None, "Подшипник", ["подшипник", "podshipnik", "bearing"]),
    "power_cable": ("854449", None, "Кабель силовой", ["кабель", "kabel", "cable"]),
}

ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
ORDINAL = {"первый": "1", "первого": "1", "второй": "2", "второго": "2", "третий": "3", "третьего": "3"}
_GRADE_RE = re.compile(r"(?:^|\s)(в/с|высш\w*(?:\s+сорт\w*)?|(?:первый|второй|третий|первого|второго|третьего|[1-4]|i{1,3}|iv)\s+сорт\w*)(?=\s|$)")
_CLASS_RE = re.compile(r"(?:^|\s)([1-4]|i{1,3}|iv)\s+класс\w*(?=\s|$)")
_SIZE_RE = re.compile(r"(?:^|\s)(ø\s*\d+(?:,\d+)?\s*мм|\d+(?:,\d+)?(?:\s*х\s*\d+(?:,\d+)?)*\s*мм)(?=\s|$)")


def _grade(text: str) -> str | None:
    m = _GRADE_RE.search(text)
    if not m:
        return None
    g = m.group(1)
    if g.startswith("высш") or g == "в/с":
        return "высший"
    first = g.split()[0]
    return ORDINAL.get(first) or ROMAN.get(first) or first


def normalize_grade(raw: str | None) -> str | None:
    if not raw:
        return None
    t = norm(raw)
    if t in ("в/с",) or t.startswith("высш"):
        return "высший"
    return _grade(t if "сорт" in t else t + " сорт")


def _class(text: str) -> str | None:
    m = _CLASS_RE.search(text)
    return (ROMAN.get(m.group(1)) or m.group(1)) if m else None


def normalize_class(raw: str | None) -> str | None:
    if not raw:
        return None
    t = norm(raw)
    return _class(t if "класс" in t else t + " класс")


def compact(raw: str | None) -> str | None:
    """Brand/size normalization: casefold, Latin look-alikes → Cyrillic, spaces/dashes removed."""
    if not raw:
        return None
    s = norm(raw).replace("x", "х").replace(" ", "")
    return s or None


def _strip(text: str, pattern: re.Pattern) -> str:
    return re.sub(r"\s+", " ", pattern.sub(" ", text)).strip()


@dataclass(frozen=True)
class Variant:
    commodity_key: str
    descriptor: str
    brand: str | None
    grade: str | None
    product_class: str | None
    size: str | None
    unit: str

    @property
    def key(self) -> str:
        return "|".join([self.commodity_key, self.descriptor, self.brand or "", self.grade or "",
                         self.product_class or "", self.size or "", self.unit])


def commodity_of(tnved: str, product_name: str) -> str | None:
    n = norm(product_name)
    hits = [k for k, (code, kw, _, _) in COMMODITIES.items() if code == tnved and (kw is None or kw in n)]
    return hits[0] if len(hits) == 1 else None


def variant_of(tnved: str, product_name: str, brand: str | None, size: str | None, grade: str | None,
               product_type: str | None, product_class: str | None, unit: str,
               known_brands: set[str]) -> Variant | None:
    ck = commodity_of(tnved, product_name)
    if ck is None:
        return None
    n = norm(product_name)
    g = normalize_grade(grade) or _grade(n)
    c = normalize_class(product_class) or _class(n)
    sz = compact(size)
    m = _SIZE_RE.search(n)
    if not sz and m:
        sz = compact(m.group(1))
    rest = _strip(_strip(_strip(n, _GRADE_RE), _CLASS_RE), _SIZE_RE)
    b = compact(brand)
    if not b:  # brand written inside the name (e.g. "Бензин автомобильный АИ-92")
        for kb in sorted(known_brands, key=len, reverse=True):
            if kb and kb in rest.replace(" ", ""):
                b = kb
                break
    if b:
        # drop the brand words from the descriptor
        words = rest.split()
        for i in range(len(words)):
            for j in range(len(words), i, -1):
                if "".join(words[i:j]).replace("x", "х") == b:
                    words = words[:i] + words[j:]
                    break
            else:
                continue
            break
        rest = " ".join(words)
    if product_type:
        rest = rest + " " + norm(product_type)
    descriptor = " ".join(sorted(set(rest.split())))
    return Variant(ck, descriptor, b, g, c, sz, unit)
