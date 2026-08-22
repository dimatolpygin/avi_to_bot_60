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


def test_fakt_korotky_vopros_pro_eto_obyavlenie():
    """Короткий/общий вопрос под объявлением («что за товар?») бот трактует как
    вопрос ПРО ЭТОТ товар и не отвечает общим «что вас интересует?» (живой баг
    21.08: под «Окно для бани» на «что за товар такой?» бот дал общее приветствие)."""
    f = _fakt_obyavleniya({"title": "Окно для бани и парной", "price_string": "1 900 ₽"})
    assert "что за товар" in f            # общий вопрос перечислен как «про это объявление»
    assert "что вас интересует" in f      # ровно то, чего делать НЕ надо
    assert "смотрит именно этот товар" in f
    # Запрет подмены товара объявления другим из каталога (окно ≠ дверь).
    assert "замену из каталога не" in f and "окно это не дверь" in f


def test_fakt_cena_obyavleniya_avtoritetna():
    """Цена объявления не перебивается каталогом (живой баг 21.08: под «Гималайская
    соль, 242 ₽» бот тянул из каталога кирпич 385 и плитку 414 и объявлял 242
    «за прошлый период»)."""
    f = _fakt_obyavleniya({"title": "Гималайская соль в ассортименте. В наличии",
                           "price_string": "242 ₽"})
    assert "242 ₽" in f
    assert "действительная" in f            # цена объявления — действительная
    assert "противоречь объявлению" in f
    assert "за прошлый период" in f         # ровно та формулировка, что запрещена
    # Деталь, которой в объявлении нет (размер/объём), — уточнить, не брать из каталога.
    assert "не бери из каталога" in f


def test_fakt_neset_opisanie_iz_fida():
    """Когда объявление есть в индексе фида — его описание уходит в факт как источник
    правды (мессенджер описание не отдаёт; ответ про размер/объём — только отсюда)."""
    f = _fakt_obyavleniya(
        {"title": "Гималайская соль в ассортименте", "price_string": "242 ₽"},
        opisanie="Размеры: 20×10×2,5/5 см, 20×20×3,5 см. Прямые поставки.",
    )
    assert "ОПИСАНИЕ ЭТОГО ОБЪЯВЛЕНИЯ" in f
    assert "источник правды" in f and "бери ОТСЮДА" in f
    assert "20×10×2,5/5 см" in f            # реальные размеры долетели до промпта
    # Чего и в описании нет — уточнить, а не брать из каталога.
    assert "не выдумывай" in f


def test_fakt_bez_opisaniya_ne_dobavlyaet_blok():
    """Нет описания в индексе (обычный случай) → блок описания не добавляется,
    поведение прежнее (бот уточняет формат на вопрос о детали)."""
    f = _fakt_obyavleniya({"title": "Окно для бани", "price_string": "1 900 ₽"})
    assert "ОПИСАНИЕ ЭТОГО ОБЪЯВЛЕНИЯ" not in f


def test_fakt_bez_zagolovka_pust():
    assert _fakt_obyavleniya({"price_string": "от 1 ₽"}) == ""
    # Даже с описанием: нет заголовка — нет факта (объявление не идентифицировано).
    assert _fakt_obyavleniya({"price_string": "от 1 ₽"}, opisanie="что-то") == ""


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
