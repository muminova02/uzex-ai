"""Local intent routing, done before any LLM call (works even while the provider is unavailable).

* ``help``: capability or help questions in natural Uzbek or Russian ("nimalarni so'ray olaman",
  "что ты умеешь"), plus greetings.
* ``explain``: a request to explain or summarize the PREVIOUS result ("bu statistikalar dan xulosa nima
  bo'ladi", "объясни этот результат"). It needs an explain word plus a reference to the previous result,
  or a short message without numbers or data question words. A request that names its own data
  ("xulosa 2025 yil bo'yicha nechta bitim") is a new question instead.
* ``injection``: unambiguous attempts to override rules, reveal instructions or run SQL/DDL. These are
  refused locally. The SQL guard and the read-only DB stay in force for everything else; this filter
  only adds a layer and never relaxes one.
"""
from __future__ import annotations

import re
import unicodedata

APOS = "‘’ʻʼ`´"


def _n(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    for a in APOS:
        s = s.replace(a, "'")
    return re.sub(r"\s+", " ", s).strip()


HELP = [
    r"\bnima(lar)?ni? so'r(ay|ash|asam|ashim)", r"\bnima(lar)? so'ra", r"\bqanday savol", r"\bnima(lar)? qila ol", r"\bnima qilasan",
    r"\bnimalar(ni)? bila", r"\bimkoniyat", r"\bqanday ishlay", r"\bqanday foydalan", r"\byordam\b", r"\bmisol(lar)? (ber|ko'rsat)",
    r"\bqo'llanma", r"\bsavol(lar)? berish mumkin", r"^(salom|assalomu alaykum|assalom)\b",
    r"\bчто (ты )?(умеешь|можешь|знаешь)", r"\bчто (можно|мне) (спросить|узнать)", r"\bкакие вопросы", r"\bо ч[её]м (можно )?спрос",
    r"\bпомо(щь|ги)\b", r"\bкак (тобой |этим )?пользоваться", r"\bкак (ты )?работаешь", r"\bпримеры вопросов", r"\bсправк",
    r"^(привет|здравствуй|добрый (день|вечер|утро))\b",
    r"\bнима(лар)?ни? сўр", r"\bқандай савол", r"\bёрдам\b",
]
EXPLAIN = [
    r"\bxulosa", r"\btushuntir", r"\bizohla", r"\bizoh ber", r"\bnima degani", r"\bnimani anglat", r"\bma'nosi", r"\bqisqacha",
    r"\bumumlashtir", r"\bsharhla", r"\bnima bo'ladi\b.*\b(xulosa|natija)", r"\bхулоса", r"\bтушунтир", r"\bизоҳла",
    r"\bобъясни", r"\bпоясни", r"\bчто (это|оно|они) (значит|означает)", r"\bчто значит", r"\bвывод", r"\bрезюм", r"\bподвед[иё] итог",
    r"\bитог", r"\bкратко", r"\bпрокомментир", r"\bинтерпрет", r"\bчто из этого следует", r"\bрасскажи подробнее",
]
DEICTIC = [r"\bbu\b", r"\bshu\b", r"\bushbu\b", r"\byuqoridagi", r"\boldingi", r"\bnatija", r"\bstatistika", r"\bбу\b", r"\bшу\b",
           r"\bэт(о|от|и|их|ого|ому|ой)\b", r"\bвыше\b", r"\bпредыдущ", r"\bпоследн", r"\bрезультат", r"\bстатистик", r"\bответ"]
DATA_WORDS = [r"\bnechta\b", r"\bqancha\b", r"\bсколько\b", r"\bкакая\b", r"\bкакой\b", r"\bкто\b", r"\btop\b", r"\bтоп\b",
              r"\bkim\b", r"\bqaysi\b"]
INJECTION = [
    r"\b(ignore|disregard|forget)\b.{0,30}\b(instruction|rule|prompt|previous)", r"\bsystem prompt", r"\bdeveloper mode",
    r"\bjailbreak", r"\b(игнорируй|игнорировать|забудь|отмени)\b.{0,30}\b(правил|инструкц|ограничен|промпт|всё|все)",
    r"\bсистемн\w* (промпт|инструкц|сообщен)", r"\bпокажи (свой |системный )?(промпт|инструкц)", r"\bpromptni ko'rsat",
    r"\bqoidalarni (unut|e'tiborsiz|bekor)", r"\bko'rsatmalarni (unut|e'tiborsiz)", r"\bbarcha qoidalarni",
    r"\b(drop|truncate|alter)\s+(table|database|schema)\b", r"\bdelete\s+from\b", r"\binsert\s+into\b", r"\bupdate\s+\w+\s+set\b",
    r"\b(attach|pragma|copy)\b\s+['\"\w]", r"\b(run|execute|выполни|bajar)\w*\b.{0,20}\bsql\b", r"\bsql\b.{0,20}\b(so'rov|запрос)\w*\b.{0,15}\b(bajar|выполн)",
]


def _any(pats, s):
    return any(re.search(p, s) for p in pats)


def looks_like_injection(text: str) -> bool:
    return _any(INJECTION, _n(text))


def classify(text: str) -> str | None:
    s = _n(text)
    if not s:
        return None
    if _any(INJECTION, s):
        return "injection"
    if _any(EXPLAIN, s):
        words = s.split()
        has_digits = bool(re.search(r"\d", s))
        data_q = _any(DATA_WORDS, s)
        if _any(DEICTIC, s) and not (has_digits and data_q):
            return "explain"
        if len(words) <= 6 and not has_digits and not data_q:
            return "explain"
    if _any(HELP, s) and not re.search(r"\d{4}", s):
        return "help"
    return None
