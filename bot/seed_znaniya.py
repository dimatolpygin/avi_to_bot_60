# -*- coding: utf-8 -*-
"""Сид базы знаний услуг: `python -m bot.seed_znaniya` (этап 11).

Раскладывает промпт аккаунтов услуг по таблицам БД:

* `knowledge_blocks` — блоки знаний (стоп-лист, гео, ориентир, «чего ты не
  знаешь»…), свой набор на каждый из двух сервисных аккаунтов;
* `account_prompts` — персона и шапка промпта.

Тексты берутся из `bot/znaniya.py` — того же места, откуда собран код-фолбэк
в `profili.py`. Источник один, поэтому БД и фолбэк не разъедутся.

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

# Только аккаунты услуг: у товарного промпт собирается из каталога.
AKKAUNTY_USLUG = ["sbsauna", "sbsauna_deshman"]


async def _zaseyat_akkaunt(sessiya, kod: str) -> int:
    """Завести недостающие блоки и персону одного аккаунта. Возвращает число
    новых строк (0 = всё уже было)."""
    account_id = await sessiya.scalar(select(Account.id).where(Account.code == kod))
    if account_id is None:
        logger.warning("📚 Аккаунта «%s» нет в БД — пропускаю (сначала `python -m bot.seed`)", kod)
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

    # Блоки знаний → knowledge_blocks, по ключу, с шагом sort 10.
    est_klyuchi = set((await sessiya.scalars(
        select(KnowledgeBlock.key).where(KnowledgeBlock.account_id == account_id))).all())
    for i, blok in enumerate(bloki_akkaunta(kod), start=1):
        if blok.key in est_klyuchi:
            logger.info("📚 «%s»: блок «%s» уже есть — не трогаю", kod, blok.key)
            continue
        sessiya.add(KnowledgeBlock(account_id=account_id, key=blok.key,
                                   title=blok.title, content=blok.content, sort=i * 10))
        dobavleno += 1
        logger.info("📚 «%s»: завожу блок «%s» (%s)", kod, blok.key, blok.title)

    return dobavleno


async def zaseyat() -> int:
    """Завести недостающее по всем аккаунтам услуг. Возвращает число новых строк."""
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
                dobavleno += await _zaseyat_akkaunt(s, kod)
        logger.info("📚 База знаний засеяна, новых записей: %d", dobavleno)
        return dobavleno
    finally:
        await zakryt(engine)


if __name__ == "__main__":
    asyncio.run(zaseyat())
