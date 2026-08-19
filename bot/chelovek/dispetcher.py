# -*- coding: utf-8 -*-
"""Рантайм диалога: дебаунс, прерываемость, отправка порциями (этап 9).

Диспетчер стоит между транспортом и ИИ-ядром и отвечает за то, КАК идёт
переписка во времени. Что именно отвечать, он не решает — за это отвечает
`bot/ai/agent.py`, сюда он приходит функцией `otvetchik(вопрос, история)`.

На один диалог живёт РОВНО ОДНА задача. Новое сообщение клиента отменяет её
целиком — и таймер склейки, и генерацию, и недоотправленные реплики. Отсюда
главное правило слоя:

    **в историю диалога уходит только то, что реально ушло в чат.**

Иначе бот «помнит» реплику, которую клиент никогда не видел, и на следующем
ходу ссылается на несказанное — со стороны это выглядит как сбой памяти.

Транспорта здесь нет намеренно: `Kanal` — это два колбэка, и на этапе 10
в них подставится aiogram, на этапе 14 — Авито Messenger API.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..logger import (log_ishodyashchee, log_oshibka, log_pauza, log_preryvanie,
                      log_sklejka, log_vhodyashchee, nachat_zapros)
from .razbivka import Tempo, razbit

# Ответ на «что-то сломалось»: клиенту нужна человеческая фраза, а не traceback
# и не «I was unable to complete the request» (баг №2 старого бота).
FOLBEK = "Секунду, что-то подвисло. Повторите, пожалуйста, вопрос."


@dataclass
class Kanal:
    """Транспорт одного чата: чем отправить реплику и чем показать «печатает».

    `pechataet` необязателен: у Авито индикатора набора нет, там задержки
    работают молча, а слой остаётся тем же.
    """

    otpravit: Callable[[str], Awaitable[None]]
    pechataet: Callable[[], Awaitable[None]] | None = None
    imya: str = "—"          # для лога: @username или id чата


#: Как получить ответ: (вопрос клиента, история диалога, ключ диалога) → текст.
#: История — список `{"role": ..., "content": ...}`, как её ждёт `bot.ai.agent`.
#:
#: Ключ (`аккаунт:чат`) нужен отвечающему, чтобы передать лид менеджеру: контакт
#: без диалога, к которому он привязан, менеджеру бесполезен. Диспетчер сам
#: ничего с ключом не делает — просто отдаёт тому, кто знает, что с ним делать.
Otvetchik = Callable[[str, list[dict], str], Awaitable[str]]


class Pamyat(Protocol):
    """История диалога. Методы async, потому что на этапе 10 за ними встанет Redis."""

    async def istoriya(self, klyuch: str) -> list[dict]: ...
    async def dopisat(self, klyuch: str, rol: str, tekst: str) -> None: ...
    async def ochistit(self, klyuch: str) -> None: ...


@dataclass
class PamyatVPamyati:
    """История в процессе — на время этапа 9.

    На этапе 10 её заменит Redis (`sbavito:dialog:{аккаунт}:{чат}`, TTL 30 минут),
    интерфейс для этого и сделан асинхронным заранее.
    """

    maks_replik: int = 12
    _dialogi: dict[str, list[dict]] = field(default_factory=dict)

    async def istoriya(self, klyuch: str) -> list[dict]:
        return list(self._dialogi.get(klyuch, []))

    async def dopisat(self, klyuch: str, rol: str, tekst: str) -> None:
        d = self._dialogi.setdefault(klyuch, [])
        # Две реплики одной роли подряд склеиваем. Так бывает штатно: клиент
        # написал ещё раз, пока мы отвечали, и его прошлый вопрос остался без
        # ответа бота. Модели такую историю отдавать нельзя — часть провайдеров
        # требует чередования ролей и падает на двух `user` подряд.
        if d and d[-1]["role"] == rol:
            d[-1]["content"] = f"{d[-1]['content']}\n{tekst}"
        else:
            d.append({"role": rol, "content": tekst})
        if len(d) > self.maks_replik:
            del d[:-self.maks_replik]

    async def ochistit(self, klyuch: str) -> None:
        self._dialogi.pop(klyuch, None)


class Dispetcher:
    """Приём сообщений, склейка пачки, ответ по-человечески."""

    def __init__(self, otvetchik: Otvetchik, *, tempo: Tempo | None = None,
                 pamyat: Pamyat | None = None, folbek: str = FOLBEK,
                 tihiy_pri_sboe: bool = False) -> None:
        self._otvetchik = otvetchik
        self.tempo = tempo or Tempo()
        self.pamyat: Pamyat = pamyat or PamyatVPamyati()
        self.folbek = folbek
        # Молчать при сбое генерации (запрос заказчика 20.08): на ошибку ИИ — в т.ч.
        # кончившийся баланс OpenRouter (402) — НЕ шлём клиенту заглушку «подвисло»,
        # а молчим (только лог). Живой менеджер видит чат в amoCRM. По умолчанию
        # False (старое поведение) — прод включает флагом из конфига.
        self.tihiy_pri_sboe = tihiy_pri_sboe
        self._zadachi: dict[str, asyncio.Task] = {}
        self._ocheredi: dict[str, list[str]] = {}
        self._nachalo_pachki: dict[str, float] = {}
        self._ridy: dict[str, str] = {}

    # ── Приём ────────────────────────────────────────────────────────────────

    @staticmethod
    def klyuch(akkaunt: str, chat: str | int) -> str:
        """Ключ диалога. Совпадает с хвостом redis-ключа этапа 10:
        `sbavito:dialog:{аккаунт}:{чат}` — один и тот же чат в разных ботах
        это разные диалоги."""
        return f"{akkaunt}:{chat}"

    def prinyat(self, akkaunt: str, chat: str | int, tekst: str,
                kanal: Kanal) -> asyncio.Task:
        """Принять сообщение клиента и (пере)запустить цикл ответа.

        Возвращает задачу цикла — транспорту она не нужна, а тестам и probe
        удобно её дождаться.
        """
        k = self.klyuch(akkaunt, chat)
        rid = self._ridy.get(k)
        if rid is None:
            # Новая пачка — новый сквозной id. Все сообщения, которые склеятся
            # в один вопрос, и ответ на них лягут в лог под ОДНИМ id.
            rid = self._ridy[k] = uuid.uuid4().hex[:8]
            self._nachalo_pachki[k] = asyncio.get_running_loop().time()
        with nachat_zapros(akkaunt, rid):
            log_vhodyashchee(kanal.imya, chat, "", tekst)

        self._ocheredi.setdefault(k, []).append(tekst)
        staraya = self._zadachi.get(k)
        if staraya and not staraya.done():
            # Отменяем и таймер склейки, и генерацию, и недоотправленный хвост:
            # после нового сообщения клиента прежний ответ уже неактуален.
            staraya.cancel()
        zadacha = asyncio.create_task(self._cikl(k, akkaunt, rid, kanal))
        self._zadachi[k] = zadacha
        return zadacha

    async def sbros(self, akkaunt: str, chat: str | int) -> None:
        """Забыть диалог: отменить работу, выкинуть очередь и историю (`/reset`)."""
        k = self.klyuch(akkaunt, chat)
        zadacha = self._zadachi.pop(k, None)
        if zadacha and not zadacha.done():
            zadacha.cancel()
        self._ocheredi.pop(k, None)
        self._nachalo_pachki.pop(k, None)
        self._ridy.pop(k, None)
        await self.pamyat.ochistit(k)

    async def dozhdatsya(self) -> None:
        """Дождаться, пока все диалоги договорят (тесты, probe, мягкая остановка)."""
        while True:
            zhivye = [z for z in self._zadachi.values() if not z.done()]
            if not zhivye:
                return
            await asyncio.gather(*zhivye, return_exceptions=True)

    async def ostanovit(self) -> None:
        """Погасить все диалоги — для graceful shutdown (этап 10)."""
        for z in list(self._zadachi.values()):
            if not z.done():
                z.cancel()
        await asyncio.gather(*self._zadachi.values(), return_exceptions=True)
        self._zadachi.clear()
        self._ocheredi.clear()
        self._nachalo_pachki.clear()
        self._ridy.clear()

    # ── Цикл ответа ──────────────────────────────────────────────────────────

    async def _cikl(self, k: str, akkaunt: str, rid: str, kanal: Kanal) -> None:
        with nachat_zapros(akkaunt, rid):
            try:
                await self._dozhdatsya_tishiny(k)
            except asyncio.CancelledError:
                return   # клиент дописывает — цикл перезапустит следующая задача
            pachka = self._ocheredi.pop(k, [])
            if not pachka:
                self._snyat_svoyu(k)
                return
            # Пачка забрана: следующее сообщение начнёт новую, со своим id.
            self._ridy.pop(k, None)
            self._nachalo_pachki.pop(k, None)
            try:
                await self._otvetit(k, pachka, kanal)
            finally:
                self._snyat_svoyu(k)

    async def _dozhdatsya_tishiny(self, k: str) -> None:
        """Окно склейки: ждём, не допишет ли клиент.

        Ждём от ПОСЛЕДНЕГО сообщения, но не дольше потолка от первого в пачке —
        иначе человек, который печатает без остановки, не дождётся ответа никогда.
        """
        loop = asyncio.get_running_loop()
        okno = self.tempo.pauza_debounsa()
        proshlo = loop.time() - self._nachalo_pachki.get(k, loop.time())
        ostalos = min(okno, self.tempo.potolok_debounsa - proshlo)
        if ostalos <= 0:
            return
        log_pauza("жду, не допишет ли клиент", ostalos)
        await asyncio.sleep(ostalos)

    async def _otvetit(self, k: str, pachka: list[str], kanal: Kanal) -> None:
        vopros = "\n".join(pachka)
        if len(pachka) > 1:
            log_sklejka(len(pachka), vopros.replace("\n", " / "))

        istoriya = await self.pamyat.istoriya(k)
        otpravleno: list[str] = []
        repliki: list[str] = []
        try:
            # Вопрос кладём в память ДО генерации: клиент его действительно
            # задал, и отмена нашего ответа этого не отменяет.
            await self.pamyat.dopisat(k, "user", vopros)
            otvet = await self._otvetchik(vopros, istoriya, k)
            if not otvet.strip():
                raise ValueError("отвечающий вернул пустой текст")

            # «Прочитал и думаю» — молча: живой продавец не начинает печатать
            # в ту же секунду, а индикатор набора в этот момент был бы враньём.
            pauza = self.tempo.pauza_chteniya()
            log_pauza("читаю сообщение", pauza)
            await asyncio.sleep(pauza)

            repliki = razbit(otvet)
            for i, replika in enumerate(repliki):
                await self._nabirat(kanal, replika)
                await kanal.otpravit(replika)
                otpravleno.append(replika)
                log_ishodyashchee(kanal.imya, replika,
                                  meta=f"[реплика {i + 1}/{len(repliki)}]")
                if i < len(repliki) - 1:
                    pauza = self.tempo.pauza_mezhdu()
                    log_pauza("пауза между репликами", pauza)
                    await asyncio.sleep(pauza)

            await self.pamyat.dopisat(k, "assistant", " ".join(otpravleno))

        except asyncio.CancelledError:
            # Клиент написал новое, пока мы отвечали. Сохраняем РОВНО то, что
            # успело уйти в чат (реплика, отменённая в момент отправки, считается
            # неотправленной — гарантий доставки у нас нет).
            if otpravleno:
                await self.pamyat.dopisat(k, "assistant", " ".join(otpravleno))
            log_preryvanie(len(otpravleno), len(repliki))
            raise
        except Exception as e:            # noqa: BLE001 — падение одного диалога
            log_oshibka(f"Ответ не сформирован: {e}", zapros=vopros)
            if self.tihiy_pri_sboe:
                # Молчим: клиенту ничего, в историю ничего. Вопрос клиента уже в
                # памяти (записан ДО генерации), а `dopisat` склеит два `user`
                # подряд — так следующий вопрос не сломает провайдера. Смысл: при
                # кончившемся балансе/сбое ИИ бот не спамит «подвисло», менеджер
                # подхватит чат из amoCRM (входящее уже зеркалировано).
                log_ishodyashchee(kanal.imya, "", meta="[молчу: сбой ИИ]")
                return
            await kanal.otpravit(self.folbek)
            log_ishodyashchee(kanal.imya, self.folbek, meta="[фолбэк]")
            # Фолбэк тоже идёт в историю: без него в ней останутся два `user`
            # подряд, а клиент увидит ответ, которого бот «не помнит».
            await self.pamyat.dopisat(k, "assistant", self.folbek)

    async def _nabirat(self, kanal: Kanal, replika: str) -> None:
        """Пауза набора реплики, всё это время держим статус «печатает»."""
        sekund = self.tempo.pauza_nabora(replika)
        log_pauza("набираю реплику", sekund, meta=f"{len(replika)} символов")
        if kanal.pechataet is None:
            await asyncio.sleep(sekund)
            return
        ostalos = sekund
        while ostalos > 0:
            # Индикатор в Telegram гаснет через ~5 секунд, поэтому обновляем
            # его по ходу набора, а не выставляем один раз в начале.
            await kanal.pechataet()
            shag = min(self.tempo.obnovlenie_pechataet, ostalos)
            await asyncio.sleep(shag)
            ostalos -= shag

    def _snyat_svoyu(self, k: str) -> None:
        """Снять задачу из реестра, только если она наша.

        При отмене нас уже сменила новая задача — затирать её нельзя, иначе
        следующее сообщение не сможет прервать текущий ответ.
        """
        if self._zadachi.get(k) is asyncio.current_task():
            self._zadachi.pop(k, None)
