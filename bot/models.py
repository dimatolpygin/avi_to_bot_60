# -*- coding: utf-8 -*-
"""Схема БД (SQLAlchemy 2.0). Всё живёт в отдельной схеме `sbavito`.

Дизайн перенесён по смыслу из аннулированной Drizzle-схемы этапа 1 плюс
решения по разбору прайса (см. «Неочевидное» в CLAUDE.md):

* **Наличие — явный enum**, по умолчанию `unknown`. Пустая ячейка файла ≠ «нет»
  на уровне БД: дефолт колонки нейтральный, а трактовку «пусто = нет в наличии»
  делает ETL, потому что так ведёт колонку заказчик. Позиция при этом остаётся
  в каталоге, `is_active` не сбрасывается.
* **Цена за штуку — источник правды**, цена за метр вычисляется ETL и хранится
  посчитанной: в файле клиента она сломана на 12 строках вагонки сорта А.
* **Ключ синхронизации прайса — артикул** (уникален в пределах аккаунта):
  141 артикул, дублей нет, штрихкодов у клиента вообще нет.
* **Рабочая ширина — отдельная колонка и она nullable**: она есть только там,
  где в названии стоят скобки (`15х95 (88)`). У полка скобок нет, и пересчёт
  в м² для него запрещён — пустое поле это запрещение и выражает.
* **Редкие атрибуты (печи, двери) — в JSONB `attrs`**, а не колонками: мощность
  и объём парилки есть у пяти позиций из 41, а объём вдобавок задан интервалом
  («от 14 до 18 м³») и матчится попаданием в диапазон.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, DateTime, Enum, ForeignKey, Index,
                        Integer, MetaData, Numeric, String, Text, UniqueConstraint,
                        func, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SHEMA = "sbavito"

# Явные имена ограничений и индексов: без них autogenerate придумывает имена сам,
# и `downgrade` не может снять то, что создал `upgrade`.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema=SHEMA, naming_convention=NAMING)


# ── Enum ─────────────────────────────────────────────────────────────────────

# Аккаунт продаёт товар (Saunamart) или услугу отделки (SB SAUNA, Дешман).
AccountKind = Enum("goods", "services", name="account_kind", schema=SHEMA)

# Наличие. `unknown` — строка, которой ETL не касался; «пусто в файле» он
# кладёт как `out` (правило заказчика), но дефолт колонки остаётся нейтральным.
Availability = Enum("in_stock", "out", "unknown", name="availability", schema=SHEMA)

# Единица цены. `piece` — штука (сюда же попадает упаковка, см. is_package),
# `linear_m` — метр погонный. м² сознательно нет: он считается на лету и только
# для позиций с рабочей шириной.
PriceUnit = Enum("piece", "linear_m", name="price_unit", schema=SHEMA)

# Кто написал реплику. `tool` — вызов инструмента, нужен для панели (этап 13),
# в память диалога для модели такие сообщения не кладутся.
MessageRole = Enum("client", "bot", "system", "tool", name="message_role", schema=SHEMA)

# Судьба лида: завели → отправили в amoCRM → или не смогли (amo лежал).
LeadStatus = Enum("new", "sent", "failed", name="lead_status", schema=SHEMA)


class VremennyeMetki:
    """Общие для всех таблиц отметки времени (на стороне БД, не приложения)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now())


# ── Аккаунты ─────────────────────────────────────────────────────────────────

class Account(Base, VremennyeMetki):
    """Три аккаунта Авито. На обкатке каждому соответствует свой Telegram-бот."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(AccountKind, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            server_default=text("true"))

    products: Mapped[list["Product"]] = relationship(back_populates="account")


# ── Прайс ────────────────────────────────────────────────────────────────────

class Product(Base, VremennyeMetki):
    """Позиция прайса. Одна строка файла = одна строка здесь.

    141 строка = 41 уникальный товар: вагонка и полок размножены по длинам
    1.0–3.0 м, у дверей, печей и камней длин нет вообще.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)

    # Ключ синхронизации с файлом клиента.
    article: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Атрибуты, вытащенные ETL из названия.
    family: Mapped[str | None] = mapped_column(String(64))        # вагонка, полок, дверь, печь
    grade: Mapped[str | None] = mapped_column(String(32))         # сорт: А, В, Экстра
    species: Mapped[str | None] = mapped_column(String(32))       # порода: липа, ольха, абаши
    length_m: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    # Рабочая ширина в мм — только из скобок в названии. Пусто = пересчёт в м² запрещён.
    working_width_mm: Mapped[int | None] = mapped_column(Integer)

    # Цены. price_apiece — из файла, price_per_m — вычислена (цена_шт / длина).
    unit: Mapped[str] = mapped_column(PriceUnit, nullable=False, server_default=text("'piece'"))
    price_apiece: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_per_m: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Цена за м² — с курса 31.07 берётся из отдельной колонки таблицы НАПРЯМУЮ
    # (плоская по сорту), не вычисляется. Пусто = колонки нет или не заполнена →
    # вычисляем как фолбэк в `cena_za_metr_kvadratnyy`. У полка пустая всегда:
    # рабочей ширины нет, м² бессмыслен (гейт держит поиск, не эта колонка).
    price_per_m2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Позиция продаётся упаковкой («Фольга алюминиевая 12 м2», «уп.10кг») — цену делить нельзя.
    is_package: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                             server_default=text("false"))

    availability: Mapped[str] = mapped_column(Availability, nullable=False,
                                              server_default=text("'unknown'"))
    characteristics: Mapped[str | None] = mapped_column(Text)
    # Редкие атрибуты: power_kw, volume_min_m3, volume_max_m3, door_height_mm, door_width_mm.
    attrs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            server_default=text("true"))

    account: Mapped["Account"] = relationship(back_populates="products")
    aliases: Mapped[list["ProductAlias"]] = relationship(
        back_populates="product", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("account_id", "article", name="uq_products_account_article"),
        Index("ix_products_account", "account_id"),
        Index("ix_products_family", "family"),
        # Триграммы: опечатки клиента («вогонка») ловятся до похода в словари.
        # Класс оператора задан через postgresql_ops, а не вписан в выражение:
        # иначе autogenerate не умеет сравнивать такой индекс и молча пропускает
        # его при проверке дрейфа схемы.
        Index("ix_products_name_trgm", "name", postgresql_using="gin",
              postgresql_ops={"name": "gin_trgm_ops"}),
    )


class ProductAlias(Base):
    """Народные названия позиции («полог» → полок, «евровагонка» → вагонка)."""

    __tablename__ = "product_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    product: Mapped["Product"] = relationship(back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("product_id", "alias", name="uq_product_aliases_product_alias"),
        Index("ix_product_aliases_alias_trgm", "alias", postgresql_using="gin",
              postgresql_ops={"alias": "gin_trgm_ops"}),
    )


class PriceMeta(Base):
    """Отметка о загрузке прайса: откуда, когда и сколько строк.

    Дату актуальности бот берёт отсюда, а не из захардкоженной строки —
    иначе он однажды скажет «прайс от такого-то числа» и соврёт.
    """

    __tablename__ = "price_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    source_file: Mapped[str] = mapped_column(String(512), nullable=False)
    price_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Счётчики последнего импорта — по ним видно идемпотентность (0/0/0 на повторе).
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    deactivated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_price_meta_account", "account_id"),)


# ── Промпты и знания ─────────────────────────────────────────────────────────

class PromptBase(Base, VremennyeMetki):
    """Общий промпт для всех аккаунтов: персона, правила по данным, стиль.

    Версии не перезаписываются, а добавляются: активна одна (`is_active`),
    чтобы правку из панели (этап 13) можно было откатить.
    """

    __tablename__ = "prompt_base"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            server_default=text("true"))
    updated_by: Mapped[str | None] = mapped_column(String(128))


class AccountPrompt(Base, VremennyeMetki):
    """Надстройка промпта на аккаунт: товары ≠ услуги ≠ бюджетная отделка."""

    __tablename__ = "account_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    persona: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            server_default=text("true"))
    updated_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (Index("ix_account_prompts_account", "account_id"),)


class KnowledgeBlock(Base, VremennyeMetki):
    """Блок базы знаний: гео, стоп-лист, гарантии, что входит в отделку (этап 11)."""

    __tablename__ = "knowledge_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            server_default=text("true"))

    __table_args__ = (
        UniqueConstraint("account_id", "key", name="uq_knowledge_blocks_account_key"),
    )


class Faq(Base, VremennyeMetki):
    """Готовые пары вопрос-ответ на аккаунт."""

    __tablename__ = "faq"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            server_default=text("true"))

    __table_args__ = (Index("ix_faq_account", "account_id"),)


# ── Диалоги и лиды ───────────────────────────────────────────────────────────

class Dialog(Base):
    """Переписка с одним клиентом в одном аккаунте.

    `chat_key` — id чата в канале (Telegram chat_id на обкатке, chat_id Авито
    в бою). Один и тот же клиент в разных аккаунтах = разные диалоги: истории
    не смешиваются, это отдельный критерий приёмки этапа 10.
    """

    __tablename__ = "dialogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)   # telegram | avito
    chat_key: Mapped[str] = mapped_column(String(128), nullable=False)
    client_name: Mapped[str | None] = mapped_column(String(255))
    client_username: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    messages: Mapped[list["Message"]] = relationship(
        back_populates="dialog", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("account_id", "channel", "chat_key", name="uq_dialogs_chat"),
        Index("ix_dialogs_last_message_at", "last_message_at"),
    )


class Message(Base):
    """Реплика диалога. `request_id` — тот самый сквозной id из логов:
    по нему строка в панели сшивается с логом обработки этого сообщения."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dialog_id: Mapped[int] = mapped_column(
        ForeignKey("dialogs.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(MessageRole, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(32))
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    dialog: Mapped["Dialog"] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_messages_dialog_created", "dialog_id", "created_at"),)


class Lead(Base):
    """Клиент оставил контакт. Пишется в БД ДО похода в amoCRM: падение CRM
    не должно терять лид, повторная отправка идёт по `status = failed`."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    dialog_id: Mapped[int | None] = mapped_column(
        ForeignKey("dialogs.id", ondelete="SET NULL"))
    phone: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)          # саммари диалога в amo
    status: Mapped[str] = mapped_column(LeadStatus, nullable=False,
                                        server_default=text("'new'"))
    amo_contact_id: Mapped[int | None] = mapped_column(BigInteger)
    amo_lead_id: Mapped[int | None] = mapped_column(BigInteger)
    amo_responsible_id: Mapped[int | None] = mapped_column(BigInteger)   # менеджер round-robin
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_leads_account_created", "account_id", "created_at"),
        Index("ix_leads_status", "status"),
    )
