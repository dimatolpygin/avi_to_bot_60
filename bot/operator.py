# -*- coding: utf-8 -*-
"""Перехват оператором: бот молчит в чате, где ответил живой менеджер (этап 14.8).

Когда менеджер отвечает клиенту вручную (в самом Авито), бот не должен влезать
поверх: на следующее сообщение клиента он бы снова ответил как ни в чём не бывало.
Поэтому поллер (`channels/avito.py`) сверяет ПОСЛЕДНЕЕ исходящее в чате со списком
реплик, которые отправил сам бот; чужое исходящее = менеджер перехватил диалог →
ставим флаг, и в этом чате бот замолкает.

Политика возврата (решение заказчика): бот молчит, пока менеджер НЕ вернёт его
вручную — кнопкой «вернуть бота» в панели (снимает флаг). Авто-возврата по таймеру
нет: взял диалог человек — он и решает, когда отдать обратно.

Состояние — в Redis (переживает рестарт, общее для поллера и панели-API: они живут
в одном процессе, но перезапускаются порознь). Redis лёг — деградируем мягко: бот
продолжает отвечать. Моргание кеша не должно ни заморозить бота навсегда, ни
глушить его ложно; поднятый флаг лежит в Redis без TTL и переживает такой сбой.
"""
from __future__ import annotations

from .logger import log_oshibka

_PREFIKS_FLAG = "sbavito:operator"       # :{kod}:{chat} → "1", пока ведёт оператор
_PREFIKS_BOT = "sbavito:botmsg"          # :{kod}:{chat} → список id реплик бота
_HRANIT_ID = 50                          # сколько последних id реплик бота помним


def _dekod(x) -> str:
    return x.decode() if isinstance(x, (bytes, bytearray)) else str(x)


class Operatory:
    """Флаг «чат ведёт живой оператор» + журнал id реплик бота (для детекции).

    `redis=None` — работаем на структурах в памяти процесса (тесты/без кеша):
    флаг всё равно действует в пределах жизни процесса.
    """

    def __init__(self, redis=None):
        self._redis = redis
        self._flagi: set[str] = set()                 # фолбэк без Redis
        self._otpravleno: dict[str, list[str]] = {}

    @staticmethod
    def _kl_flag(kod, chat) -> str:
        return f"{_PREFIKS_FLAG}:{kod}:{chat}"

    @staticmethod
    def _kl_bot(kod, chat) -> str:
        return f"{_PREFIKS_BOT}:{kod}:{chat}"

    # ── Флаг перехвата ───────────────────────────────────────────────────────

    async def vzyal(self, kod, chat) -> None:
        """Оператор перехватил чат: бот в нём замолкает."""
        klyuch = self._kl_flag(kod, chat)
        if self._redis is None:
            self._flagi.add(klyuch)
            return
        try:
            await self._redis.set(klyuch, "1")
        except Exception as e:  # noqa: BLE001 — сбой кеша не роняет диалог
            log_oshibka(f"Оператор: не поставил флаг {klyuch}: {e}")

    async def snyat(self, kod, chat) -> None:
        """Вернуть бота в чат (кнопка «вернуть бота» в панели)."""
        klyuch = self._kl_flag(kod, chat)
        if self._redis is None:
            self._flagi.discard(klyuch)
            return
        try:
            await self._redis.delete(klyuch)
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Оператор: не снял флаг {klyuch}: {e}")

    async def vedet(self, kod, chat) -> bool:
        """Ведёт ли чат оператор (бот должен молчать)."""
        klyuch = self._kl_flag(kod, chat)
        if self._redis is None:
            return klyuch in self._flagi
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
