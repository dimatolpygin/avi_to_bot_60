# -*- coding: utf-8 -*-
"""Окружение Alembic: async-движок, своя схема, чужое не трогаем.

Три вещи, без которых миграции в общей БД опасны:

1. `version_table_schema=sbavito` — таблица версий лежит в нашей схеме, а не
   в `public`, где живут соседние проекты общего контейнера `postgres16`.
2. `include_schemas=True` — иначе autogenerate не видит объекты вне
   `search_path` и на каждый прогон предлагает создать всё заново.
3. `include_object` — обратная сторона пункта 2: увидев чужие схемы, автоген
   предложил бы их УДАЛИТЬ (их же нет в наших моделях). Фильтр пропускает
   только объекты схемы `sbavito`.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import async_engine_from_config

from bot.config import load_config
from bot.models import SHEMA, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Пароль в alembic.ini не хранится — подставляем DSN из .env на лету.
config.set_main_option("sqlalchemy.url", load_config().pg.dsn)


def _nashe(object, name, type_, reflected, compare_to) -> bool:  # noqa: A002
    """Пропускать только объекты схемы sbavito.

    Сравниваем строго по имени схемы: у наших моделей она всегда проставлена
    (`MetaData(schema="sbavito")`), а у отражённых из БД чужих таблиц `public`
    схема приходит как `None` — это схема по умолчанию у соединения. Считать
    такой `None` «нашим» нельзя: автоген тут же предложит удалить чужие таблицы
    соседних проектов, которых нет в наших моделях.
    """
    shema = getattr(object, "schema", None)
    if shema is None:
        tablitsa = getattr(object, "table", None)
        if tablitsa is not None:
            shema = getattr(tablitsa, "schema", None)
    return shema == SHEMA


_OBSHCHEE = dict(
    target_metadata=target_metadata,
    version_table_schema=SHEMA,
    include_schemas=True,
    include_object=_nashe,
    compare_type=True,           # смена типа колонки должна попадать в миграцию
    compare_server_default=True,
)


def offline() -> None:
    """`alembic upgrade --sql`: печатаем SQL, к БД не подключаемся."""
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      literal_binds=True, dialect_opts={"paramstyle": "named"},
                      **_OBSHCHEE)
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    # Схему создаём здесь, а не в первой миграции: таблицу версий Alembic
    # заводит в ней ДО того, как выполнит хоть одну ревизию.
    connection.execute(sa_text(f'create schema if not exists "{SHEMA}"'))
    context.configure(connection=connection, **_OBSHCHEE)
    with context.begin_transaction():
        context.run_migrations()


async def online_async() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_migrate)
        # Коммит обязателен. В SQLAlchemy 2.0 соединение работает в режиме
        # «commit as you go», а наш `create schema` открывает транзакцию раньше,
        # чем Alembic свою — из-за этого его собственный commit становится
        # холостым, и на выходе всё откатывается. Симптом коварный: в логе
        # честное «Running upgrade», а в базе пусто.
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    offline()
else:
    asyncio.run(online_async())
