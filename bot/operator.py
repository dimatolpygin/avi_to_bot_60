# -*- coding: utf-8 -*-
"""Перехват оператором: бот молчит в чате, где ответил живой менеджер (этап 14.8).

Когда менеджер отвечает клиенту вручную (в самом Авито), бот не должен влезать
поверх: на следующее сообщение клиента он бы снова ответил как ни в чём не бывало.
Поэтому поллер (`channels/avito.py`) сверяет ПОСЛЕДНЕЕ исходящее в чате со списком
реплик, которые отправил сам бот; чужое исходящее = менеджер перехватил диалог →
ставим флаг, и в этом чате бот замолкает.

Политика возврата (решение заказчика 11.08, сменило прежнее «только вручную»): бот
молчит, пока в чате идёт разговор, и **сам включается после 3 суток тишины** — если
последний контакт (сообщение клиента ИЛИ ответ менеджера) был больше 3 дней назад,
диалог считается остывшим и боту снова можно отвечать. Ручное снятие остаётся —
кнопка «вернуть бота» в панели (`snyat`). Менеджеры сидят в amoCRM и Авито, в панель
не ходят, поэтому авто-возврат и нужен: иначе перехваченный чат заглох бы навсегда.

Реализовано через TTL флага: `vzyal` ставит флаг на 3 дня, `prodlit` продлевает его
на каждый новый контакт (поллер зовёт при входящем под оператором, вебхук amoJo —
через повторный `vzyal`). Нет активности 3 дня → флаг истёк в Redis → бот вернулся.

Состояние — в Redis (переживает рестарт, общее для поллера и панели-API: они живут
в одном процессе, но перезапускаются порознь). Redis лёг — деградируем мягко: бот
продолжает отвечать. Моргание кеша не должно ни заморозить бота навсегда, ни
глушить его ложно.
"""
from __future__ import annotations

import time

from .logger import log_oshibka

_PREFIKS_FLAG = "sbavito:operator"       # :{kod}:{chat} → "1", пока ведёт оператор
_PREFIKS_BOT = "sbavito:botmsg"          # :{kod}:{chat} → список id реплик бота
_PREFIKS_ZERK = "sbavito:opzerk"         # :{kod}:{chat} → id операторских реплик, уже в amoCRM
_HRANIT_ID = 50                          # сколько последних id реплик бота помним
TTL_VOZVRATA_S = 3 * 24 * 3600           # 3 суток тишины → бот включается сам


def _dekod(x) -> str:
    return x.decode() if isinstance(x, (bytes, bytearray)) else str(x)


class Operatory:
    """Флаг «чат ведёт живой оператор» + журнал id реплик бота (для детекции).

    `redis=None` — работаем на структурах в памяти процесса (тесты/без кеша):
    флаг всё равно действует в пределах жизни процесса. `ttl_s` — сколько тишины
    держит перехват до авто-возврата бота; `chasy` — источник времени (для тестов).
    """

    def __init__(self, redis=None, *, ttl_s: int = TTL_VOZVRATA_S, chasy=None):
        self._redis = redis
        self._ttl = ttl_s
        self._chasy = chasy or time.time
        self._flagi: dict[str, float] = {}            # фолбэк без Redis: key → истечёт в
        self._otpravleno: dict[str, list[str]] = {}
        self._zerkaleno: dict[str, list[str]] = {}

    @staticmethod
    def _kl_flag(kod, chat) -> str:
        return f"{_PREFIKS_FLAG}:{kod}:{chat}"

    @staticmethod
    def _kl_bot(kod, chat) -> str:
        return f"{_PREFIKS_BOT}:{kod}:{chat}"

    @staticmethod
    def _kl_zerk(kod, chat) -> str:
        return f"{_PREFIKS_ZERK}:{kod}:{chat}"

    # ── Флаг перехвата ───────────────────────────────────────────────────────

    async def vzyal(self, kod, chat) -> None:
        """Оператор перехватил чат: бот замолкает. Флаг живёт `ttl` (3 суток);
        новый контакт продлевает его через `prodlit`/повторный `vzyal`."""
        klyuch = self._kl_flag(kod, chat)
        if self._redis is None:
            self._flagi[klyuch] = self._chasy() + self._ttl
            return
        try:
            await self._redis.set(klyuch, "1", ex=self._ttl)
        except Exception as e:  # noqa: BLE001 — сбой кеша не роняет диалог
            log_oshibka(f"Оператор: не поставил флаг {klyuch}: {e}")

    async def prodlit(self, kod, chat) -> None:
        """Продлить перехват ещё на `ttl` от нового контакта (сообщение клиента
        или менеджера). Флага нет — ничего не делаем (не «оживляем» снятый)."""
        klyuch = self._kl_flag(kod, chat)
        if self._redis is None:
            if klyuch in self._flagi:
                self._flagi[klyuch] = self._chasy() + self._ttl
            return
        try:
            await self._redis.expire(klyuch, self._ttl)  # нет ключа → no-op
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Оператор: не продлил флаг {klyuch}: {e}")

    async def snyat(self, kod, chat) -> None:
        """Вернуть бота в чат (кнопка «вернуть бота» в панели)."""
        klyuch = self._kl_flag(kod, chat)
        if self._redis is None:
            self._flagi.pop(klyuch, None)
            return
        try:
            await self._redis.delete(klyuch)
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Оператор: не снял флаг {klyuch}: {e}")

    async def vedet(self, kod, chat) -> bool:
        """Ведёт ли чат оператор (бот должен молчать). В Redis истёкший TTL сам
        убирает флаг; в памяти проверяем срок вручную."""
        klyuch = self._kl_flag(kod, chat)
        if self._redis is None:
            srok = self._flagi.get(klyuch)
            if srok is None:
                return False
            if self._chasy() >= srok:
                self._flagi.pop(klyuch, None)     # истёк — бот вернулся
                return False
            return True
        try:
            return bool(await self._redis.get(klyuch))
        except Exception as e:  # noqa: BLE001 — кеш моргнул: не морозим бота
            log_oshibka(f"Оператор: не прочитал флаг {klyuch}: {e}")
            return False

    # ── Журнал реплик бота (детекция чужого исходящего) ──────────────────────

    async def zapomnit_otpravlennoe(self, kod, chat, msg_id) -> None:
        """Запомнить id реально отправленной ботом реплики (для детекции)."""
        if not msg_id:
            return
        klyuch = self._kl_bot(kod, chat)
        if self._redis is None:
            spisok = self._otpravleno.setdefault(klyuch, [])
            spisok.append(str(msg_id))
            del spisok[:-_HRANIT_ID]
            return
        try:
            await self._redis.rpush(klyuch, str(msg_id))
            await self._redis.ltrim(klyuch, -_HRANIT_ID, -1)
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Оператор: не записал id реплики {klyuch}: {e}")

    async def aktiven(self, kod, chat) -> bool:
        """Говорил ли бот в этом чате под 14.8 (журнал непустой).

        Пусто = холодный старт: детектор перехвата ещё не имеет базы. Последнее
        исходящее в таком чате могло быть репликой САМОГО бота ДО включения 14.8
        (в журнал она не попала), и глушить по нему нельзя — иначе на деплое бот
        замолкает во всех живых чатах разом. Сбой кеша — считаем активным (не
        зависаем в вечном базлайне; ложного глушения не даёт `bot_otpravlyal`)."""
        klyuch = self._kl_bot(kod, chat)
        if self._redis is None:
            return bool(self._otpravleno.get(klyuch))
        try:
            return bool(await self._redis.llen(klyuch))
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Оператор: не прочитал длину журнала {klyuch}: {e}")
            return True

    async def bot_otpravlyal(self, kod, chat, msg_id) -> bool:
        """Отправлял ли бот сообщение с этим id (иначе последнее исходящее —
        менеджера). Пустой id — не бот; сбой кеша — считаем «бот», чтобы не
        заглушить диалог ложно на моргании Redis."""
        if not msg_id:
            return False
        klyuch = self._kl_bot(kod, chat)
        if self._redis is None:
            return str(msg_id) in self._otpravleno.get(klyuch, [])
        try:
            spisok = await self._redis.lrange(klyuch, 0, -1)
            return str(msg_id) in [_dekod(x) for x in spisok]
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Оператор: не прочитал журнал реплик {klyuch}: {e}")
            return True

    # ── Журнал зеркалированных операторских реплик (дедуп 14.12) ──────────────

    async def operator_zerkalen(self, kod, chat, msg_id) -> bool:
        """Уже зеркалировали это ручное исходящее менеджера в amoCRM?

        Дедуп для 14.12: без него на каждом тике поллинга «последнее исходящее
        менеджера» отправлялось бы в карточку заново. Осторожно к краю: без id
        дедупить нечем, а сбой кеша не даёт проверить — в обоих случаях возвращаем
        True («уже зеркалено» → пропускаем). В живой карточке клиента лучше
        потерять зеркало одной реплики, чем задублить переписку."""
        if not msg_id:
            return True
        klyuch = self._kl_zerk(kod, chat)
        if self._redis is None:
            return str(msg_id) in self._zerkaleno.get(klyuch, [])
        try:
            spisok = await self._redis.lrange(klyuch, 0, -1)
            return str(msg_id) in [_dekod(x) for x in spisok]
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Оператор: не прочитал журнал зеркалирования {klyuch}: {e}")
            return True

    async def zapomnit_zerkalirovannoe(self, kod, chat, msg_id) -> None:
        """Пометить операторскую реплику как уже ушедшую в amoCRM (дедуп 14.12)."""
        if not msg_id:
            return
        klyuch = self._kl_zerk(kod, chat)
        if self._redis is None:
            spisok = self._zerkaleno.setdefault(klyuch, [])
            spisok.append(str(msg_id))
            del spisok[:-_HRANIT_ID]
            return
        try:
            await self._redis.rpush(klyuch, str(msg_id))
            await self._redis.ltrim(klyuch, -_HRANIT_ID, -1)
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Оператор: не записал id зеркалирования {klyuch}: {e}")
