# -*- coding: utf-8 -*-
"""Тесты фоновой синхронизации каталога из Google-таблицы (этап 16, A4).

Сеть и БД не трогаем: `prochitat_google` и `zapisat_prays` подменяются. Проверяем
именно ЛОГИКУ синка — лестницу отказа (Google лёг → работаем на БД, не падаем),
вызов перезагрузки каталога только при реальных изменениях, и что цикл живёт
до сигнала остановки.
"""
import asyncio
import types

import pytest

from bot import sinhronizatsiya
from bot.config import GoogleConfig
from bot.etl.chtenie import OshibkaPraysa
from bot.etl.import_prays import Itogi

PRAYS = object()   # прайс здесь непрозрачен: его получает только подменённый zapisat_prays


def _cfg(vkl=True, interval=600):
    return types.SimpleNamespace(google=GoogleConfig(
        creds_put="/tmp/creds.json" if vkl else "",
        tablica_id="TID", list_name="Saunamart", interval_s=interval))


def _google_otdaet(prays=PRAYS):
    def _chitat(creds, tid, lst):
        return prays
    return _chitat


def _google_padaet(exc):
    def _chitat(creds, tid, lst):
        raise exc
    return _chitat


def _zapis_vernet(itogi):
    async def _zapisat(prays, kod, Sessiya):
        return itogi
    return _zapisat


# ── Выключенный синк ─────────────────────────────────────────────────────────

async def test_vyklyuchennyy_sink_ne_hodit_v_google(monkeypatch):
    """Пустой ключ = синк выключен: ни чтения, ни записи, штатный режим."""
    hodil = False

    def _chitat(*a):
        nonlocal hodil
        hodil = True
    monkeypatch.setattr(sinhronizatsiya, "prochitat_google", _chitat)

    r = await sinhronizatsiya.sinhronizirovat_odin_raz(_cfg(vkl=False), object())
    assert r is None and hodil is False


# ── Лестница отказа: источник или запись легли ───────────────────────────────

async def test_google_nedostupen_ne_padaet_i_ne_pishet(monkeypatch):
    """Google лёг → возвращаем None, БД не трогаем, наружу не бросаем."""
    monkeypatch.setattr(sinhronizatsiya, "prochitat_google",
                        _google_padaet(OshibkaPraysa("нет доступа")))
    pisali = False

    async def _zapisat(*a):
        nonlocal pisali
        pisali = True
    monkeypatch.setattr(sinhronizatsiya, "zapisat_prays", _zapisat)

    r = await sinhronizatsiya.sinhronizirovat_odin_raz(_cfg(), object())
    assert r is None and pisali is False


async def test_lyubaya_oshibka_chteniya_ne_valit_sink(monkeypatch):
    """Не только OshibkaPraysa: таймаут/битый ответ gspread тоже гасим в лог."""
    monkeypatch.setattr(sinhronizatsiya, "prochitat_google",
                        _google_padaet(RuntimeError("таймаут сети")))
    monkeypatch.setattr(sinhronizatsiya, "zapisat_prays", _zapis_vernet(Itogi()))
    r = await sinhronizatsiya.sinhronizirovat_odin_raz(_cfg(), object())
    assert r is None


async def test_oshibka_zapisi_v_bd_ne_valit_sink(monkeypatch):
    """Обрыв БД на записи → None, транзакция откатилась, цикл продолжится."""
    monkeypatch.setattr(sinhronizatsiya, "prochitat_google", _google_otdaet())

    async def _padaet(*a):
        raise RuntimeError("соединение с БД потеряно")
    monkeypatch.setattr(sinhronizatsiya, "zapisat_prays", _padaet)

    r = await sinhronizatsiya.sinhronizirovat_odin_raz(_cfg(), object())
    assert r is None


# ── Успех и перезагрузка каталога (колбэк A5) ────────────────────────────────

async def test_uspeshnyy_sink_vozvrashchaet_itogi(monkeypatch):
    monkeypatch.setattr(sinhronizatsiya, "prochitat_google", _google_otdaet())
    itogi = Itogi(vsego=201, obnovleno=3)
    monkeypatch.setattr(sinhronizatsiya, "zapisat_prays", _zapis_vernet(itogi))

    r = await sinhronizatsiya.sinhronizirovat_odin_raz(_cfg(), object())
    assert r is itogi


async def test_perezagruzka_zovetsya_tolko_pri_izmeneniyah(monkeypatch):
    """Каталог в память перегружаем, лишь когда запись что-то изменила —
    иначе на каждый холостой цикл дёргали бы пересборку промпта зря."""
    monkeypatch.setattr(sinhronizatsiya, "prochitat_google", _google_otdaet())
    monkeypatch.setattr(sinhronizatsiya, "zapisat_prays",
                        _zapis_vernet(Itogi(vsego=201, obnovleno=1)))
    zvali = 0

    async def _posle():
        nonlocal zvali
        zvali += 1

    await sinhronizatsiya.sinhronizirovat_odin_raz(_cfg(), object(), posle_zapisi=_posle)
    assert zvali == 1


async def test_bez_izmeneniy_perezagruzku_ne_zovem(monkeypatch):
    monkeypatch.setattr(sinhronizatsiya, "prochitat_google", _google_otdaet())
    monkeypatch.setattr(sinhronizatsiya, "zapisat_prays",
                        _zapis_vernet(Itogi(vsego=201, bez_izmeneniy=201)))
    zvali = 0

    async def _posle():
        nonlocal zvali
        zvali += 1

    await sinhronizatsiya.sinhronizirovat_odin_raz(_cfg(), object(), posle_zapisi=_posle)
    assert zvali == 0


async def test_padenie_perezagruzki_ne_valit_sink(monkeypatch):
    """Сбой горячей перезагрузки не должен ронять синк: БД уже обновлена."""
    monkeypatch.setattr(sinhronizatsiya, "prochitat_google", _google_otdaet())
    itogi = Itogi(vsego=201, obnovleno=1)
    monkeypatch.setattr(sinhronizatsiya, "zapisat_prays", _zapis_vernet(itogi))

    async def _posle():
        raise RuntimeError("пересборка промпта упала")

    r = await sinhronizatsiya.sinhronizirovat_odin_raz(_cfg(), object(), posle_zapisi=_posle)
    assert r is itogi   # запись состоялась, синк выжил


# ── Цикл ─────────────────────────────────────────────────────────────────────

async def test_cikl_ostanavlivaetsya_po_sobytiyu(monkeypatch):
    """Цикл делает прогоны, пока не выставлен stop, и завершается по нему."""
    progonov = 0

    async def _odin_raz(cfg, Sessiya, *, posle_zapisi=None):
        nonlocal progonov
        progonov += 1
        return None
    monkeypatch.setattr(sinhronizatsiya, "sinhronizirovat_odin_raz", _odin_raz)

    stop = asyncio.Event()
    # Интервал крошечный: несколько прогонов успеют пройти до остановки.
    zadacha = asyncio.create_task(
        sinhronizatsiya.cikl_sinhronizatsii(_cfg(interval=0), _cfg(), stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(zadacha, timeout=2)
    assert progonov >= 1


async def test_cikl_delaet_hotya_by_odin_progon_srazu(monkeypatch):
    """Первый прогон — сразу на старте, не дожидаясь интервала."""
    progony = asyncio.Event()

    async def _odin_raz(cfg, Sessiya, *, posle_zapisi=None):
        progony.set()
    monkeypatch.setattr(sinhronizatsiya, "sinhronizirovat_odin_raz", _odin_raz)

    stop = asyncio.Event()
    zadacha = asyncio.create_task(
        sinhronizatsiya.cikl_sinhronizatsii(_cfg(interval=999), _cfg(), stop))
    await asyncio.wait_for(progony.wait(), timeout=2)   # прогон был до интервала
    stop.set()
    await asyncio.wait_for(zadacha, timeout=2)
