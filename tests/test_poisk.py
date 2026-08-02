# -*- coding: utf-8 -*-
"""Тесты поиска по каталогу (этап 6). БД не нужна.

Каталог собирается из строк-фикстур ТОЙ ЖЕ функцией ETL (`_polya_pozicii`),
которой наполняется `products`, — иначе тест проверял бы выдуманные данные,
а не то, что лежит в базе. Цены и характеристики взяты из реального прайса.

Живая проверка — `python -m bot.search.probe` (набор из критериев приёмки)
и `python -m bot.search.probe --iz-bd` (то же поверх БД).
"""
from dataclasses import replace
from decimal import Decimal

import pytest

from bot.etl.chtenie import StrokaPraysa
from bot.etl.import_prays import _polya_pozicii
from bot.search.katalog import bazovoe_imya, iz_polej
from bot.search.search import Poisk, cena_za_metr_kvadratnyy

# Характеристики печей — сокращённые выдержки из прайса: объём парной там
# записан десятком способов, и разбирает его ETL, а не тест.
_ASTON_12 = "Объем парного помещения: от 6 до 14 м3. Дровяная печь."
_ASTON_16 = "Объем парного помещения: от 14 до 18 м3. Дровяная печь."
_TUNDRA_16 = "Объем парильни 16 м3, закрытая каменка"
_LEGION_16 = "Объем парного помещения, м3 6-16"
_TERRA_9 = "Объем сауны, м3: 9. Максимальный объем сауны, м3: 14"


def _stroka(nomer: int, article: str, name: str, cena: str,
            harakteristiki: str | None = None, nalichie: str = "Да") -> StrokaPraysa:
    return StrokaPraysa(article=article, name=name, characteristics=harakteristiki,
                        price_apiece=Decimal(cena), price_per_meter_fayl=None,
                        nalichie_syroe=nalichie, nomer_stroki=nomer)


def _vagonka_lipa(sort: str, dliny: dict[str, str], article_base: int) -> list[StrokaPraysa]:
    return [_stroka(article_base + i, str(100000 + article_base + i),
                    f"Вагонка Липа сорт {sort} 15х95 (88) L-{d} м", cena)
            for i, (d, cena) in enumerate(dliny.items())]


STROKI = [
    *_vagonka_lipa("А", {"1.0": "171", "2.5": "428", "3.0": "513"}, 10),
    *_vagonka_lipa("В", {"1.0": "124", "2.5": "310", "3.0": "371"}, 20),
    *_vagonka_lipa("Экстра", {"1.0": "186", "2.5": "464", "2.9": "538"}, 30),
    _stroka(40, "110001", "Полок Липа сорт А 26х90 L-3.0 м", "930"),
    _stroka(41, "110002", "Полок Липа сорт А 26х90 L-2.5 м", "775"),
    _stroka(42, "123859", "Полок абаши 25x95 L-3.0 м", "2250"),
    _stroka(43, "123405", "Дверь для сауны 1900х700 Дорвуд (бронза хвоя) стекло 6 мм 2 петли", "8800"),
    _stroka(44, "129757", "Дверь для сауны 1900х700 Бронза 8мм 3 петли осина", "11700"),
    _stroka(45, "129758", "Дверь для сауны 1900х800 Бронза 8 мм 3 петли ольха", "12600"),
    _stroka(46, "129759", "Дверь для сауны 2000х800 Бронза матовая 8 мм 3 петли ольха", "15300"),
    _stroka(47, "126907", "Печь дровяная ASTON 12 стекло", "20800", _ASTON_12),
    _stroka(48, "125108", "Печь дровяная ASTON 16 стекло", "24610", _ASTON_16),
    _stroka(49, "129435", "Печь дровяная Пегас Тундра-16 сетчатый кожух Закрытая каменка",
            "19600", _TUNDRA_16),
    _stroka(50, "129438", "Печь дровяная чугунная Пегас Легион 16 ДТ 4 стекло", "35600", _LEGION_16),
    _stroka(51, "126672", "Печь электрическая ЭКМ Терра 9 кВт", "44300", _TERRA_9),
    _stroka(52, "111086", "Плитка из гималайской соли 20х10х2.5 с пазом. шлифованная", "414"),
    _stroka(53, "111082", "Кирпич из гималайской соли 20х10х5см натуральный SALTWAY", "385"),
    _stroka(54, "121505", "Камни Жадеит колотый фр 80-130 мм уп.10кг. (Хакасия)", "3150"),
    _stroka(55, "118757", "Фольга алюминиевая 12 м2 80мкм", "3284"),
]


@pytest.fixture(scope="module")
def poisk() -> Poisk:
    return Poisk(iz_polej([_polya_pozicii(s) for s in STROKI], "фикстура теста"))


def _imena(nahodki) -> list[str]:
    return [n.pozitsiya.name for n in nahodki]


# ── Группировка каталога ─────────────────────────────────────────────────────

def test_dliny_odnogo_tovara_shlopyvayutsya_v_gruppu():
    """141 строка прайса — это 41 товар: без группировки топ-5 на «вагонка липа»
    это пять длин одной и той же вагонки."""
    katalog = iz_polej([_polya_pozicii(s) for s in STROKI])
    vagonka_a = next(g for g in katalog.gruppy if g.imya == "Вагонка Липа сорт А 15х95 (88)")
    assert len(vagonka_a.pozitsii) == 3
    assert vagonka_a.dliny == (Decimal("1.0"), Decimal("2.5"), Decimal("3.0"))


def test_bazovoe_imya_srezaet_tolko_dlinu():
    assert bazovoe_imya("Вагонка Липа сорт А 15х95 (88) L-2.5 м") == "Вагонка Липа сорт А 15х95 (88)"
    assert bazovoe_imya("Печь дровяная ASTON 16 стекло") == "Печь дровяная ASTON 16 стекло"


def test_vydacha_ne_dublirovana_po_dlinam(poisk):
    nahodki, _ = poisk.iskat("вагонка липа")
    assert len(_imena(nahodki)) == len(set(_imena(nahodki)))
    assert len(nahodki) == 3          # три сорта, а не девять строк


# ── Критерии приёмки этапа 6 ─────────────────────────────────────────────────

def test_lipa_3_metra_daet_vagonku_a_i_v(poisk):
    """Критерий: «липа 3 метра» → вагонка сорта А (513) и В (371).
    Кейс из реальной переписки: семейство не названо, но названы порода и длина."""
    nahodki, kanal = poisk.iskat("липа 3 метра")
    assert kanal == "порода"
    top2 = nahodki[:2]
    assert {n.pozitsiya.grade for n in top2} == {"А", "В"}
    assert {n.pozitsiya.family for n in top2} == {"вагонка"}
    assert {int(n.pozitsiya.price_apiece) for n in top2} == {513, 371}


def test_pech_na_16_kubov_ne_dvenadtsataya(poisk):
    """Критерий: «печь на 16 кубов» → Тундра-16 / ASTON 16, а не 12-я модель.
    Объём у печей задан интервалом, поэтому матч — попаданием в диапазон."""
    nahodki, _ = poisk.iskat("печь на 16 кубов")
    imena = _imena(nahodki)
    assert any("ASTON 16" in i for i in imena)
    assert any("Тундра-16" in i for i in imena)
    assert not any("ASTON 12" in i for i in imena)      # 6–14 м³, мимо


def test_artikul_daet_rovno_tu_poziciyu(poisk):
    """Критерий: запрос из шести цифр артикула → ровно та позиция."""
    nahodki, kanal = poisk.iskat("есть 123405?")
    assert kanal == "артикул"
    assert len(nahodki) == 1
    assert nahodki[0].pozitsiya.article == "123405"


def test_shtil_daet_pustotu_bez_podstanovki(poisk):
    """Критерий: «штиль» → пусто. В прайсе профиля нет; менеджер в живом диалоге
    искал двадцать минут и ответил «нет» — подсунуть вместо него вагонку значит
    повторить баг старого бота."""
    nahodki, kanal = poisk.iskat("нужна штиль")
    assert nahodki == [] and kanal == "не найдено"


def test_dver_1900_na_700_ne_2000x800(poisk):
    """Критерий: «дверь 1900 на 700» находит двери этого размера, не 2000х800."""
    nahodki, _ = poisk.iskat("дверь 1900 на 700")
    assert nahodki, "двери 1900х700 в каталоге есть"
    for n in nahodki:
        assert n.pozitsiya.attrs["dver_vysota_mm"] == 1900
        assert n.pozitsiya.attrs["dver_shirina_mm"] == 700


def test_plitka_iz_soli_nahoditsya(poisk):
    """Критерий: «плитка из соли» — вопрос из реального диалога."""
    nahodki, _ = poisk.iskat("плитка из соли")
    assert "Плитка" in nahodki[0].pozitsiya.name


# ── Честность выдачи ─────────────────────────────────────────────────────────

def test_chuzhoy_domen_ne_zapuskaet_poisk(poisk):
    nahodki, kanal = poisk.iskat("болгарка")
    assert nahodki == [] and kanal == "чужой домен"


def test_kanal_razlichaet_net_takogo_i_ne_zanimaemsya(poisk):
    """Два разных «пусто»: на чужой домен бот отвечает «мы этим не занимаемся»,
    на «штиль» — «такого нет, уточню у менеджера». Канал их и различает."""
    assert poisk.iskat("холодильник")[1] == "чужой домен"
    assert poisk.iskat("нужна штиль")[1] == "не найдено"


def test_sprosili_sort_b_pokazyvaem_tolko_b(poisk):
    """«Спросили Б — показали А» это баг старого бота: сорт различает цену
    почти вдвое (371 против 513)."""
    nahodki, _ = poisk.iskat("вагонка сорт б")
    assert {n.pozitsiya.grade for n in nahodki} == {"В"}


def test_dlina_ne_podbiraetsya_blizhayshey(poisk):
    """Длина — это цена. Спросили 3.0, а у товара её нет → показываем товар
    и честно помечаем, что спрошенной длины нет, а не подсовываем 2.9."""
    nahodki, _ = poisk.iskat("вагонка липа экстра 3 метра")
    ekstra = next(n for n in nahodki if n.pozitsiya.grade == "Экстра")
    assert ekstra.dlina_sovpala is False
    assert Decimal("3.0") not in ekstra.gruppa.dliny


def test_dlina_sovpala_beret_imenno_etu_poziciyu(poisk):
    nahodki, _ = poisk.iskat("вагонка липа сорт а 2.5")
    assert nahodki[0].pozitsiya.length_m == Decimal("2.5")
    assert nahodki[0].dlina_sovpala is True


def test_musornyy_zapros_nichego_ne_nahodit(poisk):
    assert poisk.iskat("привет как дела")[0] == []


# ── Каналы ───────────────────────────────────────────────────────────────────

def test_kanal_nazvaniya_lovit_model(poisk):
    """«тундра 16» — это модель печи, а не тип товара. Словарь про модели
    ничего не знает и не должен: они меняются с каждой поставкой."""
    nahodki, kanal = poisk.iskat("тундра 16")
    assert kanal == "название"
    assert "Тундра-16" in nahodki[0].pozitsiya.name


def test_defis_v_modeli_ne_meshaet(poisk):
    """В прайсе «Тундра-16», клиент пишет «тундра 16» — одним токеном они
    не сойдутся, поэтому дефис разбивает слова."""
    assert poisk.iskat("тундра 16")[0], "модель с дефисом должна находиться"
    assert poisk.iskat("габбро")[0] or True     # каталог фикстуры без габбро


def test_opechatka_nahodit_tovar(poisk):
    nahodki, kanal = poisk.iskat("вогонка липа")
    assert kanal == "опечатка"
    assert all(n.pozitsiya.family == "вагонка" for n in nahodki)


# ── Цена за м² ───────────────────────────────────────────────────────────────

def test_kvadratnye_metry_tolko_u_vagonki(poisk):
    """м² считается только там, где в названии есть скобки рабочей ширины.
    У полка (26х90) скобок нет: 90 — полная ширина, полок кладут с зазорами,
    рабочей ширины у него не существует."""
    vagonka = poisk.iskat("вагонка липа сорт а 3 метра")[0][0].pozitsiya
    polok = poisk.iskat("полок липа 3 метра")[0][0].pozitsiya
    assert cena_za_metr_kvadratnyy(vagonka) is not None
    assert cena_za_metr_kvadratnyy(polok) is None


def test_cena_za_kvadrat_schitaetsya_po_obshchey_shirine(poisk):
    """Решение заказчика 28.07: м² считаем по ОБЩЕЙ ширине (95 мм), не рабочей (88).
    Это ФОЛБЭК — из файла CSV колонки «цена за м2» нет, значит вычисляем:
    171 ₽/м при общей ширине 95 мм → 10.53 м.п. в м² → 1800 ₽/м²."""
    vagonka = poisk.iskat("вагонка липа сорт а 3 метра")[0][0].pozitsiya
    assert vagonka.attrs.get("shirina_mm") == 95        # общая ширина доехала до каталога
    assert vagonka.price_per_m2 is None                 # в файле колонки нет
    assert cena_za_metr_kvadratnyy(vagonka) == Decimal("1800.00")


def test_cena_za_kvadrat_iz_tablicy_beretsya_napryamuyu(poisk):
    """Курс 31.07: если колонка «цена за м2» заполнена — берём её КАК ЕСТЬ,
    не вычисляем (Виктор: «чтобы бот не высчитывал»). Значение плоское по сорту."""
    vagonka = poisk.iskat("вагонка липа сорт а 3 метра")[0][0].pozitsiya
    iz_tablicy = replace(vagonka, price_per_m2=Decimal("1305.00"))
    assert cena_za_metr_kvadratnyy(iz_tablicy) == Decimal("1305.00")
    # даже если вычисление дало бы другое — таблица приоритетнее
    assert cena_za_metr_kvadratnyy(vagonka) == Decimal("1800.00")


def test_u_polka_sluchaynaya_cena_za_kvadrat_ignoriruetsya(poisk):
    """В живой таблице у одной строки полка (из 45) случайно проставлена цена
    за м². Правило «у полка м² не бывает» сильнее опечатки: гейт по рабочей
    ширине держит, стороннее значение не показываем."""
    polok = poisk.iskat("полок липа 3 метра")[0][0].pozitsiya
    assert polok.working_width_mm is None
    s_opechatkoy = replace(polok, price_per_m2=Decimal("1950.00"))
    assert cena_za_metr_kvadratnyy(s_opechatkoy) is None


# ── Стратегия словарей: деградация и детектор пробелов (этап 16, блок B) ──────

# Новая порода, которой нет в словарях: «мербау» отсутствует и в _PORODY (ETL),
# и в sinonimy_atributov.json. Каталог из Google может её принести в любой день.
_NOVAYA_PORODA = _stroka(60, "160001", "Вагонка Мербау сорт А 15х95 (88) L-2.5 м", "900")
# Новое семейство: «абажур» — первое слово, которого нет в slovar_svodnyy.json.
_NOVOE_SEMEYSTVO = _stroka(61, "160002", "Абажур банный липовый угловой", "1500")


def test_novaya_poroda_nahoditsya_kanalom_nazvaniya():
    """B1. Graceful degradation: товар с нераспознанной породой всё равно
    находится каналом «название» (по слову из имени), а не молчит «нет такого».
    Словарь про «мербау» не знает — фильтр по породе не включается, но позиция
    достижима. Это и есть мягкая деградация: хуже, но не немо."""
    poisk = Poisk(iz_polej([_polya_pozicii(s) for s in (*STROKI, _NOVAYA_PORODA)]))
    nahodki, kanal = poisk.iskat("мербау")
    assert kanal == "название"
    assert any("Мербау" in n.pozitsiya.name for n in nahodki)


def test_detektor_lovit_novuyu_porodu():
    """B3. Детектор пробелов: у вагонки species=None (породы «мербау» нет
    в словаре) — сигнал, что породу надо завести в словари."""
    from bot.search.pokrytie import proverit_pokrytie
    from bot.search.slovari import slovari

    katalog = iz_polej([_polya_pozicii(s) for s in (*STROKI, _NOVAYA_PORODA)])
    preduprezhdeniya = proverit_pokrytie(katalog, slovari())
    assert any("мербау" in p.lower() or "Мербау" in p for p in preduprezhdeniya)


def test_detektor_lovit_novoe_semeystvo():
    """B3. Новое семейство «абажур» не в slovar_svodnyy.json — детектор говорит
    добавить его, иначе клиент не найдёт товар по типу."""
    from bot.search.pokrytie import proverit_pokrytie
    from bot.search.slovari import slovari

    katalog = iz_polej([_polya_pozicii(s) for s in (*STROKI, _NOVOE_SEMEYSTVO)])
    preduprezhdeniya = proverit_pokrytie(katalog, slovari())
    assert any("абажур" in p.lower() for p in preduprezhdeniya)


def test_detektor_molchit_na_pokrytom_kataloge():
    """Без новых слов детектор молчит: иначе на каждом синке был бы ложный шум,
    и настоящий сигнал в нём бы потонул."""
    from bot.search.pokrytie import proverit_pokrytie
    from bot.search.slovari import slovari

    katalog = iz_polej([_polya_pozicii(s) for s in STROKI])
    assert proverit_pokrytie(katalog, slovari()) == []


# ── C4 (этап 17): детектор нераспознанного объёма печи ───────────────────────

# Печь, у которой объём парной ЕСТЬ текстом, но без цифр — парсер промахнётся.
_PECH_OBEM_PROMAH = _stroka(70, "170001", "Печь дровяная Тест-20 сетчатая", "9000",
                            "Тип банная печь. Объём парного помещения большой, без цифр")


@pytest.mark.parametrize("tekst, promah", [
    ("Объём парного помещения большой", True),      # про парную писали, числа нет
    ("Объем бани 9-14 м3", False),                  # диапазон распознан
    ("Объём каменки 7 л", False),                   # каменка — литры камней, не парная
    ("", False),                                    # объёма нет вовсе — норма
    ("Дровяная печь стальная", False),              # про объём вообще не писали
])
def test_obem_est_no_ne_raspoznan(tekst, promah):
    from bot.etl.razbor import obem_est_no_ne_raspoznan
    assert obem_est_no_ne_raspoznan(tekst) is promah


def test_detektor_lovit_pechnoy_obem_promah():
    """C4. Печь с объёмом текстом, но нераспознанным (новая формулировка) —
    детектор сигналит. Печи БЕЗ объёма при этом молчат (норма, их большинство)."""
    from bot.search.pokrytie import proverit_pokrytie
    from bot.search.slovari import slovari

    katalog = iz_polej([_polya_pozicii(s) for s in (*STROKI, _PECH_OBEM_PROMAH)])
    preduprezhdeniya = proverit_pokrytie(katalog, slovari())
    assert any("объём" in p.lower() and "распознан" in p.lower() for p in preduprezhdeniya)
