"""aiogram 3 Telegram bot (long polling). Handlers only adapt aiogram objects to BotCore.

Run:  .venv/bin/python -m uzex_assistant.bot
Needs UZEX_BOT_TOKEN in .env (never logged) and a prepared DB (python -m uzex_assistant.importer ensure).
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time

from .bot_core import BotCore, Out

log = logging.getLogger("uzex.bot")
SECRET_RE = re.compile(r"(\d{6,}:[A-Za-z0-9_-]{30,}|gsk_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,})")


class RedactFilter(logging.Filter):
    """Removes bot tokens and API keys from every log record (including aiogram's own)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if SECRET_RE.search(msg):
            record.msg, record.args = SECRET_RE.sub("<redacted>", msg), ()
        return True


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stderr)
    h.addFilter(RedactFilter())
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)


# ---------------------------------------------------------------- sending with Telegram limits
async def safe_send(send, text: str, markup=None, attempts: int = 4, max_wait_s: float = 30.0, sleep=asyncio.sleep):
    """``send(text, parse_mode, reply_markup)`` → message. Retries Telegram 429 (bounded) and network errors;
    on an HTML parse error resends as plain text (already-escaped text is shown literally, never as markup)."""
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
    parse_mode = "HTML"
    for attempt in range(1, attempts + 1):
        try:
            return await send(text, parse_mode, markup)
        except TelegramRetryAfter as e:
            if e.retry_after > max_wait_s or attempt == attempts:
                log.warning("telegram flood limit: retry_after=%ss, giving up", e.retry_after)
                raise
            await sleep(e.retry_after)
        except TelegramNetworkError:
            if attempt == attempts:
                raise
            await sleep(min(2 ** attempt, 8))
        except TelegramBadRequest as e:
            if "parse" in str(e).lower() and parse_mode:
                import html as _h
                text, parse_mode = _h.unescape(re.sub(r"</?(b|i|pre|code)>", "", text)), None
                continue
            raise
    return None


def _markup(out: Out):
    if not out.buttons:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                                                 for row in out.buttons])


async def send_outs(bot, chat_id: int, outs: list[Out], edit_message=None) -> None:
    skip = 0
    for i, out in enumerate(outs):
        if skip:
            skip -= 1
            continue
        mk = _markup(out)
        if i == 0 and edit_message is not None:
            async def edit(text, pm, markup, m=edit_message):
                return await bot.edit_message_text(text=text, chat_id=m.chat.id, message_id=m.message_id,
                                                   parse_mode=pm, reply_markup=markup)
            try:
                await safe_send(edit, out.html, mk)
                continue
            except Exception:  # message too old / unchanged: send a new one instead
                pass

        if out.rich:  # optional rich table message; any failure falls back to the same facts as HTML text
            try:
                from .tg_format import rich_message
                rb = dict(out.rich)
                last = outs[i + max(out.rich_covers, 1) - 1]  # evidence/clarification buttons live on the last chunk
                await bot.send_rich_message(chat_id=chat_id, rich_message=rich_message(rb, rb.pop("_banner", None)),
                                            reply_markup=_markup(last))
                skip = max(out.rich_covers, 1) - 1
                continue
            except Exception:
                log.warning("rich table message failed; sending the HTML fallback", exc_info=True)

        async def send(text, pm, markup):
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode=pm, reply_markup=markup)
        await safe_send(send, out.html, mk)


# ---------------------------------------------------------------- handlers (thin)
def build_router(core: BotCore, qlog=None):
    from aiogram import F, Router
    from aiogram.enums import ChatAction
    from aiogram.filters import Command, CommandStart

    router = Router()

    def ids(m):
        return m.chat.id, m.from_user.id, (m.from_user.language_code if m.from_user else None)

    @router.message(CommandStart())
    async def start(m):
        await send_outs(m.bot, m.chat.id, core.on_start(ids(m)[2]))

    @router.message(Command("help"))
    async def help_(m):
        await send_outs(m.bot, m.chat.id, core.on_help(ids(m)[2]))

    @router.message(Command("examples"))
    async def examples(m):
        await send_outs(m.bot, m.chat.id, core.on_examples(ids(m)[2]))

    @router.message(Command("cancel"))
    async def cancel(m):
        chat, user, lc = ids(m)
        await send_outs(m.bot, chat, core.on_cancel(chat, user, lc))

    @router.message(F.text & ~F.text.startswith("/"))
    async def question(m):
        chat, user, lc = ids(m)
        progress = None
        try:
            async def send(text, pm, markup):
                return await m.bot.send_message(chat_id=chat, text=text, parse_mode=pm, reply_markup=markup)
            progress = await safe_send(send, core.progress_text(m.text, lc))
            await m.bot.send_chat_action(chat_id=chat, action=ChatAction.TYPING)
        except Exception:
            log.exception("progress message failed")
        t0 = time.perf_counter()
        outs = await core.on_text(chat, user, m.text, lc)
        kind = core.last_kind.get((chat, user))
        if qlog is not None:
            qlog.write("telegram", {"user": user, "username": getattr(m.from_user, "username", None), "chat": chat}, m.text, kind,
                       core.last.get((chat, user)) if kind == "answer" else None, (time.perf_counter() - t0) * 1000,
                       re.sub(r"<[^>]+>", "", outs[0].html) if outs else None)
        log.info("answered chat=%s user=%s parts=%d kind=%s %s", chat, user, len(outs), kind,
                 core.last.get((chat, user)) if kind == "answer" else "")
        await send_outs(m.bot, chat, outs, edit_message=progress)

    @router.callback_query()
    async def callback(cq):
        chat = cq.message.chat.id if cq.message else cq.from_user.id
        t0 = time.perf_counter()
        toast, outs = await core.on_callback(chat, cq.from_user.id, cq.data, cq.from_user.language_code)
        if qlog is not None and outs and (cq.data or "").startswith("c:"):
            qlog.write("telegram", {"user": cq.from_user.id, "username": cq.from_user.username, "chat": chat},
                       f"[choice {(cq.data or '').rsplit(':', 1)[-1]}]", core.last_kind.get((chat, cq.from_user.id)),
                       core.last.get((chat, cq.from_user.id)), (time.perf_counter() - t0) * 1000,
                       re.sub(r"<[^>]+>", "", outs[0].html) if outs else None)
        try:
            await cq.answer(toast or "", show_alert=bool(toast))
        except Exception:
            log.warning("callback answer failed")
        log.info("callback chat=%s user=%s kind=%s toast=%s %s", chat, cq.from_user.id, (cq.data or "?")[:1], bool(toast),
                 core.last.get((chat, cq.from_user.id)) if outs else "")
        if outs:
            await send_outs(cq.bot, chat, outs)

    return router


async def _heartbeat(path) -> None:
    """Touch a file every 30 s while the event loop is alive (container health check reads its age)."""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    while True:
        p.write_text(str(int(__import__("time").time())), encoding="utf-8")
        await asyncio.sleep(30)


async def run() -> int:
    setup_logging()
    from aiogram import Bot, Dispatcher

    from .config import get_settings
    from .engine import Engine
    from .buildinfo import FrozenBuildMismatch, check_freeze
    from .importer import StaleDatabase, require_database
    from .service import QueryService

    s = get_settings()
    if s.bot_token is None or not s.bot_token.get_secret_value():
        log.error("UZEX_BOT_TOKEN is not set in .env; the bot cannot start")
        return 2
    try:
        require_database(s.data_dir, s.db_path, s.as_of)  # never re-imports at runtime
        check_freeze()
    except (StaleDatabase, FrozenBuildMismatch) as e:
        log.error("%s", e)
        return 2
    engine = Engine(s)
    if engine.planner is None:
        log.error("LLM provider not configured (%s); refusing to start a bot that cannot answer", engine.provider_error)
        engine.close()
        return 2
    service = QueryService(engine, max_concurrent=s.bot_max_concurrent, max_waiting=s.bot_max_waiting)
    core = BotCore(service, session_ttl_s=s.bot_session_ttl_s, context_ttl_s=s.bot_context_ttl_s)
    bot = Bot(token=s.bot_token.get_secret_value())
    dp = Dispatcher()
    from .qlog import QuestionLog
    dp.include_router(build_router(core, QuestionLog(s)))
    me = await bot.get_me()
    from .buildinfo import build_info
    bi = build_info()
    log.info("bot @%s started; provider=%s model=%s budget=%ss build=%s prompt=%s", me.username, engine.provider.name,
             engine.provider.model, s.request_budget_s, bi.get("build_id"), bi.get("prompt_version"))
    hb = asyncio.create_task(_heartbeat(s.bot_heartbeat_path)) if s.bot_heartbeat_path else None
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        if hb:
            hb.cancel()
        await bot.session.close()
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
