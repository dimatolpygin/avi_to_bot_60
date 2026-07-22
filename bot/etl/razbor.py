# -*- coding: utf-8 -*-
"""Разбор названия позиции на атрибуты.

Весь смысл модуля: из строки вида
`Вагонка Липа сорт А 15х95 (88) L-2.5 м` получить семейство, породу, сорт,
толщину, полную и рабочую ширину, длину — чтобы поиск (этапы 5-6) искал по
полям, а не по подстроке, и чтобы бот мог назвать цену за метр.

Три правила, которые тут зашиты намертво (обоснование — «Неочевидное» в CLAUDE.md):

1. **Рабочая ширина берётся ТОЛЬКО из скобок сразу после размера** (`15х95 (88)`).
   У полка (`26х90`) скобок нет: 90 — полная ширина, полок кладут с зазорами,
   рабочей ширины у него не существует. Пустое поле = «пересчёт в м² запрещён».
2. **Цену за метр здесь не читаем из файла**, её считает `import_prays` делением
   цены за штуку на длину: в файле эта колонка местами пуста, местами округлена.
3. **Ничего не угадываем.** Не распознали — кладём None. Пустое поле бот обходит
   («уточню у менеджера»), а выдуманное — превращается в очередной баг «врёт».
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

# ── Нормализация ─────────────────────────────────────────────────────────────

# Латиница, неотличимая от кириллицы на глаз. В прайсе клиента это встречается
# в сортах («сорт A» латинской A) и ломает точное сравнение.
_GOMOGLIFY = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "y": "у", "a": "а", "e": "е",
    "o": "о", "p": "р", "c": "с", "x": "х",
})


def normalizovat(s: str) -> str:
    """Схлопнуть пробелы и неразрывные пробелы, убрать ё. Регистр не трогаем —
    он нужен для отображения названия клиенту как есть."""
    s = unicodedata.normalize("NFKC", str(s)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


# ── Справочники ──────────────────────────────────────────────────────────────

# Семейство = первое слово названия, приведённое к канону. Словарь нужен там,
# где первое слово не самодостаточно («Камни»/«Камень», «Обливное» + «устройство»)
# или где семейство состоит из двух слов.
_SEMEYSTVA = {
    "камни": "камень",
    "обливное": "обливное устройство",
    "скотч-фольга": "скотч-фольга",
    "пульт": "пульт управления",
}

# Породы. Порядок важен: «термоосина» должна проверяться раньше «осина»,
# иначе термоосина запишется как осина и склеится с обычной в поиске.
_PORODY = [
    "термоосина", "термолипа", "термоясень",
    "липа", "ольха", "осина", "абаши", "кедр", "сосна", "хвоя", "дуб", "ясень",
]

# Сорта. `Б` → `В`: в прайсе строки называются «сорт В» (кириллическая),
# а клиент и менеджер в переписке пишут «сорт Б», читая В как латинскую B.
# Маппинг обязателен и здесь, и в словаре поиска (этап 5).
_SORTA = {"а": "А", "в": "В", "б": "В", "экстра": "Экстра", "с": "С"}

# ── Регулярки ────────────────────────────────────────────────────────────────

# Длина: `L-2.5 м`, `L- 0.8 м`, `L-1.4м`, `L 2.5 м`.
_RE_DLINA = re.compile(r"\bL\s*-?\s*(\d{1,2}[.,]\d{1,2})\s*м?\b", re.I)

# Размер сечения и рабочая ширина: `15х95 (88)`, `15*96 (88)`, `26х90`.
# Скобки ловим ТОЛЬКО вплотную к размеру — иначе в них попадёт «(0.63 м2/уп)»
# или «(бронза хвоя)».
_RE_RAZMER = re.compile(
    r"\b(\d{1,3})\s*[хx*×]\s*(\d{1,3})\s*(?:\(\s*(\d{2,3})\s*\))?")

_RE_SORT = re.compile(r"\bсорт[а-я]*\s+([А-Яа-яA-Za-z]+)", re.I)

# Мощность: `9 кВт`, `10.5 кВт`.
_RE_MOSHCHNOST = re.compile(r"(\d{1,3}(?:[.,]\d)?)\s*кВт", re.I)

# Габарит двери: `1900х700`, `680*1890`.
_RE_DVER = re.compile(r"\b(\d{3,4})\s*[хx*×]\s*(\d{3,4})\b")

# Фасовка — только там, где упаковка и есть единица продажи: «Фольга 12 м2»,
# «Камни 20кг», «уп.10кг», «(упак. 0.5 м2)». Цену такой позиции делить нельзя.
#
# Литры сознательно НЕ маркер: «Обливное устройство Бочонок 15 л» — это объём
# изделия. И «6 шт/уп» тоже не маркер: это сколько штук в пачке, а цена всё
# равно за штуку — проверено по файлу, у «Вагонка ольха STS (6 шт/уп)» стоит
# 693 ₽/шт при 289 ₽/м на длине 2.4 м. Пометить её упаковкой значит заставить
# бота сказать «делить нельзя» ровно там, где делить как раз нужно.
_RE_SHTUK_V_UPAKOVKE = re.compile(r"\(?\s*\d+\s*шт\.?\s*(?:/|в)\s*уп\w*\.?\s*\)?", re.I)
_RE_FASOVKA = re.compile(r"(?:\bуп\.?\b|упак|/\s*уп|\d+\s*(?:м2|м²|кг)\b)", re.I)

# Объём парной у печей. В характеристиках он записан семью разными способами,
# поэтому сначала ищем якорь «объём», а диапазон разбираем в отдельной функции.
_RE_YAKOR_OBEM = re.compile(r"об[ъь]?[её]м\w*", re.I)
_RE_DIAPAZON = re.compile(r"(\d{1,3})\s*(?:-|–|—|\.\.\.|до)\s*(\d{1,3})")
_RE_OT_DO = re.compile(r"от\s+(\d{1,3})\s+до\s+(\d{1,3})")
_RE_TOLKO_DO = re.compile(r"(?:до|не превышает)\s+(\d{1,3})")
_RE_ODNO_CHISLO = re.compile(r"(\d{1,3})")


@dataclass
class Razobrannoe:
    """Результат разбора одного названия."""

    family: str | None = None
    grade: str | None = None
    species: str | None = None
    length_m: Decimal | None = None
    working_width_mm: int | None = None
    is_package: bool = False
    attrs: dict = field(default_factory=dict)


def _semeystvo(nazvanie: str) -> str | None:
    slova = nazvanie.split()
    if not slova:
        return None
    pervoe = slova[0].lower().strip(".,")
    return _SEMEYSTVA.get(pervoe, pervoe)


def _sort(nazvanie: str) -> str | None:
    m = _RE_SORT.search(nazvanie)
    if not m:
        return None
    syroy = m.group(1).translate(_GOMOGLIFY).lower()
    return _SORTA.get(syroy)


def _poroda(nazvanie: str) -> str | None:
    nizhniy = nazvanie.lower().replace("ё", "е")
    for p in _PORODY:
        if p in nizhniy:
            return p
    return None


def _dlina(nazvanie: str) -> Decimal | None:
    m = _RE_DLINA.search(nazvanie)
    if not m:
        return None
    return Decimal(m.group(1).replace(",", "."))


def _razmer(nazvanie: str) -> tuple[int | None, int | None, int | None]:
    """(толщина, полная ширина, рабочая ширина). Рабочая — только из скобок.

    Ищем первое совпадение, у которого оба числа выглядят как сечение доски
    (до 200 мм): иначе в вагонке `15х95 (88) L-2.5` подхватится что-нибудь
    вроде `600х150` из названий камня.
    """
    for m in _RE_RAZMER.finditer(nazvanie):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 200 or b > 200:
            continue
        rabochaya = int(m.group(3)) if m.group(3) else None
        # Рабочая ширина не может быть больше полной — если так, это не она.
        if rabochaya is not None and rabochaya > b:
            rabochaya = None
        return a, b, rabochaya
    return None, None, None


def _obem_parnoy(tekst: str) -> tuple[int | None, int | None]:
    """Объём парилки печи из характеристик. Возвращает (мин, макс).

    Печь подбирают именно по объёму парной, и в файле он записан десятком
    способов: «объем бани 11 - 17 м3», «Объём парного помещения: от 8 до 18 м3»,
    «Объем парного помещения, м3 6-16», «объемом до 16 м3», «объем сауны, м3: 8».
    Матчить надо ПОПАДАНИЕМ В ДИАПАЗОН, поэтому одиночное число кладём и в мин,
    и в макс.

    Упоминаний объёма в одной карточке бывает несколько, и половина смысла
    лежит во втором: у Sawo это «объем сауны, м3: 8» плюс «Максимальный объем
    сауны, м3: 14», у Пегаса — «объем парильни 6» и ниже «объем парильни 14».
    Поэтому собираем ВСЕ упоминания и берём общий охват (мин из минимумов,
    макс из максимумов) — иначе печь на 6-14 м³ выглядит как печь ровно на 6.

    Объём каменки и объём заклада — это литры камней, а не парная; такие
    упоминания пропускаем, иначе печь «на 7 м³» появится там, где её нет.
    """
    if not tekst:
        return None, None
    miny: list[int] = []
    maksy: list[int] = []
    for yakor in _RE_YAKOR_OBEM.finditer(tekst):
        # Окно после якоря: дальше начинается следующая характеристика.
        okno = tekst[yakor.end():yakor.end() + 80].replace("\n", " ").replace("\t", " ")
        if re.search(r"каменк|заклад", okno[:30], re.I):
            continue
        # Единицы выкидываем, иначе «м3» отдаст цифру 3 как число.
        okno = re.sub(r"м\s*(?:3|³|\.?\s*куб\w*)|куб\w*\s*метр\w*|метр\w*\s*куб\w*",
                      " ", okno, flags=re.I)
        m = _RE_OT_DO.search(okno) or _RE_DIAPAZON.search(okno)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            miny.append(a)
            maksy.append(b)
            continue
        m = _RE_TOLKO_DO.search(okno)
        if m:
            maksy.append(int(m.group(1)))       # нижней границы тут нет
            continue
        m = _RE_ODNO_CHISLO.search(okno)
        if m:
            odno = int(m.group(1))
            miny.append(odno)
            maksy.append(odno)
    return (min(miny) if miny else None), (max(maksy) if maksy else None)


def razobrat(nazvanie: str, harakteristiki: str | None = None) -> Razobrannoe:
    """Разобрать название (и характеристики, если есть) на атрибуты."""
    n = normalizovat(nazvanie)
    r = Razobrannoe()
    r.family = _semeystvo(n)
    r.grade = _sort(n)
    r.species = _poroda(n)
    r.length_m = _dlina(n)
    # «6 шт/уп» вырезаем до проверки: иначе оно само сойдёт за признак упаковки.
    r.is_package = bool(_RE_FASOVKA.search(_RE_SHTUK_V_UPAKOVKE.sub(" ", n)))

    tolshchina, shirina, rabochaya = _razmer(n)
    r.working_width_mm = rabochaya
    if tolshchina:
        r.attrs["tolshchina_mm"] = tolshchina
    if shirina:
        r.attrs["shirina_mm"] = shirina

    if r.family == "дверь":
        m = _RE_DVER.search(n)
        if m:
            # В прайсе порядок гуляет: «1900х700» (высота×ширина), но у Grandis
            # «680*1890» (ширина×высота). Высота двери всегда больше ширины —
            # на этом и разбираем, а не на позиции числа.
            a, b = int(m.group(1)), int(m.group(2))
            r.attrs["dver_vysota_mm"], r.attrs["dver_shirina_mm"] = max(a, b), min(a, b)

    if r.family == "печь":
        m = _RE_MOSHCHNOST.search(n) or _RE_MOSHCHNOST.search(harakteristiki or "")
        if m:
            r.attrs["moshchnost_kvt"] = float(m.group(1).replace(",", "."))
        # Объём — только из характеристик. В названии дровяных печей число
        # («ASTON 12») тоже объём, но не всегда, и ошибиться тут дороже,
        # чем оставить пусто: печь подбирают именно по объёму парной.
        omin, omax = _obem_parnoy(harakteristiki or "")
        if omin is not None:
            r.attrs["obem_min_m3"] = omin
        if omax is not None:
            r.attrs["obem_max_m3"] = omax

    return r
