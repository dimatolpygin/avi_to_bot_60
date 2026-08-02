# -*- coding: utf-8 -*-
"""Тесты ETL прайса (этап 4). БД не нужна: проверяем разбор и чтение файла.

Живая проверка импорта — `python -m bot.etl.import_prays` и запросы к БД
(критерии приёмки этапа 4 в `docs/07_ROADMAP.md`).

Все ожидаемые значения взяты из реального файла заказчика — если тест упал
после правки разбора, сначала посмотри `--tolko-razbor`, а не правь тест.
"""
import csv
from decimal import Decimal

import pytest

from datetime import datetime, timezone

from bot.etl.chtenie import (MINIMUM_STROK, OshibkaPraysa, StrokaPraysa,
                             prochitat, shlopnut_dubli, sobrat_prays)
from bot.etl.import_prays import _cena_za_metr, _nalichie, _otlichaetsya, _polya_pozicii
from bot.etl.razbor import razobrat


# ── Разбор названия ──────────────────────────────────────────────────────────

def test_vagonka_razbiraetsya_polnostyu():
    r = razobrat("Вагонка Липа сорт А 15х95 (88) L-2.5 м")
    assert (r.family, r.grade, r.species) == ("вагонка", "А", "липа")
    assert r.length_m == Decimal("2.5")
    assert r.working_width_mm == 88
    assert r.attrs["tolshchina_mm"] == 15 and r.attrs["shirina_mm"] == 95
    assert r.is_package is False


def test_u_polka_rabochey_shiriny_net():
    """Скобок в названии полка нет: 90 — полная ширина, полок кладут с зазорами.
    Пустое поле = «пересчёт в м² запрещён», и это главный смысл колонки."""
    r = razobrat("Полок Липа сорт Экстра 26х90 L-2.2 м")
    assert r.working_width_mm is None
    assert r.attrs["shirina_mm"] == 90


def test_sort_b_chitaetsya_kak_v():
    """В прайсе «сорт В» кириллицей, клиент пишет «сорт Б» — это один сорт."""
    assert razobrat("Вагонка Липа сорт Б 15х95 (88) L-2.0 м").grade == "В"
    assert razobrat("Вагонка Липа сорт В 15х95 (88) L-2.0 м").grade == "В"


def test_latinskaya_a_v_sorte_ne_lomaet_razbor():
    assert razobrat("Вагонка Липа сорт A 15х95 (88) L-2.0 м").grade == "А"


def test_termoosina_ne_stanovitsya_osinoy():
    """«термоосина» содержит «осина» — без порядка проверки они склеятся."""
    assert razobrat("Вагонка термоосина ВОЛНА сорт Экстра 15х88 (77) L-2.4 м").species \
        == "термоосина"


def test_skobki_ne_iz_razmera_ne_schitayutsya_rabochey_shirinoy():
    """«(0.63 м2/уп)» и «(бронза хвоя)» — не рабочая ширина."""
    assert razobrat("Камень натуральный Кварцит Белый плитка 600х150 мм (0.63 м2/уп)") \
        .working_width_mm is None
    assert razobrat("Дверь для сауны 1900х700 Дорвуд (бронза хвоя) стекло 6 мм") \
        .working_width_mm is None


def test_fasovka_uznaetsya_a_litry_ne_fasovka():
    assert razobrat("Фольга алюминиевая 12 м2 80мкм").is_package is True
    assert razobrat("Камни Жадеит колотый фр 80-130 мм уп.10кг. (Хакасия)").is_package is True
    assert razobrat("Камни Габбро-диабаз мелкий 20кг Карелия").is_package is True
    # 35 л — объём изделия, а не упаковка: делить цену нельзя, но и пометка неверна.
    assert razobrat("Обливное устройство Учар песчаный сланец 35 л").is_package is False
    assert razobrat("Вагонка Липа сорт А 15х95 (88) L-2.5 м").is_package is False


def test_shtuk_v_upakovke_eto_ne_fasovka():
    """«6 шт/уп» — сколько штук в пачке, а цена всё равно за штуку: у этой
    позиции 693 ₽/шт при 289 ₽/м на 2.4 м. Пометить упаковкой = заставить бота
    сказать «делить нельзя» там, где делить как раз нужно."""
    r = razobrat("Вагонка ольха STS сорт Экстра 15х88 (80) L-2.4 м (6 шт/уп)")
    assert r.is_package is False
    assert r.working_width_mm == 80 and r.length_m == Decimal("2.4")


def test_dver_vysota_vsegda_bolshe_shiriny():
    """В прайсе порядок гуляет: «1900х700», но у Grandis «680*1890»."""
    a = razobrat("Дверь для сауны 1900х700 Бронза 8 мм 3 петли ольха")
    assert a.attrs["dver_vysota_mm"] == 1900 and a.attrs["dver_shirina_mm"] == 700
    b = razobrat("Дверь для сауны 680*1890 Grandis GS Anodize Brasch Бронза матовая")
    assert b.attrs["dver_vysota_mm"] == 1890 and b.attrs["dver_shirina_mm"] == 680


@pytest.mark.parametrize("harakteristiki, ozhidaem", [
    ("объем бани 9 - 14 м3", (9, 14)),
    ("Объём парного помещения: от 8 до 18 м3 Ширина: 560 мм", (8, 18)),
    ("Объем парного помещения, м3 6-16 Масса без камней, кг.85", (6, 16)),
    ("Объём парной до 18 м3 Вес печи 69 кг.", (None, 18)),
    ("объем которых не превышает 14 метров кубических", (None, 14)),
    # Два упоминания: без объединения получилось бы 8-8 вместо 8-14.
    ("объем сауны, м3: 8 Максимальный объем сауны, м3: 14 Вес камней, кг: 60", (8, 14)),
    ("объем парильни, куб. м 6 Толщина топки 4 мм. объем парильни, куб. м 14", (6, 14)),
    # Литры каменки — не объём парной.
    ("Объем помещения, м3 от 6 до 14 м3 Объем закрытой каменки, л 7", (6, 14)),
])
def test_obem_parnoy_pechi(harakteristiki, ozhidaem):
    r = razobrat("Печь дровяная Тест", harakteristiki)
    assert (r.attrs.get("obem_min_m3"), r.attrs.get("obem_max_m3")) == ozhidaem


def test_pech_bez_harakteristik_ostaetsya_bez_obema():
    """Ничего не угадываем: пустое поле бот обойдёт, выдуманное — соврёт."""
    r = razobrat("Печь дровяная ASTON 12 стекло", None)
    assert "obem_min_m3" not in r.attrs and "obem_max_m3" not in r.attrs


def test_moshchnost_iz_nazvaniya():
    assert razobrat("Печь электрическая ЭКМ Зевс 9 кВт").attrs["moshchnost_kvt"] == 9.0
    assert razobrat("Печь электрическая ЭКМ Цилиндр плюс 10.5 кВт").attrs["moshchnost_kvt"] \
        == 10.5


# ── Цена за метр ─────────────────────────────────────────────────────────────

def test_cena_za_metr_vychislyaetsya_a_ne_beretsya_iz_fayla():
    """449 ₽ за 3.0 м → 149.67 ₽/м. В файле в этой колонке 150.00."""
    assert _cena_za_metr(Decimal("449"), Decimal("3.0")) == Decimal("149.67")
    assert _cena_za_metr(Decimal("375"), Decimal("2.5")) == Decimal("150.00")


def test_bez_dliny_ceny_za_metr_net():
    assert _cena_za_metr(Decimal("8800"), None) is None
    assert _cena_za_metr(None, Decimal("2.5")) is None


def test_shtuchnaya_poziciya_ne_stanovitsya_mernoy():
    d = _polya_pozicii(_stroka("23405", "Дверь для сауны 1900х700 Бронза 8 мм", "8800"))
    assert d["unit"] == "piece" and d["price_per_m"] is None


def test_mernaya_poziciya_bez_kolonki_ceny_za_metr_vse_ravno_mernaya():
    """Единицу определяем по длине в названии, а не по заполненности колонки
    price_per_meter: колонку заказчик может не заполнить, и позиция молча
    станет «штучной» — бот перестанет называть цену за метр погонный."""
    d = _polya_pozicii(_stroka("11213", "Наличник Липа сорт Экстра 15х70 L-2.2 м", "387"))
    assert d["unit"] == "linear_m" and d["price_per_m"] == Decimal("175.91")


# ── Наличие ──────────────────────────────────────────────────────────────────

def test_bez_kolonki_nalichiya_vse_neizvestno():
    """Колонки в прайсе нет. Выдумывать наличие — ровно тот баг, из-за
    которого бота переделывают: «неизвестно» честнее «есть» и «нет»."""
    assert _nalichie(None) == "unknown"


def test_pustaya_yacheyka_eto_ne_net_a_neizvestno():
    """Правило заказчика: «Да» = есть, пусто = уточнить у менеджера.
    Считать пустую ячейку за «нет» — это и есть баг старого бота, который
    отвечал «необрезной нет», хотя она была в прайсе."""
    assert _nalichie("") == "unknown"
    assert _nalichie("   ") == "unknown"


def test_kolonka_nalichiya_chitaetsya():
    assert _nalichie("Да") == "in_stock"
    assert _nalichie("да") == "in_stock"
    assert _nalichie("нет") == "out"     # в таблице не встречается, но читаем
    assert _nalichie("3") == "in_stock"
    assert _nalichie("0") == "out"
    assert _nalichie("ерунда") == "unknown"


# ── Чтение файла и валидация ─────────────────────────────────────────────────

def _stroka(article, name, cena, cena_m=None, nalichie=None, nomer=2):
    return StrokaPraysa(
        article=article, name=name, characteristics=None,
        price_apiece=Decimal(cena),
        price_per_meter_fayl=Decimal(cena_m) if cena_m else None,
        nalichie_syroe=nalichie, nomer_stroki=nomer)


def _zapisat_csv(put, stroki, zagolovok=None):
    zagolovok = zagolovok or ["article", "nomenclature", "characteristics",
                              "price_apiece", "price_per_meter"]
    with open(put, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(zagolovok)
        w.writerows(stroki)
    return str(put)


def _normalnyy_csv(tmp_path, skolko=MINIMUM_STROK + 10):
    stroki = [[f"1{i:04d}", f"Вагонка Липа сорт А 15х95 (88) L-2.{i % 9} м", "", 375, ""]
              for i in range(skolko)]
    return _zapisat_csv(tmp_path / "prays.csv", stroki)


def test_chitaet_csv_s_temi_zhe_kolonkami(tmp_path):
    prays = prochitat(_normalnyy_csv(tmp_path))
    assert prays.vsego_strok == MINIMUM_STROK + 10
    assert prays.stroki[0].nalichie_syroe is None      # колонки наличия нет


def test_artikul_iz_excel_ne_ostaetsya_floatom(tmp_path):
    """openpyxl отдаёт числовой артикул как 23405.0 — ключ синхронизации
    разъедется между запусками, если это не срезать."""
    put = _normalnyy_csv(tmp_path)
    stroki = [["23405.0", "Дверь для сауны 1900х700", "", 8800, ""]]
    with open(put, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(stroki)
    prays = prochitat(put)
    assert prays.stroki[-1].article == "23405"


def test_obrezannyy_fayl_padaet_i_ne_daet_importa(tmp_path):
    put = _zapisat_csv(tmp_path / "obrezok.csv",
                       [["1", "Вагонка Липа сорт А L-2.5 м", "", 375, ""]] * 3)
    with pytest.raises(OshibkaPraysa) as e:
        prochitat(put)
    assert "не меньше" in str(e.value)


def test_net_obyazatelnoy_kolonki_padaet(tmp_path):
    put = _zapisat_csv(tmp_path / "bez_ceny.csv",
                       [["1", "Вагонка", ""]] * 60,
                       zagolovok=["article", "nomenclature", "characteristics"])
    with pytest.raises(OshibkaPraysa) as e:
        prochitat(put)
    assert "price_apiece" in str(e.value)


def test_fayla_net_padaet_s_russkim_soobshcheniem(tmp_path):
    with pytest.raises(OshibkaPraysa) as e:
        prochitat(str(tmp_path / "netu.xlsx"))
    assert "не найден" in str(e.value)


def test_stroki_bez_ceny_propuskayutsya_a_ne_lozhatsya_nulem(tmp_path):
    put = _normalnyy_csv(tmp_path)
    with open(put, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(["99999", "Доска липа необрезная", "", "", ""])
    prays = prochitat(put)
    assert all(s.article != "99999" for s in prays.stroki)


# ── Общее ядро и русские заголовки (источник Google, курс 31.07) ──────────────

def _russkiy_list(skolko=MINIMUM_STROK + 5):
    """Сырые строки в форме `get_all_values` живой Google-таблицы: слева пустая
    колонка, заголовок первой строкой, все ячейки — строки, русские имена цен."""
    zagolovok = ["", "article", "nomenclature", "characteristics",
                 "цена штука", "цена м.п.", "цена за м2", "Наличие"]
    stroki = [zagolovok]
    for i in range(skolko):
        stroki.append(["", f"1{i:04d}", "Вагонка Липа сорт В 15х95 (88) L-1.0 м",
                       "", "85", "85", "890", "Да"])
    return stroki


def _sobrat(syrye):
    return sobrat_prays(syrye, "тест", datetime.now(timezone.utc), "тест")


def test_russkie_zagolovki_ceny_raspoznayutsya():
    """Живая таблица несёт «цена штука», а не «price_apiece» — читаем оба диалекта."""
    prays = _sobrat(_russkiy_list())
    assert prays.vsego_strok == MINIMUM_STROK + 5
    assert prays.stroki[0].price_apiece == Decimal("85")
    assert prays.stroki[0].nalichie_syroe == "Да"


def test_zagolovok_ne_pervoy_strokoy_nahoditsya():
    """Как в CSV-выгрузке: сверху две мусорные строки, заголовок ниже."""
    syrye = [["мусор"], [""], *_russkiy_list()]
    prays = _sobrat(syrye)
    assert prays.vsego_strok == MINIMUM_STROK + 5


def test_pustoy_list_padaet_ponyatno():
    with pytest.raises(OshibkaPraysa) as e:
        _sobrat([])
    assert "пуст" in str(e.value)


def test_yacheyki_strokami_ne_lomayut_privedenie():
    """gspread отдаёт всё строками, пустое — "" (не None): цена и артикул должны
    привестись, а "" в цене за метр не стать нулём."""
    prays = _sobrat(_russkiy_list())
    s = prays.stroki[0]
    assert s.article == "10000" and s.price_apiece == Decimal("85")
    assert s.price_per_meter_fayl == Decimal("85")


def test_cena_za_m2_iz_kolonki_dohodit_do_stroki():
    """Колонка «цена за м2» живой таблицы должна доехать до StrokaPraysa."""
    prays = _sobrat(_russkiy_list())
    assert prays.stroki[0].price_per_m2_fayl == Decimal("890")


# ── Позиции без артикула: «мягкий ассортимент» ЭКМ (курс 31.07) ────────────────

def _ekm(cena="15000"):
    """Строка ЭКМ как в живой таблице: артикула нет, цена есть, наличие «Да»."""
    return ["", "", "Электрокаменка ЭКМ 6 кВт «Зевс»", "", cena, "", "", "Да"]


def test_stroka_bez_artikula_ne_teryaetsya_a_kliuchitsya_po_nazvaniyu():
    """11 ЭКМ без SKU — «мягкий ассортимент»: держим их, ключ по номенклатуре."""
    prays = _sobrat([*_russkiy_list(), _ekm()])
    ekm = [s for s in prays.stroki if s.article.startswith("nomen-")]
    assert len(ekm) == 1
    assert ekm[0].name.startswith("Электрокаменка") and ekm[0].price_apiece == Decimal("15000")


def test_kliuch_bez_artikula_stabilen_mezhdu_progonami():
    """Ключ выведен из названия и не должен «плавать»: иначе повторный синк
    задвоит ЭКМ (старый ключ гасится, новый вставляется)."""
    a = _sobrat([*_russkiy_list(), _ekm()]).stroki[-1].article
    b = _sobrat([*_russkiy_list(), _ekm()]).stroki[-1].article
    assert a == b and a.startswith("nomen-")


def test_stroka_bez_artikula_derzhitsya_dazhe_bez_ceny():
    """Заказчик сотрёт цену ЭКМ — позиция всё равно в каталоге («есть, уточню»),
    в отличие от строки С артикулом, где пустая цена = порча данных."""
    prays = _sobrat([*_russkiy_list(), _ekm(cena="")])
    ekm = [s for s in prays.stroki if s.article.startswith("nomen-")]
    assert len(ekm) == 1 and ekm[0].price_apiece is None


def test_stroka_s_artikulom_bez_ceny_po_prezhnemu_propuskaetsya():
    """Пустая цена у позиции С артикулом — это по-прежнему пропуск (защита данных)."""
    bityy = ["", "199999", "Вагонка Липа сорт В 15х95 (88) L-1.0 м", "", "", "", "", "Да"]
    prays = _sobrat([*_russkiy_list(), bityy])
    assert all(s.article != "199999" for s in prays.stroki)


# ── Дубли ────────────────────────────────────────────────────────────────────

def test_polnyy_dubl_shlopyvaetsya_bez_konflikta():
    a = _stroka("26907", "Печь дровяная ASTON 12 стекло", "20800", nomer=2)
    b = _stroka("26907", "Печь дровяная ASTON 12 стекло", "20800", nomer=99)
    stroki, dubley, konfliktov = shlopnut_dubli([a, b])
    assert len(stroki) == 1 and dubley == 1 and konfliktov == 0


def test_dubl_s_raznoy_cenoy_schitaetsya_konfliktom():
    """Молча проглотить нельзя: это поломка прайса, о ней должно быть видно."""
    a = _stroka("26907", "Печь дровяная ASTON 12 стекло", "20800", nomer=2)
    b = _stroka("26907", "Печь дровяная ASTON 12 стекло", "25000", nomer=99)
    stroki, dubley, konfliktov = shlopnut_dubli([a, b])
    assert konfliktov == 1
    assert stroki[0].price_apiece == Decimal("25000")   # берём последнюю строку


# ── Идемпотентность ──────────────────────────────────────────────────────────

class _FeykPoziciya:
    """Двойник строки products: сравнение полей — чистая функция, БД не нужна."""

    def __init__(self, **polya):
        for k, v in polya.items():
            setattr(self, k, v)


def test_te_zhe_dannye_ne_dayut_obnovleniya():
    """Иначе повторный импорт «обновлял» бы все 204 строки, а идемпотентность
    (0/0/0 на втором прогоне) — критерий приёмки этапа."""
    polya = _polya_pozicii(_stroka("72749", "Вагонка Липа сорт А 15х95 (88) L-2.5 м", "375"))
    assert _otlichaetsya(_FeykPoziciya(**polya), polya) == []


def test_izmenenie_ceny_vidno():
    polya = _polya_pozicii(_stroka("72749", "Вагонка Липа сорт А 15х95 (88) L-2.5 м", "375"))
    bylo = _FeykPoziciya(**{**polya, "price_apiece": Decimal("400")})
    assert _otlichaetsya(bylo, polya) == ["price_apiece"]


def test_cena_iz_bd_kak_decimal_s_nulyami_ne_schitaetsya_izmeneniem():
    """БД возвращает Numeric(12,2) как Decimal('375.00'), а из файла приходит
    Decimal('375') — прямое сравнение объявило бы это изменением."""
    polya = _polya_pozicii(_stroka("72749", "Вагонка Липа сорт А 15х95 (88) L-2.5 м", "375"))
    bylo = _FeykPoziciya(**{**polya, "price_apiece": Decimal("375.00")})
    assert _otlichaetsya(bylo, polya) == []
