# -*- coding: utf-8 -*-
"""Разбор запроса клиента в атрибуты (этап 5).

Вход — живая фраза («есть трёхметровая липа сорт б?»), выход — структура,
по которой этап 6 отберёт позиции каталога. Здесь ещё нет ни поиска,
ни ранжирования: только «что клиент вообще спросил».

Зеркало `bot/etl/razbor.py`, но с другой стороны прилавка: тот разбирает
названия прайса (канон заказчика), этот — речь клиента. Отсюда разные словари
и разные допущения. Общее правило одно и то же: **не угадываем**. Не поняли
атрибут — None, а не «наверное, липа».

Главная тонкость — число в запросе значит разное в зависимости от семейства:

* вагонка/полок/наличник/брус/рейка — число это ДЛИНА в метрах («три метра»);
* дверь — ГАБАРИТ в миллиметрах («1900 на 700», «стандартная»);
* печь — ОБЪЁМ парной в м³ («на 16 кубов») или МОЩНОСТЬ в кВт («9 киловатт»).

Поэтому семейство резолвится первым, а числа читаются уже под него.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from decimal import Decimal

from ..logger import logger
from .normalize import norm, slova, stem_slova, stems
from .slovari import Slovari, slovari

# Семейства, у которых в каталоге есть длина. У дверей, печей, камней, фольги
# длины нет вообще — там число из запроса значит другое.
MERNYE_SEMEYSTVA = frozenset({"вагонка", "полок", "наличник", "брус", "рейка"})

# Границы правдоподобной длины: в каталоге 1.0–3.2 м. Полметра снизу и четыре
# метра сверху — запас на «а трёхсполовинойметровую?», чтобы понять вопрос
# и честно ответить «такой длины нет», а не промолчать.
DLINA_MIN, DLINA_MAX = Decimal("0.5"), Decimal("4.0")

_RE_ARTIKUL = re.compile(r"\b\d{6}\b")
_RE_DLINA_L = re.compile(r"\bl\s*-?\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*м?\b")
_RE_DLINA_METRY = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:м|метр\w*)\b")
_RE_DLINA_SM = re.compile(r"(\d{2,4})\s*(?:см|сантиметр\w*)\b")
# Голое число («вагонка 2.5») — только для мерных семейств и только вне размера
# сечения: перед и после не должно быть цифры или знака размера, иначе
# «15х95» превратится в длину 15 м.
_RE_GOLOE_CHISLO = re.compile(r"(?<![\d.,хx*×])(\d(?:[.,]\d)?)(?![\d.,хx*×])")

_RE_GABARIT_MM = re.compile(r"\b(\d{3,4})\s*(?:[хx*×]|на)\s*(\d{3,4})\b")
_RE_GABARIT_SM = re.compile(r"\b(\d{2,3})\s*(?:[хx*×]|на)\s*(\d{2,3})\b")

_RE_OBEM = re.compile(r"(\d{1,3})\s*(?:куб\w*|м3|м³|кубометр\w*)")
_RE_MOSHCHNOST = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:квт|киловатт\w*)")

_RE_SORT = re.compile(r"сорт\w*\s+([а-яa-z]+)")

# Сорта, которые клиент называет одним словом, без слова «сорт». Однобуквенные
# сюда не попадают намеренно: «в» и «с» — предлоги, а не сорт.
_SORT_BEZ_SLOVA = ("экстра", "премиум", "высший")


@dataclass(frozen=True)
class RazobranyZapros:
    """Что удалось понять из фразы клиента."""

    tekst: str
    normalizovanny: str
    stemy: frozenset[str]
    slova: tuple[str, ...]

    chuzhoy_domen: str | None = None      # маркер не нашего профиля → поиск не нужен
    semeystvo: str | None = None          # == products.family
    kanal_semeystva: str | None = None    # точно | синоним | опечатка
    sort: str | None = None               # А | В | Экстра | С
    poroda: str | None = None             # липа | ольха | абаши | термолипа …
    dlina_m: Decimal | None = None
    dver_vysota_mm: int | None = None
    dver_shirina_mm: int | None = None
    obem_m3: int | None = None
    moshchnost_kvt: float | None = None
    artikul: str | None = None

    @property
    def pustoy(self) -> bool:
        """Ни семейства, ни артикула — искать не по чему."""
        return not self.semeystvo and not self.artikul

    def kratko(self) -> str:
        """Однострочник для лога: только то, что распозналось."""
        if self.chuzhoy_domen:
            return f"чужой домен ({self.chuzhoy_domen})"
        chasti = []
        if self.artikul:
            chasti.append(f"артикул {self.artikul}")
        if self.semeystvo:
            chasti.append(f"{self.semeystvo} [{self.kanal_semeystva}]")
        if self.poroda:
            chasti.append(self.poroda)
        if self.sort:
            chasti.append(f"сорт {self.sort}")
        if self.dlina_m is not None:
            chasti.append(f"{self.dlina_m} м")
        if self.dver_vysota_mm:
            chasti.append(f"{self.dver_vysota_mm}×{self.dver_shirina_mm} мм")
        if self.obem_m3:
            chasti.append(f"{self.obem_m3} м³")
        if self.moshchnost_kvt:
            chasti.append(f"{self.moshchnost_kvt} кВт")
        return " · ".join(chasti) or "ничего не распознано"


def _sort(qn: str, sl: Slovari) -> str | None:
    m = _RE_SORT.search(qn)
    if m:
        naydeno = sl.sort.get(m.group(1))
        if naydeno:
            return naydeno
    for s in _SORT_BEZ_SLOVA:
        if s in qn:
            return sl.sort.get(s)
    return None


def _kanonizirovat_sort(qn: str, sort: str | None) -> str:
    """Заменить сорт в тексте на канон прайса: «сорт б» → «сорт в».

    Не косметика: стемы считаются уже по канонизированному тексту, поэтому
    «вагонка сорт б» и «вагонка сорт в» дают побайтово одинаковый разбор —
    а значит и одинаковую выдачу на этапе 6, где стемы идут в скоринг.
    Без этой замены буква «б» осталась бы лишним стемом только у одного
    из двух запросов, и наборы позиций разошлись бы.
    """
    if not sort:
        return qn
    return _RE_SORT.sub(lambda m: f"сорт {sort.lower()}", qn, count=1)


def _poroda(qn: str, slova_zaprosa: tuple[str, ...], sl: Slovari) -> str | None:
    # Фразы («термо липа») — раньше одиночных слов, иначе «липа» съест «термо липа».
    for fraza, poroda in sl.poroda_fraza:
        if fraza in qn:
            return poroda
    for w in slova_zaprosa:
        if w in sl.poroda_slovo:
            return sl.poroda_slovo[w]
    return None


def _v_diapazone(v: Decimal) -> Decimal | None:
    return v if DLINA_MIN <= v <= DLINA_MAX else None


def _metry_slengom(qn: str, stemy: frozenset[str], sl: Slovari) -> Decimal | None:
    """Метры, названные словами: «два метра», «трёхметровая», «два сорок».

    Живёт отдельной функцией, потому что нужно двум разным разборам: ДЛИНЕ
    мерных семейств и ВЫСОТЕ двери. Baseline этапа 7 поймал ровно этот разрыв:
    «дверь 2000х700» разбиралась, «дверь два метра» — нет. Цифры искала
    регулярка габарита, а слова знал только словарь длин, до которого дверь
    не доходила, потому что длины у дверей не бывает.
    """
    for st in stemy:
        if st in sl.dlina_stem:
            return Decimal(str(sl.dlina_stem[st]))
    for fraza, v in sl.dlina_fraza:
        if fraza in qn:
            return Decimal(str(v))
    return None


def _dlina(qn: str, stemy: frozenset[str], sl: Slovari,
           semeystvo: str | None) -> Decimal | None:
    """Длина в метрах. Разбирается только для мерных семейств (или когда
    семейство не распозналось — «а три метра есть?» отдельной репликой)."""
    if semeystvo is not None and semeystvo not in MERNYE_SEMEYSTVA:
        return None

    m = _RE_DLINA_L.search(qn)
    if m:
        return _v_diapazone(Decimal(m.group(1).replace(",", ".")))

    slengom = _metry_slengom(qn, stemy, sl)
    if slengom is not None:
        return slengom

    m = _RE_DLINA_METRY.search(qn)
    if m:
        naydeno = _v_diapazone(Decimal(m.group(1).replace(",", ".")))
        if naydeno is not None:
            return naydeno

    m = _RE_DLINA_SM.search(qn)
    if m:
        naydeno = _v_diapazone(Decimal(m.group(1)) / 100)
        if naydeno is not None:
            return naydeno

    if semeystvo in MERNYE_SEMEYSTVA:
        m = _RE_GOLOE_CHISLO.search(qn)
        if m:
            return _v_diapazone(Decimal(m.group(1).replace(",", ".")))
    return None


def _gabarit_dveri(qn: str, stemy: frozenset[str], sl: Slovari) -> tuple[int | None, int | None]:
    """Высота и ширина двери в мм. Высота двери всегда больше ширины — на этом
    и разбираем, а не на порядке чисел: в прайсе он гуляет («1900х700»
    у одних, «680*1890» у других)."""
    m = _RE_GABARIT_MM.search(qn)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return max(a, b), min(a, b)
    # Сантиметры: «190 на 70». Ниже 60 см дверей не бывает, выше 250 — тоже.
    m = _RE_GABARIT_SM.search(qn)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 60 <= a <= 250 and 60 <= b <= 250:
            return max(a, b) * 10, min(a, b) * 10
    for slovo, (v, sh) in sl.dver_gabarit.items():
        if slovo in qn:
            return v, sh
    # «дверь 2 метра» и «дверь два метра» — это высота 2000 мм, а не длина:
    # у дверей длины нет. Цифрами её пишет регулярка, словами — словарь длин.
    metry = None
    m = _RE_DLINA_METRY.search(qn)
    if m:
        metry = Decimal(m.group(1).replace(",", "."))
    else:
        metry = _metry_slengom(qn, stemy, sl)
    if metry is not None and Decimal("1.8") <= metry <= Decimal("2.2"):
        return int(metry * 1000), None
    return None, None


def razobrat_zapros(tekst: str, sl: Slovari | None = None) -> RazobranyZapros:
    """Разобрать фразу клиента. Словари берутся из кеша процесса."""
    sl = sl or slovari()
    syroy = norm(tekst)
    sort = _sort(syroy, sl)
    qn = _kanonizirovat_sort(syroy, sort)
    slova_zaprosa = tuple(slova(qn))
    stemy = frozenset(stems(qn))

    chuzhoy = sl.chuzhoy_domen(qn)
    semeystvo, kanal = sl.semeystvo_zaprosa(qn)

    m = _RE_ARTIKUL.search(qn)
    artikul = m.group(0) if m else None

    vysota = shirina = None
    obem = None
    moshchnost = None
    if semeystvo == "дверь":
        vysota, shirina = _gabarit_dveri(qn, stemy, sl)
    if semeystvo == "печь" or semeystvo is None:
        m = _RE_OBEM.search(qn)
        if m:
            obem = int(m.group(1))
        m = _RE_MOSHCHNOST.search(qn)
        if m:
            moshchnost = float(m.group(1).replace(",", "."))

    return RazobranyZapros(
        tekst=tekst,
        normalizovanny=qn,
        stemy=stemy,
        slova=slova_zaprosa,
        chuzhoy_domen=chuzhoy,
        semeystvo=semeystvo,
        kanal_semeystva=kanal,
        sort=sort,
        poroda=_poroda(qn, slova_zaprosa, sl),
        dlina_m=_dlina(qn, stemy, sl, semeystvo),
        dver_vysota_mm=vysota,
        dver_shirina_mm=shirina,
        obem_m3=obem,
        moshchnost_kvt=moshchnost,
        artikul=artikul,
    )


# ── CLI: ручная проверка разбора ─────────────────────────────────────────────

# Набор из критериев приёмки этапа 5 — чтобы человек проверял одной командой,
# а не вспоминал формулировки.
PROVERKA = [
    "вагонка сорт б",
    "вагонка сорт в",
    "трёхметровая липа",
    "вогонка",
    "полог липа 2 метра",
    "доска на полок",
    "болгарка",
    "холодильник",
    "шампунь",
    "печь на 16 кубов",
    "дверь 1900 на 700",
    "камни жадеит",
    "есть 123405?",
    "нужна штиль",
]


def _pokazat(z: RazobranyZapros) -> None:
    print(f"\n▸ {z.tekst!r}")
    print(f"  норма:     {z.normalizovanny}")
    print(f"  стемы:     {' '.join(sorted(z.stemy))}")
    print(f"  разбор:    {z.kratko()}")


def main(argv: list[str]) -> int:
    zaprosy = argv[1:] or PROVERKA
    if not argv[1:]:
        print("Проверочный набор этапа 5 (свой запрос: python -m bot.search.zapros \"…\")")
    for q in zaprosy:
        _pokazat(razobrat_zapros(q))
    return 0


if __name__ == "__main__":
    logger.info("Разбор запроса — ручная проверка словарей (этап 5)")
    raise SystemExit(main(sys.argv))
