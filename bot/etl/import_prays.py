# -*- coding: utf-8 -*-
"""Импорт прайса в `products`: `python -m bot.etl.import_prays [файл]`.

    python -m bot.etl.import_prays                     # путь из PRICE_FILE или дефолтный
    python -m bot.etl.import_prays data/prays.xlsx     # явный файл
    python -m bot.etl.import_prays --tolko-razbor      # прогон без БД: что распозналось

Идемпотентность держится на **артикуле**: он уникален в пределах аккаунта
(`uq_products_account_article`). Повторный импорт того же файла обязан дать
0 insert / 0 update / 0 deactivated — это критерий приёмки этапа 4 и защита
от «каждый запуск переписывает каталог».

Позиция, исчезнувшая из файла, **не удаляется**, а гасится `is_active = false`:
на неё могут ссылаться диалоги и лиды, а заказчик может вернуть её завтра.

Вся запись — в ОДНОЙ транзакции. Упали на середине файла → в БД остаётся
вчерашний прайс целиком, а не половина нового.
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from ..config import load_config
from ..db import podklyuchit, sozdat_fabriku_sessiy, zakryt
from ..logger import logger
from ..models import Account, PriceMeta, Product
from .chtenie import OshibkaPraysa, Prays, StrokaPraysa, prochitat, shlopnut_dubli
from .razbor import razobrat

# Выгрузка гугл-таблицы заказчика «Инфа по стройке для бота», лист Saunamart.
# Свежая выгрузка кладётся сюда же поверх старой. НЕ путать со старым
# `материалы/Копия ИП Семенко ЕВ.xlsx` — там устаревшие цены и артикулы
# (см. шапку chtenie.py). Папка `материалы/` в git не идёт (клиентские данные),
# в контейнер монтируется только на чтение — см. docker-compose.yml.
FAYL_PO_UMOLCHANIYU = "материалы/прайс/Инфа по стройке для бота - Saunamart.csv"
AKKAUNT_PO_UMOLCHANIYU = "saunamart"

# Порог расхождения `цена_шт` и `цена_м × длина`, в рублях. Меньше рубля —
# это округление прайса, а не поломка, и в лог такое сыпать незачем.
PORVG_INVARIANTA = Decimal("1.0")

# Наличие. Правило заказчика (уточнено 22.07): **«Да» = есть, пусто = уточнить
# у менеджера**, значения «Нет» в таблице не бывает вовсе — сейчас во всех
# 141 строке стоит «Да». Пустая ячейка — это НЕ «нет в наличии»: именно так
# ошибался старый бот и говорил «нет» про товар, который есть (баг «врёт про
# наличие необрезной»). «Нет» читаем на случай, если заказчик его проставит.
_EST = {"да", "есть", "+", "в наличии", "in_stock", "true", "1"}
_NET = {"нет", "-", "нет в наличии", "out", "false", "0"}


@dataclass
class Itogi:
    """Счётчики импорта — их же кладём в `price_meta`."""

    vsego: int = 0
    dobavleno: int = 0
    obnovleno: int = 0
    bez_izmeneniy: int = 0
    pogasheno: int = 0
    dubley: int = 0
    rashozhdeniy_ceny: int = 0


def _nalichie(syroe: str | None) -> str:
    """Строка колонки наличия → значение enum `availability`.

    Пусто и «колонки нет» дают одно и то же — `unknown`, то есть «уточню
    у менеджера». Разница между «нет в наличии» и «не знаю» для клиента
    огромная: первое закрывает сделку, второе ведёт к менеджеру.
    """
    if syroe is None:            # колонки в файле нет вовсе
        return "unknown"
    z = syroe.strip().lower()
    if z == "":                  # ячейка пустая — заказчик просто не заполнил
        return "unknown"
    if z in _EST:
        return "in_stock"
    if z in _NET:
        return "out"
    # Число в колонке остатка («3 шт») — это тоже «есть».
    if z.replace(",", ".").replace(" шт", "").strip().replace(".", "").isdigit():
        return "in_stock" if float(z.split()[0].replace(",", ".")) > 0 else "out"
    logger.warning("📦 Наличие «%s» не распознано — кладу «неизвестно»", syroe)
    return "unknown"


def _cena_za_metr(cena_sht: Decimal | None, dlina: Decimal | None) -> Decimal | None:
    """Цена за метр = цена за штуку / длина. Из файла её брать нельзя:
    у 18 строк с длиной колонки нет вовсе, а где есть — она хранит базовую
    ставку группы и на 5 строках расходится с ценой за штуку."""
    if cena_sht is None or not dlina or dlina <= 0:
        return None
    return (cena_sht / dlina).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _polya_pozicii(s: StrokaPraysa) -> dict:
    """Строка файла → набор колонок `products`."""
    r = razobrat(s.name, s.characteristics)
    cena_m = _cena_za_metr(s.price_apiece, r.length_m)
    return {
        "article": s.article,
        "name": s.name,
        "family": r.family,
        "grade": r.grade,
        "species": r.species,
        "length_m": r.length_m,
        "working_width_mm": r.working_width_mm,
        # Мерная позиция определяется наличием длины в названии, а не тем,
        # заполнена ли колонка цены за метр: у 18 строк с длиной её нет.
        "unit": "linear_m" if r.length_m else "piece",
        "price_apiece": s.price_apiece,
        "price_per_m": cena_m,
        "is_package": r.is_package,
        "availability": _nalichie(s.nalichie_syroe),
        "characteristics": s.characteristics,
        "attrs": r.attrs,
        "is_active": True,
    }


def _sverit_invariant(stroki: list[StrokaPraysa]) -> int:
    """Сравнить вычисленную цену за метр с колонкой файла. Ничего не меняет —
    только пишет в лог, где прайс сам себе противоречит.

    В текущем файле расходятся 5 строк (вагонка липа В 1.4; А 1.3, 1.4, 1.9;
    Экстра 1.8) на 1–4 % — это округление базовой ставки, а не ошибка формулы.
    Строка в логе нужна, чтобы поломка посерьёзнее не проехала незамеченной.
    """
    rashozhdeniy = 0
    for s in stroki:
        if s.price_per_meter_fayl is None:
            continue
        r = razobrat(s.name, s.characteristics)
        vych = _cena_za_metr(s.price_apiece, r.length_m)
        if vych is None:
            continue
        if abs(vych - s.price_per_meter_fayl) > PORVG_INVARIANTA:
            rashozhdeniy += 1
            logger.warning(
                "💰 «%s»: в файле %s ₽/м, вычислено %s ₽/м (%s ₽/шт ÷ %s м) — беру вычисленное",
                s.name[:55], s.price_per_meter_fayl, vych, s.price_apiece, r.length_m)
    if rashozhdeniy:
        logger.info("💰 Строк с расхождением цены за метр: %d — везде взято вычисленное",
                    rashozhdeniy)
    return rashozhdeniy


def _otlichaetsya(bylo: Product, stalo: dict) -> list[str]:
    """Какие поля позиции реально изменились. Пустой список = обновлять нечего
    (иначе повторный импорт «обновлял» бы все строки и ронял идемпотентность)."""
    izmenilos = []
    for pole, novoe in stalo.items():
        if pole == "article":
            continue
        staroe = getattr(bylo, pole)
        if isinstance(novoe, Decimal) or isinstance(staroe, Decimal):
            odinakovo = (staroe is not None and novoe is not None
                         and Decimal(staroe) == Decimal(novoe)) or (staroe is None and novoe is None)
        else:
            odinakovo = staroe == novoe
        if not odinakovo:
            izmenilos.append(pole)
    return izmenilos


async def importirovat(put: str, kod_akkaunta: str = AKKAUNT_PO_UMOLCHANIYU) -> Itogi:
    """Прочитать файл, проверить, положить в БД одной транзакцией."""
    prays = prochitat(put)                       # падает ДО подключения к БД
    stroki, dubley, _ = shlopnut_dubli(prays.stroki)
    rashozhdeniy = _sverit_invariant(prays.stroki)

    cfg = load_config()
    engine = await podklyuchit(cfg)
    Sessiya = sozdat_fabriku_sessiy(engine)
    itogi = Itogi(vsego=len(stroki), dubley=dubley, rashozhdeniy_ceny=rashozhdeniy)
    try:
        async with Sessiya() as sess, sess.begin():
            akkaunt_id = await sess.scalar(
                select(Account.id).where(Account.code == kod_akkaunta))
            if not akkaunt_id:
                raise OshibkaPraysa(
                    f"❌ В БД нет аккаунта «{kod_akkaunta}».\n"
                    f"   Сначала засей справочники: python -m bot.seed"
                )

            bylo = {p.article: p for p in (await sess.scalars(
                select(Product).where(Product.account_id == akkaunt_id))).all()}
            logger.info("📦 В БД сейчас позиций аккаунта «%s»: %d", kod_akkaunta, len(bylo))

            v_fayle: set[str] = set()
            for s in stroki:
                polya = _polya_pozicii(s)
                v_fayle.add(polya["article"])
                pozicia = bylo.get(polya["article"])
                if pozicia is None:
                    sess.add(Product(account_id=akkaunt_id, **polya))
                    itogi.dobavleno += 1
                    continue
                izmenilos = _otlichaetsya(pozicia, polya)
                if not izmenilos:
                    itogi.bez_izmeneniy += 1
                    continue
                for pole in izmenilos:
                    setattr(pozicia, pole, polya[pole])
                itogi.obnovleno += 1
                logger.info("📦 Обновляю %s «%s»: %s",
                            polya["article"], polya["name"][:45], ", ".join(izmenilos))

            # Исчезла из файла — гасим, но оставляем в каталоге: на позицию
            # могут ссылаться диалоги, а заказчик может вернуть её завтра.
            for artikul, pozicia in bylo.items():
                if artikul not in v_fayle and pozicia.is_active:
                    pozicia.is_active = False
                    itogi.pogasheno += 1
                    logger.info("📦 Позиции %s «%s» больше нет в прайсе — гашу",
                                artikul, pozicia.name[:45])

            sess.add(PriceMeta(
                account_id=akkaunt_id,
                source_file=prays.istochnik,
                price_date=prays.data_praysa,
                rows_total=itogi.vsego,
                inserted=itogi.dobavleno,
                updated=itogi.obnovleno,
                deactivated=itogi.pogasheno,
            ))

        logger.info("📦 Импорт завершён: добавлено %d, обновлено %d, без изменений %d, "
                    "погашено %d (позиций в файле: %d)",
                    itogi.dobavleno, itogi.obnovleno, itogi.bez_izmeneniy,
                    itogi.pogasheno, itogi.vsego)
        return itogi
    finally:
        await zakryt(engine)


def _tolko_razbor(put: str) -> None:
    """Прогон без БД: показать, что распозналось. Нужен, чтобы проверять
    правки разбора на живом файле и не гадать по памяти."""
    import collections

    prays = prochitat(put)
    stroki, dubley, konfliktov = shlopnut_dubli(prays.stroki)
    rashozhdeniy = _sverit_invariant(prays.stroki)
    razobrano = [(_polya_pozicii(s)) for s in stroki]

    sem = collections.Counter(p["family"] for p in razobrano)
    print(f"\nФайл: {prays.istochnik}   дата: {prays.data_praysa:%d.%m.%Y %H:%M}")
    print(f"Строк пригодных: {prays.vsego_strok}, дублей: {dubley} "
          f"(с расхождением: {konfliktov}), позиций: {len(razobrano)}")
    print(f"Расхождений цены за метр с файлом: {rashozhdeniy}")
    print(f"Мерных (linear_m): {sum(1 for p in razobrano if p['unit'] == 'linear_m')}, "
          f"штучных: {sum(1 for p in razobrano if p['unit'] == 'piece')}")
    print(f"С рабочей шириной: {sum(1 for p in razobrano if p['working_width_mm'])}, "
          f"фасовкой: {sum(1 for p in razobrano if p['is_package'])}")
    print(f"Наличие: " + ", ".join(
        f"{k}={v}" for k, v in collections.Counter(p["availability"] for p in razobrano).items()))
    print("\nСемейства: " + ", ".join(f"{k}×{v}" for k, v in sem.most_common()))
    print("\nБез семейства/сорта/породы там, где ожидались:")
    for p in razobrano:
        if p["family"] in ("вагонка", "полок") and not (p["grade"] and p["species"]):
            print(f"   {p['article']:12} {p['name'][:60]} сорт={p['grade']} порода={p['species']}")
    print("\nПечи без объёма парной в attrs:")
    net = [p for p in razobrano if p["family"] == "печь" and "obem_max_m3" not in p["attrs"]]
    for p in net:
        print(f"   {p['article']:12} {p['name'][:60]}")
    print(f"   итого {len(net)} из {sem.get('печь', 0)}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    tolko_razbor = "--tolko-razbor" in argv
    if tolko_razbor:
        argv.remove("--tolko-razbor")
    put = argv[0] if argv else os.environ.get("PRICE_FILE") or FAYL_PO_UMOLCHANIYU
    kod = argv[1] if len(argv) > 1 else AKKAUNT_PO_UMOLCHANIYU

    try:
        if tolko_razbor:
            _tolko_razbor(put)
        else:
            asyncio.run(importirovat(put, kod))
    except OshibkaPraysa as e:
        # Текст уже человеческий — печатаем как есть, БД не тронута.
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
