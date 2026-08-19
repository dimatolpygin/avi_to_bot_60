# -*- coding: utf-8 -*-
"""Тесты рубильника ответов из листа-пульта Google (запрос заказчика 19.08).

Сети нет: разбор листа — чистая функция; синк — на подменённом `prochitat_rubilnik`;
ядро — прямым вызовом `ustanovit_rubilnik`/`otvechaet`.
"""
from __future__ import annotations

import pytest

from bot.config import Config, GoogleConfig, OpenRouterConfig, PgConfig
from bot.core import Yadro
from bot.pamyat import PamyatRedis
from bot.etl.google_rubilnik import (OshibkaRubilnika, prochitat_rubilnik,
                                     razobrat_rubilnik)


# ── Разбор листа ─────────────────────────────────────────────────────────────

def test_razobrat_bazovyy():
    syrye = [
        ["Пульт ботов — да/нет"],
        ["код", "бот", "отвечает"],
        ["saunamart", "Saunamart (товары)", "да"],
        ["sbsauna", "SB SAUNA", "нет"],
        ["sbsauna_deshman", "Дешман", ""],           # пусто = отвечает
    ]
    assert razobrat_rubilnik(syrye) == {
        "saunamart": True, "sbsauna": False, "sbsauna_deshman": True}


def test_razobrat_sinonimy_kolonok_i_znacheniy():
    syrye = [["аккаунт", "вкл"], ["sbsauna", "off"], ["saunamart", "1"]]
    assert razobrat_rubilnik(syrye) == {"sbsauna": False, "saunamart": True}


def test_razobrat_bez_shapki_oshibka():
    with pytest.raises(OshibkaRubilnika):
        razobrat_rubilnik([["ничего", "полезного"], ["sbsauna", "да"]])


def test_razobrat_propuskaet_krivoy_kod_i_dubli():
    syrye = [["код", "отвечает"],
             ["", "нет"],                 # пустой код — мимо
             ["не кириллица", "нет"],     # некорректный ключ — мимо
             ["sbsauna", "нет"],
             ["sbsauna", "да"]]           # дубль — берём первый (нет)
    assert razobrat_rubilnik(syrye) == {"sbsauna": False}


def test_prochitat_zavorachivaet_setevuyu_oshibku(monkeypatch):
    # _syrye_stroki_google падает OshibkaPraysa (нет ключа/доступа) — пробрасываем как есть.
    from bot.etl import google_rubilnik
    from bot.etl.google_prays import OshibkaPraysa

    def _bum(*a, **k):
        raise OshibkaPraysa("нет доступа")

    monkeypatch.setattr(google_rubilnik, "_syrye_stroki_google", _bum)
    with pytest.raises(OshibkaPraysa):
        prochitat_rubilnik("k", "t", "Рубильник")


# ── Ядро: флаг и применение ──────────────────────────────────────────────────

def _yadro() -> Yadro:
    cfg = Config(
        pg=PgConfig(host="h", port=1, user="u", password="p", database="d", schema="s"),
        redis_url="redis://x",
        openrouter=OpenRouterConfig(api_key="k", model="m", base_url="u"),
        log_level="info",
        google=GoogleConfig(creds_put="", tablica_id="t", list_name="l", interval_s=600),
        telegram_tokeny={"saunamart": "", "sbsauna": "", "sbsauna_deshman": ""},
    )
    return Yadro(cfg, PamyatRedis(None))


def test_otvechaet_default_true():
    assert _yadro().otvechaet("sbsauna") is True     # нет записи → отвечает


def test_ustanovit_rubilnik_glushit_i_vklyuchaet():
    ya = _yadro()
    ya.ustanovit_rubilnik({"sbsauna": False, "saunamart": True})
    assert ya.otvechaet("sbsauna") is False
    assert ya.otvechaet("saunamart") is True
    assert ya.otvechaet("sbsauna_deshman") is True   # не упомянут → дефолт
    ya.ustanovit_rubilnik({"sbsauna": True})         # включили обратно
    assert ya.otvechaet("sbsauna") is True


# ── Синк ─────────────────────────────────────────────────────────────────────

def _cfg(creds="x", rubilnik=True) -> Config:
    return Config(
        pg=PgConfig(host="h", port=1, user="u", password="p", database="d", schema="s"),
        redis_url="redis://x",
        openrouter=OpenRouterConfig(api_key="k", model="m", base_url="u"),
        log_level="info",
        google=GoogleConfig(creds_put=creds, tablica_id="t", list_name="l",
                            interval_s=600, rubilnik=rubilnik, list_rubilnik="Рубильник"),
        telegram_tokeny={"saunamart": "", "sbsauna": "", "sbsauna_deshman": ""},
    )


async def test_sink_primenyaet_tolko_podnyatye(monkeypatch):
    from bot import sinhronizatsiya_rubilnika as sr

    monkeypatch.setattr(sr, "prochitat_rubilnik",
                        lambda *a, **k: {"sbsauna": False, "chuzhoy": True})
    primeneno = {}
    tolko = await sr.sinhronizirovat_rubilnik_odin_raz(
        _cfg(), ["sbsauna", "saunamart"], primeneno.update)
    # chuzhoy отфильтрован (не поднят), saunamart в листе нет → его не трогаем.
    assert tolko == {"sbsauna": False}
    assert primeneno == {"sbsauna": False}


async def test_sink_vyklyuchen_bez_kredov():
    from bot import sinhronizatsiya_rubilnika as sr
    assert await sr.sinhronizirovat_rubilnik_odin_raz(
        _cfg(creds=""), ["sbsauna"], lambda k: None) is None


async def test_sink_sbrosa_ne_delaet_pri_sboe(monkeypatch):
    # Лист недоступен → primenit НЕ зовём (состояние ботов остаётся прежним).
    from bot import sinhronizatsiya_rubilnika as sr

    def _bum(*a, **k):
        raise RuntimeError("нет сети")

    monkeypatch.setattr(sr, "prochitat_rubilnik", _bum)
    zvano = []
    rez = await sr.sinhronizirovat_rubilnik_odin_raz(
        _cfg(), ["sbsauna"], lambda k: zvano.append(k))
    assert rez is None and zvano == []
