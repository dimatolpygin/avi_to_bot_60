# -*- coding: utf-8 -*-
"""Фоновая синхронизация каталога из Google-таблицы (этап 16, курс 31.07).

Заказчик правит прайс прямо в своей гугл-таблице, а бот подтягивает изменения
сам — без нас и без ручного `import_prays`. Раз в `GOOGLE_SYNC_INTERVAL_S` секунд
фоновая задача читает таблицу и обновляет `products`; горячую перезагрузку каталога
в память ядра подключает этап A5 (колбэк `posle_zapisi`).

Кеш двухуровневый (курс 31.07):
    Google-таблица  →  БД (`products`)  →  каталог в памяти ядра
Бот отвечает из памяти и в Google на каждое сообщение НЕ ходит. Лестница отказа:
- Google недоступен (сеть, доступ, лист) → лог, БД остаётся на прошлой версии,
  бот работает на ней; ретрай в следующем цикле.
- Запись в БД не удалась → лог, транзакция откатилась целиком (полприайса в БД
  не остаётся), ретрай в следующем цикле.
- Крайний офлайн-фолбэк (нет и БД) — файл прайса; он живёт в `core._katalog`
  при старте, синка не касается.

Главное свойство: **фоновый цикл не падает** из-за того, что источник на минуту
прилёг. Наружу ошибки не бросаются — только в лог; цикл живёт под супервизором
`main._supervise` до сигнала остановки.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .config import Config
from .etl.chtenie import OshibkaPraysa
from .etl.google_prays import prochitat_google
from .etl.import_prays import AKKAUNT_PO_UMOLCHANIYU, Itogi, zapisat_prays
from .logger import logger

# Синхронизируем только товарный аккаунт: прайс есть лишь у Saunamart, у двух
# аккаунтов услуг каталога не существует вовсе.
AKKAUNT = AKKAUNT_PO_UMOLCHANIYU

# Колбэк «каталог в БД обновился» — этап A5 передаёт сюда горячую перезагрузку
# памяти ядра. Вызывается только когда запись реально что-то изменила.
PosleZapisi = Callable[[], Awaitable[None]]


async def sinhronizirovat_odin_raz(
    cfg: Config, Sessiya, *, posle_zapisi: PosleZapisi | None = None
) -> Itogi | None:
    """Один цикл синка: Google-таблица → БД → (опц.) перезагрузка каталога.

    Возвращает `Itogi` при успешной записи, `None` если источник или запись
    не удались (беда уже в логе, БД осталась на прошлой версии). Наружу не
    бросает: фоновый цикл не должен падать из-за минутного сбоя Google.
    """
    if not cfg.google.vklyuchena:
        return None

    g = cfg.google
    try:
        # Чтение таблицы блокирующее (gspread + сеть) — уводим в поток, чтобы
        # не морозить event loop с живыми диалогами.
        prays = await asyncio.to_thread(
            prochitat_google, g.creds_put, g.tablica_id, g.list_name)
    except OshibkaPraysa as e:
        logger.warning("🔄 Google-таблица недоступна (%s) — работаю на версии из БД", e)
        return None
    except Exception as e:  # noqa: BLE001 — сеть/таймаут/битый ответ gspread
        logger.warning("🔄 Google-таблица: непредвиденный сбой чтения (%s) — "
                       "работаю на версии из БД", e)
        return None

    try:
        itogi = await zapisat_prays(prays, AKKAUNT, Sessiya)
    except OshibkaPraysa as e:
        logger.error("🔄 Синк: запись в БД не удалась: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 — обрыв БД, откат транзакции
        logger.error("🔄 Синк: непредвиденная ошибка записи в БД: %s", e, exc_info=True)
        return None

    izmenilos = itogi.dobavleno or itogi.obnovleno or itogi.pogasheno
    if izmenilos:
        logger.info("🔄 Синк: каталог обновлён из таблицы (+%d ~%d -%d)",
                    itogi.dobavleno, itogi.obnovleno, itogi.pogasheno)
    if posle_zapisi is not None and izmenilos:
        try:
            await posle_zapisi()
        except Exception as e:  # noqa: BLE001 — перезагрузка не должна ронять синк
            logger.error("🔄 Синк: перезагрузка каталога в память не удалась: %s",
                         e, exc_info=True)
    return itogi


async def cikl_sinhronizatsii(
    cfg: Config, Sessiya, stop: asyncio.Event, *, posle_zapisi: PosleZapisi | None = None
) -> None:
    """Фоновый цикл: раз в `interval_s` тянем таблицу в БД, пока не придёт `stop`.

    Первый прогон — сразу на старте: каталог в памяти уже поднят при подготовке
    ядра (из БД или файла), синк лишь догоняет БД до свежей таблицы. Дальше —
    по интервалу. Живёт под супервизором `main._supervise`.
    """
    interval = cfg.google.interval_s
    logger.info("🔄 Синхронизация каталога из Google-таблицы включена: лист «%s», период %d с",
                cfg.google.list_name, interval)
    while not stop.is_set():
        await sinhronizirovat_odin_raz(cfg, Sessiya, posle_zapisi=posle_zapisi)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("🔄 Синхронизация каталога остановлена")
