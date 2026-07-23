# -*- coding: utf-8 -*-
"""Загрузка словарей домена и резолв по ним (этап 5).

Четыре файла в `data/`, все правятся руками без единой строчки кода
(критерий приёмки этапа: правка словаря подхватывается перезапуском):

* `slovar_svodnyy.json` — 13 семейств каталога, ключ = `products.family`;
* `sinonimy_atributov.json` — сорта (`Б ≡ В`) и породы в клиентском языке;
* `sleng_razmerov.json` — «трёхметровая» → 3.0, «стандартная» дверь → 1900×700;
* `chuzhoy_domen.json` — маркеры не нашего профиля, гейт абстейна.

Три вещи, которые здесь неочевидны:

1. **Гейт чужого домена срабатывает ДО поиска** (используется на этапе 6).
   Иначе «болгарка» цепляется за общее слово и выдаёт мусор — в telewin этот
   гейт поднял абстейн с 34% до 91%.
2. **Точное совпадение считается по стемам, фаззи — по словам.** У «вогонка»
   и «вагонка» сходство 0.86, а у стемов «вогонк»/«вагонк» — 0.83, то есть
   стемминг убивает ровно тот сигнал, ради которого фаззи-канал и заведён.
3. **Словари кешируются в памяти процесса.** Как и каталог товаров: правка
   файла на живом процессе не видна до рестарта — это осознанный размен
   в пользу скорости на каждом сообщении.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from ..logger import logger
from .fuzzy import word_sim
from .normalize import norm, slova, stem_slova, stems

# data/ лежит в корне проекта: bot/search/slovari.py → bot/search → bot → корень.
KATALOG_DANNYH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")

# Порог фаззи-канала. 0.85 подобран под пару «вогонка»↔«вагонка» (0.857):
# опечатка в один символ проходит, соседнее слово («вагон», «полка») — нет.
PORG_OPECHATKI = 0.85

# Короче четырёх символов фаззи не считаем: на трёхбуквенных словах любая пара
# выглядит похожей, и «печь» начинает находиться в «речь».
MIN_DLINA_OPECHATKI = 4

# Служебные ключи словарей — комментарии для человека, а не данные.
def _bez_sluzhebnyh(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


@dataclass(frozen=True)
class Semeystvo:
    """Одно семейство товаров из сводного словаря."""

    klyuch: str                      # == products.family
    kanon: str
    rodovoe: str
    sinonimy: tuple[str, ...]
    # Семейство, которое клиент имеет в виду, назвав только породу и длину
    # («липа 3 метра»). Родовой канал поиска (этап 6) поднимает его в топ.
    rodovoe_po_umolchaniyu: bool = False
    # Стемы каждого варианта названия (ключ, канон, каждый синоним) — точный канал.
    varianty: tuple[frozenset[str], ...] = field(repr=False, default=())
    # Однословные варианты как слова — фаззи-канал (опечатки).
    odnoslovnye: tuple[str, ...] = field(repr=False, default=())


@dataclass(frozen=True)
class Slovari:
    """Все словари домена, загруженные и проиндексированные."""

    semeystva: dict[str, Semeystvo]
    sort: dict[str, str]                          # «б» → «В»
    poroda_slovo: dict[str, str]                  # «липовая» → «липа»
    poroda_fraza: tuple[tuple[str, str], ...]     # («термо липа», «термолипа»), длинные первыми
    dlina_stem: dict[str, float]                  # стем «трёхметровая» → 3.0
    dlina_fraza: tuple[tuple[str, float], ...]    # («три метра», 3.0), длинные первыми
    dver_gabarit: dict[str, tuple[int, int]]      # «стандартная» → (1900, 700)
    chuzhoy_kvalifikatory: tuple[str, ...]
    chuzhoy_tovary: tuple[re.Pattern, ...] = field(repr=False, default=())
    istochnik: str = ""

    # ── Резолв семейства ─────────────────────────────────────────────────────

    def semeystvo_zaprosa(self, tekst: str) -> tuple[str | None, str | None]:
        """Семейство по запросу клиента → (ключ, канал) или (None, None).

        Канал — чем нашли: `точно` (совпало имя семейства), `синоним`
        («полог», «каменка», «доска на полок»), `опечатка` (фаззи ≥ 0.85).
        Канал попадает в лог, чтобы по нему было видно, какой словарь работает.
        """
        st = stems(tekst)
        # Кандидат: (длина совпавшего варианта, вес, ключ, канал). Длина стоит
        # первой, потому что побеждать должно САМОЕ КОНКРЕТНОЕ совпадение:
        # у «камни для печи» одинаково хорошо совпадает и ключ «печь» (одно
        # слово), и синоним камня «камни для печи» (два) — и клиент имел в виду
        # второе. При равной длине надёжнее имя семейства, чем синоним.
        luchshee: tuple[int, int, str, str] | None = None
        for klyuch, sem in self.semeystva.items():
            for i, variant in enumerate(sem.varianty):
                if not variant or not variant <= st:
                    continue
                # i == 0 — имя семейства, i == 1 — его канон: и то и другое
                # «точно». «Синоним» остаётся за народными формами («полог»,
                # «каменка»), чтобы по каналу в логе было видно, что сработал
                # именно словарь сленга, а не собственное имя товара.
                kanal = "точно" if i <= 1 else "синоним"
                ves = 2 if i <= 1 else 1
                kandidat = (len(variant), ves, klyuch, kanal)
                if luchshee is None or kandidat > luchshee:
                    luchshee = kandidat
        if luchshee:
            return luchshee[2], luchshee[3]

        # Фаззи: слово запроса, похожее на название семейства. Считаем по словам,
        # не по стемам — стемминг съедает разницу, ради которой канал и нужен.
        luchshaya_para: tuple[float, str] | None = None
        for w in slova(tekst):
            if len(w) < MIN_DLINA_OPECHATKI:
                continue
            for klyuch, sem in self.semeystva.items():
                for variant in sem.odnoslovnye:
                    s = word_sim(w, variant)
                    if s >= PORG_OPECHATKI and (luchshaya_para is None or s > luchshaya_para[0]):
                        luchshaya_para = (s, klyuch)
        if luchshaya_para:
            return luchshaya_para[1], "опечатка"
        return None, None

    # ── Гейт чужого домена ───────────────────────────────────────────────────

    def chuzhoy_domen(self, tekst: str) -> str | None:
        """Маркер чужого домена в запросе или None.

        Квалификаторы («керамическая плитка», «входная дверь») ловятся
        подстрокой: они уточняют слова, которые у нас есть. Чужие товары
        («болгарка») — по границе слова, чтобы не срабатывать внутри других.
        """
        qn = norm(tekst)
        for k in self.chuzhoy_kvalifikatory:
            if k in qn:
                return k
        for rx in self.chuzhoy_tovary:
            m = rx.search(qn)
            if m:
                return m.group(0)
        return None


def _sobrat_semeystva(syroy: dict) -> dict[str, Semeystvo]:
    semeystva: dict[str, Semeystvo] = {}
    for klyuch, e in _bez_sluzhebnyh(syroy).items():
        sinonimy = tuple(e.get("синонимы", []))
        kanon = e.get("канон", "")
        # Порядок вариантов важен: нулевой — сам ключ (канал «точно»).
        nazvaniya = [klyuch, kanon, *sinonimy]
        varianty = tuple(frozenset(stems(n)) for n in nazvaniya if n and stems(n))
        odnoslovnye = tuple({klyuch, *(s for s in nazvaniya if s and " " not in s)})
        semeystva[klyuch] = Semeystvo(
            klyuch=klyuch, kanon=kanon, rodovoe=e.get("родовое", ""),
            sinonimy=sinonimy, varianty=varianty, odnoslovnye=odnoslovnye,
            rodovoe_po_umolchaniyu=bool(e.get("родовое_по_умолчанию", False)),
        )
    return semeystva


def _chitat(katalog: str, imya: str) -> dict:
    put = os.path.join(katalog, imya)
    if not os.path.exists(put):
        raise FileNotFoundError(
            f"❌ Не найден словарь {imya} в {katalog}. Он лежит в репозитории "
            f"(data/), проверь, не сбит ли рабочий каталог.")
    with open(put, encoding="utf-8") as f:
        return json.load(f)


def zagruzit(katalog: str | None = None) -> Slovari:
    """Прочитать и проиндексировать все четыре словаря. Кеш не трогает."""
    katalog = katalog or KATALOG_DANNYH

    svodnyy = _chitat(katalog, "slovar_svodnyy.json")
    atributy = _chitat(katalog, "sinonimy_atributov.json")
    sleng = _chitat(katalog, "sleng_razmerov.json")
    chuzhoy = _chitat(katalog, "chuzhoy_domen.json")

    semeystva = _sobrat_semeystva(svodnyy)

    sort = {norm(k): v for k, v in _bez_sluzhebnyh(atributy.get("сорт", {})).items()}

    poroda_slovo: dict[str, str] = {}
    poroda_fraza: list[tuple[str, str]] = []
    for poroda, formy in _bez_sluzhebnyh(atributy.get("порода", {})).items():
        for f in formy:
            fn = norm(f)
            if " " in fn:
                poroda_fraza.append((fn, poroda))
            else:
                poroda_slovo[fn] = poroda
    # Длинные фразы раньше коротких: «термо липа» должна сработать до «липа».
    poroda_fraza.sort(key=lambda p: -len(p[0]))

    dlina_stem = {stem_slova(k): float(v)
                  for k, v in _bez_sluzhebnyh(sleng.get("длина_слово", {})).items()}
    dlina_fraza = sorted(
        ((norm(k), float(v)) for k, v in _bez_sluzhebnyh(sleng.get("длина_фраза", {})).items()),
        key=lambda p: -len(p[0]))

    dver_gabarit = {norm(k): (int(v[0]), int(v[1]))
                    for k, v in _bez_sluzhebnyh(sleng.get("дверь_габарит", {})).items()}

    kval = tuple(norm(k) for k in chuzhoy.get("квалификаторы", []))
    tovary = tuple(re.compile(rf"\b{re.escape(norm(t))}") for t in chuzhoy.get("чужие_товары", []))

    s = Slovari(
        semeystva=semeystva, sort=sort,
        poroda_slovo=poroda_slovo, poroda_fraza=tuple(poroda_fraza),
        dlina_stem=dlina_stem, dlina_fraza=tuple(dlina_fraza),
        dver_gabarit=dver_gabarit,
        chuzhoy_kvalifikatory=kval, chuzhoy_tovary=tovary,
        istochnik=katalog,
    )
    logger.info(
        f"📚 Словари загружены: {len(semeystva)} семейств, {len(sort)} сортов, "
        f"{len(poroda_slovo) + len(poroda_fraza)} форм пород, "
        f"{len(dlina_stem) + len(dlina_fraza)} разговорных длин, "
        f"{len(kval) + len(tovary)} маркеров чужого домена ({katalog})")
    return s


_kesh: Slovari | None = None


def slovari() -> Slovari:
    """Словари из кеша процесса; первый вызов читает файлы с диска."""
    global _kesh
    if _kesh is None:
        _kesh = zagruzit()
    return _kesh


def sbrosit_kesh() -> None:
    """Забыть загруженное — следующий `slovari()` перечитает файлы.
    Нужен тестам и ручной перезагрузке словарей без рестарта процесса."""
    global _kesh
    _kesh = None
