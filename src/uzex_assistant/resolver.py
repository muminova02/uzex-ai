"""Deterministic entity resolution against the real catalogs in the prepared DB.

Rules:
* Exact identifiers (INN, broker code, deal/contract number) resolve directly when they exist.
* Names are compared after normalization (normalize.skeleton: Unicode, case, apostrophes,
  Cyrillic→Latin, legal-form words). An exact skeleton match, or a single company whose name
  contains every token of the mention, resolves. Several matches mean ``ambiguous``. A fuzzy
  (similar) match is never accepted; it is only offered as a "did you mean" candidate.
* Products resolve to a commodity (products.py: verified TN VED + keyword rules), then to the
  variants matching the brand/grade/class/size/descriptor words given. Variants are never merged.
* "Ташкент"/"Toshkent" without "city"/"region" is ambiguous (city vs region).
"""
from __future__ import annotations

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import duckdb

from .normalize import norm, skeleton, stem_match, tokens
from .plan import EntityRef
from .products import _CLASS_RE, _GRADE_RE, _SIZE_RE, _class, _grade, compact, normalize_class, normalize_grade

MAX_CANDIDATES = 8
STOPWORDS = {"и", "по", "для", "на", "в", "с", "из", "of", "the", "va", "bo'yicha", "uchun", "марки", "марка",
             "сорт", "сорта", "класс", "класса", "товар", "товара", "продукт", "mahsulot", "tovar", "brand", "grade"}

REGION_STEMS: dict[str, list[str]] = {
    "andijan": ["andij", "andizh"], "bukhara": ["buxar", "buxor", "bukhar"], "fergana": ["fergan", "fargon", "ferghan"],
    "jizzakh": ["djizak", "jizza", "jizak", "dzhizak"], "khorezm": ["xorezm", "xorazm", "khorezm", "khorazm"],
    "namangan": ["namangan"], "navoi": ["navoi"], "kashkadarya": ["kashkadar", "qashqadar"],
    "samarkand": ["samarkand", "samarqand"], "syrdarya": ["sirdar", "syrdar"],
    "surkhandarya": ["surxandar", "surxondar", "surkhandar", "surkhondar"],
    "karakalpakstan": ["karakalpak", "qoraqalpog", "qoraqalpoq"],
}
TASHKENT_STEMS = ["toshkent", "tashkent"]
CITY_WORDS = {"shahri", "shahar", "shahrida", "shaxri", "shaxar", "gorod", "goroda", "g", "city", "stolitsa", "poytaxt"}
REGION_WORDS = {"viloyati", "viloyat", "viloyatida", "viloyatidagi", "oblast", "oblasti", "oblasty", "region", "oblastь"}


@dataclass
class Resolved:
    kind: str
    text: str
    role: str
    status: str  # resolved | ambiguous | not_found
    method: str = ""
    condition: str = ""  # SQL over deal_facts with ? placeholders (constant column names)
    params: list[Any] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    display: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    normalized: str = ""
    note: str = ""


class Catalog:
    """In-memory catalogs loaded once from the read-only DB."""

    def deal_ids_exist(self, did: str) -> bool:
        return did in self._deal_ids

    def contract_exists(self, cid: str) -> bool:
        return cid in self._contract_ids

    def __init__(self, con: duckdb.DuckDBPyConnection):
        q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
        self.participants = {r[0]: {"inn": r[0], "name": r[1], "role_code": r[2], "region_key": r[3]}
                             for r in q("select inn, name, role_code, region_key from participants")}
        self.brokers = {r[0]: {"broker_code": r[0], "name": r[1], "inn": r[2], "region_key": r[3]}
                        for r in q("select broker_code, name, inn, region_key from brokers")}
        self.broker_by_inn = {b["inn"]: code for code, b in self.brokers.items()}
        deal_counts = dict(q("select seller_inn, count(*) from deals group by 1") + q("select buyer_inn, count(*) from deals group by 1"))
        for inn, p in self.participants.items():
            p["deals"] = deal_counts.get(inn, 0)
        self.regions = {r[0]: {"region_key": r[0], "uz": r[1], "ru": r[2], "uz_cyrl": r[3]} for r in q("select * from regions")}
        self.commodities = {r[0]: {"key": r[0], "name_ru": r[1], "tnved": r[2], "aliases": __import__("json").loads(r[3])}
                            for r in q("select * from commodities")}
        self.variants: dict[str, dict] = {}
        for r in q("""select pc.variant_key, any_value(pc.commodity_key), any_value(pc.variant_descriptor), any_value(pc.brand_norm),
                             any_value(pc.grade_norm), any_value(pc.class_norm), any_value(pc.size_norm), any_value(c.quantity_unit),
                             count(distinct pc.contract_id), list(distinct c.product_name order by c.product_name)[1:3]
                      from product_catalog pc join contracts c using (contract_id) group by 1"""):
            self.variants[r[0]] = {"variant_key": r[0], "commodity_key": r[1], "descriptor": r[2], "brand": r[3], "grade": r[4],
                                   "class": r[5], "size": r[6], "unit": r[7], "contracts": r[8], "examples": r[9], "deals": 0}
        for vk, n, disp in q("select product_variant_key, count(*), mode(deal_product_name) from deal_facts group by 1"):
            self.variants[vk]["deals"] = n
            self.variants[vk]["display"] = disp  # most frequent original product string (verbatim)
        # original spelling of each normalized brand (most frequent in contracts), for display only
        from collections import Counter
        bc: dict[str, Counter] = defaultdict(Counter)
        for (b,) in q("select brand from contracts where brand is not null"):
            bc[compact(b)][b] += 1
        self.brand_original = {k: c.most_common(1)[0][0] for k, c in bc.items()}
        self.brands = defaultdict(set)
        for v in self.variants.values():
            if v["brand"]:
                self.brands[v["commodity_key"]].add(v["brand"])
        self._deal_ids = {r[0] for r in q("select deal_id from deals")}
        self._contract_ids = {r[0] for r in q("select contract_id from contracts")}
        self.filter_values = {
            "product_category": [r[0] for r in q("select distinct product_category from contracts order by 1")],
            "product_group": [r[0] for r in q("select distinct product_group from contracts order by 1")],
            "industry": [r[0] for r in q("select distinct industry from contracts order by 1")],
            "tnved_code": [r[0] for r in q("select distinct tnved_code from contracts order by 1")],
            "seller_activity": [r[0] for r in q("select distinct activity from participants order by 1")],
            "buyer_activity": [r[0] for r in q("select distinct activity from participants order by 1")],
            "delivery_basis": [r[0] for r in q("select distinct delivery_basis from contracts order by 1")],
        }
        # name indexes
        self._p_skel: dict[str, list[str]] = defaultdict(list)
        self._p_tok: dict[str, set[str]] = defaultdict(set)
        for inn, p in self.participants.items():
            sk = skeleton(p["name"])
            p["skeleton"] = sk
            self._p_skel[sk].append(inn)
            for t in sk.split():
                self._p_tok[t].add(inn)
        self._b_skel: dict[str, list[str]] = defaultdict(list)
        for code, b in self.brokers.items():
            b["skeleton"] = skeleton(b["name"])
            self._b_skel[b["skeleton"]].append(code)


class Resolver:
    def __init__(self, catalog: Catalog):
        self.c = catalog

    def resolve(self, e: EntityRef) -> Resolved:
        fn = {"product": self._product, "participant": self._participant, "broker": self._broker,
              "region": self._region, "deal": self._deal, "contract": self._contract}[e.kind]
        return fn(e)

    # ------------------------------------------------------------ ids
    @staticmethod
    def _digits(e: EntityRef, n: int | None = None) -> str | None:
        for s in (e.catalog_hint, e.text):
            if s:
                m = re.search(r"\b(\d{%s})\b" % (n if n else "1,9"), s)
                if m:
                    return m.group(1)
        return None

    def _deal(self, e: EntityRef) -> Resolved:
        did = self._digits(e)
        r = Resolved("deal", e.text, e.role, "not_found", "deal_id", normalized=did or "")
        if did and self.c.deal_ids_exist(did):
            r.status, r.condition, r.params, r.ids = "resolved", "deal_id = ?", [did], [did]
            r.display = [{"deal_id": did}]
        return r

    def _contract(self, e: EntityRef) -> Resolved:
        cid = self._digits(e)
        r = Resolved("contract", e.text, e.role, "not_found", "contract_id", normalized=cid or "")
        if cid and self.c.contract_exists(cid):
            r.status, r.condition, r.params, r.ids = "resolved", "contract_id = ?", [cid], [cid]
            r.display = [{"contract_id": cid}]
        return r

    # ------------------------------------------------------------ companies
    def _participant_condition(self, inn: str, role: str) -> tuple[str, str]:
        p = self.c.participants[inn]
        col_role = role if role in ("seller", "buyer") else p["role_code"]
        return (f"{col_role}_inn = ?", col_role)

    def _participant(self, e: EntityRef) -> Resolved:
        inn = self._digits(e, 9)
        if inn and inn in self.c.participants:
            return self._p_resolved(e, inn, "inn_exact", inn)
        if inn and inn in self.c.broker_by_inn:  # an INN of a broker, not a company
            alt = EntityRef(kind="broker", text=e.text, catalog_hint=inn, role="any_broker")
            r = self._broker(alt)
            r.note = "inn_belongs_to_broker"
            return r
        for text, method in ((e.text, "name_exact"), (e.catalog_hint, "hint_name_exact")):
            if not text:
                continue
            sk = skeleton(text)
            inns = self.c._p_skel.get(sk, [])
            if len(inns) == 1:
                return self._p_resolved(e, inns[0], method, sk)
            if len(inns) > 1:
                return self._p_ambiguous(e, inns, "same_normalized_name", sk)
        sk = skeleton(e.catalog_hint or e.text)
        toks = [t for t in sk.split() if len(t) >= 2]
        if toks:
            sets = [self.c._p_tok.get(t, set()) for t in toks]
            hit = set.intersection(*sets) if sets else set()
            if len(hit) == 1:
                return self._p_resolved(e, next(iter(hit)), "all_tokens_contained", sk)
            if len(hit) > 1:
                return self._p_ambiguous(e, list(hit), "several_names_contain_mention", sk)
        close = difflib.get_close_matches(sk, list(self.c._p_skel), n=5, cutoff=0.82)
        r = Resolved("participant", e.text, e.role, "not_found", "none", normalized=sk)
        if close:
            r.status, r.method = "ambiguous", "similar_names_only"
            r.candidates = [self._p_card(i) for s in close for i in self.c._p_skel[s]][:MAX_CANDIDATES]
        return r

    def _p_card(self, inn: str) -> dict:
        p = self.c.participants[inn]
        return {"inn": inn, "name": p["name"], "role": p["role_code"], "region_key": p["region_key"], "deals": p["deals"]}

    def _p_resolved(self, e, inn, method, normalized) -> Resolved:
        cond, col_role = self._participant_condition(inn, e.role)
        r = Resolved("participant", e.text, col_role, "resolved", method, cond, [inn], [inn],
                     [self._p_card(inn)], normalized=normalized)
        if e.role in ("seller", "buyer") and e.role != self.c.participants[inn]["role_code"]:
            r.note = f"company_role_is_{self.c.participants[inn]['role_code']}"
        return r

    def _p_ambiguous(self, e, inns, method, normalized) -> Resolved:
        cards = sorted((self._p_card(i) for i in inns), key=lambda c: (-c["deals"], c["inn"]))
        return Resolved("participant", e.text, e.role, "ambiguous", method, candidates=cards[:MAX_CANDIDATES],
                        normalized=normalized, note=f"{len(inns)} matches")

    def _broker(self, e: EntityRef) -> Resolved:
        cond_for = {"seller_broker": "seller_broker_code = ?", "buyer_broker": "buyer_broker_code = ?"}
        role = e.role if e.role in cond_for else "any_broker"

        def done(code, method, normalized):
            b = self.c.brokers[code]
            if role == "any_broker":
                cond, params = "(seller_broker_code = ? OR buyer_broker_code = ?)", [code, code]
            else:
                cond, params = cond_for[role], [code]
            return Resolved("broker", e.text, role, "resolved", method, cond, params, [code],
                            [{"broker_code": code, "name": b["name"], "inn": b["inn"]}], normalized=normalized)

        for s in (e.catalog_hint, e.text):
            m = re.search(r"\bbk\s?0*(\d{1,3})\b", norm(s or ""))
            if m:
                code = f"BK{int(m.group(1)):03d}"
                if code in self.c.brokers:
                    return done(code, "broker_code_exact", code)
        inn = self._digits(e, 9)
        if inn and inn in self.c.broker_by_inn:
            return done(self.c.broker_by_inn[inn], "broker_inn_exact", inn)
        for text in (e.text, e.catalog_hint):
            if not text:
                continue
            sk = skeleton(text)
            if len(self.c._b_skel.get(sk, [])) == 1:
                return done(self.c._b_skel[sk][0], "name_exact", sk)
            toks = [t for t in sk.split() if len(t) >= 2 and t not in ("broker", "brokerlik", "brokerskaya", "kontora", "idorasi")]
            hits = [code for code, b in self.c.brokers.items() if toks and all(t in b["skeleton"].split() for t in toks)]
            if len(hits) == 1:
                return done(hits[0], "all_tokens_contained", sk)
            if len(hits) > 1:
                return Resolved("broker", e.text, role, "ambiguous", "several_names_contain_mention",
                                candidates=[{"broker_code": h, "name": self.c.brokers[h]["name"]} for h in hits[:MAX_CANDIDATES]],
                                normalized=sk)
        sk = skeleton(e.catalog_hint or e.text)
        close = difflib.get_close_matches(sk, list(self.c._b_skel), n=5, cutoff=0.82)
        r = Resolved("broker", e.text, role, "not_found", "none", normalized=sk)
        if close:
            r.status, r.method = "ambiguous", "similar_names_only"
            r.candidates = [{"broker_code": c, "name": self.c.brokers[c]["name"]} for s in close for c in self.c._b_skel[s]]
        return r

    # ------------------------------------------------------------ regions
    def _region(self, e: EntityRef) -> Resolved:
        col = {"seller_region": "seller_region_key", "buyer_region": "buyer_region_key"}.get(e.role, "warehouse_region_key")
        role = col.replace("_key", "")
        hint = (e.catalog_hint or "").strip()
        if e.text.strip() in self.c.regions:  # an exact region key (e.g. from a clarification choice)
            hint = e.text.strip()
            keys, sk = {hint}, hint
        elif hint in self.c.regions:
            keys = {hint}
            sk = hint
            # the hint must agree with the text when the text names a region at all
            text_keys = self._region_keys(skeleton(e.text, drop_legal=False))
            if text_keys and hint not in text_keys:
                keys = text_keys
        else:
            sk = skeleton(e.text + " " + hint, drop_legal=False)
            keys = self._region_keys(sk)
        r = Resolved("region", e.text, role, "not_found", "region_alias", normalized=sk)
        if len(keys) == 1:
            k = next(iter(keys))
            reg = self.c.regions[k]
            r.status, r.condition, r.params, r.ids = "resolved", f"{col} = ?", [k], [k]
            r.display = [{"region_key": k, "uz": reg["uz"], "ru": reg["ru"]}]
        elif len(keys) > 1:
            r.status, r.method = "ambiguous", "city_or_region"
            r.candidates = [{"region_key": k, "uz": self.c.regions[k]["uz"], "ru": self.c.regions[k]["ru"]} for k in sorted(keys)]
        return r

    @staticmethod
    def _region_keys(sk: str) -> set[str]:
        toks = sk.split()
        keys = {k for k, stems in REGION_STEMS.items() if any(t.startswith(s) for t in toks for s in stems)}
        if any(t.startswith(s) for t in toks for s in TASHKENT_STEMS):
            city = any(t in CITY_WORDS for t in toks)
            region = any(t in REGION_WORDS for t in toks) or any(t.startswith("tashkentsk") for t in toks)
            if city and not region:
                keys.add("tashkent_city")
            elif region and not city:
                keys.add("tashkent_region")
            else:
                keys |= {"tashkent_city", "tashkent_region"}
        return keys

    # ------------------------------------------------------------ products
    def _commodities_in(self, text: str) -> tuple[set[str], set[str], set[str]]:
        """Best full-alias matches, the mention tokens they used, and partial (one-word) matches.

        Score = (alias words matched, words matched exactly). A longer or more exact alias wins;
        a tie leaves several commodities, which the caller reports as ambiguous.
        """
        toks = tokens(text)
        best, used, score, partial = set(), set(), (0, 0), set()
        for key, com in self.c.commodities.items():
            for alias in com["aliases"] + [com["name_ru"]]:
                at = tokens(alias)
                matched = [next((t for t in toks if stem_match(t, a)), None) for a in at]
                if any(matched):
                    partial.add(key)
                if all(matched):
                    sc = (len(at), sum(1 for m, a in zip(matched, at) if m == a))
                    if sc > score:
                        best, used, score = {key}, set(matched), sc
                    elif sc == score:
                        best.add(key)
                        used |= set(matched)
        return best, used, partial

    def _product(self, e: EntityRef) -> Resolved:
        hint = e.catalog_hint or ""
        if hint.startswith("variant:") and hint[8:] in self.c.variants:  # exact variant (clarification choice)
            v = self.c.variants[hint[8:]]
            com = self.c.commodities[v["commodity_key"]]
            return Resolved("product", e.text, e.role, "resolved", "variant_key_exact", "product_variant_key = ?",
                            [v["variant_key"]], [v["commodity_key"]],
                            [{"commodity_key": v["commodity_key"], "name_ru": com["name_ru"], "brand": v["brand"],
                              "grade": v["grade"], "class": v["class"], "size": v["size"]}],
                            [self._v_card(v)], normalized=v["variant_key"])
        text_hits, used, partial = self._commodities_in(e.text)
        hint_hits, hint_used, _ = self._commodities_in(e.catalog_hint) if e.catalog_hint else (set(), set(), set())
        if len(text_hits) == 1 and (len(hint_hits) != 1 or hint_hits == text_hits):
            keys, method = text_hits, "commodity_alias"
        elif len(text_hits) == 1:  # text and the LLM hint name different commodities: do not choose
            keys, method = text_hits | hint_hits, "text_and_hint_disagree"
        elif len(hint_hits) == 1 and (not text_hits or hint_hits <= text_hits):
            keys, method, used = hint_hits, "llm_catalog_hint_verified", hint_used
        else:
            keys, method = text_hits or hint_hits, "commodity_alias"
        r = Resolved("product", e.text, e.role, "not_found", method, normalized=norm(e.text))
        if not keys and partial:  # e.g. "масло": sunflower or cottonseed oil
            keys, r.method = partial, "partial_name_only"
        if not keys:
            return r
        if len(keys) > 1:
            r.status = "ambiguous"
            r.candidates = [{"commodity_key": k, "name_ru": self.c.commodities[k]["name_ru"]} for k in sorted(keys)]
            return r
        ck = next(iter(keys))
        source = e.text if method == "commodity_alias" else e.catalog_hint
        n = norm(source)
        want = {
            "brand": compact(e.brand) or next((b for b in sorted(self.c.brands[ck], key=len, reverse=True)
                                               if b in n.replace(" ", "").replace("x", "х")), None),
            "grade": normalize_grade(e.grade) or _grade(n),
            "class": normalize_class(e.product_class) or _class(n),
            "size": compact(e.size) or (compact(_SIZE_RE.search(n).group(1)) if _SIZE_RE.search(n) else None),
        }
        rest = _SIZE_RE.sub(" ", _CLASS_RE.sub(" ", _GRADE_RE.sub(" ", n)))
        words = [t for t in rest.split() if t not in used and t not in STOPWORDS and len(t) > 2
                 and not (want["brand"] and t.replace("x", "х") in want["brand"])]
        variants = [v for v in self.c.variants.values() if v["commodity_key"] == ck]
        for attr in ("brand", "grade", "class", "size"):
            if want[attr]:
                variants = [v for v in variants if v[attr] == want[attr]]
        for w in words:
            variants = [v for v in variants if any(stem_match(w, d) for d in v["descriptor"].split())]
        com = self.c.commodities[ck]
        r.ids = [ck]
        r.display = [{"commodity_key": ck, "name_ru": com["name_ru"], **{k: v for k, v in want.items() if v},
                      **({"descriptor_words": words} if words else {})}]
        if not variants:
            r.status, r.method = "ambiguous", "no_variant_with_these_attributes"
            allv = [v for v in self.c.variants.values() if v["commodity_key"] == ck]
            r.candidates = [self._v_card(v) for v in sorted(allv, key=lambda v: -v["deals"])][:MAX_CANDIDATES]
            return r
        r.status = "resolved"
        if not any(want.values()) and not words:
            r.condition, r.params = "commodity_key = ?", [ck]
        else:
            keys = sorted(v["variant_key"] for v in variants)
            r.condition = f"product_variant_key IN ({', '.join('?' * len(keys))})"
            r.params = keys
        r.candidates = [self._v_card(v) for v in sorted(variants, key=lambda v: -v["deals"])]
        return r

    @staticmethod
    def _v_card(v: dict) -> dict:
        return {k: v[k] for k in ("variant_key", "descriptor", "brand", "grade", "class", "size", "unit", "deals", "examples")}

    # ------------------------------------------------------------ catalog filter values
    def filter_value(self, field_name: str, value: Any) -> tuple[str, list]:
        """Resolve a catalog filter value: returns (status, values)."""
        allowed = self.c.filter_values[field_name]
        vals = value if isinstance(value, list) else [value]
        out = []
        for val in vals:
            exact = [a for a in allowed if norm(str(a)) == norm(str(val))]
            if exact:
                out.append(exact[0])
                continue
            toks = tokens(str(val))
            near = [a for a in allowed if toks and all(any(stem_match(t, x) for x in tokens(a)) for t in toks)]
            if len(near) == 1:
                out.append(near[0])
            else:
                return ("ambiguous" if near else "not_found"), (near or allowed)
        return "resolved", out
