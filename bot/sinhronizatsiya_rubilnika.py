# -*- coding: utf-8 -*-
"""Фоновый синк листа-пульта «Рубильник» (запрос заказчика 19.08).

Раз в `GOOGLE_SYNC_INTERVAL_S` читаем вкладку-пульт и применяем состояние
{код: отвечает?} к ядру (`Yadro.ustanovit_rubilnik`). НЕЗАВИСИМ от синка знаний и
каталога: нужен только ключ Google (`GOOGLE_CREDS_PUT`) и флаг `GOOGLE_RUBILNIK`
(по умолчанию on). Цикл живёт под супервизором `main._supervise`.

    Вкладка «Рубильник»  →  {код: отвечает?}  →  Yadro.ustanovit_rubilnik

⚠️ Безопасность отказа: если лист недоступен (сеть/нет вкладки/битый ответ),
состояние НЕ трогаем — ранее заглушённый бот остаётся заглушённым, включённый
включённым. Обнулять к «все отвечают» на сбое чтения нельзя: заказчик мог заглушить
бота осознанно, и молчаливое «само включилось» — хуже, чем «осталось как было».
"""
from __future__ import annotations

import asyncio
from typing import Callable

from .config import Config
from .etl.google_rubilnik import prochitat_rubilnik
from .logger import logger

# Колбэк «применить состояние листа к ядру» — сюда main передаёт Yadro.ustanovit_rubilnik.
Primenit = Callable[[dict[str, bool]], None]


async def sinhronizirovat_rubilnik_odin_raz(
    cfg: Config, kody: list[str], primenit: Primenit
) -> dict[str, bool] | None:
    """Один цикл: прочитать лист-пульт и применить к поднятым аккаунтам.

    Возвращает применённую карту (только по `kody`) или None, если синк выключен
    либо лист не прочитался. Чужие коды из листа игнорируем — глушим лишь то, что
    реально поднято."""
    if not cfg.google.vklyuchena or not cfg.google.rubilnik:
        return None
    g = cfg.google
    try:
        karta = await asyncio.to_thread(
            prochitat_rubilnik, g.creds_put, g.tablica_id, g.list_rubilnik)
    except Exception as e:  # noqa: BLE001 — сеть/нет листа/битый ответ gspread
        logger.warning("🔌 Лист-пульт недоступен (%s) — оставляю текущее состояние ботов", e)
        return None
    tolko = {k: v for k, v in karta.items() if k in kody}
    primenit(tolko)
    return tolko


async def cikl_sinhronizatsii_rubilnika(
    cfg: Config, stop: asyncio.Event, kody: list[str], primenit: Primenit
) -> None:
    """Фоновый цикл синка рубильника: раз в `interval_s` тянем лист-пульт, пока не
    придёт `stop`. Первый прогон — сразу на старте (догоняем состояние до листа)."""
    interval = cfg.google.interval_s
    logger.info("🔌 Синк листа-пульта («%s») включён: аккаунты %s, период %d с",
                cfg.google.list_rubilnik, ", ".join(kody) or "—", interval)
    while not stop.is_set():
        await sinhronizirovat_rubilnik_odin_raz(cfg, kody, primenit)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("🔌 Синк листа-пульта остановлен")
