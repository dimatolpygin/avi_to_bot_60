# -*- coding: utf-8 -*-
"""Сид базы знаний: `python -m bot.seed_znaniya` (этап 11; товарный — этап 19 Шаг 2).

Раскладывает блоки знаний по таблицам БД:

* УСЛУГИ (SB SAUNA, Дешман): `knowledge_blocks` (стоп-лист, гео, ориентир,
  «чего ты не знаешь»…) + `account_prompts` (персона и шапка). Тексты —
  `bot/znaniya.py`;
* ТОВАРНЫЙ (Saunamart): `knowledge_blocks` (персона, доставка, контакты, FAQ,
  возражения, сорта…), БЕЗ `account_prompts` — персона зашита в скелет промпта.
  Тексты — `bot/znaniya_tovar.py`.

Источник текстов один с код-фолбэком (`profili.py` / `agent.py`), поэтому БД и
фолбэк не разъедутся.

**Идемпотентно и бережно**: существующие строки не трогаем вовсе — повторный
запуск ничего не дублирует и не затирает правки, сделанные через панель
(этап 13). Добавляются только недостающие блоки. Отдельная команда, а не часть
`bot.seed`: базовые сиды (аккаунты, заглушка промпта) нужны для старта, а база
знаний — контент этапа 11, и её удобно пересевать независимо.
"""
import asyncio

from sqlalchemy import select

from .config import load_config
from .db import podklyuchit, sozdat_fabriku_sessiy, zakryt
from .logger import logger
from .models import Account, AccountPrompt, KnowledgeBlock
from .profili import profil
from .znaniya import PERSONY, bloki_akkaunta
from .znaniya_tovar import BLOKI_SAUNAMART

# Аккаунты услуг: у них персона в account_prompts + блоки знаний.
AKKAUNTY_USLUG = ["sbsauna", "sbsauna_deshman"]
# Товарный: блоки знаний ЕСТЬ (этап 19 Шаг 2), а персоны в account_prompts НЕТ —
# она зашита в скелет промпта (`znaniya_tovar.SISTEMNY_SHABLON`).
AKKAUNT_TOVARNYY = "saunamart"


async def _zaseyat_bloki(sessiya, account_id: int, kod: str, bloki) -> int:
    """Завести недостающие блоки знаний по ключу, с шагом sort 10. Существующие
    не трогаем (идемпотентно, правки панели/вкладки не затираем)."""
    est_klyuchi = set((await sessiya.scalars(
        select(KnowledgeBlock.key).where(KnowledgeBlock.account_id == account_id))).all())
    dobavleno = 0
    for i, blok in enumerate(bloki, start=1):
        if blok.key in est_klyuchi:
            logger.info("📚 «%s»: блок «%s» уже есть — не трогаю", kod, blok.key)
            continue
        sessiya.add(KnowledgeBlock(account_id=account_id, key=blok.key,
                                   title=blok.title, content=blok.content, sort=i * 10))
        dobavleno += 1
        logger.info("📚 «%s»: завожу блок «%s» (%s)", kod, blok.key, blok.title)
    return dobavleno


async def _account_id(sessiya, kod: str) -> int | None:
    account_id = await sessiya.scalar(select(Account.id).where(Account.code == kod))
    if account_id is None:
        logger.warning("📚 Аккаунта «%s» нет в БД — пропускаю (сначала `python -m bot.seed`)", kod)
    return account_id


async def _zaseyat_uslugi(sessiya, kod: str) -> int:
    """Завести недостающие персону и блоки аккаунта УСЛУГ. Возвращает число новых строк."""
    account_id = await _account_id(sessiya, kod)
    if account_id is None:
        return 0

    dobavleno = 0
    # Персона и шапка → account_prompts (одна активная версия на аккаунт).
    est_ap = await sessiya.scalar(
        select(AccountPrompt.id)
        .where(AccountPrompt.account_id == account_id, AccountPrompt.is_active.is_(True)))
    if est_ap:
        logger.info("📚 «%s»: персона уже есть (id %s) — не трогаю", kod, est_ap)
    else:
        persona = PERSONY[kod]
        sessiya.add(AccountPrompt(account_id=account_id, version=1,
                                  persona=persona.imya, body=persona.shapka,
                                  updated_by="seed_znaniya"))
        dobavleno += 1
        logger.info("📚 «%s»: завожу персону «%s»", kod, persona.imya)

    dobavleno += await _zaseyat_bloki(sessiya, account_id, kod, bloki_akkaunta(kod))
    return dobavleno


async def _zaseyat_tovarnyy(sessiya, kod: str) -> int:
    """Завести недостающие блоки знаний ТОВАРНОГО аккаунта (без персоны). Число новых строк."""
    account_id = await _account_id(sessiya, kod)
    if account_id is None:
        return 0
    return await _zaseyat_bloki(sessiya, account_id, kod, BLOKI_SAUNAMART)


async def zaseyat() -> int:
    """Завести недостающее по всем аккаунтам (услуги + товарный). Число новых строк."""
    cfg = load_config()
    engine = await podklyuchit(cfg)
    Sessiya = sozdat_fabriku_sessiy(engine)
    dobavleno = 0
    try:
        async with Sessiya() as s, s.begin():
            for kod in AKKAUNTY_USLUG:
                # profil() заодно проверяет, что код известен и он не товарный.
                if profil(kod).tovarnyy:
                    continue
                dobavleno += await _zaseyat_uslugi(s, kod)
            if profil(AKKAUNT_TOVARNYY).tovarnyy:
                dobavleno += await _zaseyat_tovarnyy(s, AKKAUNT_TOVARNYY)
        logger.info("📚 База знаний засеяна, новых записей: %d", dobavleno)
        return dobavleno
    finally:
        await zakryt(engine)


if __name__ == "__main__":
    asyncio.run(zaseyat())
