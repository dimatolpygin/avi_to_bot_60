# -*- coding: utf-8 -*-
"""Тесты словарей и разбора запроса (этап 5). БД и файл прайса не нужны.

Живая проверка — `python -m bot.search.zapros` (печатает разбор проверочного
набора из критериев приёмки этапа 5, см. `docs/07_ROADMAP.md`).

Названия каталога вшиты в тест списком (`KATALOG`), а не читаются из
`материалы/прайс/` — прайс лежит вне git (клиентские материалы), а тест
про ложные срабатывания гейта нужен и на чистом клоне.
"""
import json
import shutil
from decimal import Decimal

import pytest

from bot.search.fuzzy import edit_ratio, similarity, word_sim
from bot.search.normalize import norm, slova, soft_has, stems
from bot.search.slovari import (KATALOG_DANNYH, PORG_OPECHATKI, sbrosit_kesh,
                                slovari, zagruzit)
from bot.search.zapros import razobrat_zapros

# Все 41 базовое название каталога Saunamart (без размножения по длинам).
KATALOG = [
    "Брус профилированный сосна 50х40",
    "Вагонка Липа сорт А 15х95 (88) L-2.5 м",
    "Вагонка Липа сорт В 15х95 (88) L-2.0 м",
    "Вагонка Липа сорт Экстра 15х95 (88) L-3.0 м",
    "Вагонка ольха STS сорт Экстра 15х88 (80) (6 шт/уп) L-2.4 м",
    "Дверь для сауны 1900х700 Бронза 8 мм 3 петли ольха",
    "Дверь для сауны 1900х700 Бронза 8мм 3 петли осина",
    "Дверь для сауны 1900х700 Бронза матовая 8 мм 3 петли ольха",
    "Дверь для сауны 1900х700 Дорвуд (бронза хвоя) стекло 6 мм 2 петли",
    "Дверь для сауны 1900х800 Бронза 8 мм 3 петли ольха",
    "Дверь для сауны 1900х800 Бронза матовая 8 мм 3 петли ольха",
    "Дверь для сауны 2000х700 Бронза 8 мм 3 петли ольха",
    "Дверь для сауны 2000х700 Бронза матовая 8 мм 3 петли ольха",
    "Дверь для сауны 2000х800 Бронза 8 мм 3 петли ольха",
    "Дверь для сауны 2000х800 Бронза матовая 8 мм 3 петли ольха",
    "Камни Габбро-диабаз мелкий 20кг Карелия",
    "Камни Жадеит колотый фр 80-130 мм уп.10кг. (Хакасия)",
    "Кирпич из гималайской соли 20х10х5см натуральный SALTWAY",
    "Короб вентиляция (3 части)",
    "Наличник Липа сорт Экстра 15х70 L-2.2 м",
    "Печь дровяная ASTON 12 стекло",
    "Печь дровяная ASTON 16 стекло",
    "Печь дровяная Пегас Тундра-14 сетчатый кожух Закрытая каменка",
    "Печь дровяная Пегас Тундра-14 со стеклом сетчатый кожух Закрытая каменка",
    "Печь дровяная Пегас Тундра-16 сетчатый кожух Закрытая каменка",
    "Печь дровяная Пегас Тундра-16 со стеклом сетчатый кожух Закрытая каменка",
    "Печь дровяная чугунная Пегас Легион 16 ДТ 4 стекло",
    "Печь дровяная чугунная Пегас Легион 16 панорамная дверка",
    "Печь дровяная чугунная Пегас Легион 22 ДТ 4 стекло",
    "Печь дровяная чугунная Пегас Легион 22 панорамная дверка",
    "Печь электрическая ЭКМ Терра 9 кВт",
    "Печь электрическая ЭКМ Терра плюс 9 кВт встроенное управление",
    "Печь электрическая ЭКМ Цилиндр плюс 9 кВт встроенное управление",
    "Плитка из гималайской соли 20х10х2.5 с пазом. шлифованная торцованная с 2х сторон",
    "Полок Липа сорт А 26х90 L-2.2 м",
    "Полок Липа сорт Экстра 26х90 L-3.0 м",
    "Полок абаши 25x95 L-2.4 м",
    "Полок термолипа сорт А 25х90 L-2.5 м",
    "Рейка профилированная сосна 20х40",
    "Спил  можжевельника",
    "Фольга алюминиевая 12 м2 80мкм",
]

# Семейства каталога — ровно то, что кладёт в products.family ETL (этап 4).
SEMEYSTVA_KATALOGA = {
    "вагонка", "полок", "дверь", "печь", "камень", "наличник", "брус",
    "рейка", "фольга", "плитка", "кирпич", "короб", "спил",
}


# ── Нормализация ─────────────────────────────────────────────────────────────

def test_latinskie_gomoglify_stanovyatsya_kirillicey():
    """«сорт A» латинской A клиент пишет постоянно — на глаз не отличить."""
    assert norm("Сорт A") == norm("сорт а")
    assert norm("ВАГОНКА") == "вагонка"


def test_yo_i_nerazryvnyy_probel_ne_lomayut_sravnenie():
    assert norm("Трёхметровая\xa0липа") == "трехметровая липа"


def test_stop_slova_ne_popadayut_v_stemy():
    """«сколько стоит вагонка» и «вагонка» должны выглядеть одинаково."""
    assert stems("сколько стоит вагонка") == stems("вагонка")


def test_slovoformy_shodyatsya_v_odin_stem():
    assert stems("вагонки") == stems("вагонку") == stems("вагонка")


def test_soft_has_svyazyvaet_sokrashchenie_s_polnoy_formoy():
    assert soft_has({"профилирован"}, "профилир")
    assert soft_has({"алюминиев"}, "алюмин")


def test_soft_has_ne_lovit_korotkie():
    """Порог в 4 символа обязателен: иначе «печ» цепляется за «печать»."""
    assert not soft_has({"печат"}, "печ")
    assert not soft_has({"вагонк"}, "дуб")


# ── Фаззи ────────────────────────────────────────────────────────────────────

def test_opechatka_v_odin_simvol_prohodit_porog():
    assert word_sim("вогонка", "вагонка") >= PORG_OPECHATKI


def test_sosednie_slova_porog_ne_prohodyat():
    """Порог 0.85 отделяет опечатку от другого слова."""
    assert word_sim("вагон", "вагонка") < PORG_OPECHATKI
    assert word_sim("дверь", "зверь") < PORG_OPECHATKI
    assert word_sim("болгарка", "вагонка") < PORG_OPECHATKI


def test_stemming_ubil_by_fazzi():
    """Почему фаззи считается по словам, а не по стемам: у стемов «вогонк»
    и «вагонк» сходство ниже порога, и опечатка перестала бы находиться."""
    assert word_sim("вогонк", "вагонк") < PORG_OPECHATKI


def test_trigrammnaya_i_levenshteynovskaya_dopolnyayut_drug_druga():
    """На коротких словах триграммы проваливаются — отсюда максимум из двух."""
    assert similarity("вогонка", "вагонка") < 0.5
    assert edit_ratio("вогонка", "вагонка") > 0.85


# ── Словари ──────────────────────────────────────────────────────────────────

def test_klyuchi_slovarya_sovpadayut_s_semeystvami_kataloga():
    """Ключ словаря = products.family. Разъедутся — поиск не свяжет резолв
    запроса с каталогом, и найдено будет ничего."""
    assert set(slovari().semeystva) == SEMEYSTVA_KATALOGA


def test_slovari_gruzyatsya_i_ne_pusty():
    sl = slovari()
    assert sl.sort["б"] == "В"
    assert sl.poroda_slovo["липовая"] == "липа"
    assert sl.dver_gabarit["стандартная"] == (1900, 700)


# ── Критерии приёмки этапа 5 ─────────────────────────────────────────────────

def test_sort_b_i_sort_v_dayut_odinakovyy_razbor():
    """Критерий: «вагонка сорт б» и «вагонка сорт в» — один набор позиций.
    Разбор совпадает целиком, включая стемы: именно они пойдут в скоринг
    этапа 6, и разойдись они — разойдётся и выдача."""
    b = razobrat_zapros("вагонка сорт б")
    v = razobrat_zapros("вагонка сорт в")
    assert b.sort == v.sort == "В"
    assert b.semeystvo == v.semeystvo == "вагонка"
    assert b.stemy == v.stemy
    assert b.normalizovanny == v.normalizovanny


def test_sort_b_v_lyuboy_raskladke():
    assert razobrat_zapros("вагонка сорт B").sort == "В"     # латинская B
    assert razobrat_zapros("вагонка сорта Б").sort == "В"


def test_trehmetrovaya_lipa_eto_dlina_3_meters():
    """Критерий: «трёхметровая липа» распознаётся как длина 3.0 м."""
    z = razobrat_zapros("трёхметровая липа")
    assert z.dlina_m == Decimal("3.0")
    assert z.poroda == "липа"


@pytest.mark.parametrize("zapros,dlina", [
    ("вагонка три метра", "3.0"),
    ("полок два сорок", "2.4"),
    ("вагонка полторашка", "1.5"),
    ("вагонка L-2.5 м", "2.5"),
    ("вагонка 250 см", "2.5"),
    ("вагонка 2,5", "2.5"),
])
def test_raznye_sposoby_nazvat_dlinu(zapros, dlina):
    assert razobrat_zapros(zapros).dlina_m == Decimal(dlina)


def test_razmer_secheniya_ne_stanovitsya_dlinoy():
    """«15х95» — сечение доски, а не 15 метров."""
    assert razobrat_zapros("вагонка 15х95").dlina_m is None


def test_u_dveri_i_pechi_dliny_net():
    """У этих семейств длины в каталоге нет — число значит другое."""
    assert razobrat_zapros("дверь 2 метра").dlina_m is None
    assert razobrat_zapros("печь на 16 кубов").dlina_m is None


@pytest.mark.parametrize("zapros", ["болгарка", "холодильник", "шампунь",
                                    "керамическая плитка", "входная дверь"])
def test_chuzhoy_domen_otsekaetsya(zapros):
    """Критерий: чужой домен отсекается гейтом ДО поиска."""
    assert razobrat_zapros(zapros).chuzhoy_domen is not None


@pytest.mark.parametrize("nazvanie", KATALOG)
def test_gate_ne_srabatyvaet_na_kataloge(nazvanie):
    """Гейт не имеет права зацепить ни одну позицию прайса — иначе бот
    откажется от собственного товара."""
    assert razobrat_zapros(nazvanie).chuzhoy_domen is None


@pytest.mark.parametrize("zapros", [
    "штиль", "веник дубовый", "шайка для бани", "ковш", "аромамасло",
    "вагонка липа", "печь для бани", "камни жадеит", "дверь в парную",
    "полок абаши", "фольга для бани", "плитка из соли",
])
def test_bannye_zaprosy_gate_ne_gate(zapros):
    """Банное, чего нет в прайсе (штиль, веник, ковш), — НЕ чужой домен:
    там честный ответ «нет в наличии», а не «мы этим не занимаемся»."""
    assert razobrat_zapros(zapros).chuzhoy_domen is None


def test_opechatka_vogonka_nahodit_vagonku():
    """Критерий: «вогонка» находит вагонку (фаззи-канал)."""
    z = razobrat_zapros("вогонка")
    assert z.semeystvo == "вагонка"
    assert z.kanal_semeystva == "опечатка"


def test_polog_i_polok_odno_semeystvo():
    """Критерий: народное «полог» ведёт к семейству полка."""
    assert razobrat_zapros("полог липа").semeystvo == "полок"
    assert razobrat_zapros("полок липа").semeystvo == "полок"


@pytest.mark.parametrize("zapros,semeystvo", [
    ("каменка", "печь"),
    ("электрокаменка", "печь"),
    ("доска на полок", "полок"),
    ("обшивочная доска", "вагонка"),
    ("камни для печи", "камень"),
    ("обналичка", "наличник"),
    ("вентиляционный короб", "короб"),
    ("соляная плитка", "плитка"),
    ("можжевельник", "спил"),
    ("пароизоляция", "фольга"),
])
def test_narodnye_nazvaniya_vedut_k_semeystvu(zapros, semeystvo):
    assert razobrat_zapros(zapros).semeystvo == semeystvo


def test_shtil_ne_rezolvitsya_v_vagonku():
    """Профиля «штиль» в прайсе нет. Записать его синонимом вагонки значит
    выдать не то, что просили, — это баг старого бота, а не фича."""
    assert razobrat_zapros("нужна штиль").semeystvo is None


def test_artikul_uznaetsya_v_lyuboy_fraze():
    assert razobrat_zapros("есть 123405?").artikul == "123405"
    assert razobrat_zapros("вагонка 15х95").artikul is None


@pytest.mark.parametrize("zapros,vysota,shirina", [
    ("дверь 1900 на 700", 1900, 700),
    ("дверь 1900х700", 1900, 700),
    ("дверь 190 на 70", 1900, 700),
    ("дверь стандартная", 1900, 700),
])
def test_gabarit_dveri(zapros, vysota, shirina):
    z = razobrat_zapros(zapros)
    assert (z.dver_vysota_mm, z.dver_shirina_mm) == (vysota, shirina)


def test_obem_i_moshchnost_pechi():
    assert razobrat_zapros("печь на 16 кубов").obem_m3 == 16
    assert razobrat_zapros("печь электрическая 9 квт").moshchnost_kvt == 9.0


def test_termolipa_ne_stanovitsya_lipoy():
    """Как и в ETL: «термолипа» содержит «липа» и склеится без порядка проверки."""
    assert razobrat_zapros("полок термолипа").poroda == "термолипа"


def test_pravka_slovarya_podhvatyvaetsya_perezagruzkoy(tmp_path):
    """Критерий: правка словаря подхватывается перезапуском без правки кода."""
    for imya in ("slovar_svodnyy.json", "sinonimy_atributov.json",
                 "sleng_razmerov.json", "chuzhoy_domen.json"):
        shutil.copy(f"{KATALOG_DANNYH}/{imya}", tmp_path / imya)

    put = tmp_path / "slovar_svodnyy.json"
    svodnyy = json.loads(put.read_text(encoding="utf-8"))
    svodnyy["вагонка"]["синонимы"].append("евродоска")
    put.write_text(json.dumps(svodnyy, ensure_ascii=False), encoding="utf-8")

    do = razobrat_zapros("евродоска")
    posle = razobrat_zapros("евродоска", zagruzit(str(tmp_path)))
    assert do.semeystvo is None and posle.semeystvo == "вагонка"


def test_sbros_kesha_perechityvaet_fayly():
    sbrosit_kesh()
    assert len(slovari().semeystva) == len(SEMEYSTVA_KATALOGA)
