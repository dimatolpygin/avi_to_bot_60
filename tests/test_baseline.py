# -*- coding: utf-8 -*-
"""Тесты eval-харнесса (этап 7). БД не нужна: каталог берётся из выгрузки прайса.

Харнесс меряет качество поиска, поэтому главный вопрос к нему не «считает ли
он проценты», а **ловит ли он регресс**. Проверка на живом коде бессмысленна:
там всё зелёное. Поэтому ниже словари намеренно портятся, и тест требует,
чтобы метрика упала ниже ворот. Если она не упала — харнесс не работает,
как бы красиво он ни печатал сводку.

Живая проверка человеком — `python -m bot.search.baseline --podrobno`.
"""
import json
import os
import shutil

import pytest

from bot.etl.import_prays import FAYL_PO_UMOLCHANIYU
from bot.search import baseline
from bot.search.katalog import iz_fayla_praysa
from bot.search.slovari import KATALOG_DANNYH, zagruzit


def _skip_bez_praysa() -> None:
    """Клиентский прайс `материалы/прайс/*.csv` — вне git (публичный репо, CI).
    Нет файла → тест на живом каталоге пропускаем, а не роняем."""
    if not os.path.exists(FAYL_PO_UMOLCHANIYU):
        pytest.skip("прайс материалы/прайс/*.csv вне git — тест на живом каталоге пропущен")


@pytest.fixture(scope="module")
def katalog():
    _skip_bez_praysa()
    return iz_fayla_praysa()


@pytest.fixture(scope="module")
def svodki(katalog):
    return baseline.progon(katalog)


def _slovari_s_porchey(tmp_path, imya_fayla: str, portit) -> object:
    """Копия словарей во временную папку с испорченным одним файлом."""
    for f in os.listdir(KATALOG_DANNYH):
        if f.endswith(".json"):
            shutil.copy(os.path.join(KATALOG_DANNYH, f), tmp_path / f)
    put = tmp_path / imya_fayla
    d = json.loads(put.read_text(encoding="utf-8"))
    portit(d)
    put.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return zagruzit(str(tmp_path))


# ── Наборы на месте и разумны ────────────────────────────────────────────────


def test_nabory_chitayutsya():
    zolotoy = baseline._chitat_nabor(baseline.ZOLOTOY)
    slepoy = baseline._chitat_nabor(baseline.SLEPOY)
    absteyn = baseline._chitat_nabor(baseline.ABSTEYN)
    assert len(zolotoy) >= 30, "золотой набор должен покрывать не меньше 30 запросов"
    assert len(slepoy) >= 60
    assert len(absteyn) >= 15


def test_zolotoy_nabor_ssylaetsya_na_realnye_tovary(katalog):
    """Опечатка в названии товара внутри набора превратила бы его в вечный
    провал, который «чинят» правкой поиска. Имена сверяются с каталогом."""
    imena = {g.imya for g in katalog.gruppy}
    for e in baseline._chitat_nabor(baseline.ZOLOTOY):
        for pole in ("топ1", "в_топ5", "не_должно"):
            for imya in e.get(pole, []):
                assert imya in imena, f"{e['запрос']}: в поле {pole} нет такого товара — {imya}"


def test_slepoy_nabor_ssylaetsya_na_realnye_semeystva(katalog):
    semeystva = {g.family for g in katalog.gruppy}
    for e in baseline._chitat_nabor(baseline.SLEPOY):
        if "семейство" in e:
            assert e["семейство"] in semeystva, f"{e['запрос']}: нет семейства {e['семейство']}"


# ── Текущее состояние ────────────────────────────────────────────────────────


def test_vorota_etapa_proydeny(svodki):
    assert svodki["золотой"].top1 >= baseline.PORG_ZOLOTOY_TOP1
    assert svodki["абстейн"].top1 >= baseline.PORG_ABSTEYNA
    assert svodki["золотой"].narusheniy_zapreta == 0
    assert baseline.vorota_proydeny(svodki)


def test_otchet_soderzhit_cifry(svodki, tmp_path):
    put = baseline.sohranit(svodki, "тестовый каталог", str(tmp_path / "BASELINE.md"))
    tekst = open(put, encoding="utf-8").read()
    assert "top-1" in tekst and "золотой" in tekst and "абстейн" in tekst
    assert "тестовый каталог" in tekst


# ── Главное: харнесс ловит регресс ───────────────────────────────────────────


def test_porcha_slovarya_sortov_ronyaet_zolotoy(katalog, svodki, tmp_path):
    """Убираем маппинг «сорт Б» → «сорт В» — тот самый, без которого бот
    на «сорт б» отвечает «нет такого».

    Порог 90% эту потерю ПЕРЕЖИВАЕТ (два запроса из сорока одного), и это
    ровно причина, по которой у харнесса есть сравнение с прошлым замером:
    ворота молчат, дельта — нет.
    """
    sl = _slovari_s_porchey(
        tmp_path, "sinonimy_atributov.json",
        lambda d: d["сорт"].pop("б"))
    isporchennye = baseline.progon(katalog, sl)
    assert isporchennye["золотой"].top1 < svodki["золотой"].top1
    assert baseline.regressii(isporchennye, baseline.snimok(svodki))


def test_regress_lovitsya_cherez_sohranyonnyy_otchet(katalog, svodki, tmp_path):
    """Полный цикл, которым пользуются руками: сохранили замер → сломали
    словарь → следующий прогон читает отчёт и показывает просадку."""
    otchet = str(tmp_path / "BASELINE.md")
    baseline.sohranit(svodki, "каталог из файла", otchet)
    bylo = baseline.predydushchiy_snimok(otchet)
    assert bylo is not None and bylo["золотой_top1"] == svodki["золотой"].top1

    sl = _slovari_s_porchey(tmp_path, "sinonimy_atributov.json",
                            lambda d: d["сорт"].pop("б"))
    upavshie = {imya for imya, _, _ in baseline.regressii(baseline.progon(katalog, sl), bylo)}
    # Просело и попадание в топ-1, и запреты: без маппинга сорт в запросе
    # не разбирается, жёсткий фильтр не срабатывает — и на «вагонка сорт б»
    # в выдачу возвращается сорт А. Это и есть баг старого бота целиком.
    assert upavshie == {"золотой_top1", "нарушений"}


def test_bez_otchyota_sravnivat_ne_s_chem(tmp_path):
    assert baseline.predydushchiy_snimok(str(tmp_path / "нет-такого.md")) is None


def test_porcha_geyta_ronyaet_absteyn(katalog, tmp_path):
    """Опустошаем гейт чужого домена — «керамическая плитка для ванной»
    и «микроволновая печь» цепляются за наши семейства и дают выдачу."""
    def opustoshit(d):
        d["квалификаторы"] = []
        d["чужие_товары"] = []

    sl = _slovari_s_porchey(tmp_path, "chuzhoy_domen.json", opustoshit)
    isporchennye = baseline.progon(katalog, sl)
    assert isporchennye["абстейн"].top1 < baseline.PORG_ABSTEYNA
    assert not baseline.vorota_proydeny(isporchennye)


def test_porcha_semeystva_ronyaet_slepoy(katalog, tmp_path):
    """Выкидываем народные формы полка («полог», «доска на полок», «лежак») —
    слепой набор должен просесть: именно он ловит потерю разговорных форм."""
    def obrezat(d):
        d["полок"]["синонимы"] = []

    sl = _slovari_s_porchey(tmp_path, "slovar_svodnyy.json", obrezat)
    do = baseline.progon(katalog)
    posle = baseline.progon(katalog, sl)
    assert posle["слепой"].top1 < do["слепой"].top1
