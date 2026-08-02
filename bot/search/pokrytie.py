# -*- coding: utf-8 -*-
"""Детектор слов каталога, не покрытых словарями (этап 16, B3).

Каталог подтягивается из Google-таблицы сам, а словари домена — нет, и это
надо развести честно. Появилась в таблице новая порода или новое семейство —
поиск по атрибуту молча перестаёт их различать: `species=None`, семейство не
резолвится. Бот не падает, просто отвечает хуже — тихий провал, который
всплывёт только в живом диалоге. Детектор превращает тишину в явный сигнал:
после загрузки каталога сверяет его слова со словарями и пишет `WARNING`
«добавь строку в словарь».

Что ловим:
- **новое семейство** — `family` из каталога, которого нет в `slovar_svodnyy.json`.
  Клиент назовёт его — семейство не резолвится, поиск уходит в канал названия
  (найдёт по слову, но фильтры по семейству/атрибутам не работают).
- **нераспознанная порода** — у вагонки/полка/наличника/бруса/рейки `species=None`.
  У этих семейств порода в названии есть всегда, поэтому None здесь значит не
  «породы нет», а «слова нет в словаре пород».
- **объём парной печи не считался** (этап 17, C4) — в описании печи есть текст про
  объём, но парсер его не распознал (новая формулировка). Молчим, когда объёма
  нет вовсе — это норма, у большинства печей он просто не заполнен.

Стратегия курса 31.07: **каталог авто, словари — полуавто с явным сигналом**.
Полная автогенерация словарей невозможна — клиентские синонимы («абаш»→абаши,
«полог»→полок) человеческое знание, из таблицы не выводимое. Детектор словари
сам НЕ правит, он говорит, ЧТО добавить руками.
"""
from __future__ import annotations

from ..etl.razbor import obem_est_no_ne_raspoznan
from ..logger import logger
from .katalog import Katalog
from .slovari import Slovari

# Семейства, у которых порода в названии есть всегда: тут `species=None` —
# сигнал о нераспознанной породе, а не об её отсутствии. Совпадает по составу
# с мерными семействами, но смысл иной (там — «есть длина»), поэтому свой список.
PORODNYE_SEMEYSTVA = frozenset({"вагонка", "полок", "наличник", "брус", "рейка"})


def _porod_token(nazvanie: str) -> str:
    """Слово-кандидат в породу — второе слово названия («Вагонка Мербау …» →
    «мербау»). Нужно только чтобы не спамить лог одинаковыми предупреждениями
    по каждой длине одной и той же новой породы."""
    slova = nazvanie.split()
    return slova[1].lower().strip(".,") if len(slova) > 1 else nazvanie[:20]


def proverit_pokrytie(katalog: Katalog, sl: Slovari) -> list[str]:
    """Сверить слова каталога со словарями. Пишет `WARNING` на каждый пробел
    и возвращает список сообщений (пустой — словари покрывают каталог).

    Ничего не правит и не роняет: это сигнал человеку, а не отказ. Зовётся
    при старте (`Yadro.podgotovit`) и после каждого синка (`perezagruzit_katalog`).
    """
    izvestnye_semeystva = set(sl.semeystva)
    novye_semeystva: dict[str, str] = {}          # семейство → пример названия
    porody_bez_slovarya: dict[str, str] = {}      # токен-кандидат → пример названия
    pechi_obem_promah: dict[str, str] = {}        # печь → пример названия

    for g in katalog.gruppy:
        fam = g.family
        if fam and fam not in izvestnye_semeystva:
            novye_semeystva.setdefault(fam, g.obrazec.name)
        if fam in PORODNYE_SEMEYSTVA and g.species is None:
            porody_bez_slovarya.setdefault(_porod_token(g.obrazec.name), g.obrazec.name)
        # C4 (этап 17): у печи объём написан текстом, но парсер его не считал.
        # Молчим на печах, где объёма нет вовсе (норма) — ловим только промах формата.
        if fam == "печь" and obem_est_no_ne_raspoznan(g.obrazec.characteristics):
            pechi_obem_promah.setdefault(g.imya, g.obrazec.name)

    preduprezhdeniya: list[str] = []
    for fam, primer in sorted(novye_semeystva.items()):
        s = (f"новое семейство «{fam}» не в slovar_svodnyy.json "
             f"(пример: «{primer[:60]}») — клиент не найдёт его по типу товара")
        preduprezhdeniya.append(s)
        logger.warning("📚 Пробел словаря: %s", s)
    for _, primer in sorted(porody_bez_slovarya.items()):
        s = (f"порода не распознана: «{primer[:60]}» — добавь её в "
             f"bot/etl/razbor.py (_PORODY) и data/sinonimy_atributov.json")
        preduprezhdeniya.append(s)
        logger.warning("📚 Пробел словаря: %s", s)
    for _, primer in sorted(pechi_obem_promah.items()):
        s = (f"объём парной не распознан: «{primer[:60]}» — в описании он есть, "
             f"но формулировка новая; проверь запись или поправь разбор "
             f"(bot/etl/razbor.py, _obem_parnoy)")
        preduprezhdeniya.append(s)
        logger.warning("📚 Пробел разбора: %s", s)

    if not preduprezhdeniya:
        logger.info("📚 Словари покрывают каталог: новых семейств и пород нет")
    else:
        logger.warning("📚 Пробелов словаря: %d — поиск по ним работает хуже, "
                       "пополни словари", len(preduprezhdeniya))
    return preduprezhdeniya
