# -*- coding: utf-8 -*-
"""Лексический поиск по каталогу (этап 6). Без ИИ — чистый детерминизм.

Порт скоринга `ball()` из telewin (эталон 92% top-1 на 12 000 позиций),
пересаженный на нашу предметку: там различали сверло по бетону и по металлу,
здесь — сорт вагонки, породу, длину подрезки, объём парной.

Каналы, в порядке срабатывания:

1. **чужой домен** — «болгарка» → пусто ДО поиска. Иначе общее слово цепляется
   за каталог и выдаёт мусор; в telewin этот гейт поднял абстейн с 34% до 91%.
2. **артикул** — шесть цифр, короткое замыкание на позицию.
3. **семейство** — резолв словарём (точно / синоним / опечатка), скоринг внутри.
4. **порода** — родовой канал: «липа 3 метра» семейства не называет, но порода
   и длина заданы. Кандидаты — все товары этой породы.
5. **название** — клиент назвал модель или марку («тундра 16», «saltway»).
   Словарь про модели ничего не знает и знать не должен: они меняются
   с поставкой, а название позиции и так лежит в каталоге.
6. **не найдено** — честная пустая выдача.

Отсечки, из-за которых этот файл вообще существует в таком виде:

* **Ничего не найдено → пусто, без «похожего наугад».** Кейс из живого диалога:
  клиент просил «штиль», менеджер искал двадцать минут и ответил «нет».
  Выдать вместо штиля обычную вагонку — это баг старого бота, а не забота.
* **Длина не подбирается «ближайшей».** Длина это цена; подсунуть 2.9 вместо
  3.0 значит соврать. Не совпала — показываем товар и говорим, какие длины есть.
* **Выдача — товары, а не строки прайса.** 141 строка это 41 товар, размноженный
  по длинам (см. `katalog.py`): без группировки топ-5 на «вагонка липа» — это
  пять длин одной и той же вагонки.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..logger import logger
from .katalog import Gruppa, Katalog, Pozitsiya
from .normalize import soft_has
from .slovari import Slovari, slovari
from .zapros import RazobranyZapros, razobrat_zapros

# ── Веса. Меняются только вместе с прогоном baseline (этап 7) ────────────────

VES_SEMEYSTVA = 3.0          # семейство названо/резолвлено словарём
VES_RODOVOGO = 3.0           # семейство «по умолчанию» в родовом канале
VES_NAZVANIYA = 3.0          # слово из названия товара («тундра», «жадеит»)
VES_ATRIBUT_SOVPAL = 4.0     # сорт, порода, длина, габарит, объём
# Названный атрибут — жёсткий фильтр, а не пожелание: несовпавшее выбрасывается
# из выдачи совсем. «Спросили сорт Б — показали А» (371 против 513 ₽) и
# «спросили 1900х700 — показали 2000х800» это ровно тот баг старого бота,
# из-за которого его и меняют.
#
# Раньше здесь стоял штраф −4.0 при пороге 2.0, и предполагалось, что этого
# хватит. Baseline этапа 7 показал, что нет: у «вагонка липа сорт а» позиция
# сорта В получала −4.0 за сорт, но +4.0 за совпавшую породу и +1.2 за каждый
# общий стем — и всплывала обратно. Штраф гасит только тогда, когда других
# сигналов мало; отбор атрибутом обязан работать всегда, поэтому он больше
# не балл, а фильтр.
#
# Длина — единственное мягкое исключение: соседняя длина это ТОТ ЖЕ товар,
# и честнее показать его с пометкой «спрошенной длины нет», чем промолчать.
SHTRAF_DLINA = 1.0
VES_BLIZOSTI_OBEMA = 2.0     # печь «на 16» ближе к Тундре-16, чем к Легиону 6–16
VES_UTOCHNYAYUSHCHEGO = 2.5  # слово, различающее товары внутри семейства
VES_PERESECHENIYA = 1.2      # каждый общий стем запроса и названия
VES_NALICHIYA = 0.3          # при прочих равных — то, что есть на складе

# Ниже этого балла выдача считается шумом. Семейство даёт 3.0, поэтому порог
# 2.0 пропускает «нашли семейство» и отсекает случайное пересечение слов.
PORG_BALLA = 2.0

# Слово различает товары внутри кандидатов, если встречается меньше чем
# у половины из них: «вагонка» есть у всех вагонок и не различает ничего,
# «матовая» — у части дверей, и это именно то, что клиент уточнил.
DOLYA_UTOCHNYAYUSHCHEGO = 0.5

# Канал названия принимает только содержательные слова: на трёх буквах в имя
# товара попадает что угодно («дт», «мм», «шт»).
MIN_DLINA_SLOVA_NAZVANIYA = 4


@dataclass(frozen=True)
class Nahodka:
    """Товар в выдаче: группа целиком плюс позиция, которую спросили."""

    gruppa: Gruppa
    ball: float
    pozitsiya: Pozitsiya          # совпавшая по длине либо образец группы
    dlina_sovpala: bool = True    # False = длину спросили, но такой нет

    @property
    def name(self) -> str:
        return self.pozitsiya.name

    def kratko(self) -> str:
        p = self.pozitsiya
        cena = f"{p.price_apiece:.0f} ₽/шт" if p.price_apiece is not None else "цена уточняется"
        if p.price_per_m is not None:
            cena += f" · {p.price_per_m:.0f} ₽/м"
        hvost = ""
        dliny = self.gruppa.dliny
        if len(dliny) > 1:
            hvost = f" · длины {min(dliny)}–{max(dliny)} ({len(dliny)} шт)"
        if not self.dlina_sovpala:
            hvost += " · спрошенной длины нет"
        return f"{p.name} — {cena}{hvost}"


class Poisk:
    """Поиск по каталогу одного аккаунта. Каталог держится в памяти."""

    def __init__(self, katalog: Katalog, sl: Slovari | None = None):
        self.katalog = katalog
        self.sl = sl or slovari()
        # Семейства, которыми клиент отвечает на родовой запрос («липа 3 метра»).
        self.rodovye_po_umolchaniyu = {
            k for k, s in self.sl.semeystva.items() if s.rodovoe_po_umolchaniyu}

    # ── Публичный вход ───────────────────────────────────────────────────────

    def iskat(self, tekst: str, top: int = 5) -> tuple[list[Nahodka], str]:
        """Найти товары по фразе клиента → (находки, канал).

        Пустой список — это ответ, а не ошибка: «мы этим не занимаемся»
        (чужой домен) или «такого нет» (не найдено). Канал различает их,
        и ИИ-слой на этапе 8 отвечает по-разному.
        """
        z = razobrat_zapros(tekst, self.sl)

        if z.chuzhoy_domen:
            logger.info("🔎 «%s» → чужой домен (%s), поиск не запускаю", tekst, z.chuzhoy_domen)
            return [], "чужой домен"

        if z.artikul:
            poz = self.katalog.po_artikulu.get(z.artikul)
            if poz:
                gruppa = self.katalog.gruppa_pozitsii(poz)
                nahodka = Nahodka(gruppa=gruppa, ball=100.0, pozitsiya=poz)
                logger.info("🔎 «%s» → канал артикул: %s", tekst, poz.name)
                return [nahodka], "артикул"

        kandidaty, kanal = self._kandidaty(z)
        if not kandidaty:
            logger.info("🔎 «%s» → не найдено (разбор: %s)", tekst, z.kratko())
            return [], "не найдено"

        utochnyayushchie = self._utochnyayushchie(z, kandidaty)
        ocenennye = [self._ocenit(g, z, kanal, utochnyayushchie) for g in kandidaty]
        ocenennye = [n for n in ocenennye if n is not None and n.ball >= PORG_BALLA]
        ocenennye.sort(key=lambda n: -n.ball)

        if not ocenennye:
            logger.info("🔎 «%s» → не найдено (все кандидаты ниже порога %.1f)", tekst, PORG_BALLA)
            return [], "не найдено"

        itog = ocenennye[:top]
        logger.info("🔎 «%s» → канал %s, найдено %d: %s", tekst, kanal, len(itog),
                    "; ".join(f"{n.pozitsiya.name} ({n.ball:.1f})" for n in itog))
        return itog, kanal

    # ── Отбор кандидатов ─────────────────────────────────────────────────────

    def _kandidaty(self, z: RazobranyZapros) -> tuple[list[Gruppa], str]:
        if z.semeystvo:
            gruppy = list(self.katalog.po_semeystvu.get(z.semeystvo, ()))
            if gruppy:
                return gruppy, z.kanal_semeystva or "семейство"
        # Родовой канал: семейство не названо, но названа порода («липа 3 метра»).
        # Клиент так говорит постоянно — порода для него и есть товар.
        if z.poroda:
            gruppy = [g for g in self.katalog.gruppy if g.species == z.poroda]
            if gruppy:
                return gruppy, "порода"
        # Канал названия: клиент назвал модель или марку, а не тип товара —
        # «тундра 16», «жадеит», «saltway». Словарь про такое ничего не знает
        # и знать не должен: модели меняются с каждой поставкой, а название
        # позиции уже лежит в каталоге.
        gruppy = [g for g in self.katalog.gruppy
                  if any(len(w) >= MIN_DLINA_SLOVA_NAZVANIYA and w in g.stemy
                         for w in z.stemy)]
        if gruppy:
            return gruppy, "название"
        return [], "не найдено"

    def _utochnyayushchie(self, z: RazobranyZapros, kandidaty: list[Gruppa]) -> set[str]:
        """Стемы запроса, различающие товары внутри отобранных кандидатов."""
        if not kandidaty:
            return set()
        razlichayushchie = set()
        for w in z.stemy:
            if w[:1].isdigit():
                continue
            skolko = sum(1 for g in kandidaty if soft_has(set(g.stemy), w))
            if 0 < skolko / len(kandidaty) < DOLYA_UTOCHNYAYUSHCHEGO:
                razlichayushchie.add(w)
        return razlichayushchie

    # ── Скоринг ──────────────────────────────────────────────────────────────

    def _ocenit(self, g: Gruppa, z: RazobranyZapros, kanal: str,
                utochnyayushchie: set[str]) -> Nahodka | None:
        """Балл товара или None, если названный клиентом атрибут не совпал.

        None — это не «ноль баллов», а «в выдачу не идёт ни при каких условиях»:
        см. комментарий у SHTRAF_DLINA о том, почему отбор атрибутом сделан
        фильтром, а не штрафом.
        """
        s = 0.0
        obrazec = g.obrazec

        if kanal in ("точно", "синоним", "опечатка", "семейство"):
            s += VES_SEMEYSTVA
        elif kanal == "порода" and g.family in self.rodovye_po_umolchaniyu:
            # «липа 3 метра» — это вагонка: полок клиент называет полком.
            s += VES_RODOVOGO
        elif kanal == "название":
            s += VES_NAZVANIYA

        if z.sort:
            if g.grade != z.sort:
                return None
            s += VES_ATRIBUT_SOVPAL
        if z.poroda:
            if g.species != z.poroda:
                return None
            s += VES_ATRIBUT_SOVPAL

        pozitsiya, dlina_sovpala = obrazec, True
        if z.dlina_m is not None:
            nayden = g.po_dline(z.dlina_m)
            if nayden is not None:
                s += VES_ATRIBUT_SOVPAL
                pozitsiya = nayden
            else:
                s -= SHTRAF_DLINA
                dlina_sovpala = False

        if z.dver_vysota_mm:
            ball = self._ball_dveri(obrazec, z)
            if ball is None:
                return None
            s += ball
        if z.obem_m3:
            ball = self._ball_obema(obrazec, z.obem_m3)
            if ball is None:
                return None
            s += ball
        if z.moshchnost_kvt:
            if obrazec.attrs.get("moshchnost_kvt") != z.moshchnost_kvt:
                return None
            s += VES_ATRIBUT_SOVPAL

        for w in utochnyayushchie:
            if soft_has(set(g.stemy), w):
                s += VES_UTOCHNYAYUSHCHEGO

        s += VES_PERESECHENIYA * len(z.stemy & g.stemy)
        if pozitsiya.v_nalichii:
            s += VES_NALICHIYA

        return Nahodka(gruppa=g, ball=s, pozitsiya=pozitsiya, dlina_sovpala=dlina_sovpala)

    def _ball_dveri(self, p: Pozitsiya, z: RazobranyZapros) -> float | None:
        """Габарит двери; None — размер не тот, позиция выбывает.

        Высота без ширины («дверь два метра») — половина сигнала: 2000 отсекает
        1900-е, но между 700 и 800 не выбирает."""
        vysota = p.attrs.get("dver_vysota_mm")
        shirina = p.attrs.get("dver_shirina_mm")
        if vysota is None:
            return 0.0
        if vysota != z.dver_vysota_mm:
            return None
        if z.dver_shirina_mm is None:
            return VES_ATRIBUT_SOVPAL / 2
        return VES_ATRIBUT_SOVPAL if shirina == z.dver_shirina_mm else None

    def _ball_obema(self, p: Pozitsiya, nuzhno: int) -> float | None:
        """Объём парной задан ИНТЕРВАЛОМ («от 14 до 18 м³»), поэтому матчим
        попаданием в диапазон, а не равенством — это единственное место, где
        перенос весов из telewin заведомо не работал бы как есть.
        Не попали в интервал — None, позиция выбывает.

        Плюс близость к середине диапазона: под «16 кубов» и Тундра-16 (ровно 16),
        и Легион 6–16 формально подходят, но клиент имел в виду первую.
        """
        omin = p.attrs.get("obem_min_m3")
        omax = p.attrs.get("obem_max_m3")
        if omin is None and omax is None:
            return 0.0
        nizhe = omin if omin is not None else omax
        vyshe = omax if omax is not None else omin
        if not (nizhe <= nuzhno <= vyshe):
            return None
        centr = (nizhe + vyshe) / 2
        blizost = max(0.0, 1.0 - abs(centr - nuzhno) / 10)
        return VES_ATRIBUT_SOVPAL + VES_BLIZOSTI_OBEMA * blizost


def cena_za_metr_kvadratnyy(p: Pozitsiya) -> Decimal | None:
    """Цена за м² — ТОЛЬКО у вагонки (там, где из скобок в названии взята рабочая
    ширина). У полка (`26х90`) скобок нет: полок кладут с зазорами, рабочей ширины
    у него не существует, и пересчёт в м² для него бессмыслен. Поэтому гейт стоит
    на рабочей ширине — пустое поле здесь запрещает пересчёт. Гейт держится даже
    если в таблице у полка случайно проставлена цена за м² (в живой таблице такое
    есть у одной строки из 45) — правило «у полка м² не бывает» сильнее опечатки.

    Дальше два источника, в порядке приоритета:
    1. **Колонка «цена за м2» из таблицы** (курс 31.07, решение Виктора «чтобы бот
       не высчитывал»). Она плоская по сорту и лежит в `price_per_m2`. Заполнена —
       берём как есть.
    2. **Фолбэк-вычисление**, где колонка пуста: цену за метр делим на ОБЩУЮ ширину
       доски (`shirina_mm`, у липы 95 мм), а не на рабочую (88) — решение заказчика
       28.07 «считаем по общей ширине». Общая ширина в attrs; нет её — фолбэк
       на рабочую, чтобы расчёт не отвалился совсем.
    """
    if p.working_width_mm is None:
        return None
    if p.price_per_m2 is not None:
        return p.price_per_m2
    if p.price_per_m is None:
        return None
    shirina = p.attrs.get("shirina_mm") or p.working_width_mm
    metrov_v_kvadrate = Decimal(1000) / Decimal(shirina)
    return (p.price_per_m * metrov_v_kvadrate).quantize(Decimal("0.01"))
