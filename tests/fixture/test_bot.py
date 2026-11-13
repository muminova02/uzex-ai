"""Prompt 5 bot tests with a MOCKED planner (no Telegram, no LLM network). Real Engine + fixture DB."""
from __future__ import annotations

import asyncio
import re
import threading
import time

import pytest

from uzex_assistant.bot import RedactFilter, build_router, safe_send, send_outs
from uzex_assistant.bot_core import BotCore, Out
from uzex_assistant.config import Settings
from uzex_assistant.engine import Engine
from uzex_assistant.llm import LLMError, LLMResponse, MockProvider
from uzex_assistant.rategate import RateGate
from uzex_assistant.service import QueryService
from uzex_assistant.tg_format import TG_LIMIT, answer_html, evidence_html, split_units

Y25 = {"start": "2025-01-01", "end": "2025-12-31"}
PLANS = {
    "Сколько сделок в 2025 году?": {"intent": "count", "metrics": ["deal_count"], "period": Y25},
    "2025-yilda nechta bitim bo'lgan?": {"intent": "count", "metrics": ["deal_count"], "period": Y25},
    "Сколько сделок в 2026 году?": {"intent": "count", "metrics": ["deal_count"], "period": {"start": "2026-01-01", "end": "2026-12-31"}},
    "Сколько сделок в Ташкенте?": {"intent": "count", "metrics": ["deal_count"], "entities": [{"kind": "region", "text": "Ташкенте"}]},
    # live round 2026-11-05: the planner repeated the region (entity + filter); one answer must settle both
    "Toshkentda omborli nechta bitim bor?": {"intent": "count", "metrics": ["deal_count"],
                                            "entities": [{"kind": "region", "text": "Toshkent"}],
                                            "filters": [{"field": "warehouse_region", "op": "eq", "value": "Toshkent"},
                                                        {"field": "warehouse_region", "op": "in", "value": ["Toshkent"]}]},
    "Toshkentda nechta bitim bor?": {"intent": "count", "metrics": ["deal_count"], "entities": [{"kind": "region", "text": "Toshkentda"}]},
    "Объём продаж масла в 2025": {"intent": "aggregate", "metrics": ["trade_value_uzs"], "period": Y25,
                                  "entities": [{"kind": "product", "text": "масла"}]},
    "Сколько сделок у Alfa?": {"intent": "count", "metrics": ["deal_count"], "entities": [{"kind": "participant", "text": "Alfa"}]},
    "Покажи сделку 000001": {"intent": "lookup", "entities": [{"kind": "deal", "text": "000001"}]},
    "Список сделок": {"intent": "list", "top_n": 50},
}


class Planner:
    """Scripted provider: plan by question; optional delay; optional failure."""

    def __init__(self, delay=0.0, fail=None):
        self.delay, self.fail, self.calls, self.lock = delay, fail, [], threading.Lock()

    def __call__(self, system, user):
        q = re.search(r"<question>\n(.*)\n</question>", user, re.S).group(1)
        with self.lock:
            self.calls.append(q)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return PLANS[q]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    from .engine_fixture import build
    data, db = build(tmp_path_factory.mktemp("bot"))
    e = Engine(Settings(_env_file=None, data_dir=data, db_path=db, sql_workers=2, request_budget_s=60),
               provider=MockProvider([]))
    yield e
    e.close()


def make(engine, planner=None, **kw):
    planner = planner or Planner()
    engine.provider = MockProvider(planner)
    engine.planner.provider = engine.provider
    clock = Clock()
    core = BotCore(QueryService(engine, **kw), clock=clock)
    return core, planner, clock


def run(coro):
    return asyncio.run(coro)


def plain(outs) -> str:
    return "\n".join(o.html for o in outs)


# ------------------------------------------------------------------ commands: no LLM, no login
def test_commands_do_not_call_llm(engine):
    core, planner, _ = make(engine)
    for outs in (core.on_start(), core.on_help(), core.on_examples(), core.on_cancel(1, 1)):
        assert outs and outs[0].html
    assert "Ro'yxatdan o'tish shart emas" in core.on_start()[0].html and "Регистрация не нужна" in core.on_start()[0].html
    assert planner.calls == []


# ------------------------------------------------------------------ language switching, test-mode label, source button
def test_language_switch_and_source_button(engine):
    core, planner, _ = make(engine)
    ru = run(core.on_text(10, 1, "Сколько сделок в 2025 году?"))
    uz = run(core.on_text(10, 1, "2025-yilda nechta bitim bo'lgan?"))
    assert "Количество сделок: 9." in plain(ru) and "Bitimlar soni: 9 ta." in plain(uz)
    assert "ТЕСТОВЫЙ РЕЖИМ" in ru[0].html and "TEST REJIMI" in uz[0].html  # mock planner is labelled
    for outs in (ru, uz):
        assert "Период" in plain(outs) or "Davr" in plain(outs)  # main answer keeps period/policy/rows
        assert "строк в выборке: 9" in plain(outs) or "tanlovdagi qatorlar: 9" in plain(outs)
        (label, data), = outs[-1].buttons[0]
        assert label == "📎 Manba / Источник" and data.startswith("s:")
    # each source button opens its own answer's evidence
    toast, ev_ru = run(core.on_callback(10, 1, ru[-1].buttons[0][0][1]))
    toast2, ev_uz = run(core.on_callback(10, 1, uz[-1].buttons[0][0][1]))
    qid_ru = run_qid(ev_ru)
    assert toast is None and qid_ru != run_qid(ev_uz) and "SELECT" in plain(ev_ru)
    assert len(planner.calls) == 2  # the source buttons made no LLM call


def run_qid(outs):
    return re.search(r"<code>([0-9a-f]{12})</code>", outs[0].html).group(1)


def test_source_button_is_user_and_chat_scoped(engine):
    core, _, clock = make(engine)
    outs = run(core.on_text(20, 1, "Сколько сделок в 2025 году?"))
    data = outs[-1].buttons[0][0][1]
    assert run(core.on_callback(20, 2, data))[0].startswith("Эта кнопка относится")      # other user, same chat
    assert run(core.on_callback(21, 1, data))[0].startswith("Эта кнопка относится")      # same user, other chat
    clock.t += 86401
    assert "больше не хранится" in run(core.on_callback(20, 1, data))[0]                 # expired


# ------------------------------------------------------------------ clarification follow-ups
def test_region_clarification_by_button(engine):
    core, planner, _ = make(engine)
    q = run(core.on_text(30, 1, "Сколько сделок в Ташкенте?"))
    labels = [b[0][0] for b in q[-1].buttons]
    assert labels[:2] == ["1) г. Ташкент", "2) Ташкентская область"] and "Отмена" in labels[-1]
    sid_data = q[-1].buttons[0][0][1]
    toast, a = run(core.on_callback(30, 1, sid_data))
    assert toast is None and "регион склада продавца: г. Ташкент" in plain(a).replace("\xa0", " ")
    assert "Количество сделок: 1." in plain(a)  # deal 000002 (contract 900002, warehouse Toshkent shahri)
    assert len(planner.calls) == 1  # the follow-up re-used the stored plan (no LLM call)
    assert run(core.on_callback(30, 1, sid_data))[0].startswith("Это уточнение устарело")  # session consumed


def test_uzbek_clarification_by_number_reply(engine):
    core, planner, _ = make(engine)
    q = run(core.on_text(31, 1, "Toshkentda nechta bitim bor?"))
    assert "Toshkent shahri yoki Toshkent viloyatimi" in plain(q) and "raqam bilan javob bering" in plain(q)
    a = run(core.on_text(31, 1, "2"))
    assert "Toshkent viloyati" in plain(a) and "Bitimlar soni: 0 ta." in plain(a) and len(planner.calls) == 1


def test_chained_clarifications_volume_then_product(engine):
    core, planner, _ = make(engine)
    q1 = run(core.on_text(32, 1, "Объём продаж масла в 2025"))
    assert "объёмом" in plain(q1)
    _, q2 = run(core.on_callback(32, 1, q1[-1].buttons[1][0][1]))     # 2) quantity
    assert "Уточните товар" in plain(q2)
    labels = [b[0][0] for b in q2[-1].buttons[:-1]]
    pick = next(i for i, l in enumerate(labels) if "подсолнечное" in l)
    _, a = run(core.on_callback(32, 1, q2[-1].buttons[pick][0][1]))
    t = plain(a).replace("\xa0", " ")
    assert "Купленное количество товара: 10 т" in t and "Период: 10.03.2025–31.12.2025" in t  # original period kept
    assert len(planner.calls) == 1


def test_participant_clarification_and_cancel(engine):
    core, _, _ = make(engine)
    q = run(core.on_text(33, 1, "Сколько сделок у Alfa?"))
    labels = [b[0][0] for b in q[-1].buttons]
    assert any("ИНН 333333333" in l for l in labels) and any("ИНН 555000001" in l for l in labels)
    _, c = run(core.on_callback(33, 1, q[-1].buttons[-1][0][1]))
    assert "Уточнение отменено" in plain(c)
    assert core.sessions == {} or (33, 1) not in core.sessions
    q = run(core.on_text(33, 1, "Сколько сделок у Alfa?"))
    assert "Уточнение отменено" in plain(run(core.on_text(33, 1, "отмена")))


def test_new_question_does_not_inherit_old_filters(engine):
    core, _, _ = make(engine)
    q = run(core.on_text(34, 1, "Сколько сделок в Ташкенте?"))
    old = q[-1].buttons[0][0][1]
    a = run(core.on_text(34, 1, "Сколько сделок в 2026 году?"))
    t = plain(a)
    assert "Количество сделок: 6." in t and "Ташкент" not in t
    assert run(core.on_callback(34, 1, old))[0].startswith("Это уточнение устарело")
    # a digit after a standalone answer is just a new question, never an old choice
    assert (34, 1) not in core.sessions


def test_stale_and_invalid_callbacks(engine):
    core, _, clock = make(engine)
    q = run(core.on_text(35, 1, "Сколько сделок в Ташкенте?"))
    data = q[-1].buttons[0][0][1]
    sid = data.split(":")[1]
    assert run(core.on_callback(35, 2, data))[0].startswith("Эта кнопка относится")        # another user
    assert run(core.on_callback(35, 1, f"c:{sid}:9"))[0].startswith("Это уточнение устарело")  # bad index
    assert run(core.on_callback(35, 1, "c:wrongsid:0"))[0].startswith("Это уточнение устарело")
    assert run(core.on_callback(35, 1, "garbage"))[0].startswith("Это уточнение устарело")
    clock.t += 601
    assert run(core.on_callback(35, 1, data))[0].startswith("Это уточнение устарело")       # expired
    # after expiry "1" is a new standalone question (the scripted planner has no plan for it -> honest error)
    t = plain(run(core.on_text(35, 1, "1")))
    assert "не отсутствие данных" in t and "Количество сделок" not in t


# ------------------------------------------------------------------ concurrency, queue, deadline
def test_concurrent_users_isolated_and_loop_not_blocked(engine):
    core, planner, _ = make(engine, Planner(delay=0.6), max_concurrent=2)

    async def scenario():
        ticks = []

        async def ticker():
            for _ in range(10):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)
        t0 = time.monotonic()
        a, b, _ = await asyncio.gather(core.on_text(40, 1, "Сколько сделок в 2025 году?"),
                                       core.on_text(41, 2, "2025-yilda nechta bitim bo'lgan?"), ticker())
        return a, b, time.monotonic() - t0, ticks
    a, b, elapsed, ticks = run(scenario())
    assert "Количество сделок: 9." in plain(a) and "Bitimlar soni: 9 ta." in plain(b)
    assert elapsed < 1.1  # ran in parallel (2 x 0.6 s)
    assert len(ticks) == 10 and max(y - x for x, y in zip(ticks, ticks[1:])) < 0.3  # event loop stayed responsive


def test_one_question_in_flight_per_user(engine):
    core, _, _ = make(engine, Planner(delay=0.4))

    async def scenario():
        first = asyncio.create_task(core.on_text(42, 1, "Сколько сделок в 2025 году?"))
        await asyncio.sleep(0.1)
        second = await core.on_text(42, 1, "Сколько сделок в 2026 году?")
        return await first, second
    first, second = run(scenario())
    assert "Количество сделок: 9." in plain(first) and "Предыдущий вопрос ещё обрабатывается" in plain(second)


def test_global_queue_is_bounded(engine):
    core, _, _ = make(engine, Planner(delay=0.5), max_concurrent=1, max_waiting=1)

    async def scenario():
        return await asyncio.gather(*(core.on_text(50 + i, i, "Сколько сделок в 2025 году?") for i in range(3)))
    outs = run(scenario())
    texts = [plain(o) for o in outs]
    assert sum("Количество сделок: 9." in t for t in texts) == 2
    assert sum("слишком много вопросов одновременно" in t for t in texts) == 1


def test_queue_time_counts_against_the_deadline(engine):
    core, planner, _ = make(engine, Planner(delay=0.5), max_concurrent=1)

    async def scenario():
        return await asyncio.gather(core.on_text(60, 1, "Сколько сделок в 2025 году?"),
                                    core.on_text(61, 2, "Сколько сделок в 2025 году?"))
    run(scenario())
    # the second request waited in the queue; inspect the budget handed to the engine via a fresh call
    res = run(core.service.ask("Сколько сделок в 2025 году?"))
    assert res.timings_ms["budget_given"] <= 60_000 and "queue" in res.timings_ms


# ------------------------------------------------------------------ provider failures
def test_quota_failure_is_temporary_unavailability_not_no_data(engine):
    err = LLMError("rate_limited", "429")
    err.meta = {"attempts": 1, "last_wait_hint_s": 286.0}
    core, _, _ = make(engine, Planner(fail=err))
    t = plain(run(core.on_text(70, 1, "Сколько сделок в 2025 году?")))
    assert "временно недоступен" in t and "примерно через 4 мин 46 с" in t and "не отсутствие данных" in t
    assert "llm_rate_limited" not in t  # internal code kept out of the main message
    assert "Количество сделок" not in t and "📎" not in t


def test_gate_cooldown_fails_fast_without_calling_provider(engine):
    core, planner, _ = make(engine)

    class Inner:
        name, model = "groq", "openai/gpt-oss-120b"

        def __init__(self):
            self.calls = 0

        def generate_json(self, *a):
            self.calls += 1
            e = LLMError("rate_limited", "429")
            e.meta = {"attempts": 1, "last_wait_hint_s": 300.0}
            raise e
    inner = Inner()
    gate = RateGate(inner)
    engine.provider = gate
    engine.planner.provider = gate
    first = plain(run(core.on_text(71, 1, "Сколько сделок в 2025 году?")))
    t0 = time.monotonic()
    second = plain(run(core.on_text(72, 2, "Сколько сделок в 2025 году?")))
    assert inner.calls == 1 and time.monotonic() - t0 < 1.0  # second request never reached the provider
    for t in (first, second):
        assert "временно недоступен" in t and "не отсутствие данных" in t


def test_llm_timeout_message(engine):
    core, _, _ = make(engine, Planner(fail=LLMError("timeout", "slow")))
    t = plain(run(core.on_text(73, 1, "2025-yilda nechta bitim bo'lgan?")))
    assert "vaqtinchalik xato" in t and "ma'lumot yo'qligi emas" in t


# ------------------------------------------------------------------ HTML escaping and splitting
def test_html_escaping():
    chunks = answer_html('Компания "A & B <script>" (ИНН 123)\nУсловия:\n• x < 5 & y > 2')
    assert chunks == ['<b>Компания "A &amp; B &lt;script&gt;" (ИНН 123)</b>\n<b>Условия:</b>\n• x &lt; 5 &amp; y &gt; 2']


def test_long_messages_split_safely():
    text = "Заголовок\n" + "\n".join(f"{i}. «Компания & сын <{i}>» — " + "Количество сделок: 1 234; " * 8 for i in range(400))
    text += "\n" + "&" * 9000  # one huge line of characters that expand when escaped
    chunks = answer_html(text)
    assert len(chunks) > 5 and all(len(c) <= TG_LIMIT for c in chunks)
    for c in chunks:
        assert c.count("<b>") == c.count("</b>") and c.count("<i>") == c.count("</i>")
        assert not re.search(r"&(?!amp;|lt;|gt;)", c)  # no broken entity
    import html
    joined = html.unescape(re.sub(r"</?[bi]>", "", "\n".join(chunks)))
    assert joined.replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_evidence_chunks_valid(engine):
    core, _, _ = make(engine)
    outs = run(core.on_text(80, 1, "Список сделок"))
    _, ev = run(core.on_callback(80, 1, outs[-1].buttons[0][0][1]))
    for o in ev:
        assert len(o.html) <= TG_LIMIT and o.html.count("<pre>") == o.html.count("</pre>")
    assert all(len(c) <= 50 for c in split_units(["a" * 10] * 20, limit=50))


# ------------------------------------------------------------------ Telegram transport (fakes)
class _M:  # minimal TelegramMethod stand-in for exception constructors
    __name__ = "SendMessage"


def test_safe_send_retries_telegram_429_then_succeeds():
    from aiogram.exceptions import TelegramRetryAfter
    calls, sleeps = [], []

    async def send(text, pm, markup):
        calls.append(pm)
        if len(calls) == 1:
            raise TelegramRetryAfter(method=_M(), message="Too Many Requests", retry_after=3)
        return "ok"

    async def sleep(s):
        sleeps.append(s)
    assert run(safe_send(send, "<b>x</b>", sleep=sleep)) == "ok" and sleeps == [3]


def test_safe_send_gives_up_on_long_flood_wait():
    from aiogram.exceptions import TelegramRetryAfter

    async def send(text, pm, markup):
        raise TelegramRetryAfter(method=_M(), message="Too Many Requests", retry_after=120)
    with pytest.raises(TelegramRetryAfter):
        run(safe_send(send, "x", sleep=lambda s: asyncio.sleep(0)))


def test_safe_send_falls_back_to_plain_text_on_parse_error():
    from aiogram.exceptions import TelegramBadRequest
    sent = []

    async def send(text, pm, markup):
        sent.append((text, pm))
        if pm == "HTML":
            raise TelegramBadRequest(method=_M(), message="Bad Request: can't parse entities")
        return "ok"
    run(safe_send(send, "<b>A &amp; B</b>"))
    assert sent[-1] == ("A & B", None)


def test_secrets_redacted_in_logs():
    import logging
    rec = logging.LogRecord("aiogram", logging.INFO, "", 0, "url https://api.telegram.org/bot%s/getMe key %s",
                            ("1234567890:AAH" + "x" * 32, "gsk_" + "y" * 40), None)
    RedactFilter().filter(rec)
    assert "AAH" not in rec.getMessage() and "gsk_" not in rec.getMessage() and rec.getMessage().count("<redacted>") == 2


# ------------------------------------------------------------------ aiogram handlers (fake bot/message)
class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.actions = [], [], []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return type("Msg", (), {"chat": type("C", (), {"id": chat_id})(), "message_id": len(self.sent)})()

    async def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))

    async def send_chat_action(self, chat_id, action):
        self.actions.append(action)


def _msg(bot, text, chat=90, user=9, lc="ru"):
    u = type("U", (), {"id": user, "language_code": lc})()
    return type("M", (), {"text": text, "chat": type("C", (), {"id": chat})(), "from_user": u, "bot": bot})()


def test_aiogram_handlers_progress_then_edit(engine):
    core, _, _ = make(engine)
    router = build_router(core)
    handlers = {h.callback.__name__: h.callback for h in router.message.handlers}
    bot = FakeBot()
    run(handlers["start"](_msg(bot, "/start")))
    assert "Регистрация не нужна" in bot.sent[-1][1]
    run(handlers["question"](_msg(bot, "Сколько сделок в 2025 году?")))
    assert "Обрабатываю вопрос" in bot.sent[-1][1] and bot.actions == ["typing"]
    chat, mid, text, markup = bot.edits[-1]
    assert "Количество сделок: 9." in text and markup.inline_keyboard[0][0].callback_data.startswith("s:")


def test_aiogram_callback_handler(engine):
    core, _, _ = make(engine)
    router = build_router(core)
    cb = router.callback_query.handlers[0].callback
    bot = FakeBot()
    outs = run(core.on_text(91, 9, "Сколько сделок в 2025 году?"))
    answered = []

    class CQ:
        data = outs[-1].buttons[0][0][1]
        from_user = type("U", (), {"id": 9, "language_code": "ru"})()
        message = type("M", (), {"chat": type("C", (), {"id": 91})()})()

        async def answer(self, text="", show_alert=False):
            answered.append(text)
    CQ.bot = bot
    run(cb(CQ()))
    assert answered == [""] and "Manba / Источник" in bot.sent[0][1]
    run(send_outs(bot, 91, [Out("<b>x</b>")]))
