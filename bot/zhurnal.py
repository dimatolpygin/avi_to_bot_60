# -*- coding: utf-8 -*-
"""Журнал диалога: полная лента (клиент + бот) в Postgres для панели (этап 14.7).

Зачем отдельно от истории в Redis (`pamyat.py`): та живёт 7 суток и нужна модели
для контекста, а панель-виджет в amoCRM показывает переписку менеджеру и хочет её
ДОЛГО и целиком (`messages`/`dialogs` из схемы этапа 3). До 14.7 эти таблицы были
заведены, но в живом потоке не заполнялись — лента жила только в Redis.

Пишем **best-effort, как зеркало amoCRM** (`crm/amojo.py`): БД легла — диалог
клиента падать не должен, поэтому ошибка идёт в лог и глотается. Точка врезки — на
уровне адаптера Авито (там известны канал `avito`, `chat_id`, имя автора),
симметрично `Zerkalo`: входящее клиента и КАЖДАЯ реально отправленная реплика бота.
«Только отправленное» соблюдается само собой — `otpravit` зовётся лишь для реплик,
которые ушли в чат (отменённый хвост сюда не попадает).
"""
from __future__ import annotations

from sqlalchemy import func, select

from .logger import log_oshibka
from .models import Account, Dialog, Message


class Zhurnal:
    """Пишет реплики диалога одного аккаунта в Postgres. `fabrika_sessiy=None` —
    журнал выключен (нет БД): все методы становятся no-op, бот работает как раньше."""

    def __init__(self, fabrika_sessiy, kod: str, kanal: str) -> None:
        self._fabrika = fabrika_sessiy
        self._kod = kod
        self._kanal = kanal

    async def vhodyashchee(self, chat_key, tekst, *, imya: str | None = None,
                           rid: str | None = None) -> None:
        """Реплика клиента → строка с ролью client."""
        await self._zapisat(chat_key, "client", tekst, imya=imya, rid=rid)

    async def ishodyashchee(self, chat_key, tekst, *, rid: str | None = None) -> None:
        """Реально отправленная реплика бота → строка с ролью bot."""
        await self._zapisat(chat_key, "bot", tekst, rid=rid)

    async def _zapisat(self, chat_key, rol: str, tekst: str, *,
                       imya: str | None = None, rid: str | None = None) -> None:
        if self._fabrika is None or not (tekst or "").strip():
            return
        try:
            async with self._fabrika() as sessiya:
                akk = (await sessiya.execute(
                    select(Account).where(Account.code == self._kod)
                )).scalar_one_or_none()
                if akk is None:
                    return                      # аккаунт не засеян — писать некуда
                dialog = (await sessiya.execute(
                    select(Dialog).where(Dialog.account_id == akk.id,
                                         Dialog.channel == self._kanal,
                                         Dialog.chat_key == str(chat_key))
                )).scalar_one_or_none()
                if dialog is None:
                    dialog = Dialog(account_id=akk.id, channel=self._kanal,
                                    chat_key=str(chat_key), client_username=imya)
                    sessiya.add(dialog)
                    await sessiya.flush()       # нужен dialog.id для сообщения
                else:
                    dialog.last_message_at = func.now()
                    if imya and not dialog.client_username:
                        dialog.client_username = imya
                sessiya.add(Message(dialog_id=dialog.id, role=rol, body=tekst,
                                    request_id=rid))
                await sessiya.commit()
        except Exception as e:  # noqa: BLE001 — best-effort, диалог не роняем
            log_oshibka(f"Журнал диалога не записан ({self._kod}/{chat_key}): {e}")
