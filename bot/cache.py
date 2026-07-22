# -*- coding: utf-8 -*-
"""Подключение к Redis (существующий контейнер `redis`, :6379).

Redis держит память диалога (`sbavito:dialog:{аккаунт}:{chat}`), дебаунс
входящих реплик и очередь round-robin по менеджерам. Здесь — только клиент
и проверка живости при старте; сами структуры заводят этапы 9-12.
"""
import asyncio

import redis.asyncio as aioredis

from .config import Config
from .logger import logger

_POPYTOK = 5
_PAUZA_S = 2.0

# Общий префикс ключей: БД Redis делится с соседними проектами.
PREFIKS = "sbavito"


async def podklyuchit(cfg: Config) -> aioredis.Redis:
    """Поднять клиент и убедиться, что Redis отвечает на ping."""
    client = aioredis.from_url(cfg.redis_url, decode_responses=True)
    posledn: Exception | None = None
    for popytka in range(1, _POPYTOK + 1):
        try:
            await client.ping()
            info = await client.info("server")
            logger.info("🧰 Redis подключён: %s", _bez_parolya(cfg.redis_url))
            logger.info("🧰 Версия сервера: %s · префикс ключей «%s:»",
                        info.get("redis_version", "?"), PREFIKS)
            return client
        except Exception as e:  # noqa: BLE001
            posledn = e
            logger.warning("🧰 Redis не отвечает (попытка %d из %d): %s",
                           popytka, _POPYTOK, e)
            if popytka < _POPYTOK:
                await asyncio.sleep(_PAUZA_S)
    await client.aclose()
    raise SystemExit(
        f"❌ Не удалось подключиться к Redis {_bez_parolya(cfg.redis_url)} "
        f"за {_POPYTOK} попыток: {posledn}\n"
        f"   Проверь, что контейнер redis поднят и REDIS_URL в .env верный."
    )


async def zakryt(client: aioredis.Redis | None) -> None:
    if client is not None:
        await client.aclose()
        logger.info("🧰 Соединение с Redis закрыто")


def _bez_parolya(url: str) -> str:
    """redis://user:pass@host:6379/3 → redis://user@host:6379/3 (для лога)."""
    if "@" not in url:
        return url
    shema, hvost = url.split("://", 1)
    kreds, adres = hvost.rsplit("@", 1)
    user = kreds.split(":", 1)[0]
    return f"{shema}://{user}@{adres}" if user else f"{shema}://{adres}"
