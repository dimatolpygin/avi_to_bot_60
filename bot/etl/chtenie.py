# -*- coding: utf-8 -*-
"""Чтение прайса клиента и валидация ДО похода в БД.

Источник — лист **`table`** файла `Копия ИП Семенко ЕВ.xlsx`. Он единственный,
где колонки названы `article, nomenclature, characteristics, price_apiece,
price_per_meter`. В файле есть ещё `Лист24`, `Лист22`, `цены склад сауны`,
`Липа` — те же товары в других единицах и с другими ценами. Смешение этих
срезов и есть причина бага «бот выдаёт разные цены», поэтому берём один лист
и только его.

Формат CSV с теми же заголовками тоже читается — на случай, если заказчик
начнёт выгружать прайс отдельным файлом.

Главное правило модуля: **до записи в БД файл должен быть проверен целиком**.
Битый или обрезанный прайс не должен ополовинить каталог — лучше упасть
с русским объяснением и оставить в БД вчерашние данные.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from ..logger import logger
from .razbor import normalizovat

LIST_PO_UMOLCHANIYU = "table"

# Заголовки, без которых читать нечего.
OBYAZATELNYE_KOLONKI = ("article", "nomenclature", "price_apiece")
# Колонка наличия. В текущем файле её нет ни на одном листе — заказчик её пока
# не ведёт. Если заведёт, ETL подхватит без правки кода.
KOLONKA_NALICHIYA = ("наличие", "в наличии", "availability", "stock")

# Меньше этого числа строк — файл почти наверняка обрезан. В прайсе 229 строк;
# порог 50 отсекает аварию, но переживёт сокращение ассортимента вдвое.
MINIMUM_STROK = 50


class OshibkaPraysa(Exception):
    """Файл прайса непригоден. Текст сообщения идёт человеку как есть."""


@dataclass(frozen=True)
class StrokaPraysa:
    """Одна строка файла, приведённая к типам. Ещё без разбора названия."""

    article: str
    name: str
    characteristics: str | None
    price_apiece: Decimal | None
    price_per_meter_fayl: Decimal | None   # только для сверки инварианта, в БД не идёт
    nalichie_syroe: str | None             # None = колонки в файле нет
    nomer_stroki: int


@dataclass(frozen=True)
class Prays:
    """Прочитанный и проверенный файл целиком."""

    stroki: list[StrokaPraysa]
    istochnik: str
    data_praysa: datetime
    vsego_strok: int


# ── Приведение ячеек ─────────────────────────────────────────────────────────

def _artikul(v) -> str | None:
    """Артикул из ячейки. openpyxl отдаёт числовые артикулы как float
    (`23405.0`) — иначе ключ синхронизации разъедется между запусками."""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = normalizovat(v)
    # То же самое, когда файл пришёл CSV: строка «23405.0».
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s or None


def _cena(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
        c = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return c if c > 0 else None


# ── Чтение ───────────────────────────────────────────────────────────────────

def _indeksy_kolonok(zagolovok: list, put: str) -> dict[str, int]:
    """Сопоставить заголовки файла с нужными колонками. Нет обязательной —
    падаем сразу, до чтения строк."""
    naydeno: dict[str, int] = {}
    for i, h in enumerate(zagolovok):
        if h is None:
            continue
        klyuch = normalizovat(h).lower()
        if klyuch in ("article", "nomenclature", "characteristics",
                      "price_apiece", "price_per_meter"):
            naydeno.setdefault(klyuch, i)
        elif klyuch in KOLONKA_NALICHIYA:
            naydeno.setdefault("nalichie", i)

    net = [k for k in OBYAZATELNYE_KOLONKI if k not in naydeno]
    if net:
        raise OshibkaPraysa(
            f"❌ В прайсе «{put}» нет обязательных колонок: {', '.join(net)}.\n"
            f"   Найдены: {', '.join(sorted(naydeno)) or '— ни одной'}.\n"
            f"   Нужен лист с заголовками article / nomenclature / price_apiece."
        )
    return naydeno


def _syrye_stroki_xlsx(put: str, list_name: str) -> list[list]:
    try:
        import openpyxl
    except ImportError as e:                                   # pragma: no cover
        raise OshibkaPraysa(f"❌ Не установлен openpyxl, читать xlsx нечем: {e}")

    try:
        wb = openpyxl.load_workbook(put, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 — сюда попадает и «файл не xlsx», и битый архив
        raise OshibkaPraysa(f"❌ Не удалось открыть прайс «{put}»: {e}")

    try:
        if list_name not in wb.sheetnames:
            raise OshibkaPraysa(
                f"❌ В прайсе «{put}» нет листа «{list_name}».\n"
                f"   Листы в файле: {', '.join(wb.sheetnames)}."
            )
        return [list(r) for r in wb[list_name].iter_rows(values_only=True)]
    finally:
        wb.close()


def _syrye_stroki_csv(put: str) -> list[list]:
    # utf-8-sig: выгрузка из Excel почти всегда с BOM.
    with open(put, encoding="utf-8-sig", newline="") as f:
        obrazec = f.read(4096)
        f.seek(0)
        try:
            dialekt = csv.Sniffer().sniff(obrazec, delimiters=",;\t")
        except csv.Error:
            dialekt = csv.excel
        return [list(r) for r in csv.reader(f, dialekt)]


def prochitat(put: str, list_name: str = LIST_PO_UMOLCHANIYU) -> Prays:
    """Прочитать файл и проверить его целиком. Любая беда → `OshibkaPraysa`."""
    if not os.path.isfile(put):
        raise OshibkaPraysa(
            f"❌ Файл прайса не найден: «{put}».\n"
            f"   Проверь путь; в контейнере прайс лежит в /app/data/."
        )

    rasshirenie = os.path.splitext(put)[1].lower()
    syrye = (_syrye_stroki_csv(put) if rasshirenie == ".csv"
             else _syrye_stroki_xlsx(put, list_name))
    if not syrye:
        raise OshibkaPraysa(f"❌ Прайс «{put}» пуст — ни одной строки.")

    kolonki = _indeksy_kolonok(syrye[0], put)
    est_nalichie = "nalichie" in kolonki
    logger.info("📄 Прайс «%s», лист «%s»: колонка наличия %s",
                os.path.basename(put), list_name,
                "есть" if est_nalichie else "ОТСУТСТВУЕТ — всё пойдёт как «наличие неизвестно»")

    def yacheyka(stroka: list, klyuch: str):
        i = kolonki.get(klyuch)
        return stroka[i] if i is not None and i < len(stroka) else None

    stroki: list[StrokaPraysa] = []
    bez_ceny = bez_nazvaniya = 0
    for nomer, syraya in enumerate(syrye[1:], start=2):
        art = _artikul(yacheyka(syraya, "article"))
        if not art:
            continue                                   # хвост пустых строк листа
        nazvanie = yacheyka(syraya, "nomenclature")
        nazvanie = normalizovat(nazvanie) if nazvanie else ""
        if not nazvanie:
            bez_nazvaniya += 1
            logger.warning("📄 Строка %d: артикул %s без названия — пропускаю", nomer, art)
            continue
        cena = _cena(yacheyka(syraya, "price_apiece"))
        if cena is None:
            bez_ceny += 1
            logger.warning("📄 Строка %d: «%s» без цены — пропускаю", nomer, nazvanie[:60])
            continue
        har = yacheyka(syraya, "characteristics")
        nal = yacheyka(syraya, "nalichie") if est_nalichie else None
        stroki.append(StrokaPraysa(
            article=art,
            name=nazvanie,
            characteristics=normalizovat(har) if har else None,
            price_apiece=cena,
            price_per_meter_fayl=_cena(yacheyka(syraya, "price_per_meter")),
            nalichie_syroe=("" if nal is None else normalizovat(nal)) if est_nalichie else None,
            nomer_stroki=nomer,
        ))

    if bez_nazvaniya or bez_ceny:
        logger.warning("📄 Пропущено строк: без названия %d, без цены %d",
                       bez_nazvaniya, bez_ceny)

    if len(stroki) < MINIMUM_STROK:
        raise OshibkaPraysa(
            f"❌ В прайсе «{put}» пригодных строк {len(stroki)}, "
            f"а должно быть не меньше {MINIMUM_STROK}.\n"
            f"   Похоже, файл обрезан или это не тот лист. В БД ничего не меняю."
        )

    data = datetime.fromtimestamp(os.path.getmtime(put), tz=timezone.utc)
    logger.info("📄 Прочитано строк: %d, дата прайса: %s",
                len(stroki), data.strftime("%d.%m.%Y %H:%M"))
    return Prays(stroki=stroki, istochnik=os.path.basename(put),
                 data_praysa=data, vsego_strok=len(stroki))


# ── Дубли артикулов ──────────────────────────────────────────────────────────

def shlopnut_dubli(stroki: list[StrokaPraysa]) -> tuple[list[StrokaPraysa], int, int]:
    """Свести строки к одной на артикул. Возвращает (строки, дублей, конфликтов).

    В файле клиента 25 дублей, и все **полные**: артикул, название и цена
    совпадают, дублируется вся строка (печи ЭКМ, фольга, камни, пульты).
    Такой дубль схлопывается без потери данных — ровно поэтому ключом
    синхронизации выбран артикул.

    Если однажды всплывёт дубль с РАЗНЫМИ ценами — это уже поломка прайса,
    молча её проглатывать нельзя: пишем WARNING с обеими ценами и берём
    последнюю строку (она ниже в файле, обычно свежее).
    """
    po_artikulu: dict[str, StrokaPraysa] = {}
    dubley = konfliktov = 0
    for s in stroki:
        bylo = po_artikulu.get(s.article)
        if bylo is not None:
            dubley += 1
            if bylo.price_apiece != s.price_apiece or bylo.name != s.name:
                konfliktov += 1
                logger.warning(
                    "📄 Артикул %s повторяется с разными данными: строка %d «%s» %s ₽ "
                    "против строки %d «%s» %s ₽ — беру последнюю",
                    s.article, bylo.nomer_stroki, bylo.name[:40], bylo.price_apiece,
                    s.nomer_stroki, s.name[:40], s.price_apiece)
        po_artikulu[s.article] = s

    if dubley:
        logger.info("📄 Дублей артикула схлопнуто: %d (из них с расхождением: %d), "
                    "позиций осталось: %d", dubley, konfliktov, len(po_artikulu))
    return list(po_artikulu.values()), dubley, konfliktov
