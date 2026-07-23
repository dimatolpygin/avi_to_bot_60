# -*- coding: utf-8 -*-
"""История диалога в Redis (этап 10).

Реализация протокола `Pamyat` из этапа 9 — тот же async-интерфейс, что
у `PamyatVPamyati`, поэтому диспетчер о подмене не знает.

Ключ `sbavito:dialog:{аккаунт}:{чат}` — **аккаунт в ключе обязателен**: один
и тот же chat_id приходит в трёх разных ботов, и без него клиент, написавший
Saunamart и SB SAUNA, получил бы одну общую историю на двоих.

Отдельное решение — **сбой Redis не роняет диалог**. Клиенту важнее получить
ответ без памяти, чем «секунду, что-то подвисло»: моргание кеша не должно
выглядеть как поломка бота. Ошибка при этом пишется в лог целиком, молча
история не теряется.
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis

from .cache import PREFIKS
from .logger import log_oshibka

# Семь суток, а не полчаса. На Авито переписка идёт рывками: спросили утром,
# ответили вечером, вернулись через два дня. При TTL в полчаса бот встречал
# такого клиента с полной амнезией и здоровался заново — поймано на живом
# диалоге 23.07 (пауза 35 минут посреди разговора). Диалог — это 12 коротких
# реплик, места он не стоит; живой менеджер историю переписки видит всегда.
TTL_S = 7 * 24 * 60 * 60
MAKS_REPLIK = 12     # последние 12 реплик; больше модели только мешает


class PamyatRedis:
    """История диалогов всех аккаунтов в одном Redis."""

    def __init__(self, client: aioredis.Redis, *, ttl_s: int = TTL_S,
                 maks_replik: int = MAKS_REPLIK) -> None:
        self._r = client
        self._ttl = ttl_s
        self._maks = maks_replik

    @staticmethod
    def redis_klyuch(klyuch: str) -> str:
        return f"{PREFIKS}:dialog:{klyuch}"

    async def istoriya(self, klyuch: str) -> list[dict]:
        try:
            syrye = await self._r.lrange(self.redis_klyuch(klyuch), 0, -1)
        except Exception as e:  # noqa: BLE001 — см. модульную docstring
            log_oshibka(f"Redis недоступен, отвечаю без истории диалога: {e}")
            return []
        istoriya = []
        for zapis in syrye:
            try:
                istoriya.append(json.loads(zapis))
            except json.JSONDecodeError:
                log_oshibka(f"Битая запись истории в {klyuch}: {zapis[:120]!r}")
        return istoriya

    async def dopisat(self, klyuch: str, rol: str, tekst: str) -> None:
        k = self.redis_klyuch(klyuch)
        try:
            # Две реплики одной роли подряд склеиваем — правило этапа 9: часть
            # провайдеров падает на двух `user` подряд.
            posledn = await self._r.lindex(k, -1)
            if posledn:
                try:
                    z = json.loads(posledn)
                except json.JSONDecodeError:
                    z = None
                if z and z.get("role") == rol:
                    z["content"] = f"{z['content']}\n{tekst}"
                    await self._r.lset(k, -1, json.dumps(z, ensure_ascii=False))
                    await self._r.expire(k, self._ttl)
                    return
            await self._r.rpush(k, json.dumps({"role": rol, "content": tekst},
                                              ensure_ascii=False))
            await self._r.ltrim(k, -self._maks, -1)
            await self._r.expire(k, self._ttl)
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Не смог записать реплику в историю {klyuch}: {e}")

    async def ochistit(self, klyuch: str) -> None:
        try:
            await self._r.delete(self.redis_klyuch(klyuch))
        except Exception as e:  # noqa: BLE001
            log_oshibka(f"Не смог очистить историю {klyuch}: {e}")
