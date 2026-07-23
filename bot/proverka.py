# -*- coding: utf-8 -*-
"""Самопроверка каркаса: `python -m bot.proverka`.

Показывает руками то, что обещают критерии приёмки этапа 2:
  1. конфиг читается, обязательные переменные на месте;
  2. Postgres и Redis реально отвечают (не только ping — в Redis пишем и читаем);
  3. лог одного «входящего сообщения» сшит одним request-id по всей цепочке
     👤 → 🔧 → 🧠 → 🤖, а у следующего сообщения id уже другой;
  4. при `LOG_LEVEL=debug` в цепочке появляются промпт и ответ модели,
     на `info` — не появляются;
  5. словари домена (этап 5) читаются с диска и резолвят живые формулировки.
Диалога и ИИ на этом этапе ещё нет, поэтому цепочка — демонстрационная,
с теми же помощниками логирования, которыми будет пользоваться боевое ядро.
"""
import asyncio

from sqlalchemy import text

from .config import load_config
from .logger import (DEBUG_LOGI, log_ishodyashchee, log_tool_call, log_vhodyashchee,
                     log_vyzov_ii, logger, nachat_zapros)
from .search.zapros import razobrat_zapros
from . import cache, db


async def _demo_dialog(akkaunt: str, vopros: str) -> str:
    """Одно «входящее сообщение» целиком — весь его лог несёт общий rid."""
    with nachat_zapros(akkaunt) as rid:
        log_vhodyashchee("test_klient", 111222333, "Проверка", vopros)
        log_tool_call("search_products", {"query": vopros, "assortiment": akkaunt},
                      "найдено: 0 (каталога ещё нет, этап 4)")
        log_vyzov_ii(
            "anthropic/claude-haiku-4.5", 0,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            messages=[{"role": "user", "content": vopros}],
            otvet="(демо-ответ, ИИ подключается на этапе 8)",
        )
        log_ishodyashchee("test_klient", "демо-ответ каркаса", ms=0)
        return rid


async def _proverit_shemu(engine, shema: str) -> None:
    """Схема поднята миграциями и засеяна: показываем ревизию, число таблиц
    и аккаунты. Пусто → значит забыли `alembic upgrade head` или `bot.seed`."""
    async with engine.connect() as conn:
        revizia = (await conn.execute(text(
            f'select version_num from "{shema}".alembic_version'))).scalar()
        tablits = (await conn.execute(text(
            "select count(*) from information_schema.tables where table_schema = :s"),
            {"s": shema})).scalar_one()
        akkaunty = (await conn.execute(text(
            f'select code, kind from "{shema}".accounts order by id'))).all()
    logger.info("🗄  Ревизия схемы: %s · таблиц: %s", revizia, tablits)
    logger.info("🗄  Аккаунты: %s",
                ", ".join(f"{c} ({k})" for c, k in akkaunty) or "нет — запусти python -m bot.seed")


def _proverit_slovari() -> None:
    """Словари домена читаются и резолвят живые формулировки (этап 5).
    БД для этого не нужна — разбор запроса детерминирован и работает офлайн."""
    for vopros in ("вагонка сорт б", "трёхметровая липа", "болгарка"):
        logger.info("📚 %r → %s", vopros, razobrat_zapros(vopros).kratko())


async def main() -> None:
    logger.info("🔎 Самопроверка каркаса")
    cfg = load_config()
    logger.info("⚙️  Конфиг прочитан: БД %s, схема «%s», модель %s",
                cfg.pg.bez_parolya(), cfg.pg.schema, cfg.openrouter.model)
    _proverit_slovari()

    engine = await db.podklyuchit(cfg)
    redis_client = await cache.podklyuchit(cfg)
    try:
        await _proverit_shemu(engine, cfg.pg.schema)

        klyuch = f"{cache.PREFIKS}:proverka"
        await redis_client.set(klyuch, "живой", ex=60)
        znachenie = await redis_client.get(klyuch)
        await redis_client.delete(klyuch)
        logger.info("🧰 Запись/чтение ключа %s: %r", klyuch, znachenie)

        rid1 = await _demo_dialog("saunamart", "почём липа 3 метра")
        rid2 = await _demo_dialog("sbsauna", "сколько стоит отделка парной")
        if rid1 == rid2:
            raise SystemExit("❌ Разные сообщения получили одинаковый request-id")
        logger.info("🔎 Сквозной id: у первого сообщения %s, у второго %s — не смешались",
                    rid1, rid2)
        logger.info("🔎 Промпт и ответ модели в логе: %s (LOG_LEVEL=%s)",
                    "да" if DEBUG_LOGI else "нет", cfg.log_level)
        logger.info("✅ Самопроверка пройдена")
    finally:
        await cache.zakryt(redis_client)
        await db.zakryt(engine)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
