# -*- coding: utf-8 -*-
"""Прогон запросов через поиск: `python -m bot.search.probe "липа 3 метра"`.

Каталог по умолчанию читается прямо из выгрузки прайса — БД поднимать не надо.
`--iz-bd` берёт `products` аккаунта, то есть ровно то, с чем работает бот.
Оба пути обязаны давать одинаковую выдачу; расхождение значит, что импорт
и файл разошлись, и это само по себе находка.

Без аргументов гоняет набор из критериев приёмки этапа 6 — им и проверяют этап.
"""
from __future__ import annotations

import asyncio
import sys

from ..logger import logger
from .katalog import iz_fayla_praysa
from .search import Poisk

# Запросы из критериев приёмки этапа 6. Все — из живых диалогов клиента.
PROVERKA = [
    "липа 3 метра",
    "печь на 16 кубов",
    "123405",
    "нужна штиль",
    "дверь 1900 на 700",
    "плитка из соли",
    "вагонка сорт б",
    "полок абаши",
    "болгарка",
]


async def _katalog_iz_bd(kod_akkaunta: str):
    from ..config import load_config
    from ..db import podklyuchit, sozdat_fabriku_sessiy, zakryt
    from .katalog import zagruzit_iz_bd

    cfg = load_config()
    engine = await podklyuchit(cfg)
    try:
        Sessiya = sozdat_fabriku_sessiy(engine)
        async with Sessiya() as sess:
            return await zagruzit_iz_bd(sess, kod_akkaunta)
    finally:
        await zakryt(engine)


def _pokazat(poisk: Poisk, zapros: str) -> None:
    nahodki, kanal = poisk.iskat(zapros)
    print(f"\n▸ {zapros!r}   → канал: {kanal}")
    if not nahodki:
        print("  (пусто — так и должно быть, если товара нет или домен чужой)")
        return
    for i, n in enumerate(nahodki, 1):
        print(f"  {i}. [{n.ball:5.1f}] {n.kratko()}")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    iz_bd = "--iz-bd" in argv
    akkaunt = "saunamart"

    katalog = asyncio.run(_katalog_iz_bd(akkaunt)) if iz_bd else iz_fayla_praysa()
    poisk = Poisk(katalog)

    zaprosy = args or PROVERKA
    if not args:
        print("Проверочный набор этапа 6 (свой запрос: python -m bot.search.probe \"…\")")
    for q in zaprosy:
        _pokazat(poisk, q)
    return 0


if __name__ == "__main__":
    logger.info("Поиск по каталогу — ручная проверка (этап 6)")
    raise SystemExit(main(sys.argv))
