"""Telegram-independent bot logic (handlers in bot.py only adapt aiogram objects to this).

* Commands (/start /help /examples /cancel), source buttons and formatting never call the LLM.
* Free text is a standalone question unless it is a numeric choice or "cancel" for a pending
  clarification of the same chat and user. A standalone question drops the pending session, so
  old filters are never carried over.
* Clarification sessions are scoped to (chat_id, user_id). They keep the original question and the
  resolved plan, carry a random session id in every button, expire after ``session_ttl_s`` and can
  be cancelled. Source buttons are scoped to (chat_id, user_id, answer_id).
* One question in flight per user. The global queue and deadline live in QueryService.
"""
from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import asyncio

from .engine import AnswerResult, QueryPlan
from .explain import explain
from .intents import classify
from .lang import Lang, detect, latn_to_cyrl
from .render import NB, Renderer
from .service import QueryService
from .tg_format import answer_html, esc, evidence_html

CANCEL_WORDS = {"отмена", "отменить", "cancel", "bekor", "bekor qilish", "бекор", "стоп", "stop"}
MAX_CHOICES = 6


@dataclass
class Out:
    html: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # rows of (label, callback_data)
    rich: dict | None = None  # renderer blocks for an optional Telegram rich-table message (html chunks stay the fallback)
    rich_covers: int = 0      # how many consecutive outs (this one included) the rich message replaces


@dataclass
class Session:
    sid: str
    chat_id: int
    user_id: int
    question: str
    plan: dict
    reason_code: str
    entity_text: str | None
    candidates: list[dict]
    lang: str
    script: str
    expires: float
    skip: list = field(default_factory=list)  # checks already settled by earlier choices in this chain


@dataclass
class AnswerRecord:
    chat_id: int
    user_id: int
    result: AnswerResult
    expires: float


T = {
    "start": ("Здравствуйте! Я отвечаю на вопросы о сделках биржи UZEX (данные 03.01.2022–30.10.2026, синтетический набор). "
              "Регистрация не нужна — просто напишите вопрос по-русски или по-узбекски.\n/help — справка, /examples — примеры, /cancel — отменить уточнение.",
              "Assalomu alaykum! Men UZEX birjasi bitimlari haqidagi savollarga javob beraman (03.01.2022–30.10.2026, sintetik ma'lumotlar). "
              "Ro'yxatdan o'tish shart emas — savolingizni o'zbek yoki rus tilida yozing.\n/help — yordam, /examples — misollar, /cancel — aniqlashtirishni bekor qilish."),
    "help": ("Я считаю ответы прямо по базе сделок: количество, суммы, цены, рейтинги, сравнения периодов, карточки сделок и компаний. "
             "В каждом ответе указаны период, условия, единицы, правило статусов и число строк; кнопка «Manba / Источник» показывает SQL и проверочные данные. "
             "Если вопрос неоднозначен, я задам один уточняющий вопрос с вариантами. Прогнозов и данных вне набора (прибыль, реквизиты) нет.",
             "Javoblarni to'g'ridan-to'g'ri bitimlar bazasidan hisoblayman: soni, summalar, narxlar, reytinglar, davrlarni solishtirish, bitim va kompaniya kartochkalari. "
             "Har bir javobda davr, shartlar, birliklar, holat qoidasi va qatorlar soni ko'rsatiladi; «Manba / Источник» tugmasi SQL va tekshiruv ma'lumotlarini ko'rsatadi. "
             "Savol noaniq bo'lsa, variantlar bilan bitta aniqlashtiruvchi savol beraman. Prognozlar va to'plamda yo'q ma'lumotlar (foyda, rekvizitlar) yo'q."),
    "examples": ("Примеры вопросов:\n• Сколько сделок было заключено в 2025 году?\n• Топ-5 продавцов по сумме сделок в 2025 году\n"
                 "• Средневзвешенная цена бензина АИ-92 в 2025 году\n• Сравни количество сделок в 2026 и 2025 годах\n"
                 "• Покажи сделку 387831\n• Сколько сделок в долларах США?",
                 "Savollar misollari:\n• 2025-yilda nechta bitim tuzilgan?\n• 2025-yilda bitimlar summasi bo'yicha eng yirik 5 ta sotuvchi kim?\n"
                 "• 2025-yilda AI-92 benzinning o'rtacha tortilgan narxi qancha?\n• 2026 va 2025-yillardagi bitimlar sonini solishtir\n"
                 "• 387831-raqamli bitim haqida ma'lumot\n• AQSh dollarida nechta bitim tuzilgan?"),
    "busy_user": ("Предыдущий вопрос ещё обрабатывается — дождитесь ответа.", "Oldingi savol hali ko'rib chiqilmoqda — javobni kuting."),
    "progress": ("⏳ Обрабатываю вопрос…", "⏳ Savol ko'rib chiqilmoqda…"),
    "cancelled": ("Уточнение отменено. Можно задать новый вопрос.", "Aniqlashtirish bekor qilindi. Yangi savol berishingiz mumkin."),
    "nothing": ("Нет активного уточнения.", "Faol aniqlashtirish yo'q."),
    "stale": ("Это уточнение устарело или уже не действует. Задайте вопрос заново.",
              "Bu aniqlashtirish eskirgan yoki endi amal qilmaydi. Savolni qaytadan bering."),
    "not_yours": ("Эта кнопка относится к вопросу другого пользователя.", "Bu tugma boshqa foydalanuvchining savoliga tegishli."),
    "src_gone": ("Источник для этого ответа больше не хранится. Задайте вопрос заново.",
                 "Bu javob manbasi endi saqlanmagan. Savolni qaytadan bering."),
    "cancel_btn": ("✖ Отмена", "✖ Bekor qilish"),
    "choose": ("Выберите вариант кнопкой или ответьте номером.", "Variantni tugma orqali tanlang yoki raqam bilan javob bering."),
    "no_context": ("Какой результат пояснить? Сейчас нет недавнего ответа в этом чате (или он устарел). Сначала задайте вопрос о данных, затем попросите пояснить.",
                   "Qaysi natijani izohlash kerak? Bu chatda yaqinda olingan javob yo'q (yoki eskirgan). Avval ma'lumotlar haqida savol bering, keyin izoh so'rang."),
    "categories": ("Что я умею (ответы считаются по базе сделок 03.01.2022–30.10.2026):\n"
                   "• количество сделок за период, по форме контракта, статусу, валюте, региону, товару\n"
                   "• суммы сделок в сумах (USD пересчитываются по курсу из набора) и в исходной валюте\n"
                   "• средневзвешенные цены, количества в натуральных единицах (по единицам и вариантам товара)\n"
                   "• рейтинги продавцов, покупателей, брокеров, регионов\n"
                   "• сравнение двух периодов (неполный 2026 год сравнивается за тот же срок)\n"
                   "• списки сделок, карточки сделки, контракта, компании (по ИНН) и брокера\n"
                   "• пояснение уже полученного ответа: «объясни этот результат»\n"
                   "Нет в данных: прибыль, себестоимость, реквизиты, контакты, прогнозы.",
                   "Nimalarni qila olaman (javoblar 03.01.2022–30.10.2026 bitimlar bazasidan hisoblanadi):\n"
                   "• davr, shartnoma shakli, holat, valyuta, hudud, tovar bo'yicha bitimlar soni\n"
                   "• bitimlar summasi so'mda (USD to'plamdagi kurs bo'yicha o'giriladi) va asl valyutada\n"
                   "• o'rtacha tortilgan narxlar, natural birlikdagi miqdorlar (birlik va tovar variantlari bo'yicha)\n"
                   "• sotuvchilar, xaridorlar, brokerlar, hududlar reytinglari\n"
                   "• ikki davrni solishtirish (to'liq bo'lmagan 2026-yil xuddi shu muddat bilan solishtiriladi)\n"
                   "• bitimlar ro'yxati, bitim, shartnoma, kompaniya (STIR bo'yicha) va broker kartochkalari\n"
                   "• olingan javobni izohlash: «bu natijani tushuntir»\n"
                   "Ma'lumotlarda yo'q: foyda, tannarx, rekvizitlar, kontaktlar, prognozlar."),
    "test_mode": ("ТЕСТОВЫЙ РЕЖИМ: разбор вопроса выполнен без настоящей языковой модели.",
                  "TEST REJIMI: savol haqiqiy til modelisiz tahlil qilindi."),
}
SOURCE_BTN = "📎 Manba / Источник"


class BotCore:
    def __init__(self, service: QueryService, session_ttl_s: float = 600, answer_ttl_s: float = 86400,
                 max_answers: int = 2000, clock: Callable[[], float] = time.monotonic, context_ttl_s: float = 1800):
        self.service, self.session_ttl, self.answer_ttl, self.max_answers = service, session_ttl_s, answer_ttl_s, max_answers
        self.clock = clock
        self.sessions: dict[tuple[int, int], Session] = {}
        self.answers: OrderedDict[str, AnswerRecord] = OrderedDict()
        self._busy: set[tuple[int, int]] = set()
        self.last: dict[tuple[int, int], dict] = {}  # safe per-answer summary for logs (no message text)
        self.last_kind: dict = {}  # answer | help | explain | no_context | cancelled | busy (also logged)
        self.context_ttl = context_ttl_s
        self.last_result: dict[tuple[int, int], tuple[str, float]] = {}  # key -> (answer id, expires) for explanations

    # ------------------------------------------------------------ language helpers
    @staticmethod
    def ui_lang(text: str | None, lang_code: str | None = None) -> tuple[str, str]:
        if text and text.strip() and not text.strip().startswith("/"):
            d = detect(text)
            if d.confident:
                return d.lang, d.script
        if (lang_code or "").startswith("uz"):
            return "uz", "latn"
        return "ru", "cyrl"

    @staticmethod
    def tr(key: str, lang: tuple[str, str]) -> str:
        ru, uz = T[key]
        if lang[0] == "ru":
            return ru
        return latn_to_cyrl(uz) if lang[1] == "cyrl" else uz

    # ------------------------------------------------------------ commands (no LLM)
    def on_start(self, lang_code: str | None = None) -> list[Out]:
        return [Out(esc(T["start"][1]) + "\n\n" + esc(T["start"][0]))]

    def on_help(self, lang_code: str | None = None) -> list[Out]:
        return [Out(esc(T["help"][1]) + "\n\n" + esc(T["help"][0]))]

    def on_examples(self, lang_code: str | None = None) -> list[Out]:
        return [Out(esc(T["examples"][1]) + "\n\n" + esc(T["examples"][0]))]

    def on_cancel(self, chat_id: int, user_id: int, lang_code: str | None = None) -> list[Out]:
        s = self.sessions.pop((chat_id, user_id), None)
        lang = (s.lang, s.script) if s else self.ui_lang(None, lang_code)
        return [Out(esc(self.tr("cancelled" if s else "nothing", lang)))]

    def progress_text(self, text: str, lang_code: str | None = None) -> str:
        return esc(self.tr("progress", self.ui_lang(text, lang_code)))

    # ------------------------------------------------------------ free text
    async def on_text(self, chat_id: int, user_id: int, text: str, lang_code: str | None = None) -> list[Out]:
        key = (chat_id, user_id)
        text = (text or "").strip()
        if not text:
            return []
        lang = self.ui_lang(text, lang_code)
        if key in self._busy:
            self.last_kind[key] = "busy"
            return [Out(esc(self.tr("busy_user", lang)))]
        session = self._valid_session(key)
        if session and text.casefold() in CANCEL_WORDS:
            self.sessions.pop(key, None)
            self.last_kind[key] = "cancelled"
            return [Out(esc(self.tr("cancelled", (session.lang, session.script))))]
        intent = classify(text)
        if intent == "help":  # local, no LLM, works during provider cooldown
            self.last_kind[key] = "help"
            return self._help_outs(lang)
        if intent == "explain":
            return self._explain_outs(key, text, lang)
        self._busy.add(key)
        try:
            if session and text.isdigit() and 1 <= int(text) <= min(len(session.candidates), MAX_CHOICES):
                return await self._follow(session, int(text) - 1)
            self.sessions.pop(key, None)  # a new standalone question: nothing is inherited
            if intent == "injection":  # refused locally by the engine (no LLM call, no SQL, no queue)
                res = await asyncio.to_thread(self.service.engine.answer, text)
                return self._outs(key, res)
            res = await self.service.ask(text)
            return self._outs(key, res)
        finally:
            self._busy.discard(key)

    # ------------------------------------------------------------ callbacks
    async def on_callback(self, chat_id: int, user_id: int, data: str, lang_code: str | None = None) -> tuple[str | None, list[Out]]:
        key = (chat_id, user_id)
        parts = (data or "").split(":")
        lang = self.ui_lang(None, lang_code)
        if parts[0] == "s" and len(parts) == 2:
            rec = self.answers.get(parts[1])
            if rec is None or rec.expires < self.clock():
                return self.tr("src_gone", lang), []
            if (rec.chat_id, rec.user_id) != key:
                return self.tr("not_yours", lang), []
            return None, [Out(h) for h in evidence_html(rec.result)]
        if parts[0] in ("c", "x") and len(parts) >= 2:
            s = self.sessions.get(key)
            other = next((x for x in self.sessions.values() if x.sid == parts[1] and (x.chat_id, x.user_id) != key), None)
            if other is not None:
                return self.tr("not_yours", lang), []
            if s is None or s.sid != parts[1] or s.expires < self.clock():
                if s is not None and s.expires < self.clock():
                    self.sessions.pop(key, None)
                return self.tr("stale", lang), []
            slang = (s.lang, s.script)
            if parts[0] == "x":
                self.sessions.pop(key, None)
                return None, [Out(esc(self.tr("cancelled", slang)))]
            try:
                idx = int(parts[2])
            except (IndexError, ValueError):
                return self.tr("stale", slang), []
            if not 0 <= idx < min(len(s.candidates), MAX_CHOICES):
                return self.tr("stale", slang), []
            if key in self._busy:
                return self.tr("busy_user", slang), []
            self._busy.add(key)
            try:
                return None, await self._follow(s, idx)
            finally:
                self._busy.discard(key)
        return self.tr("stale", lang), []

    # ------------------------------------------------------------ local help / explanations
    def _help_outs(self, lang) -> list[Out]:
        return [Out(f"<b>{esc(self.tr('categories', lang).splitlines()[0])}</b>\n"
                    + esc("\n".join(self.tr("categories", lang).splitlines()[1:])) + "\n\n" + esc(self.tr("examples", lang)))]

    def _explain_outs(self, key, text: str, lang) -> list[Out]:
        ref = self.last_result.get(key)
        rec = self.answers.get(ref[0]) if ref else None
        if ref is None or ref[1] < self.clock() or rec is None:
            self.last_result.pop(key, None)
            self.last_kind[key] = "no_context"
            return [Out(esc(self.tr("no_context", lang)))]
        r = explain(rec.result, Lang(lang[0], lang[1], True, "question"), self.service.engine.catalog)
        if not r.grounded:  # never show an explanation with an ungrounded number
            return [Out(esc(self.tr("no_context", lang)))]
        outs = [Out(c) for c in answer_html(r.text)]
        outs[-1].buttons = [[(SOURCE_BTN, f"s:{ref[0]}")]]
        self.last_kind[key] = "explain"
        return outs

    # ------------------------------------------------------------ internals
    def _valid_session(self, key) -> Session | None:
        s = self.sessions.get(key)
        if s and s.expires < self.clock():
            self.sessions.pop(key, None)
            return None
        return s

    async def _follow(self, s: Session, idx: int) -> list[Out]:
        key = (s.chat_id, s.user_id)
        self.sessions.pop(key, None)  # one choice per session; a chained clarification creates a new one
        plan = QueryPlan.model_validate(s.plan)
        res, skip = await self.service.follow_up(s.question, plan, s.reason_code, s.candidates[idx], s.entity_text, set(s.skip))
        return self._outs(key, res, skip)

    def _store_answer(self, key, res: AnswerResult) -> str:
        aid = secrets.token_urlsafe(8)
        self.answers[aid] = AnswerRecord(key[0], key[1], res, self.clock() + self.answer_ttl)
        while len(self.answers) > self.max_answers:
            self.answers.popitem(last=False)
        return aid

    def _outs(self, key, res: AnswerResult, skip: set | None = None) -> list[Out]:
        self.last_kind[key] = "answer"
        self.last[key] = {"status": res.status, "reason": res.reason_code, "lang": f"{res.language}/{res.script}",
                          "query_id": res.evidence.query_id if res.evidence else None, "timings_ms": res.timings_ms,
                          "grounded": (res.rendered or {}).get("grounded"), "model": (res.llm or {}).get("served_by"),
                          "blocks": (res.rendered or {}).get("blocks"),
                          # diagnostics: the free SQL actually executed (no secrets; never shown in the answer body)
                          "custom_sql": next((q.sql for q in reversed(res.evidence.queries) if q.purpose == "custom"), None)
                          if res.evidence else None}
        text = (res.rendered or {}).get("text") or res.status
        lang = (res.language, res.script)
        banner = self.tr("test_mode", lang) if self.service.test_mode and res.status != "execution_error" else None
        self.last[key]["banner"] = banner  # the structured (table) view must carry the same test-mode disclosure
        chunks = answer_html(text, banner)
        outs = [Out(c) for c in chunks]
        if res.status == "ambiguous" and res.clarification and res.clarification.candidates and res.plan is not None:
            sid = secrets.token_urlsafe(6)
            self.sessions[key] = Session(sid, key[0], key[1], res.question or "", res.plan.model_dump(mode="json"),
                                         res.reason_code or "", res.clarification.entity_text, res.clarification.candidates[:MAX_CHOICES],
                                         res.language, res.script, self.clock() + self.session_ttl, sorted(skip or ()))
            r = Renderer(self.service.engine.catalog)
            r.lang = res.language
            r.catalog_strings = []
            rows = []
            for i, c in enumerate(res.clarification.candidates[:MAX_CHOICES]):
                label = r._cand(c).replace("⟦", "").replace("⟧", "").replace(NB, " ")
                if res.language == "uz" and res.script == "cyrl":
                    label = latn_to_cyrl(r._cand(c))
                rows.append([(f"{i + 1}) {label}"[:60], f"c:{sid}:{i}")])
            rows.append([(self.tr("cancel_btn", lang), f"x:{sid}")])
            outs[-1].html += "\n\n" + esc(self.tr("choose", lang))
            outs[-1].buttons = rows
            aid = self._store_answer(key, res)
            self.last_result[key] = (aid, self.clock() + self.context_ttl)
        else:
            aid = self._store_answer(key, res)  # also for errors: diagnostics live behind the source button
            self.last_result[key] = (aid, self.clock() + self.context_ttl)
            outs[-1].buttons = [[(SOURCE_BTN, f"s:{aid}")]]
            blocks = (res.rendered or {}).get("blocks")
            if blocks and getattr(self.service.engine.s, "tg_rich_tables", False):
                outs[0].rich, outs[0].rich_covers = {**blocks, "_banner": banner}, len(outs)
        return outs
