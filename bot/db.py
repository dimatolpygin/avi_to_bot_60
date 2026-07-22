# -*- coding: utf-8 -*-
"""Подключение к Postgres (SQLAlchemy 2.0 async + asyncpg).

БД — существующий контейнер `postgres16` (:5432), своя БД `sbavito` и своя
схема `sbavito`: образ общий с соседними проектами, менять его нельзя.
Модели и миграции появятся на этапе 3, здесь — только движок, фабрика сессий
и проверка живости соединения при старте.
"""
import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy import text

from .config import Config
from .logger import logger

# Старт приложения и старт БД могут разъехаться (compose, ребут сервера) —
# несколько попыток с паузой вместо мгновенного падения.
_POPYTOK = 5
_PAUZA_S = 2.0


def sozdat_engine(cfg: Config) -> AsyncEngine:
    """Движок с пулом. `search_path` сразу на нашу схему — код пишет запросы
    без префикса `sbavito.`, а в чужие схемы общей БД не лезет."""
    return create_async_engine(
        cfg.pg.dsn,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,   # соединение могло протухнуть за ночь простоя
        echo=False,
        connect_args={"server_settings": {"search_path": f"{cfg.pg.schema},public"}},
    )


def sozdat_fabriku_sessiy(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def podklyuchit(cfg: Config) -> AsyncEngine:
    """Поднять движок и убедиться, что БД реально отвечает. Логирует версию
    сервера и наличие схемы. Не достучались за все попытки → падаем с русским
    объяснением: без БД боту делать нечего."""
    engine = sozdat_engine(cfg)
    posledn: Exception | None = None
    for popytka in range(1, _POPYTOK + 1):
        try:
            async with engine.connect() as conn:
                versiya = (await conn.execute(text("select version()"))).scalar_one()
                est_shema = (await conn.execute(
                    text("select exists(select 1 from information_schema.schemata "
                         "where schema_name = :s)"),
                    {"s": cfg.pg.schema},
                )).scalar_one()
            logger.info("🗄  Postgres подключён: %s", cfg.pg.bez_parolya())
            logger.info("🗄  Версия сервера: %s", str(versiya).split(" on ")[0])
            if est_shema:
                logger.info("🗄  Схема «%s» на месте", cfg.pg.schema)
            else:
                logger.warning("🗄  Схемы «%s» ещё нет — её создаст Alembic на этапе 3",
                               cfg.pg.schema)
            return engine
        except Exception as e:  # noqa: BLE001 — причина уходит в лог целиком
            posledn = e
            logger.warning("🗄  Postgres не отвечает (попытка %d из %d): %s",
                           popytka, _POPYTOK, e)
            if popytka < _POPYTOK:
                await asyncio.sleep(_PAUZA_S)
    await engine.dispose()
    raise SystemExit(
        f"❌ Не удалось подключиться к Postgres {cfg.pg.bez_parolya()} "
        f"за {_POPYTOK} попыток: {posledn}\n"
        f"   Проверь, что контейнер postgres16 поднят и переменные PG* в .env верные."
    )


async def zakryt(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()
        logger.info("🗄  Соединения с Postgres закрыты")
