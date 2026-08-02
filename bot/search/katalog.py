# -*- coding: utf-8 -*-
"""Каталог товаров в памяти процесса (этап 6).

Сорок один товар — это не тот объём, ради которого стоит ходить в SQL на каждое
сообщение: каталог грузится один раз при старте и живёт в памяти, поиск идёт
на Python. **Следствие, о которое легко споткнуться: после обновления прайса
живой процесс держит старый каталог до рестарта.**

Главная особенность наших данных — **141 строка прайса это 41 товар**, размноженный
по длинам: у вагонки липа сорт А двадцать одна строка, различающаяся только `L-x.x`.
Поэтому каталог хранит не плоский список, а **группы**: базовое имя (название без
длины) → позиции по возрастанию длины. Иначе выдача «вагонка липа» это пять
строк одного и того же товара, а клиенту нужно пять РАЗНЫХ товаров.

Два источника, одна и та же правда:
* `zagruzit_iz_bd()` — боевой путь, `products` аккаунта;
* `iz_fayla_praysa()` — тот же ETL-разбор прямо из выгрузки, без БД. Нужен
  для `probe` и тестов поиска: качество поиска проверяется чаще, чем поднимается
  окружение, и ждать Postgres ради одного запроса незачем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select

from ..logger import logger
from .normalize import stems

# «L-2.5 м», «L- 0.8 м» в конце названия — то, чем различаются строки одной группы.
_RE_DLINA_V_IMENI = re.compile(r"\s*L\s*-?\s*\d{1,2}[.,]\d{1,2}\s*м?", re.I)


def bazovoe_imya(name: str) -> str:
    """Название без длины — ключ группы. «Вагонка Липа сорт А 15х95 (88) L-2.5 м»
    и та же строка с L-3.0 м это ОДИН товар в двух длинах."""
    return _RE_DLINA_V_IMENI.sub("", name).strip()


@dataclass(frozen=True)
class Pozitsiya:
    """Одна строка прайса, уже разобранная ETL. Поля повторяют `products`."""

    article: str
    name: str
    family: str | None = None
    grade: str | None = None
    species: str | None = None
    length_m: Decimal | None = None
    working_width_mm: int | None = None
    unit: str = "piece"
    price_apiece: Decimal | None = None
    price_per_m: Decimal | None = None
    price_per_m2: Decimal | None = None      # из таблицы напрямую, пусто = вычислим
    is_package: bool = False
    availability: str = "unknown"
    attrs: dict = field(default_factory=dict)
    id: int | None = None

    @property
    def v_nalichii(self) -> bool:
        return self.availability == "in_stock"


@dataclass(frozen=True)
class Gruppa:
    """Товар целиком: базовое имя + все его длины.

    У немерных позиций (дверь, печь, камни) в группе ровно одна позиция —
    отдельного случая для них в поиске не нужно.
    """

    imya: str
    pozitsii: tuple[Pozitsiya, ...]        # по возрастанию длины
    stemy: frozenset[str] = field(repr=False, default=frozenset())

    @property
    def obrazec(self) -> Pozitsiya:
        """Позиция-представитель, когда длина не спрошена: самая длинная.
        По ней менеджер и называет цену («сорт А 513 р шт» — это 3 метра)."""
        return self.pozitsii[-1]

    @property
    def family(self) -> str | None:
        return self.obrazec.family

    @property
    def grade(self) -> str | None:
        return self.obrazec.grade

    @property
    def species(self) -> str | None:
        return self.obrazec.species

    @property
    def dliny(self) -> tuple[Decimal, ...]:
        return tuple(p.length_m for p in self.pozitsii if p.length_m is not None)

    def po_dline(self, dlina: Decimal | None) -> Pozitsiya | None:
        """Позиция ровно этой длины или None. Никакого «ближайшего»: длина
        подрезки — это цена, и подсунуть 2.9 вместо 3.0 значит соврать."""
        if dlina is None:
            return None
        for p in self.pozitsii:
            if p.length_m == dlina:
                return p
        return None


@dataclass(frozen=True)
class Katalog:
    """Товары аккаунта, проиндексированные под поиск."""

    gruppy: tuple[Gruppa, ...]
    po_artikulu: dict[str, Pozitsiya] = field(repr=False, default_factory=dict)
    po_semeystvu: dict[str, tuple[Gruppa, ...]] = field(repr=False, default_factory=dict)
    istochnik: str = ""

    @property
    def vsego_pozitsiy(self) -> int:
        return sum(len(g.pozitsii) for g in self.gruppy)

    def gruppa_pozitsii(self, p: Pozitsiya) -> Gruppa | None:
        imya = bazovoe_imya(p.name)
        return next((g for g in self.gruppy if g.imya == imya), None)


def sobrat(pozitsii: list[Pozitsiya], istochnik: str = "") -> Katalog:
    """Сгруппировать позиции по базовому имени и построить индексы."""
    po_imeni: dict[str, list[Pozitsiya]] = {}
    po_artikulu: dict[str, Pozitsiya] = {}
    for p in pozitsii:
        po_imeni.setdefault(bazovoe_imya(p.name), []).append(p)
        po_artikulu[p.article] = p

    gruppy = []
    for imya, spisok in po_imeni.items():
        # Без длины (немерные) — в конец, чтобы `obrazec` брал осмысленную позицию.
        spisok.sort(key=lambda p: p.length_m if p.length_m is not None else Decimal("0"))
        gruppy.append(Gruppa(imya=imya, pozitsii=tuple(spisok), stemy=frozenset(stems(imya))))
    gruppy.sort(key=lambda g: g.imya)

    po_semeystvu: dict[str, list[Gruppa]] = {}
    for g in gruppy:
        po_semeystvu.setdefault(g.family or "", []).append(g)

    katalog = Katalog(
        gruppy=tuple(gruppy),
        po_artikulu=po_artikulu,
        po_semeystvu={k: tuple(v) for k, v in po_semeystvu.items()},
        istochnik=istochnik,
    )
    logger.info("🗂  Каталог загружен: %d позиций → %d товаров, семейств %d (%s)",
                katalog.vsego_pozitsiy, len(gruppy), len(po_semeystvu), istochnik or "—")
    return katalog


def iz_polej(polya: list[dict], istochnik: str = "") -> Katalog:
    """Каталог из набора словарей с колонками `products` (выход ETL)."""
    izvestnye = {f.name for f in Pozitsiya.__dataclass_fields__.values()}
    return sobrat([Pozitsiya(**{k: v for k, v in p.items() if k in izvestnye})
                   for p in polya], istochnik)


def iz_fayla_praysa(put: str | None = None) -> Katalog:
    """Каталог прямо из выгрузки прайса, без БД — для probe и тестов поиска."""
    from ..etl.chtenie import prochitat
    from ..etl.import_prays import FAYL_PO_UMOLCHANIYU, _polya_pozicii

    put = put or FAYL_PO_UMOLCHANIYU
    prays = prochitat(put)
    return iz_polej([_polya_pozicii(s) for s in prays.stroki], f"файл {prays.istochnik}")


async def zagruzit_iz_bd(sessiya, kod_akkaunta: str) -> Katalog:
    """Каталог аккаунта из `products`. Погашенные позиции (`is_active = false`)
    в поиск не идут: они остались в БД, чтобы не терять историю, но продавать
    их нельзя."""
    from ..models import Account, Product

    akkaunt_id = await sessiya.scalar(select(Account.id).where(Account.code == kod_akkaunta))
    if not akkaunt_id:
        raise LookupError(f"❌ В БД нет аккаунта «{kod_akkaunta}» — засей: python -m bot.seed")

    stroki = (await sessiya.scalars(
        select(Product)
        .where(Product.account_id == akkaunt_id, Product.is_active.is_(True))
        .order_by(Product.name, Product.length_m))).all()

    return sobrat([Pozitsiya(
        id=p.id, article=p.article, name=p.name, family=p.family, grade=p.grade,
        species=p.species, length_m=p.length_m, working_width_mm=p.working_width_mm,
        unit=p.unit, price_apiece=p.price_apiece, price_per_m=p.price_per_m,
        price_per_m2=p.price_per_m2,
        is_package=p.is_package, availability=p.availability, attrs=dict(p.attrs or {}),
    ) for p in stroki], f"БД, аккаунт {kod_akkaunta}")
