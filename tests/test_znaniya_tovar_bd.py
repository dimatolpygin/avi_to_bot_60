# -*- coding: utf-8 -*-
"""Тесты этапа 19 Шаг 2 (проводка): блоки товарного из БД, сборка промпта в ядре,
горячая перезагрузка, сид.

Сети и БД нет: путь «из БД» — на фейковой сессии с очередью результатов, ядро —
на фабрике, отдающей фейковую сессию; сид — на сессии, копящей `add`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.config import Config, GoogleConfig, OpenRouterConfig, PgConfig
from bot.core import Yadro
from bot.pamyat import PamyatRedis
from bot.znaniya_tovar import bloki_iz_bd

STUB_KATALOG = SimpleNamespace(po_semeystvu={})


# ── Фейки ────────────────────────────────────────────────────────────────────

class _FakeScalars:
    def __init__(self, items): self._items = list(items)
    def all(self): return list(self._items)


class _SessBloki:
    """Сессия: scalar → id аккаунта, scalars → строки блоков (в порядке запросов)."""

    def __init__(self, account_id, bloki):
        self.account_id = account_id
        self._bloki = bloki

    async def scalar(self, _stmt):
        return self.account_id

    async def scalars(self, _stmt):
        return _FakeScalars(self._bloki)


def _fabrika(account_id, bloki):
    sess = _SessBloki(account_id, bloki)

    class _CM:
        async def __aenter__(self): return sess
        async def __aexit__(self, *a): return False

    return lambda: _CM()


def _row(key, content, sort=10):
    return SimpleNamespace(key=key, content=content, sort=sort, is_active=True)


def _cfg() -> Config:
    return Config(
        pg=PgConfig(host="h", port=1, user="u", password="p", database="d", schema="s"),
        redis_url="redis://x",
        openrouter=OpenRouterConfig(api_key="k", model="m", base_url="u"),
        log_level="info",
        google=GoogleConfig(creds_put="", tablica_id="t", list_name="l", interval_s=600),
        telegram_tokeny={"saunamart": "", "sbsauna": "", "sbsauna_deshman": ""},
    )


# ── bloki_iz_bd ──────────────────────────────────────────────────────────────

async def test_bloki_iz_bd_none_bez_akkaunta():
    assert await bloki_iz_bd(_SessBloki(None, []), "saunamart") is None


async def test_bloki_iz_bd_none_kogda_pusto():
    assert await bloki_iz_bd(_SessBloki(1, []), "saunamart") is None


async def test_bloki_iz_bd_vozvrat_karty_klyuch_tekst():
    rows = [_row("kontakty", "КОНТАКТЫ ИЗ БД"), _row("dostavka", "ДОСТАВКА ИЗ БД")]
    karta = await bloki_iz_bd(_SessBloki(1, rows), "saunamart")
    assert karta == {"kontakty": "КОНТАКТЫ ИЗ БД", "dostavka": "ДОСТАВКА ИЗ БД"}


# ── Сборка товарного промпта в ядре ──────────────────────────────────────────

async def test_sobrat_tovar_iz_bd_i_privetstvie():
    """Блоки из БД идут в промпт, приветствие из блока — в кеш; механика на месте."""
    yadro = Yadro(_cfg(), PamyatRedis(None))
    yadro._fabrika_sessiy = _fabrika(1, [
        _row("kontakty", "КОНТАКТЫ: Москва, Тестовая, 1."),
        _row("privetstvie", "Привет из БД!"),
    ])
    prompt = await yadro._sobrat_tovar("saunamart", STUB_KATALOG)
    assert "Москва, Тестовая, 1" in prompt
    assert "ЖЁСТКИЕ ПРАВИЛА ПО ДАННЫМ" in prompt   # механика из скелета
    assert "<<" not in prompt                       # маркеров не осталось
    assert yadro._privetstviya["saunamart"] == "Привет из БД!"


async def test_sobrat_tovar_folbek_kogda_bd_pusta():
    """Пустая БД → код-фолбэк (== монолит), приветствие из профиля."""
    yadro = Yadro(_cfg(), PamyatRedis(None))
    yadro._fabrika_sessiy = _fabrika(1, [])   # блоков нет
    prompt = await yadro._sobrat_tovar("saunamart", STUB_KATALOG)
    assert "Ты — Александра, менеджер розничных продаж" in prompt
    assert "202-54-97" in prompt                # дефолтные контакты
    from bot.profili import profil
    assert yadro._privetstviya["saunamart"] == profil("saunamart").privetstvie


async def test_sobrat_tovar_bez_fabriki_folbek():
    yadro = Yadro(_cfg(), PamyatRedis(None))  # _fabrika_sessiy = None
    prompt = await yadro._sobrat_tovar("saunamart", STUB_KATALOG)
    assert "Ты — Александра" in prompt


# ── Горячая перезагрузка товарного ───────────────────────────────────────────

async def test_perezagruzka_tovarnogo_menyaet_prompt():
    yadro = Yadro(_cfg(), PamyatRedis(None))
    yadro._poiski["saunamart"] = SimpleNamespace(katalog=STUB_KATALOG)
    yadro._prompty["saunamart"] = "СТАРЫЙ ПРОМПТ"
    yadro._fabrika_sessiy = _fabrika(1, [_row("kontakty", "НОВЫЕ КОНТАКТЫ ИЗ ВКЛАДКИ")])
    assert await yadro.perezagruzit_prompt_tovarnyy("saunamart") is True
    assert "НОВЫЕ КОНТАКТЫ ИЗ ВКЛАДКИ" in yadro._prompty["saunamart"]
    assert "ЖЁСТКИЕ ПРАВИЛА ПО ДАННЫМ" in yadro._prompty["saunamart"]


async def test_perezagruzka_tovarnogo_ne_zatiraet_na_pustoy_bd():
    """Пустая/сбойная БД при перезагрузке НЕ затирает рабочий промпт (в отличие
    от старта — там код-фолбэк)."""
    yadro = Yadro(_cfg(), PamyatRedis(None))
    yadro._poiski["saunamart"] = SimpleNamespace(katalog=STUB_KATALOG)
    yadro._prompty["saunamart"] = "РАБОЧИЙ ПРОМПТ"
    yadro._fabrika_sessiy = _fabrika(1, [])   # блоков нет
    assert await yadro.perezagruzit_prompt_tovarnyy("saunamart") is False
    assert yadro._prompty["saunamart"] == "РАБОЧИЙ ПРОМПТ"


async def test_perezagruzka_uslug_ne_beret_tovarnyy():
    """Диспетчеризация: путь услуг отказывает на товарном коде."""
    yadro = Yadro(_cfg(), PamyatRedis(None))
    assert await yadro.perezagruzit_prompt_uslug("saunamart") is False
    # и наоборот: товарный путь отказывает на услугах
    assert await yadro.perezagruzit_prompt_tovarnyy("sbsauna") is False


# ── Сид товарного ────────────────────────────────────────────────────────────

class _SessSeed:
    def __init__(self, est_klyuchi):
        self._est = est_klyuchi
        self.added = []

    async def scalars(self, _stmt):
        return _FakeScalars(self._est)

    def add(self, obj):
        self.added.append(obj)


async def test_seed_tovarnogo_zavodit_vse_bloki():
    from bot.seed_znaniya import _zaseyat_bloki
    from bot.znaniya_tovar import BLOKI_SAUNAMART
    s = _SessSeed(est_klyuchi=[])
    n = await _zaseyat_bloki(s, account_id=1, kod="saunamart", bloki=BLOKI_SAUNAMART)
    assert n == len(BLOKI_SAUNAMART) == 10
    assert {o.key for o in s.added} == {b.key for b in BLOKI_SAUNAMART}


async def test_seed_tovarnogo_idempotenten():
    from bot.seed_znaniya import _zaseyat_bloki
    from bot.znaniya_tovar import BLOKI_SAUNAMART
    est = [b.key for b in BLOKI_SAUNAMART]        # всё уже есть
    s = _SessSeed(est_klyuchi=est)
    n = await _zaseyat_bloki(s, account_id=1, kod="saunamart", bloki=BLOKI_SAUNAMART)
    assert n == 0 and s.added == []
