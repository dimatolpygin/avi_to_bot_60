# -*- coding: utf-8 -*-
"""Очеловечивание рантайма (этап 9): дробление ответа, задержки, дебаунс,
прерываемость.

Слой сознательно ничего не знает о транспорте: диспетчер получает `Kanal`
с двумя колбэками («отправить реплику», «показать печатает»), и на этапе 10
в них подставляются методы aiogram, на этапе 14 — Авито Messenger API.
"""
from .razbivka import Tempo, razbit
from .dispetcher import Dispetcher, Kanal, Pamyat, PamyatVPamyati

__all__ = ["Tempo", "razbit", "Dispetcher", "Kanal", "Pamyat", "PamyatVPamyati"]
