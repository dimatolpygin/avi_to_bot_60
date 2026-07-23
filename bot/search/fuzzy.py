# -*- coding: utf-8 -*-
"""Сходство слов для фаззи-канала (этап 5): опечатки клиента → слова каталога.

Две метрики, максимум из них. Одной не хватает:

* **триграммная** (совместима с `pg_trgm`, если поиск когда-нибудь уедет в SQL)
  ловит перестановки и вставки, но проваливается на коротких словах;
* **левенштейновская** точна на коротких: «вогонка»↔«вагонка» даёт 0.86,
  а триграммная на той же паре — 0.45.

Порог по проекту — 0.85 (`PORG_OPECHATKI` в `slovari.py`): «вогонка» проходит,
«вагон» и «болгарка» — нет. Считаем сами, без `rapidfuzz`: тридцать строк кода
против зависимости, которую пришлось бы ставить и в тестовое окружение.
"""
from __future__ import annotations

import re

_SLOVO = re.compile(r"[a-zа-я0-9]+")


def trigrams(s: str) -> set[str]:
    """Триграммы как в `pg_trgm`: слово дополняется двумя пробелами слева
    и одним справа, чтобы начало и конец слова весили больше середины."""
    tg = set()
    for w in _SLOVO.findall(s.lower()):
        w2 = "  " + w + " "
        for i in range(len(w2) - 2):
            tg.add(w2[i:i + 3])
    return tg


def similarity(a: str, b: str) -> float:
    """Триграммное сходство в [0,1]: |A∩B| / |A∪B| (определение pg_trgm)."""
    A, B = trigrams(a), trigrams(b)
    if not A and not B:
        return 0.0
    obshie = len(A & B)
    return obshie / (len(A) + len(B) - obshie)


def _levenshtein(a: str, b: str) -> int:
    """Расстояние Левенштейна, память O(len(b))."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def edit_ratio(a: str, b: str) -> float:
    """Нормализованное сходство по Левенштейну: 1 − dist/max(len)."""
    if not a and not b:
        return 0.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b))


def word_sim(a: str, b: str) -> float:
    """Сходство двух слов: максимум триграммного и левенштейновского."""
    return max(similarity(a, b), edit_ratio(a, b))
