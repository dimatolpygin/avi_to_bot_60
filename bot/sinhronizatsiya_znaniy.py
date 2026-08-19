# -*- coding: utf-8 -*-
"""Фоновая синхронизация БАЗЫ ЗНАНИЙ из Google-таблицы (этап 19).

Аналог `sinhronizatsiya.py` (каталог), но для блоков знаний: раз в
`GOOGLE_SYNC_INTERVAL_S` секунд читаем вкладку знаний каждого аккаунта и обновляем
`knowledge_blocks`; горячую перезагрузку промпта в памяти ядра подключает колбэк
`posle_zapisi` (для услуг — `Yadro.perezagruzit_prompt_uslug`, для товарного —
`perezagruzit_prompt_tovarnyy`; диспетчеризация в `main`). Шаг 1 — услуги
(SB SAUNA, Дешман), Шаг 2 — товарный (вкладка «Saunamart (1)»). Логика синка
одинакова: аккаунт резолвится по коду, персона (`account_prompts`) не трогается.

    Вкладка Google  →  БД (`knowledge_blocks`)  →  промпт услуг в памяти ядра

Лестница отказа — та же, что у каталога: источник лёг → работаем на версии из БД,
запись упала → откат, наружу не бросаем. Фоновый цикл не падает из-за минутного
сбоя Google и живёт под супервизором `main._supervise`.

Отличия от каталога:
- аккаунтов несколько (услуги), у каждого своя вкладка (`LISTY_ZNANIY`);
- синхронизация НЕ трогает персону/шапку (`account_prompts`) — только блоки знаний;
  персона правится в БД (открытый вопрос роадмапа), сюда не входит;
- удалённую из вкладки строку (сироту) НЕ гасим молча, а предупреждаем в лог:
  выключать блок заказчик должен колонкой «Вкл» = «нет», а не удалением строки;
- СЛУЖЕБНЫЕ блоки услуг (`SLUZHEBNYE_KLYUCHI`: механика и предохранители) синк НЕ
  трогает — во вкладке они стоят заглушкой, их текст остаётся из кода/сида, иначе
  заглушка затёрла бы, например, запрет боту называть выдуманные цены.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from sqlalchemy import select

from .config import Config
from .etl.google_znaniya import (LISTY_ZNANIY, SLUZHEBNYE_KLYUCHI, BlokStroka,
                                 OshibkaZnaniy, prochitat_znaniya)
from .logger import logger

# Колбэк «блоки аккаунта в БД обновились» — сюда main передаёт горячую
# перезагрузку промпта услуг. Зовётся только при реальных изменениях.
PosleZapisi = Callable[[str], Awaitable[None]]


@dataclass
class ItogiZnaniy:
    """Итог синка одного аккаунта: сколько блоков добавлено/обновлено/без изменений
    и сколько «сирот» (есть в БД активными, но нет во вкладке)."""

    dobavleno: int = 0
    obnovleno: int = 0
    bez_izmeneniy: int = 0
    siroty: list[str] = field(default_factory=list)

    @property
    def izmenilos(self) -> bool:
        return bool(self.dobavleno or self.obnovleno)


class _Sushchestvuyushchiy(Protocol):
    """Минимум, который нужен планировщику от строки БД (для чистого теста)."""
    title: str
    content: str
    sort: int
    is_active: bool


@dataclass
class Plan:
    """Что сделать с блоками аккаунта: чистый результат сверки вкладки с БД."""

    vstavit: list[tuple[BlokStroka, int]] = field(default_factory=list)          # (блок, sort)
    obnovit: list[tuple[object, BlokStroka, int]] = field(default_factory=list)  # (строка_БД, блок, sort)
    bez_izmeneniy: int = 0
    siroty: list[str] = field(default_factory=list)


def splanirovat(sushchestvuyushchie: dict[str, _Sushchestvuyushchiy],
                vkladka: list[BlokStroka],
                zashchishchennye: frozenset[str] = frozenset()) -> Plan:
    """Сверить блоки вкладки с блоками БД. Чистая функция — вся логика синка здесь.

    Порядок вкладки задаёт `sort` (шаг 10, как в сиде). Совпадение по ключу:
    поля не изменились → «без изменений»; изменились → в обновление; ключа в БД
    нет → во вставку. Ключи, что есть в БД активными, но пропали из вкладки, —
    «сироты» (в лог, не гасим): удаление строки не должно тихо ронять блок.

    `zashchishchennye` — СЛУЖЕБНЫЕ (code-owned) ключи: механика/предохранители, во
    вкладке стоящие заглушкой. Их НЕ вставляем и НЕ обновляем из вкладки (иначе
    заглушка затёрла бы механику в БД) и сиротами не считаем (в БД они из сида).
    """
    plan = Plan()
    vo_vkladke: set[str] = set()
    for i, blok in enumerate(vkladka, start=1):
        vo_vkladke.add(blok.key)
        if blok.key in zashchishchennye:
            continue   # служебный блок: вкладка его не трогает, текст из кода/сида
        sort = i * 10
        cur = sushchestvuyushchie.get(blok.key)
        if cur is None:
            plan.vstavit.append((blok, sort))
        elif (cur.title, cur.content, cur.sort, cur.is_active) == \
                (blok.title, blok.content, sort, blok.vkl):
            plan.bez_izmeneniy += 1
        else:
            plan.obnovit.append((cur, blok, sort))
    plan.siroty = [k for k, v in sushchestvuyushchie.items()
                   if k not in vo_vkladke and v.is_active and k not in zashchishchennye]
    return plan


async def zapisat_znaniya(vkladka: list[BlokStroka], kod: str, Sessiya) -> ItogiZnaniy:
    """Применить блоки вкладки к `knowledge_blocks` аккаунта. Одна транзакция.

    Персону/шапку (`account_prompts`) не трогаем. Сироты — только в лог. Пустая
    вкладка (0 блоков) — считаем сбоем разбора и НЕ применяем: иначе один кривой
    синк обнулил бы все блоки услуг. Возвращает `ItogiZnaniy`.
    """
    from .models import Account, KnowledgeBlock

    if not vkladka:
        raise OshibkaZnaniy(f"вкладка знаний «{kod}» разобралась в 0 блоков — не применяю")

    async with Sessiya() as s, s.begin():
        account_id = await s.scalar(select(Account.id).where(Account.code == kod))
        if account_id is None:
            raise OshibkaZnaniy(f"аккаунта «{kod}» нет в БД (сначала `python -m bot.seed`)")

        sushchestvuyushchie = {
            b.key: b for b in (await s.scalars(
                select(KnowledgeBlock).where(KnowledgeBlock.account_id == account_id))).all()}

        plan = splanirovat(sushchestvuyushchie, vkladka, SLUZHEBNYE_KLYUCHI)

        for blok, sort in plan.vstavit:
            s.add(KnowledgeBlock(account_id=account_id, key=blok.key, title=blok.title,
                                 content=blok.content, sort=sort, is_active=blok.vkl))
        for cur, blok, sort in plan.obnovit:
            cur.title, cur.content, cur.sort, cur.is_active = \
                blok.title, blok.content, sort, blok.vkl

    itogi = ItogiZnaniy(dobavleno=len(plan.vstavit), obnovleno=len(plan.obnovit),
                        bez_izmeneniy=plan.bez_izmeneniy, siroty=plan.siroty)
    if itogi.izmenilos:
        logger.info("📚 Синк знаний «%s»: блоки обновлены (+%d ~%d =%d)",
                    kod, itogi.dobavleno, itogi.obnovleno, itogi.bez_izmeneniy)
    if itogi.siroty:
        logger.warning("📚 Синк знаний «%s»: в БД есть активные блоки, которых НЕТ во "
                       "вкладке: %s. Чтобы выключить блок, ставь «Вкл»=«нет», а не "
                       "удаляй строку — сейчас он остаётся прежним.",
                       kod, ", ".join(itogi.siroty))
    return itogi


async def sinhronizirovat_znaniya_odin_raz(
    cfg: Config, Sessiya, kody: list[str], *, posle_zapisi: PosleZapisi | None = None
) -> dict[str, ItogiZnaniy | None]:
    """Один цикл синка знаний по всем переданным аккаунтам услуг.

    На каждый аккаунт: читаем его вкладку → применяем к БД → (опц.) горячая
    перезагрузка промпта. Сбой одного аккаунта не мешает остальным. Возвращает
    {код: Итоги|None}, где None — источник или запись этого аккаунта не удались.
    """
    if not cfg.google.vklyuchena:
        return {}

    g = cfg.google
    rezultat: dict[str, ItogiZnaniy | None] = {}
    for kod in kody:
        list_name = LISTY_ZNANIY.get(kod)
        if not list_name:
            continue   # у аккаунта нет вкладки знаний (напр. товарный на Шаге 1)
        try:
            vkladka = await asyncio.to_thread(
                prochitat_znaniya, g.creds_put, g.tablica_id, list_name)
        except OshibkaZnaniy as e:
            logger.warning("📚 Вкладка знаний «%s» недоступна (%s) — работаю на версии из БД", kod, e)
            rezultat[kod] = None
            continue
        except Exception as e:  # noqa: BLE001 — сеть/таймаут/битый ответ gspread
            logger.warning("📚 Вкладка знаний «%s»: непредвиденный сбой чтения (%s) — "
                           "работаю на версии из БД", kod, e)
            rezultat[kod] = None
            continue

        try:
            itogi = await zapisat_znaniya(vkladka, kod, Sessiya)
        except OshibkaZnaniy as e:
            logger.error("📚 Синк знаний «%s»: запись не применена: %s", kod, e)
            rezultat[kod] = None
            continue
        except Exception as e:  # noqa: BLE001 — обрыв БД, откат транзакции
            logger.error("📚 Синк знаний «%s»: непредвиденная ошибка записи (%s)", kod, e,
                         exc_info=True)
            rezultat[kod] = None
            continue

        rezultat[kod] = itogi
        if itogi.izmenilos and posle_zapisi is not None:
            try:
                await posle_zapisi(kod)
            except Exception as e:  # noqa: BLE001 — перезагрузка не роняет синк
                logger.error("📚 Синк знаний «%s»: перезагрузка промпта не удалась (%s)",
                             kod, e, exc_info=True)
    return rezultat


async def cikl_sinhronizatsii_znaniy(
    cfg: Config, Sessiya, stop: asyncio.Event, kody: list[str], *,
    posle_zapisi: PosleZapisi | None = None
) -> None:
    """Фоновый цикл синка знаний: раз в `interval_s` тянем вкладки в БД, пока не
    придёт `stop`. Первый прогон — сразу на старте (догоняем БД до вкладки)."""
    interval = cfg.google.interval_s
    listy = ", ".join(LISTY_ZNANIY.get(k, "?") for k in kody)
    logger.info("📚 Синхронизация базы знаний из Google-таблицы включена: вкладки «%s», "
                "период %d с", listy, interval)
    while not stop.is_set():
        await sinhronizirovat_znaniya_odin_raz(cfg, Sessiya, kody, posle_zapisi=posle_zapisi)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("📚 Синхронизация базы знаний остановлена")
