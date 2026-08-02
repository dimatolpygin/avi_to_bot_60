# -*- coding: utf-8 -*-
"""Тесты этапа 11: база знаний услуг.

Сети и БД нет: чистый ассемблер проверяется напрямую, путь «из БД» — на
фейковой сессии с очередью результатов, фолбэк ядра — на падающей фабрике.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.config import Config, GoogleConfig, OpenRouterConfig, PgConfig
from bot.core import Yadro
from bot.pamyat import PamyatRedis
from bot.znaniya import (Blok, bloki_akkaunta, prompt_iz_bd, prompt_iz_koda,
                         sobrat)


# ── Наборы блоков на аккаунт ─────────────────────────────────────────────────

def test_deshman_neset_prodazhu_materialov_a_sbsauna_net():
    """Ключевое различие направлений: бюджетный продаёт материалы отдельно."""
    klyuchi_sbsauna = {b.key for b in bloki_akkaunta("sbsauna")}
    klyuchi_deshman = {b.key for b in bloki_akkaunta("sbsauna_deshman")}
    assert "materialy_otdelno" in klyuchi_deshman
    assert "materialy_otdelno" not in klyuchi_sbsauna
    assert "garantiya_ceny" in klyuchi_deshman and "garantiya_ceny" not in klyuchi_sbsauna


def test_obshchie_bloki_est_u_oboih_akkauntov():
    for kod in ("sbsauna", "sbsauna_deshman"):
        klyuchi = {b.key for b in bloki_akkaunta(kod)}
        assert {"usluga", "chto_vhodit", "chego_ne_delaem", "geografiya",
                "chego_ne_znaesh", "kak_rabotat"} <= klyuchi, kod


def test_vedenie_est_u_oboih():
    """Ведение к сделке — общий блок услуг обоих аккаунтов."""
    for kod in ("sbsauna", "sbsauna_deshman"):
        assert "vedenie" in {b.key for b in bloki_akkaunta(kod)}, kod
        p = prompt_iz_koda(kod, "SB SAUNA")
        assert "двигает разговор вперёд" in p and "справочник" in p, kod


def test_kontakty_bez_levogo_sayta():
    """Контакты у обоих; сайт только правильный, левого «(sbsauna.ru)» из шапки
    персоны больше нет — из-за него бот выдавал клиенту чужой адрес."""
    for kod in ("sbsauna", "sbsauna_deshman"):
        assert "kontakty" in {b.key for b in bloki_akkaunta(kod)}, kod
        p = prompt_iz_koda(kod, "SB SAUNA")
        assert "saunayug.ru" in p and "9:00" in p, kod
        assert "(sbsauna.ru)" not in p, kod


def test_oplata_u_oboih_pechi_tolko_u_deshmana():
    """Оплата (нал/безнал, физ+юр) — общий факт обеих услуг; печи с Harvia — только
    у Дешмана. Новые блоки стоят В КОНЦЕ, чтобы пересев добавил их без коллизии sort."""
    assert bloki_akkaunta("sbsauna")[-1].key == "oplata"
    assert bloki_akkaunta("sbsauna_deshman")[-1].key == "pechi"
    for kod in ("sbsauna", "sbsauna_deshman"):
        p = prompt_iz_koda(kod, "SB SAUNA")
        assert "наличный и безналичный" in p and "физлицами" in p, kod
    assert "Harvia" in prompt_iz_koda("sbsauna_deshman", "SB SAUNA")
    assert "Harvia" not in prompt_iz_koda("sbsauna", "SB SAUNA")


def test_neizvestnyy_akkaunt_padaet_ponyatno():
    with pytest.raises(KeyError, match="нет базы знаний"):
        bloki_akkaunta("saunamart")


# ── Сборка кодом (фолбэк) ────────────────────────────────────────────────────

@pytest.mark.parametrize("kod", ["sbsauna", "sbsauna_deshman"])
def test_prompt_iz_koda_neset_geo_stoplist_i_personu(kod):
    p = prompt_iz_koda(kod, "SB SAUNA")
    assert "ЮФО" in p and "СКФО" in p and "Крым" in p          # гео
    assert "ЧЕГО МЫ НЕ ДЕЛАЕМ" in p                            # стоп-лист
    assert "Роман" in p                                        # персона
    # Правила стиля приклеены и в мужском роде (персона — Роман).
    assert "длинных тире" in p and "записал" in p


def test_orientiry_raznye_u_dvuh_napravleniy():
    assert "500 тысяч" in prompt_iz_koda("sbsauna", "SB SAUNA")
    assert "350 тысяч" in prompt_iz_koda("sbsauna_deshman", "SB SAUNA")
    assert "не продаём" in prompt_iz_koda("sbsauna", "SB SAUNA")
    assert "МАТЕРИАЛЫ ОТДЕЛЬНО мы продать можем" in prompt_iz_koda("sbsauna_deshman", "SB SAUNA")


def test_deshman_akcent_na_cene_i_skidka_pod_svoi_brigady():
    """Замечание заказчика: у Дешмана не было акцента на «дешман» и оффера материала
    со скидкой 20% для клиентов со своими монтажниками. Оффер — только у бюджетного."""
    p = prompt_iz_koda("sbsauna_deshman", "SB SAUNA")
    assert "низкие цены" in p
    assert "20%" in p and "свои" in p.lower()
    assert "20%" not in prompt_iz_koda("sbsauna", "SB SAUNA")


def test_pravka_bloka_menyaet_sobrannyy_prompt():
    """Критерий этапа на уровне ассемблера: текст блока определяет ответ."""
    bloki = bloki_akkaunta("sbsauna")
    bazovyy = sobrat("шапка", "Роман", "SB SAUNA", bloki)

    metka = "ГЕО ПРАВИМ: работаем теперь только по Краснодарскому краю"
    izmenennye = [Blok(b.key, b.title, metka) if b.key == "geografiya" else b
                  for b in bloki]
    pravlenyy = sobrat("шапка", "Роман", "SB SAUNA", izmenennye)

    assert metka in pravlenyy
    assert metka not in bazovyy
    assert "ЮФО" not in pravlenyy          # старый текст гео ушёл
    assert pravlenyy != bazovyy


# ── Сборка из БД ─────────────────────────────────────────────────────────────

class _FakeScalars:
    def __init__(self, items): self._items = list(items)
    def all(self): return list(self._items)
    def first(self): return self._items[0] if self._items else None


class _FakeSessiya:
    """Отдаёт заготовленные результаты в том порядке, в каком их просит
    `prompt_iz_bd`: сначала scalar(id аккаунта), потом scalars(блоки),
    потом scalars(персона)."""

    def __init__(self, account_id, bloki, ap):
        self.account_id = account_id
        self._ocheredi = [list(bloki), [ap] if ap is not None else []]
        self._i = 0

    async def scalar(self, _stmt):
        return self.account_id

    async def scalars(self, _stmt):
        rez = self._ocheredi[self._i]
        self._i += 1
        return _FakeScalars(rez)


def _blok_row(key, content, sort):
    return SimpleNamespace(key=key, title=key, content=content, sort=sort)


async def test_iz_bd_none_kogda_net_akkaunta():
    s = _FakeSessiya(None, [], None)
    assert await prompt_iz_bd(s, "sbsauna", "SB SAUNA") is None


async def test_iz_bd_none_kogda_bloki_pusty():
    """Незасеянная БД не гасит бота — вызывающий падает на код-фолбэк."""
    s = _FakeSessiya(1, [], None)
    assert await prompt_iz_bd(s, "sbsauna", "SB SAUNA") is None


async def test_iz_bd_sobiraet_iz_blokov_i_shapki():
    ap = SimpleNamespace(body="ШАПКА ИЗ БД", persona="Роман")
    bloki = [_blok_row("geografiya", "ГЕО ИЗ БД", 10),
             _blok_row("orientir_cena", "ОРИЕНТИР ИЗ БД", 20)]
    p = await prompt_iz_bd(_FakeSessiya(1, bloki, ap), "sbsauna", "SB SAUNA")
    assert p is not None
    assert "ШАПКА ИЗ БД" in p and "ГЕО ИЗ БД" in p and "ОРИЕНТИР ИЗ БД" in p
    assert "длинных тире" in p          # правила стиля приклеены и здесь


async def test_iz_bd_pravka_bloka_menyaet_otvet():
    ap = SimpleNamespace(body="ШАПКА", persona="Роман")
    do = await prompt_iz_bd(
        _FakeSessiya(1, [_blok_row("geografiya", "ГЕО СТАРОЕ", 10)], ap),
        "sbsauna", "SB SAUNA")
    posle = await prompt_iz_bd(
        _FakeSessiya(1, [_blok_row("geografiya", "ГЕО НОВОЕ", 10)], ap),
        "sbsauna", "SB SAUNA")
    assert "ГЕО СТАРОЕ" in do and "ГЕО СТАРОЕ" not in posle
    assert "ГЕО НОВОЕ" in posle and do != posle


# ── Фолбэк ядра при недоступной БД ───────────────────────────────────────────

def _cfg() -> Config:
    return Config(
        pg=PgConfig(host="h", port=1, user="u", password="p", database="d", schema="s"),
        redis_url="redis://x",
        openrouter=OpenRouterConfig(api_key="k", model="m", base_url="u"),
        log_level="info",
        google=GoogleConfig(creds_put="", tablica_id="t", list_name="l", interval_s=600),
        telegram_tokeny={"saunamart": "", "sbsauna": "", "sbsauna_deshman": ""},
    )


def _padayushchaya_fabrika():
    """Фабрика сессий, чья сессия падает на входе, — имитация лежащей БД."""
    class _CM:
        async def __aenter__(self):
            raise RuntimeError("БД легла")
        async def __aexit__(self, *a):
            return False
    return lambda: _CM()


async def test_yadro_beret_kod_folbek_kogda_bd_nedostupna():
    yadro = Yadro(_cfg(), PamyatRedis(None))
    await yadro.podgotovit(["sbsauna"], fabrika_sessiy=_padayushchaya_fabrika())
    p = yadro._prompty["sbsauna"]
    assert "Роман" in p and "500 тысяч" in p and "ЮФО" in p
