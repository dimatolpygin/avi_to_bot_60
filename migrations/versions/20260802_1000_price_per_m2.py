"""Колонка products.price_per_m2 — цена за м² из таблицы напрямую (курс 31.07).

Заказчик добавил в Google-таблицу отдельную колонку «цена за м2» (плоскую по
сорту), чтобы бот брал её как есть, а не высчитывал. Храним её отдельно; где
пусто — цена за м² вычисляется фолбэком на выдаче (`cena_za_metr_kvadratnyy`).

Ревизия: b2f4a1c9d7e3
Предыдущая: 7cac61ed44d2
Создана: 2026-08-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b2f4a1c9d7e3'
down_revision: Union[str, None] = '7cac61ed44d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'products',
        sa.Column('price_per_m2', sa.Numeric(precision=12, scale=2), nullable=True),
        schema='sbavito',
    )


def downgrade() -> None:
    op.drop_column('products', 'price_per_m2', schema='sbavito')
