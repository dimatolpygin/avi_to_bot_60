# -*- coding: utf-8 -*-
"""Адаптер Telegram: один бот на один аккаунт Авито (этап 10).

Тонкий слой. Всё, что он делает: принимает апдейт, отдаёт текст ядру и умеет
отправить реплику с индикатором набора. Диалог, память, задержки и дробление —
в ядре и в диспетчере этапа 9, чтобы адаптер Авито (этап 14) писался с нуля
за вечер и ничего доменного не тянул.

Три бота живут в ОДНОМ процессе разными задачами polling. Падение одного
не должно ронять соседей — за этим следит супервизор в `bot/main.py`.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher as AiogramDispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from ..chelovek.dispetcher import Kanal
from ..core import Yadro
from ..logger import log_ishodyashchee, log_oshibka, logger, nachat_zapros
from ..profili import profil


def _kanal(bot: Bot, chat_id: int, imya: str) -> Kanal:
    """Колбэки транспорта для диспетчера: отправить реплику и показать набор."""
    async def otpravit(tekst: str) -> None:
        await bot.send_message(chat_id, tekst)

    async def pechataet() -> None:
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception as e:  # noqa: BLE001
            # Индикатор — украшение: клиент переживёт его отсутствие, а вот
            # падение всей отправки из-за него — нет.
            logger.debug("Индикатор набора не выставился: %s", e)

    return Kanal(otpravit=otpravit, pechataet=pechataet, imya=imya)


def _imya_klienta(m: Message) -> str:
    u = m.from_user
    if u is None:
        return "—"
    return u.username or u.full_name or str(u.id)


def sobrat_bota(kod: str, token: str, yadro: Yadro) -> tuple[Bot, AiogramDispatcher]:
    """Бот и его хендлеры под конкретный аккаунт."""
    prof = profil(kod)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    dp = AiogramDispatcher()

    @dp.message(CommandStart())
    async def na_start(m: Message) -> None:
        with nachat_zapros(kod):
            logger.info("👤 @%s (id:%s) → /start", _imya_klienta(m), m.chat.id)
            await yadro.sbros(kod, m.chat.id)
            # Приветствие уходит в обход модели, поэтому кладём его в историю
            # руками: иначе на первый же вопрос бот представится второй раз.
            tekst = await yadro.zapomnit_privetstvie(kod, m.chat.id)
            await m.answer(tekst)
            log_ishodyashchee(_imya_klienta(m), tekst, meta="[приветствие]")

    @dp.message(Command("reset"))
    async def na_reset(m: Message) -> None:
        with nachat_zapros(kod):
            logger.info("👤 @%s (id:%s) → /reset", _imya_klienta(m), m.chat.id)
            await yadro.sbros(kod, m.chat.id)
            tekst = "Начнём заново. Чем помочь?"
            await m.answer(tekst)
            log_ishodyashchee(_imya_klienta(m), tekst, meta="[сброс]")

    @dp.message(F.text)
    async def na_tekst(m: Message) -> None:
        yadro.obrabotat(kod, m.chat.id, m.text or "",
                        _kanal(bot, m.chat.id, _imya_klienta(m)))

    @dp.message()
    async def na_proche(m: Message) -> None:
        """Фото, голосовые, файлы. Авито их тоже присылает, и молчать нельзя."""
        with nachat_zapros(kod):
            logger.info("👤 @%s (id:%s) → вложение без текста", _imya_klienta(m), m.chat.id)
            tekst = ("Вложение вижу, но прочитать его не могу. Напишите, пожалуйста, "
                     "текстом, что нужно.")
            await m.answer(tekst)
            log_ishodyashchee(_imya_klienta(m), tekst, meta="[вложение]")

    logger.info("🤖 Бот аккаунта «%s» собран, персона: %s из %s",
                kod, prof.menedzher, prof.kompaniya)
    return bot, dp


async def zapustit(kod: str, token: str, yadro: Yadro) -> None:
    """Поллинг одного бота. Возврат = штатное завершение, исключение = падение
    (его подхватит супервизор и перезапустит канал)."""
    bot, dp = sobrat_bota(kod, token, yadro)
    try:
        ya = await bot.get_me()
        logger.info("📡 «%s» → @%s (%s), поллинг запущен", kod, ya.username, ya.first_name)
        # drop_pending_updates: после простоя не отвечаем на вчерашние сообщения —
        # ответ через сутки хуже молчания.
        await dp.start_polling(bot, handle_signals=False, drop_pending_updates=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log_oshibka(f"Канал Telegram «{kod}» упал: {e}")
        raise
    finally:
        await bot.session.close()
        logger.info("📡 «%s»: поллинг остановлен", kod)
