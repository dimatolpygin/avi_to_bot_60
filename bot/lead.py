# -*- coding: utf-8 -*-
"""Передача лида менеджеру: контакт клиента + выжимка диалога.

Клиент сам оставил телефон — значит переписку дальше ведёт человек, и ему
нужен не только номер, но и о чём был разговор. Выжимку пишет модель тем же
вызовом, которым отдаёт контакт: диалог у неё уже в контексте, отдельный поход
к ней стоил бы денег и времени на ровном месте.

Лид ложится в `sbavito.leads` со статусом `new` и ждёт отправки в amoCRM.
Порядок именно такой — **сначала БД, потом CRM**, — потому что падение amo
не должно терять контакт: повторная отправка идёт по `status = failed`.
Ключей канала amoCRM на 23.07 ещё нет (заявка в поддержке), поэтому сейчас
живёт первая половина: сохранение и заметный след в логе. Этап 12 подставит
отправку, ничего здесь не переписывая.

Сбой БД лид не роняет: ошибка идёт в лог целиком, а клиент получает свой
ответ. Потерять строку в таблице неприятно, оборвать живой диалог — хуже.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from .logger import log_lead, log_oshibka
from .models import Account, Dialog, Lead

# Телефон в переписке приходит как угодно: «8 900 123-45-67», «+7(900)1234567».
# В amo уедет то, что написал клиент, но в БД кладём нормализованный вид —
# иначе один и тот же человек с двумя разными записями считается двумя лидами.
_NE_CIFRA = re.compile(r"\D+")


def normalizovat_telefon(syroy: str | None) -> str | None:
    """«8 (900) 123-45-67» → «+79001234567». Не похоже на телефон — вернём как есть."""
    if not syroy:
        return None
    cifry = _NE_CIFRA.sub("", syroy)
    if len(cifry) == 11 and cifry[0] in "78":
        return f"+7{cifry[1:]}"
    if len(cifry) == 10:
        return f"+7{cifry}"
    return syroy.strip() or None


@dataclass(frozen=True)
class DannyeLida:
    """Что бот узнал о клиенте к моменту передачи менеджеру."""

    kod_akkaunta: str
    chat: str
    kanal: str = "telegram"          # на этапе 14 станет «avito»
    telefon: str | None = None
    imya: str | None = None
    vyzhimka: str = ""               # о чём был разговор, словами модели
    imya_v_kanale: str | None = None  # @username или имя из профиля мессенджера


async def sohranit_lead(fabrika_sessiy, dannye: DannyeLida) -> int | None:
    """Записать лид в БД. Возвращает id строки или None, если не вышло.

    Диалог заводится (или находится) здесь же: менеджеру нужен не голый номер,
    а переписка, к которой он привязан. Уникальность диалога — по тройке
    аккаунт + канал + чат, как в схеме этапа 3.
    """
    if fabrika_sessiy is None:
        log_oshibka("Лид некуда сохранить: фабрика сессий не передана в ядро")
        return None
    try:
        async with fabrika_sessiy() as sessiya:
            akk = (await sessiya.execute(
                select(Account).where(Account.code == dannye.kod_akkaunta)
            )).scalar_one_or_none()
            if akk is None:
                log_oshibka(f"Лид не сохранён: аккаунта «{dannye.kod_akkaunta}» нет в БД")
                return None

            dialog = (await sessiya.execute(
                select(Dialog).where(Dialog.account_id == akk.id,
                                     Dialog.channel == dannye.kanal,
                                     Dialog.chat_key == str(dannye.chat))
            )).scalar_one_or_none()
            if dialog is None:
                dialog = Dialog(account_id=akk.id, channel=dannye.kanal,
                                chat_key=str(dannye.chat),
                                client_name=dannye.imya,
                                client_username=dannye.imya_v_kanale)
                sessiya.add(dialog)
                await sessiya.flush()
            elif dannye.imya and not dialog.client_name:
                dialog.client_name = dannye.imya

            lid = Lead(account_id=akk.id, dialog_id=dialog.id,
                       phone=normalizovat_telefon(dannye.telefon),
                       name=dannye.imya, note=dannye.vyzhimka or None,
                       status="new")
            sessiya.add(lid)
            await sessiya.commit()
            log_lead(lid.id, dannye.telefon, dannye.imya, dannye.vyzhimka)
            return lid.id
    except Exception as e:  # noqa: BLE001 — см. модульную docstring
        log_oshibka(f"Лид не сохранён ({dannye.telefon}): {e}")
        return None
