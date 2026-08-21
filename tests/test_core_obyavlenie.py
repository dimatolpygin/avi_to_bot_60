# -*- coding: utf-8 -*-
"""Факт объявления в системном промпте (этап 14, подэтап 14.2).

Адаптер Авито кладёт объявление чата через `zapomnit_obyavlenie`, а ядро
подмешивает его в `sistemny` по ключу диалога. Проверяем без сети и БД.
"""
from __future__ import annotations

from bot.config import (Config, GoogleConfig, OpenRouterConfig, PgConfig)
from bot.core import Yadro, _fakt_obyavleniya
from bot.pamyat import PamyatRedis


def _yadro() -> Yadro:
    cfg = Config(
        pg=PgConfig(host="h", port=1, user="u", password="p", database="d", schema="s"),
        redis_url="redis://x",
        openrouter=OpenRouterConfig(api_key="k", model="m", base_url="u"),
        log_level="info",
        google=GoogleConfig(creds_put="", tablica_id="t", list_name="l", interval_s=600),
        telegram_tokeny={"sbsauna": ""},
    )
    return Yadro(cfg, PamyatRedis(None))


def test_fakt_beret_zagolovok_i_cenu():
    f = _fakt_obyavleniya({"title": "Отделка бани под ключ", "price_string": "от 74 000 ₽"})
    assert "Отделка бани под ключ" in f and "от 74 000 ₽" in f
    assert "объёмы по объявлению не считай" in f


def test_fakt_perebivaet_pustuyu_vydachu():
    """Активное объявление = товар в продаже: под ним бот НЕ говорит «нет в базе»
    и не обещает «уточню у поставщика», даже когда поиск пуст (жалоба заказчика
    про товары из объявлений, которых нет в каталоге — соль, трубы дымохода)."""
    f = _fakt_obyavleniya({"title": "Гималайская соль в ассортименте",
                           "price_string": "1 200 ₽"})
    assert "в продаже" in f
    # Явный запрет отшивающих формулировок и обещания «уточню у поставщика».
    assert "нет в прайсе" in f and "нет в базе" in f
    assert "уточню у поставщика" in f
    # И явная защита от выдумывания цен сверх объявления + гейт чужого профиля.
    assert "не выдумывай" in f and "не наш профиль" in f


def test_fakt_bez_zagolovka_pust():
    assert _fakt_obyavleniya({"price_string": "от 1 ₽"}) == ""


def test_sistemny_podmeshivaet_obyavlenie_po_klyuchu():
    ya = _yadro()
    ya._prompty["sbsauna"] = "БАЗОВЫЙ ПРОМПТ"
    ya.zapomnit_obyavlenie("sbsauna", "c1", {"title": "Отделка под ключ"})

    s_obyavl = ya._sistemny("sbsauna", "sbsauna:c1")
    s_bez = ya._sistemny("sbsauna", "sbsauna:c2")     # другой чат — факта нет

    assert "БАЗОВЫЙ ПРОМПТ" in s_obyavl and "Отделка под ключ" in s_obyavl
    assert s_bez == "БАЗОВЫЙ ПРОМПТ"


def test_zabyt_obyavlenie_snimaet_fakt():
    ya = _yadro()
    ya._prompty["sbsauna"] = "БАЗА"
    ya.zapomnit_obyavlenie("sbsauna", "c1", {"title": "Тест"})
    ya.zapomnit_obyavlenie("sbsauna", "c1", None)     # чат отвязался
    assert ya._sistemny("sbsauna", "sbsauna:c1") == "БАЗА"
