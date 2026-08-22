# -*- coding: utf-8 -*-
"""Санация и загрузка индекса описаний из фида автозагрузки Авито (этап 14).

Разбор самого xlsx не тестируем (файл-фид в репозиторий не кладём — он большой и
принадлежит аккаунту заказчика); проверяем санацию текста и слияние JSON-индексов,
на которых держится вся ценность: точные размеры доходят до бота, а телефоны/CTA — нет.
"""
from __future__ import annotations

import json

from bot.etl.opisaniya_feed import _sanirovat, zagruzit_indeks


def test_sanirovat_snimaet_html_i_sohranyaet_razmery():
    raw = ("<p><strong>Окно для бани</strong></p>"
           "<p>Стандартные размеры (мм): 300x400, 500x600.<br/>"
           "Материал: липа камерной сушки.</p>")
    s = _sanirovat(raw)
    assert "<" not in s and ">" not in s          # HTML срезан
    assert "300x400" in s and "500x600" in s      # размеры сохранены
    assert "липа камерной сушки" in s


def test_sanirovat_vykidyvaet_telefony_i_cta():
    raw = ("Гималайская соль. Размеры: 20×10 см.\n"
           "☎ ЗВОНИТЕ И ДЕЛАЙТЕ ЗАКАЗ ПРЯМО СЕЙЧАС\n"
           "=================================\n"
           "Тел: +7 938 433-70-21\n"
           "Пишите в WhatsApp 89384337021")
    s = _sanirovat(raw)
    assert "20×10 см" in s                         # продуктовая суть осталась
    assert "☎" not in s                            # телефонный знак убран
    assert "938" not in s and "89384337021" not in s   # телефоны убраны
    assert "WhatsApp" not in s and "ЗВОНИТЕ" not in s


def test_sanirovat_podrezaet_dlinnoe():
    raw = "А" * 5000
    s = _sanirovat(raw)
    assert len(s) <= 1001 and s.endswith("…")


def test_zagruzit_indeks_slivaet_fayly_i_beret_opisanie(tmp_path):
    (tmp_path / "opisaniya_saunamart.json").write_text(
        json.dumps({"111": {"title": "Соль", "opisanie": "Размеры 20×10"},
                    "222": {"title": "Пусто", "opisanie": ""}}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "opisaniya_sbsauna.json").write_text(
        json.dumps({"333": {"title": "Окно", "opisanie": "Липа"}}, ensure_ascii=False),
        encoding="utf-8")
    idx = zagruzit_indeks(str(tmp_path))
    assert idx["111"] == "Размеры 20×10"
    assert idx["333"] == "Липа"            # слились оба файла (item_id уникален)
    assert "222" not in idx                # пустое описание в индекс не попадает


def test_zagruzit_indeks_bez_faylov_pust(tmp_path):
    assert zagruzit_indeks(str(tmp_path)) == {}
