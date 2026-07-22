# -*- coding: utf-8 -*-
"""Тесты схемы (этап 3): проверяем сами модели, БД не нужна.

Живая проверка миграций — `alembic upgrade head` / `downgrade -1` и
`python -m bot.proverka`; здесь ловится то, что легко сломать правкой моделей
и трудно заметить глазами.
"""
from bot.models import SHEMA, Base, Dialog, Product


def test_vse_tablitsy_v_svoey_sheme():
    """Ни одна таблица не должна уехать в public — там соседние проекты."""
    chuzhie = [t.fullname for t in Base.metadata.sorted_tables if t.schema != SHEMA]
    assert chuzhie == []


def test_nabor_tablits():
    imena = {t.name for t in Base.metadata.sorted_tables}
    assert imena == {
        "accounts", "products", "product_aliases", "price_meta",
        "prompt_base", "account_prompts", "knowledge_blocks", "faq",
        "dialogs", "messages", "leads",
    }


def test_nalichie_po_umolchaniyu_neizvestno():
    """Пустой остаток не должен превращаться в «нет» на уровне БД:
    дефолт нейтральный, трактовку делает ETL этапа 4."""
    kolonka = Product.__table__.c.availability
    assert kolonka.server_default.arg.text == "'unknown'"
    assert kolonka.nullable is False


def test_pozitsiya_ne_propadaet_iz_kataloga():
    """`is_active` по умолчанию true: снятая с наличия позиция остаётся
    в каталоге, иначе бот скажет «такого не бывает» вместо «сейчас нет»."""
    assert Product.__table__.c.is_active.server_default.arg.text == "true"


def test_artikul_klyuch_sinhronizatsii():
    """Артикул уникален в пределах аккаунта — на нём держится идемпотентный
    upsert прайса (этап 4)."""
    uq = [c for c in Product.__table__.constraints
          if c.__class__.__name__ == "UniqueConstraint"]
    kolonki = [tuple(col.name for col in c.columns) for c in uq]
    assert ("account_id", "article") in kolonki


def test_rabochaya_shirina_mozhet_byt_pustoy():
    """У полка скобок в названии нет и рабочей ширины не существует —
    колонка обязана допускать NULL, иначе ETL придумает число."""
    assert Product.__table__.c.working_width_mm.nullable is True


def test_dialogi_ne_smeshivayutsya_mezhdu_akkauntami():
    """Один и тот же chat_id в разных ботах — разные диалоги (критерий этапа 10)."""
    uq = [c for c in Dialog.__table__.constraints
          if c.__class__.__name__ == "UniqueConstraint"]
    kolonki = [tuple(col.name for col in c.columns) for c in uq]
    assert ("account_id", "channel", "chat_key") in kolonki


def test_trigrammnye_indeksy_na_meste():
    """Опечатки клиента ловятся триграммами по названию и синониму."""
    imena = {i.name for i in Product.__table__.indexes} | \
            {i.name for i in Base.metadata.tables[f"{SHEMA}.product_aliases"].indexes}
    assert "ix_products_name_trgm" in imena
    assert "ix_product_aliases_alias_trgm" in imena
