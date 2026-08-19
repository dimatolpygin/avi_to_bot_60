# -*- coding: utf-8 -*-
"""Тесты этапа 19 Шаг 2: разложение промпта ТОВАРНОГО аккаунта на блоки.

Ключевой инвариант — ИДЕНТИЧНОСТЬ: собранный из дефолтных блоков `SISTEMNY`
байт-в-байт совпадает с монолитом, что был до Шага 2 (golden-файл). Пока это
так, поведение бота Saunamart не изменилось, а редактируемость появилась.

Сети и БД нет: скелет и подстановка блоков — чистые функции.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bot.ai import agent
from bot.znaniya_tovar import (BLOKI_SAUNAMART, SISTEMNY_SHABLON, Blok,
                               podstavit_bloki)

GOLDEN = (Path(__file__).parent / "data" / "sistemny_saunamart.golden.txt").read_text(
    encoding="utf-8")

# Блоки, которые заказчик правит во вкладке «Saunamart (1)» (см. xlsx-первоисточник).
OZHIDAEMYE_KLYUCHI = ["persona", "privetstvie", "chto_prodaem", "sorta", "dostavka",
                      "upakovka", "skidki", "vozrazheniya", "faq", "kontakty"]


# ── Идентичность ─────────────────────────────────────────────────────────────

def test_sistemny_iz_blokov_identichen_monolitu():
    """Главный гейт Шага 2: сборка из дефолтных блоков == прежний монолит."""
    assert agent.SISTEMNY == GOLDEN


def test_sobrat_prompt_bez_blokov_identichen():
    """`sobrat_prompt(bloki=None)` — тот же путь, плейсхолдеры подставлены,
    маркеров блоков не осталось."""
    prompt = agent.sobrat_prompt(SimpleNamespace(po_semeystvu={}))
    assert "{АССОРТИМЕНТ}" not in prompt and "{ПРАВИЛА_СТИЛЯ}" not in prompt
    assert "<<" not in prompt
    # Персона и контакты — из блоков, механика — из скелета.
    assert "Ты — Александра, менеджер розничных продаж" in prompt
    assert "saunamart.ru" in prompt
    assert "ЖЁСТКИЕ ПРАВИЛА ПО ДАННЫМ" in prompt


# ── Состав и порядок блоков ──────────────────────────────────────────────────

def test_klyuchi_i_poryadok_kak_vo_vkladke():
    assert [b.key for b in BLOKI_SAUNAMART] == OZHIDAEMYE_KLYUCHI


def test_kazhdyy_blok_neset_svoy_marker_v_skelete():
    """Каждый блок (кроме приветствия) имеет свой `<<ключ>>` в скелете."""
    for b in BLOKI_SAUNAMART:
        if b.key == "privetstvie":
            assert "<<privetstvie>>" not in SISTEMNY_SHABLON  # приветствие не в промпте
        else:
            assert f"<<{b.key}>>" in SISTEMNY_SHABLON, b.key


def test_privetstvie_sovpadaet_s_profilem():
    """Блок privetstvie = стартовое сообщение профиля Saunamart (без рассинхрона)."""
    from bot.profili import profil
    privet = next(b.content for b in BLOKI_SAUNAMART if b.key == "privetstvie")
    assert privet == profil("saunamart").privetstvie


# ── Перекрытие блоков из БД ───────────────────────────────────────────────────

def test_bloki_iz_bd_perekryvayut_default():
    """Правка блока (как из вкладки) меняет промпт, механика остаётся."""
    prompt = agent.sobrat_prompt(
        SimpleNamespace(po_semeystvu={}),
        bloki={"kontakty": "КОНТАКТЫ: новый адрес, Москва, улица Тестовая, 1."})
    assert "Москва, улица Тестовая, 1" in prompt
    assert "202-54-97" not in prompt  # старый контакт-блок вытеснен правкой
    assert "ЖЁСТКИЕ ПРАВИЛА ПО ДАННЫМ" in prompt  # механика не пострадала
    assert "<<" not in prompt


def test_nepolnyy_nabor_blokov_dobiraetsya_iz_koda():
    """В БД только один блок — остальные из код-фолбэка, маркеров не остаётся."""
    prompt = agent.sobrat_prompt(
        SimpleNamespace(po_semeystvu={}), bloki={"dostavka": "Доставка бесплатная везде."})
    assert "Доставка бесплатная везде." in prompt
    assert "Ты — Александра" in prompt  # persona из дефолта
    assert "<<" not in prompt


def test_podstavit_bloki_ignoriruet_lishnie_klyuchi():
    out = podstavit_bloki("текст <<a>> хвост", {"a": "X", "b": "неиспользуемый"})
    assert out == "текст X хвост"
