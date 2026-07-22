# -*- coding: utf-8 -*-
"""Тесты каркаса (этап 2): конфиг и сквозной request-id.

БД и Redis тут не нужны — их живая проверка в `python -m bot.proverka`.
"""
import asyncio

import pytest

from bot.config import OshibkaKonfiga, PgConfig, load_config, must
from bot.logger import nachat_zapros, tekushchiy_akkaunt, tekushchiy_rid


def test_must_padaet_s_russkim_soobshcheniem(monkeypatch):
    monkeypatch.delenv("PROVERKA_PEREMENNAYA", raising=False)
    with pytest.raises(OshibkaKonfiga) as e:
        must("PROVERKA_PEREMENNAYA", zachem="для теста")
    assert "PROVERKA_PEREMENNAYA" in str(e.value)
    assert "для теста" in str(e.value)


def test_must_ne_prinimaet_probely(monkeypatch):
    monkeypatch.setenv("PROVERKA_PEREMENNAYA", "   ")
    with pytest.raises(OshibkaKonfiga):
        must("PROVERKA_PEREMENNAYA")


def test_pgport_ne_chislo_padaet(monkeypatch):
    monkeypatch.setenv("PGPORT", "пять тысяч")
    monkeypatch.setenv("PGPASSWORD", "x")
    monkeypatch.setenv("PGDATABASE", "sbavito")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/3")
    with pytest.raises(OshibkaKonfiga) as e:
        load_config()
    assert "PGPORT" in str(e.value)


def test_parol_ne_utekaet_v_log():
    pg = PgConfig(host="h", port=5432, user="postgres", password="sekret",
                  database="sbavito", schema="sbavito")
    assert "sekret" in pg.dsn            # для драйвера пароль нужен
    assert "sekret" not in pg.bez_parolya()  # а в лог он не попадает


def test_rid_svoy_u_kazhdogo_soobshcheniya():
    with nachat_zapros("saunamart") as rid1:
        assert tekushchiy_rid() == rid1
        assert tekushchiy_akkaunt() == "saunamart"
    with nachat_zapros("sbsauna") as rid2:
        assert tekushchiy_rid() == rid2
    assert rid1 != rid2
    assert tekushchiy_rid() == "—"       # вне обработки — прочерк


def test_zadannyy_rid_ispolzuetsya():
    """Транспорт может принести свой id (например, из вебхука Авито)."""
    with nachat_zapros("sbsauna_deshman", rid="avito123") as rid:
        assert rid == "avito123"
        assert tekushchiy_rid() == "avito123"


async def test_rid_ne_smeshivaetsya_mezhdu_zadachami():
    """Два клиента пишут одновременно — их логи не должны слипнуться.
    Это то, ради чего в логгере contextvars, а не глобальная переменная."""
    sobrano: list[tuple[str, str]] = []

    async def dialog(akkaunt: str, pauza: float) -> None:
        with nachat_zapros(akkaunt) as rid:
            await asyncio.sleep(pauza)          # переключение задач посередине
            sobrano.append((tekushchiy_akkaunt(), tekushchiy_rid()))
            assert tekushchiy_rid() == rid

    await asyncio.gather(dialog("saunamart", 0.02), dialog("sbsauna", 0.01))

    akkaunty = {a for a, _ in sobrano}
    ridy = {r for _, r in sobrano}
    assert akkaunty == {"saunamart", "sbsauna"}
    assert len(ridy) == 2
