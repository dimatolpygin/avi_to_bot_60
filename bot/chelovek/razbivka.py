# -*- coding: utf-8 -*-
"""Дробление ответа на реплики и живые задержки (этап 9).

Здесь только чистые функции и числа — ни сети, ни asyncio. Рантайм, который
этим пользуется (дебаунс, отправка, прерывание), лежит в `dispetcher.py`.

Что делает бота похожим на человека в переписке, по убыванию важности:

1. **Он не отвечает мгновенно.** Мгновенный ответ на вопрос про цену — самый
   заметный признак робота, заметнее любого стиля текста.
2. **Он не пишет простынёй.** Длинную мысль человек шлёт двумя-тремя короткими
   сообщениями, а не одним абзацем.
3. **Его паузы не одинаковые.** Ровно 10 секунд на каждый ответ выдают таймер,
   поэтому все задержки и порог разреза — случайные из диапазона.

Числа взяты из `vk0043/humanize.py` (демо, выигравшее сделку) и там обкатаны
на живых диалогах. В telewin этого нет сознательно: там опт и скорость ответа
ценнее правдоподобия, у нас — розница на Авито, где переписка идёт как с продавцом.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, replace

# Короткие ответы (а их у нас большинство — промпт держит две-три фразы) уходят
# одним сообщением. Дробим только заметно длинные.
CELIKOM_DO = 210
REZ_MIN = 110
REZ_MAX = 190

# Больше трёх сообщений подряд от продавца читаются как спам-рассылка, а не как
# живой человек. Остаток склеивается в последнюю реплику.
MAKS_REPLIK = 3

_GRANICA_PREDLOZHENIYA = re.compile(r"(?<=[.!?…])\s+")


def razbit(tekst: str) -> list[str]:
    """Ответ → реплики в порядке отправки.

    Режем ТОЛЬКО по границам предложений. Резать внутри предложения нельзя:
    в наших ответах живут цены и размеры («513 рублей за штуку», «1900х700»),
    и разрыв посередине превращает верное число в бессмыслицу — это тот же
    класс ошибки, что и выдуманная цена. Поэтому предложение длиннее лимита
    уходит целиком, а не подгоняется под порог.
    """
    tekst = (tekst or "").strip()
    if not tekst:
        return []

    repliki: list[str] = []
    # Одиночные переносы строк `stil.py` оставляет — это отдельные мысли,
    # и человек отправил бы их разными сообщениями.
    for blok in (b.strip() for b in tekst.split("\n")):
        if not blok:
            continue
        if len(blok) <= CELIKOM_DO:
            repliki.append(blok)
            continue
        # Порог разреза свой на каждый кусок: одинаковая длина реплик палит бота
        # не хуже одинаковых пауз.
        predel = random.randint(REZ_MIN, REZ_MAX)
        tekushchaya = ""
        for predlozhenie in _GRANICA_PREDLOZHENIYA.split(blok):
            kandidat = f"{tekushchaya} {predlozhenie}".strip()
            if tekushchaya and len(kandidat) > predel:
                repliki.append(tekushchaya)
                tekushchaya = predlozhenie
                predel = random.randint(REZ_MIN, REZ_MAX)
            else:
                tekushchaya = kandidat
        if tekushchaya:
            repliki.append(tekushchaya)

    if len(repliki) > MAKS_REPLIK:
        hvost = " ".join(repliki[MAKS_REPLIK - 1:])
        repliki = [*repliki[:MAKS_REPLIK - 1], hvost]
    return repliki or [tekst]


@dataclass(frozen=True)
class Tempo:
    """Диапазоны задержек. Все паузы берутся из них случайно.

    Отдельным объектом, а не константами модуля, ровно по одной причине:
    тесты и `probe --bystro` обязаны прогонять тот же код без реальных
    двадцати секунд ожидания — `uskorit()` сжимает все интервалы разом.
    """

    chtenie: tuple[float, float] = (2.0, 5.0)          # «прочитал и думаю», молча
    nabor_baza: tuple[float, float] = (9.0, 13.0)      # набор реплики
    nabor_za_simvol: float = 0.06
    nabor_potolok: float = 26.0                        # длинную реплику не ждём вечно
    mezhdu_replikami: tuple[float, float] = (1.0, 2.5)
    # Окно тишины после последнего сообщения клиента: пишет дальше — ждём дальше.
    okno_debounsa: tuple[float, float] = (2.5, 4.5)
    # Предохранитель: непрерывно пишущий человек всё равно получит ответ.
    potolok_debounsa: float = 12.0
    # Индикатор «печатает» в Telegram живёт ~5 секунд, обновляем чаще.
    obnovlenie_pechataet: float = 4.0

    def uskorit(self, k: float) -> "Tempo":
        """Тот же темп, сжатый в k раз (k=0.1 — десятая доля от боевого)."""
        def d(p: tuple[float, float]) -> tuple[float, float]:
            return (p[0] * k, p[1] * k)
        return replace(
            self,
            chtenie=d(self.chtenie),
            nabor_baza=d(self.nabor_baza),
            nabor_za_simvol=self.nabor_za_simvol * k,
            nabor_potolok=self.nabor_potolok * k,
            mezhdu_replikami=d(self.mezhdu_replikami),
            okno_debounsa=d(self.okno_debounsa),
            potolok_debounsa=self.potolok_debounsa * k,
            obnovlenie_pechataet=self.obnovlenie_pechataet * k,
        )

    def pauza_chteniya(self) -> float:
        return random.uniform(*self.chtenie)

    def pauza_nabora(self, replika: str) -> float:
        """Время набора реплики: база плюс поправка на длину, с потолком."""
        baza = random.uniform(*self.nabor_baza)
        return min(baza + len(replika) * self.nabor_za_simvol, self.nabor_potolok)

    def pauza_mezhdu(self) -> float:
        return random.uniform(*self.mezhdu_replikami)

    def pauza_debounsa(self) -> float:
        return random.uniform(*self.okno_debounsa)
