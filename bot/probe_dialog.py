# -*- coding: utf-8 -*-
"""Неинтерактивный прогон диалога по любому из трёх аккаунтов (проверка этапа 10).

Зачем отдельно от `bot.ai.probe` и `bot.chelovek.probe`:

* `bot.ai.probe` знает только товарный аккаунт и зовёт агента напрямую — мимо
  ядра, памяти и очеловечивания;
* `bot.chelovek.probe --zhivoy` идёт через весь слой, но интерактивен: сценарий
  из десяти реплик им не прогонишь и в лог не положишь.

Здесь то же ядро, что у живых ботов (`Yadro` → диспетчер → агент), но реплики
клиента подаются списком, а канал печатает расшифровку в консоль. Память —
в процессе, не в Redis: прогон не должен затирать живые диалоги.

⚠️ Ходит в OpenRouter и тратит деньги (диалог из десяти реплик ≈ 1-2 цента).

    python -m bot.probe_dialog saunamart "почём липа" "а сорт б"
    python -m bot.probe_dialog sbsauna --fayl scenariy.json
    python -m bot.probe_dialog saunamart --pachkoy "дверь" "печь" "ольха"

`--pachkoy` шлёт реплики без ожидания ответа — так проверяется склейка
и прерываемость: ровно то, что делает живой клиент, дописывающий вопрос.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from .chelovek.dispetcher import Kanal, PamyatVPamyati
from .chelovek.razbivka import Tempo
from .config import load_config
from .core import Yadro
from .profili import PROFILI


class KonsolKanal:
    """Канал-расшифровка: реплики бота падают в консоль с секундомером."""

    def __init__(self, start: float) -> None:
        self.start = start
        self.repliki: list[str] = []

    def kanal(self) -> Kanal:
        return Kanal(otpravit=self._otpravit, imya="probe")

    async def _otpravit(self, tekst: str) -> None:
        self.repliki.append(tekst)
        print(f"  [{time.monotonic() - self.start:5.1f}с] 🤖 {tekst}", flush=True)


async def progon(kod: str, repliki: list[str], *, pachkoy: bool, uskorenie: float,
                 chat: str = "probe") -> int:
    cfg = load_config()
    yadro = Yadro(cfg, PamyatVPamyati(), tempo=Tempo().uskorit(uskorenie))
    await yadro.podgotovit([kod])

    privet = await yadro.zapomnit_privetstvie(kod, chat)
    print(f"\n=== {kod} ===\n  [  0.0с] 🤖 {privet}   [приветствие /start]\n", flush=True)

    start = time.monotonic()
    kanal = KonsolKanal(start)
    zadachi = []
    for tekst in repliki:
        print(f"  [{time.monotonic() - start:5.1f}с] 👤 {tekst}", flush=True)
        zadachi.append(yadro.obrabotat(kod, chat, tekst, kanal.kanal()))
        if pachkoy:
            # Пачкой — значит клиент дописывает, не дожидаясь ответа: пауза
            # меньше окна склейки, чтобы сработали и склейка, и прерывание.
            await asyncio.sleep(0.3)
        else:
            await asyncio.gather(*zadachi, return_exceptions=True)
            zadachi.clear()
    await yadro.dispetcher(kod).dozhdatsya()
    await yadro.ostanovit()

    print(f"\n  Итого реплик бота: {len(kanal.repliki)}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Прогон диалога через ядро (тратит деньги)")
    p.add_argument("akkaunt", choices=sorted(PROFILI), help="код аккаунта")
    p.add_argument("repliki", nargs="*", help="реплики клиента по порядку")
    p.add_argument("--fayl", help="json-список реплик вместо аргументов")
    p.add_argument("--pachkoy", action="store_true",
                   help="слать не дожидаясь ответа (проверка склейки и прерывания)")
    p.add_argument("--uskorenie", type=float, default=0.05,
                   help="во сколько раз сжать паузы (1.0 — живой темп)")
    a = p.parse_args()

    repliki = a.repliki
    if a.fayl:
        with open(a.fayl, encoding="utf-8") as f:
            repliki = json.load(f)
    if not repliki:
        p.error("нечего спрашивать: дайте реплики или --fayl")

    return asyncio.run(progon(a.akkaunt, repliki, pachkoy=a.pachkoy,
                              uskorenie=a.uskorenie))


if __name__ == "__main__":
    sys.exit(main())
