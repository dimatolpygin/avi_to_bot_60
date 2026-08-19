# -*- coding: utf-8 -*-
"""Тесты синка базы знаний услуг из Google-таблицы (этап 19, Шаг 1).

Три слоя, все без сети и БД:
- разбор вкладки (`razobrat_bloki`) — чистый, над сырыми строками;
- сверка вкладки с БД (`splanirovat`) — чистая логика синка (вставить/обновить/
  без изменений/сироты);
- оркестрация (`sinhronizirovat_znaniya_odin_raz`) — лестница отказа и колбэк
  перезагрузки, с подменёнными чтением и записью (как у теста синка каталога).
"""
import asyncio
import types

import pytest

from bot import sinhronizatsiya_znaniy as sz
from bot.config import GoogleConfig
from bot.etl.google_znaniya import (BlokStroka, OshibkaZnaniy, razobrat_bloki)


# ── Разбор вкладки ───────────────────────────────────────────────────────────

def _list_s_shapkoy(*data_rows):
    """Сырые строки как в выгрузке: заголовок листа, инструкция, шапка, данные."""
    return [
        ["Saunamart (1) — товары", "", "", "", ""],           # заголовок листа
        ["Правьте только столбец «Текст»…", "", "", "", ""],   # инструкция
        ["ключ_блока", "Раздел", "Текст", "Вкл", "Примечание"],  # шапка
        *data_rows,
    ]


def test_razbor_nahodit_shapku_i_chitaet_bloki():
    syrye = _list_s_shapkoy(
        ["dostavka", "Доставка", "По Краснодару 1000 р", "да", "⚠️ правьте"],
        ["kontakty", "Контакты", "saunamart.ru", "да", ""],
    )
    bloki = razobrat_bloki(syrye)
    assert [b.key for b in bloki] == ["dostavka", "kontakty"]
    assert bloki[0].title == "Доставка"
    assert bloki[0].content == "По Краснодару 1000 р"
    assert bloki[0].vkl is True


def test_razbor_propuskaet_zagolovok_i_instrukciyu():
    """Строки над шапкой (заголовок листа, инструкция) в блоки не попадают."""
    syrye = _list_s_shapkoy(["dostavka", "Доставка", "текст", "да", ""])
    bloki = razobrat_bloki(syrye)
    assert len(bloki) == 1 and bloki[0].key == "dostavka"


def test_vkl_da_net_pusto():
    syrye = _list_s_shapkoy(
        ["a", "A", "t", "да", ""],
        ["b", "B", "t", "нет", ""],
        ["c", "C", "t", "", ""],       # пусто = включено
    )
    vkl = {b.key: b.vkl for b in razobrat_bloki(syrye)}
    assert vkl == {"a": True, "b": False, "c": True}


def test_nekorrektnyy_klyuch_propuskaetsya():
    """Кириллический/с пробелом ключ — это не строка данных (памятка/мусор)."""
    syrye = _list_s_shapkoy(
        ["Доставка", "x", "t", "да", ""],     # кириллица — пропуск
        ["dostavka", "Доставка", "t", "да", ""],
    )
    bloki = razobrat_bloki(syrye)
    assert [b.key for b in bloki] == ["dostavka"]


def test_pamyatka_sboku_ne_popadaet_v_bloki():
    """Памятка справа (пустой столбец ключа, текст в колонке G) — не блок."""
    syrye = _list_s_shapkoy(
        ["dostavka", "Доставка", "t", "да", "", "", "ПАМЯТКА — что можно менять"],
        ["", "", "", "", "", "", "• Столбец «Текст» можно править"],
    )
    bloki = razobrat_bloki(syrye)
    assert [b.key for b in bloki] == ["dostavka"]


def test_dubl_klyucha_beret_pervyy():
    syrye = _list_s_shapkoy(
        ["a", "Первый", "t1", "да", ""],
        ["a", "Второй", "t2", "да", ""],
    )
    bloki = razobrat_bloki(syrye)
    assert len(bloki) == 1 and bloki[0].title == "Первый"


def test_net_shapki_oshibka():
    with pytest.raises(OshibkaZnaniy):
        razobrat_bloki([["что-то", "без", "шапки"]])


def test_kolonki_po_imeni_a_ne_pozicii():
    """Заказчик вставил столбец слева — колонки находятся по именам шапки."""
    syrye = [
        ["", "ключ_блока", "Раздел", "Текст", "Вкл"],
        ["", "dostavka", "Доставка", "1000 р", "да"],
    ]
    bloki = razobrat_bloki(syrye)
    assert bloki[0].key == "dostavka" and bloki[0].content == "1000 р"


# ── Сверка вкладки с БД (чистая логика синка) ────────────────────────────────

def _bd(title, content, sort, is_active=True):
    return types.SimpleNamespace(title=title, content=content, sort=sort, is_active=is_active)


def test_novyy_klyuch_vo_vstavku():
    plan = sz.splanirovat({}, [BlokStroka("dostavka", "Доставка", "t", True)])
    assert len(plan.vstavit) == 1 and not plan.obnovit
    assert plan.vstavit[0][1] == 10   # sort = i*10


def test_izmenivshiysya_blok_v_obnovlenie():
    est = {"dostavka": _bd("Доставка", "старый", 10, True)}
    plan = sz.splanirovat(est, [BlokStroka("dostavka", "Доставка", "новый", True)])
    assert len(plan.obnovit) == 1 and not plan.vstavit
    assert plan.bez_izmeneniy == 0


def test_neizmennyy_blok_bez_izmeneniy():
    est = {"dostavka": _bd("Доставка", "тот же", 10, True)}
    plan = sz.splanirovat(est, [BlokStroka("dostavka", "Доставка", "тот же", True)])
    assert plan.bez_izmeneniy == 1 and not plan.vstavit and not plan.obnovit


def test_pereklyuchenie_vkl_eto_izmenenie():
    est = {"dostavka": _bd("Доставка", "t", 10, is_active=True)}
    plan = sz.splanirovat(est, [BlokStroka("dostavka", "Доставка", "t", vkl=False)])
    assert len(plan.obnovit) == 1


def test_sirota_v_bd_no_ne_vo_vkladke():
    est = {"dostavka": _bd("Доставка", "t", 10, True),
           "staryy": _bd("Старый", "t", 20, True)}
    plan = sz.splanirovat(est, [BlokStroka("dostavka", "Доставка", "t", True)])
    assert plan.siroty == ["staryy"]


def test_vyklyuchennaya_sirota_ne_schitaetsya():
    """Уже выключенный блок, пропавший из вкладки, сиротой не считаем (шум)."""
    est = {"vykl": _bd("Выкл", "t", 10, is_active=False)}
    plan = sz.splanirovat(est, [])
    assert plan.siroty == []


# ── Оркестрация: лестница отказа и колбэк перезагрузки ───────────────────────

def _cfg(vkl=True, interval=600):
    return types.SimpleNamespace(google=GoogleConfig(
        creds_put="/tmp/creds.json" if vkl else "",
        tablica_id="TID", list_name="Saunamart", interval_s=interval))


def _chitalka(bloki):
    def _ch(creds, tid, lst):
        return bloki
    return _ch


def _zapis(itogi):
    async def _z(vkladka, kod, Sessiya):
        return itogi
    return _z


async def test_vyklyuchennyy_sink_nichego_ne_delaet(monkeypatch):
    hodil = False

    def _ch(*a):
        nonlocal hodil
        hodil = True
    monkeypatch.setattr(sz, "prochitat_znaniya", _ch)
    r = await sz.sinhronizirovat_znaniya_odin_raz(_cfg(vkl=False), object(), ["sbsauna"])
    assert r == {} and hodil is False


async def test_akkaunt_bez_vkladki_propuskaetsya(monkeypatch):
    monkeypatch.setattr(sz, "prochitat_znaniya", _chitalka([BlokStroka("a", "A", "t")]))
    monkeypatch.setattr(sz, "zapisat_znaniya", _zapis(sz.ItogiZnaniy()))
    r = await sz.sinhronizirovat_znaniya_odin_raz(_cfg(), object(), ["saunamart"])
    assert r == {}   # у товарного вкладки знаний нет (Шаг 1)


async def test_google_lyog_vozvrat_none_bez_perezagruzki(monkeypatch):
    def _padaet(*a):
        raise OshibkaZnaniy("нет доступа")
    monkeypatch.setattr(sz, "prochitat_znaniya", _padaet)
    zvali = []

    async def _posle(kod):
        zvali.append(kod)
    r = await sz.sinhronizirovat_znaniya_odin_raz(
        _cfg(), object(), ["sbsauna"], posle_zapisi=_posle)
    assert r == {"sbsauna": None} and zvali == []


async def test_izmeneniya_zovut_perezagruzku_s_kodom(monkeypatch):
    monkeypatch.setattr(sz, "prochitat_znaniya", _chitalka([BlokStroka("a", "A", "t")]))
    monkeypatch.setattr(sz, "zapisat_znaniya", _zapis(sz.ItogiZnaniy(obnovleno=1)))
    zvali = []

    async def _posle(kod):
        zvali.append(kod)
    await sz.sinhronizirovat_znaniya_odin_raz(
        _cfg(), object(), ["sbsauna"], posle_zapisi=_posle)
    assert zvali == ["sbsauna"]


async def test_bez_izmeneniy_perezagruzku_ne_zovem(monkeypatch):
    monkeypatch.setattr(sz, "prochitat_znaniya", _chitalka([BlokStroka("a", "A", "t")]))
    monkeypatch.setattr(sz, "zapisat_znaniya", _zapis(sz.ItogiZnaniy(bez_izmeneniy=12)))
    zvali = []

    async def _posle(kod):
        zvali.append(kod)
    await sz.sinhronizirovat_znaniya_odin_raz(
        _cfg(), object(), ["sbsauna"], posle_zapisi=_posle)
    assert zvali == []


async def test_odin_akkaunt_upal_drugoy_sinknulsya(monkeypatch):
    """Сбой чтения одного аккаунта не мешает синку соседнего."""
    def _ch(creds, tid, lst):
        if lst == "SB SAUNA":
            raise OshibkaZnaniy("лёг")
        return [BlokStroka("a", "A", "t")]
    monkeypatch.setattr(sz, "prochitat_znaniya", _ch)
    monkeypatch.setattr(sz, "zapisat_znaniya", _zapis(sz.ItogiZnaniy(obnovleno=1)))
    r = await sz.sinhronizirovat_znaniya_odin_raz(
        _cfg(), object(), ["sbsauna", "sbsauna_deshman"])
    assert r["sbsauna"] is None
    assert r["sbsauna_deshman"] is not None and r["sbsauna_deshman"].izmenilos


async def test_padenie_perezagruzki_ne_valit_sink(monkeypatch):
    monkeypatch.setattr(sz, "prochitat_znaniya", _chitalka([BlokStroka("a", "A", "t")]))
    monkeypatch.setattr(sz, "zapisat_znaniya", _zapis(sz.ItogiZnaniy(dobavleno=1)))

    async def _posle(kod):
        raise RuntimeError("пересборка промпта упала")
    r = await sz.sinhronizirovat_znaniya_odin_raz(
        _cfg(), object(), ["sbsauna"], posle_zapisi=_posle)
    assert r["sbsauna"].dobavleno == 1   # запись состоялась, синк выжил


# ── Цикл ─────────────────────────────────────────────────────────────────────

async def test_cikl_ostanavlivaetsya_po_sobytiyu(monkeypatch):
    progonov = 0

    async def _odin(cfg, Sessiya, kody, *, posle_zapisi=None):
        nonlocal progonov
        progonov += 1
        return {}
    monkeypatch.setattr(sz, "sinhronizirovat_znaniya_odin_raz", _odin)

    stop = asyncio.Event()
    zadacha = asyncio.create_task(
        sz.cikl_sinhronizatsii_znaniy(_cfg(interval=0), object(), stop, ["sbsauna"]))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(zadacha, timeout=2)
    assert progonov >= 1
