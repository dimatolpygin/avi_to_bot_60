# -*- coding: utf-8 -*-
"""Прогон реплик через агента — то, чем этап 8 проверяется руками.

    python -m bot.ai.probe                 # набор из критериев приёмки этапа 8
    python -m bot.ai.probe "есть штиль?"   # своя реплика
    python -m bot.ai.probe --dialog        # живой диалог в консоли (с историей)

Каждый ответ прогоняется через **детерминированные проверки**, которые человеку
глазами делать долго и легко пропустить:

* стиль — длинное тире, markdown, эмодзи, список столбиком;
* выпрашивание телефона — главный баг старого бота;
* **выдуманные числа** — любое число из ответа сверяется с прайсом и промптом.
  Это прямой критерий приёмки «все названные цены совпадают с БД»: модель может
  назвать правдоподобную, но несуществующую цену, и на глаз это не ловится.

Проверки не заменяют чтение ответов человеком: они ловят форму, а живость,
уместность и то, не соврал ли бот по смыслу, видит только человек.
"""
from __future__ import annotations

import asyncio
import re
import sys
from decimal import Decimal

from ..config import load_config
from ..logger import logger, nachat_zapros
from ..search.katalog import iz_fayla_praysa
from ..search.search import Poisk, cena_za_metr_kvadratnyy
from .agent import OtvetAgenta, otvetit, sobrat_prompt

# Реплики из критериев приёмки этапа 8 плюс кейсы из живых диалогов клиента.
# Кортеж: (реплика, что именно проверяем глазами).
PROVERKA: list[tuple[str, str]] = [
    ("сколько стоит липа 3 метра",
     "вилка двух сортов с ценами за штуку, как менеджер в переписке"),
    ("а сорт б почём",
     "продолжение диалога: не здоровается второй раз, помнит про липу"),
    ("есть штиль?",
     "честное «нет», без подстановки похожей вагонки"),
    ("дверь 1900 на 700 сколько",
     "цена именно этого габарита, не 2000х800"),
    ("печь на 16 кубов посоветуйте",
     "Тундра-16 или ASTON 16, не 12-я модель"),
    ("сколько нужно вагонки на парную 2 на 3",
     "расчёт не делает, честно отправляет к менеджеру"),
    ("а почём квадратный метр вагонки липа",
     "цена за м² названа сразу (это основная цена), считается по общей ширине"),
    ("продаёте болгарку?",
     "не наш профиль, коротко и без поиска по прайсу"),
    ("камни для печки есть",
     "жадеит или габбро с ценой, наличие как в прайсе"),
    ("вагонка ольха 2.7 есть?",
     "такой длины нет: говорит об этом прямо и перечисляет, какие есть"),
]

# Многоходовый кусок: реплики, идущие подряд с общей историей. Первые две
# из PROVERKA специально связаны — «а сорт б почём» без контекста бессмысленно.
DLINA_DIALOGA = 2


# ── Проверки формы ───────────────────────────────────────────────────────────

_TIRE = re.compile(r"[—–]")
_MARKDOWN = re.compile(r"\*\*|__|^#{1,6}\s|^\s*[-*•]\s+|^\s*\d{1,2}[.)]\s+", re.M)
_EMODZI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")
# Между глаголом и «номером» модель вставляет вводные («оставьте, пожалуйста, номер»),
# поэтому между ними допускается несколько слов, а не один пробел.
_PROSIT_TELEFON = re.compile(
    r"(?:оставьте|напишите|укажите|пришлите|дайте|скинь\w*|нужен|необходим\w*)"
    r"(?:[\s,]+\w+){0,3}[\s,]+(?:номер|телефон|контакт)|"
    r"(?:номер|телефон)\s+(?:телефона\s+)?(?:для|чтобы)",
    re.I)
_CHISLO = re.compile(r"\d[\d\s]*(?:[.,]\d+)?")

# Числа ниже этого в ответе — это длины, количества, размеры в описании, годы
# гарантии. Цены у нас начинаются от 171 рубля за метр вагонки сорта А.
MIN_CHISLO_CENY = 100


def _dopustimye_chisla(poisk: Poisk, sistemny: str) -> set[int]:
    """Числа, которые бот имеет право назвать: всё из прайса и всё из промпта."""
    dopustimye: set[int] = set()
    for gruppa in poisk.katalog.gruppy:
        for p in gruppa.pozitsii:
            if p.article.isdigit():        # у ЭКМ ключ синтетический (nomen-…)
                dopustimye.add(int(p.article))
            for cena in (p.price_apiece, p.price_per_m):
                if cena is not None:
                    dopustimye.update(_okrestnost(cena))
            kv = cena_za_metr_kvadratnyy(p)
            if kv is not None:
                dopustimye.update(_okrestnost(kv))
            if p.length_m is not None:
                dopustimye.add(int(p.length_m * 100))    # «2.5 м» как 250 см
                dopustimye.add(int(p.length_m * 1000))
            if p.working_width_mm:
                dopustimye.add(p.working_width_mm)
            for v in p.attrs.values():
                if isinstance(v, (int, float)):
                    dopustimye.add(int(v))
    # Числа из системного промпта: доставка 1000 рублей и подобное бот назвать вправе.
    dopustimye.update(int(m.group(0)) for m in re.finditer(r"\d+", sistemny))
    return dopustimye


def _okrestnost(cena: Decimal) -> set[int]:
    """Цена и её округления: модель законно скажет «около 171» вместо 171.43."""
    c = float(cena)
    return {int(c), round(c), int(c) + 1}


def _vydumannye_chisla(otvet: str, dopustimye: set[int]) -> list[int]:
    chuzhie = []
    for m in _CHISLO.finditer(otvet):
        syroe = m.group(0).replace(" ", "").replace(",", ".")
        try:
            znachenie = float(syroe)
        except ValueError:
            continue
        if znachenie < MIN_CHISLO_CENY:
            continue
        celoe = int(znachenie)
        if celoe not in dopustimye:
            chuzhie.append(celoe)
    return chuzhie


def _zamechaniya(otvet: str, dopustimye: set[int]) -> list[str]:
    zam = []
    if _TIRE.search(otvet):
        zam.append("длинное тире")
    if _MARKDOWN.search(otvet):
        zam.append("markdown или список столбиком")
    if _EMODZI.search(otvet):
        zam.append("эмодзи")
    if _PROSIT_TELEFON.search(otvet):
        zam.append("ПРОСИТ ТЕЛЕФОН — главный баг старого бота")
    if otvet.count("\n") >= 3:
        zam.append("больше трёх строк, похоже на простыню")
    vydumannye = _vydumannye_chisla(otvet, dopustimye)
    if vydumannye:
        zam.append(f"числа не из прайса: {vydumannye}")
    return zam


def _pokazat(replika: str, chto_smotrim: str, r: OtvetAgenta, dopustimye: set[int]) -> bool:
    print(f"\n▸ клиент: {replika}")
    print(f"  смотрим: {chto_smotrim}")
    hvost = f"поиск {r.zaprosy_poiska} → {r.naydeno} поз." if r.zaprosy_poiska else "поиск НЕ вызывался"
    if r.forsirovan_poisk:
        hvost += " · 🧯 поиск форсирован предохранителем"
    print(f"  [{hvost}]")
    for stroka in r.otvet.split("\n"):
        print(f"  Александра: {stroka}")
    zam = _zamechaniya(r.otvet, dopustimye)
    if zam:
        print(f"  ⚠ {'; '.join(zam)}")
    return not zam


# ── Прогоны ──────────────────────────────────────────────────────────────────


async def _nabor() -> int:
    cfg = load_config().openrouter
    poisk = Poisk(iz_fayla_praysa())
    dopustimye = _dopustimye_chisla(poisk, sobrat_prompt(poisk.katalog))

    print("Набор этапа 8. Первые две реплики идут ОДНИМ диалогом (проверка истории).")
    chisto = True
    istoriya: list[dict] = []
    for i, (replika, chto) in enumerate(PROVERKA):
        # Первые реплики — связанный диалог, дальше каждая с чистого листа:
        # так видно и работу истории, и поведение на холодном старте.
        if i == DLINA_DIALOGA:
            istoriya = []
        with nachat_zapros("saunamart"):
            r = await otvetit(cfg, poisk, istoriya, replika)
        istoriya = r.istoriya if i < DLINA_DIALOGA else []
        chisto &= _pokazat(replika, chto, r, dopustimye)

    print("\n" + ("✅ Замечаний по форме нет — читай ответы по смыслу."
                  if chisto else "⚠ Есть замечания по форме, см. пометки выше."))
    return 0 if chisto else 1


async def _svoi(repliki: list[str]) -> int:
    cfg = load_config().openrouter
    poisk = Poisk(iz_fayla_praysa())
    dopustimye = _dopustimye_chisla(poisk, sobrat_prompt(poisk.katalog))
    istoriya: list[dict] = []
    for replika in repliki:
        with nachat_zapros("saunamart"):
            r = await otvetit(cfg, poisk, istoriya, replika)
        istoriya = r.istoriya
        _pokazat(replika, "своя реплика", r, dopustimye)
    return 0


async def _dialog() -> int:
    cfg = load_config().openrouter
    poisk = Poisk(iz_fayla_praysa())
    dopustimye = _dopustimye_chisla(poisk, sobrat_prompt(poisk.katalog))
    istoriya: list[dict] = []
    print("Диалог с ботом Saunamart. Пустая строка или Ctrl+C — выход.\n")
    while True:
        try:
            replika = input("вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not replika:
            return 0
        with nachat_zapros("saunamart"):
            r = await otvetit(cfg, poisk, istoriya, replika)
        istoriya = r.istoriya
        print(f"Александра: {r.otvet}")
        zam = _zamechaniya(r.otvet, dopustimye)
        if zam:
            print(f"  ⚠ {'; '.join(zam)}")
        print()


def main(argv: list[str]) -> int:
    repliki = [a for a in argv[1:] if not a.startswith("--")]
    if "--dialog" in argv:
        return asyncio.run(_dialog())
    if repliki:
        return asyncio.run(_svoi(repliki))
    return asyncio.run(_nabor())


if __name__ == "__main__":
    logger.info("Прогон реплик через ИИ-агента (этап 8)")
    raise SystemExit(main(sys.argv))
