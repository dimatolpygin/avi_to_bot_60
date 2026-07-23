# -*- coding: utf-8 -*-
"""Прогон очеловечивания — то, чем этап 9 проверяется руками.

    python -m bot.chelovek.probe            # четыре сценария в реальном темпе (~2 мин)
    python -m bot.chelovek.probe --bystro   # то же, ускоренно, чтобы просто увидеть ход
    python -m bot.chelovek.probe --zhivoy   # живой чат в консоли, ходит в OpenRouter

Сценарии идут на **подменённом отвечающем**: сети и денег не тратят, а проверяют
ровно то, за что отвечает этап 9, — время и порядок реплик. Слева в каждой строке
секундомер от начала сценария: по нему видно и паузы, и их разброс.

`--zhivoy` — единственный режим, который ходит в модель и тратит деньги. Он нужен,
чтобы перебить бота на середине ответа своими руками: наберите три коротких
сообщения подряд и посмотрите, что придёт один ответ, а не три.
"""
from __future__ import annotations

import asyncio
import sys
import time

from ..logger import logger
from .dispetcher import Dispetcher, Kanal, PamyatVPamyati
from .razbivka import Tempo

AKKAUNT = "saunamart"
CHAT = "probe"

KOROTKIY = "Липа три метра есть, сорт А по 513 рублей за штуку. Сколько метров нужно?"

DLINNYY = (
    "Липа три метра есть двух сортов: А по 513 рублей за штуку и В по 371. "
    "Разница только в сучках, на прочность она не влияет. "
    "Сорт А идёт почти без сучков, его чаще берут на стены на виду. "
    "Сорт В подешевле и выглядит живее, многие берут именно его. "
    "Какой из них вам посчитать?"
)


class KonsolKanal:
    """Канал в консоль: реплики и индикатор набора с секундомером."""

    def __init__(self) -> None:
        self.nachalo = time.monotonic()
        self.repliki: list[str] = []
        self._pechataet_pokazan = False

    def _chasy(self) -> str:
        return f"[{time.monotonic() - self.nachalo:5.1f} с]"

    def sbros_chasov(self) -> None:
        self.nachalo = time.monotonic()

    def klient(self, tekst: str) -> None:
        print(f"{self._chasy()}   клиент → {tekst}")

    async def otpravit(self, tekst: str) -> None:
        self.repliki.append(tekst)
        print(f"{self._chasy()}   Александра → {tekst}")
        self._pechataet_pokazan = False

    async def pechataet(self) -> None:
        # Индикатор обновляется каждые несколько секунд, но в консоли печатаем
        # его один раз на реплику — иначе он забьёт весь вывод.
        if not self._pechataet_pokazan:
            print(f"{self._chasy()}   … печатает")
            self._pechataet_pokazan = True

    def kanal(self) -> Kanal:
        return Kanal(otpravit=self.otpravit, pechataet=self.pechataet, imya="probe")


class Zaglushka:
    """Отвечающий без сети: спит «как модель» и отдаёт заготовленный текст."""

    def __init__(self, otvet: str, dumaet: float = 2.0) -> None:
        self.otvet = otvet
        self.dumaet = dumaet
        self.voprosy: list[str] = []

    async def __call__(self, vopros: str, istoriya: list[dict]) -> str:
        self.voprosy.append(vopros)
        await asyncio.sleep(self.dumaet)
        return self.otvet


def _zagolovok(nomer: int, nazvanie: str, chto_smotret: str) -> None:
    print(f"\n{'─' * 78}\nСценарий {nomer}. {nazvanie}\nЧто смотреть: {chto_smotret}\n")


def _itog(uslovie: bool, tekst: str) -> bool:
    print(f"  {'✅' if uslovie else '❌'} {tekst}")
    return uslovie


# ── Сценарии ─────────────────────────────────────────────────────────────────

async def _odinochnyy(tempo: Tempo, k: float) -> bool:
    _zagolovok(1, "Обычный вопрос",
               "ответ приходит не мгновенно; перед ним виден набор")
    kanal = KonsolKanal()
    zaglushka = Zaglushka(KOROTKIY, dumaet=2.0 * k)
    d = Dispetcher(zaglushka, tempo=tempo, pamyat=PamyatVPamyati())

    kanal.klient("липа три метра почём")
    d.prinyat(AKKAUNT, CHAT, "липа три метра почём", kanal.kanal())
    await d.dozhdatsya()
    proshlo = time.monotonic() - kanal.nachalo
    return _itog(len(kanal.repliki) == 1 and proshlo > 5 * k,
                 f"одна реплика, ответ через {proshlo:.1f} с (мгновенного ответа нет)")


async def _sklejka(tempo: Tempo, k: float) -> bool:
    _zagolovok(2, "Три сообщения подряд",
               "клиент дописывает мысль тремя сообщениями — ответ должен быть ОДИН")
    kanal = KonsolKanal()
    zaglushka = Zaglushka(KOROTKIY, dumaet=1.5 * k)
    d = Dispetcher(zaglushka, tempo=tempo, pamyat=PamyatVPamyati())

    for tekst, pauza in [("здравствуйте", 1.5), ("нужна вагонка липа", 1.5),
                         ("три метра", 0.0)]:
        kanal.klient(tekst)
        d.prinyat(AKKAUNT, CHAT, tekst, kanal.kanal())
        if pauza:
            await asyncio.sleep(pauza * k)
    await d.dozhdatsya()

    ok = _itog(len(zaglushka.voprosy) == 1,
               f"поиск и модель вызваны {len(zaglushka.voprosy)} раз (нужен 1)")
    if zaglushka.voprosy:
        print(f"     вопрос целиком: {zaglushka.voprosy[0]!r}")
    return ok


async def _droblenie(tempo: Tempo, k: float) -> bool:
    _zagolovok(3, "Длинный ответ",
               "приходит 2–3 репликами с паузами, а не простынёй одним куском")
    kanal = KonsolKanal()
    d = Dispetcher(Zaglushka(DLINNYY, dumaet=1.5 * k), tempo=tempo,
                   pamyat=PamyatVPamyati())

    kanal.klient("расскажите про липу, чем сорта отличаются")
    d.prinyat(AKKAUNT, CHAT, "расскажите про липу, чем сорта отличаются", kanal.kanal())
    await d.dozhdatsya()

    ok = _itog(2 <= len(kanal.repliki) <= 3, f"реплик: {len(kanal.repliki)} (нужно 2–3)")
    return ok and _itog(" ".join(kanal.repliki) == DLINNYY,
                        "текст не потерян и не переставлен")


async def _preryvanie(tempo: Tempo, k: float) -> bool:
    _zagolovok(4, "Клиент перебивает",
               "новое сообщение отменяет ответ; в память идёт только отправленное")
    kanal = KonsolKanal()
    pamyat = PamyatVPamyati()
    zaglushka = Zaglushka(DLINNYY, dumaet=1.5 * k)
    d = Dispetcher(zaglushka, tempo=tempo, pamyat=pamyat)

    kanal.klient("расскажите про липу")
    d.prinyat(AKKAUNT, CHAT, "расскажите про липу", kanal.kanal())
    # Ждём первую реплику и перебиваем: остаток ответа клиент уже не увидит.
    while not kanal.repliki:
        await asyncio.sleep(0.05 * k)
    await asyncio.sleep(0.5 * k)
    kanal.klient("хотя нет, покажите лучше полок")
    d.prinyat(AKKAUNT, CHAT, "хотя нет, покажите лучше полок", kanal.kanal())
    await d.dozhdatsya()

    istoriya = await pamyat.istoriya(Dispetcher.klyuch(AKKAUNT, CHAT))
    otvety = [z["content"] for z in istoriya if z["role"] == "assistant"]
    prervannyy = otvety[0] if otvety else ""
    print("\n  из прерванного ответа бот запомнил: " + (prervannyy or "(ничего)"))
    return _itog(prervannyy == kanal.repliki[0] and prervannyy != DLINNYY,
                 "запомнено ровно то, что клиент успел увидеть, без хвоста")


# ── Живой режим ──────────────────────────────────────────────────────────────

async def _zhivoy(tempo: Tempo) -> int:
    """Чат в консоли поверх настоящего ИИ-ядра. Тратит деньги."""
    from ..config import load_config
    from ..ai.agent import otvetit
    from ..search.katalog import iz_fayla_praysa
    from ..search.search import Poisk

    cfg = load_config().openrouter
    poisk = Poisk(iz_fayla_praysa())

    async def otvetchik(vopros: str, istoriya: list[dict]) -> str:
        return (await otvetit(cfg, poisk, istoriya, vopros)).otvet

    kanal = KonsolKanal()
    d = Dispetcher(otvetchik, tempo=tempo, pamyat=PamyatVPamyati())
    print("Живой чат с Александрой. Пишите как клиент; можно несколько сообщений\n"
          "подряд — они склеятся в один вопрос. Пустая строка — выход.\n")
    try:
        while True:
            stroka = (await asyncio.to_thread(input)).strip()
            if not stroka:
                break
            d.prinyat(AKKAUNT, CHAT, stroka, kanal.kanal())
    except (EOFError, KeyboardInterrupt):
        print()
    await d.dozhdatsya()
    return 0


# ── Запуск ───────────────────────────────────────────────────────────────────

async def _stsenarii(tempo: Tempo, k: float) -> int:
    itogi = [await _odinochnyy(tempo, k), await _sklejka(tempo, k),
             await _droblenie(tempo, k), await _preryvanie(tempo, k)]
    print(f"\n{'─' * 78}")
    if all(itogi):
        print("✅ Все четыре сценария отработали как надо.")
        print("   Глазами проверьте главное: паузы разной длины и ответ, который "
              "не приходит мгновенно.")
        return 0
    print(f"❌ Провалено сценариев: {sum(1 for i in itogi if not i)} из {len(itogi)}")
    return 1


def main(argv: list[str]) -> int:
    # Ускорение сжимает ВСЕ интервалы разом, включая паузы между сообщениями
    # клиента в сценариях, — иначе склейка перестала бы попадать в окно.
    k = 0.15 if "--bystro" in argv else 1.0
    tempo = Tempo().uskorit(k) if k != 1.0 else Tempo()
    if "--zhivoy" in argv:
        return asyncio.run(_zhivoy(tempo))
    if k == 1.0:
        print("Прогон в реальном темпе, займёт около двух минут. "
              "Быстро и без пауз: --bystro")
    return asyncio.run(_stsenarii(tempo, k))


if __name__ == "__main__":
    logger.info("Прогон очеловечивания (этап 9)")
    raise SystemExit(main(sys.argv))
