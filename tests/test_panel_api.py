# -*- coding: utf-8 -*-
"""Тесты HTTP-API панели (этап 14.7, кирпич 2).

Чтение из БД — на фейковой сессии (как в test_zhurnal): проверяем форму данных
списка чатов и переписки, 404 на отсутствующий диалог. Токен — чистой функцией.
Ручки и middleware (401/CORS/health) — на тестовом aiohttp-сервере без БД:
фабрика сессий подменена, важно поведение веб-слоя, а не SQL.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from bot.models import Account, Dialog, Message
from bot.panel import api


# ── Фейковые сессия/фабрика ───────────────────────────────────────────────────

class _Rezult:
    def __init__(self, stroki=None, skalyar=None, scalars_list=None):
        self._stroki = stroki or []
        self._skalyar = skalyar
        self._scalars_list = scalars_list or []

    def all(self):
        return self._stroki

    def scalar_one_or_none(self):
        return self._skalyar

    def scalars(self):
        return _Scalars(self._scalars_list)


class _Scalars:
    def __init__(self, spisok):
        self._spisok = spisok

    def all(self):
        return self._spisok


class _Sessiya:
    """Отдаёт заранее заготовленные результаты по порядку вызовов execute."""

    def __init__(self, *rezultaty):
        self._rezultaty = list(rezultaty)
        self._n = 0

    async def execute(self, _stmt):
        r = self._rezultaty[self._n]
        self._n += 1
        return r

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


def _fabrika(*rezultaty):
    return lambda: _Sessiya(*rezultaty)


_T = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


# ── Чтение: список чатов ──────────────────────────────────────────────────────

async def test_spisok_chatov_forma_i_poryadok():
    d = Dialog(id=7, account_id=1, channel="avito", chat_key="u2i-abc",
               client_name=None, client_username="avito:42")
    d.last_message_at = _T
    fab = _fabrika(_Rezult(stroki=[(d, "sbsauna", "Чем помочь?")]))

    chaty = await api.spisok_chatov(fab)
    assert len(chaty) == 1
    c = chaty[0]
    assert c["dialog_id"] == 7 and c["account"] == "sbsauna"
    assert c["channel"] == "avito" and c["chat_key"] == "u2i-abc"
    assert c["client_username"] == "avito:42"
    assert c["preview"] == "Чем помочь?"
    assert c["last_message_at"] == _T.isoformat()


async def test_spisok_chatov_pusto():
    assert await api.spisok_chatov(_fabrika(_Rezult(stroki=[]))) == []


# ── Чтение: переписка диалога ─────────────────────────────────────────────────

async def test_soobshcheniya_dialoga_est():
    dialog = Dialog(id=5, account_id=1, channel="avito", chat_key="u2i-abc")
    m1 = Message(id=1, dialog_id=5, role="client", body="привет", request_id="r1")
    m1.created_at = _T
    m2 = Message(id=2, dialog_id=5, role="bot", body="Чем помочь?", request_id="r1")
    m2.created_at = _T
    fab = _fabrika(_Rezult(skalyar=dialog), _Rezult(scalars_list=[m1, m2]))

    msgs = await api.soobshcheniya_dialoga(fab, 5)
    assert [m["role"] for m in msgs] == ["client", "bot"]
    assert msgs[0]["body"] == "привет" and msgs[0]["request_id"] == "r1"
    assert msgs[1]["created_at"] == _T.isoformat()


async def test_soobshcheniya_dialoga_net_dialoga():
    fab = _fabrika(_Rezult(skalyar=None))
    assert await api.soobshcheniya_dialoga(fab, 999) is None


async def test_soobshcheniya_dialoga_pustoy():
    dialog = Dialog(id=6, account_id=1, channel="avito", chat_key="x")
    fab = _fabrika(_Rezult(skalyar=dialog), _Rezult(scalars_list=[]))
    assert await api.soobshcheniya_dialoga(fab, 6) == []


# ── Токен ─────────────────────────────────────────────────────────────────────

def test_token_veren():
    req = make_mocked_request("GET", "/api/chats",
                              headers={"Authorization": "Bearer sekret"})
    assert api.token_veren(req, "sekret") is True
    assert api.token_veren(req, "drugoy") is False


def test_token_bez_bearer_prefiksa():
    req = make_mocked_request("GET", "/api/chats", headers={"Authorization": "sekret"})
    assert api.token_veren(req, "sekret") is True


def test_token_pustoy_nikogda_ne_veren():
    req = make_mocked_request("GET", "/api/chats", headers={"Authorization": "Bearer "})
    assert api.token_veren(req, "") is False


def test_token_net_zagolovka():
    req = make_mocked_request("GET", "/api/chats")
    assert api.token_veren(req, "sekret") is False


# ── Веб-слой: middleware, health, 401 ─────────────────────────────────────────
# Поднимаем aiohttp на тестовом сервере руками (TestClient/TestServer), без
# плагина pytest-aiohttp — он не в зависимостях, а pytest-asyncio уже крутит луп.

def _prilozhenie(fab=None):
    return api.sozdat_prilozhenie(fab or _fabrika(_Rezult(stroki=[])),
                                  token="sekret", origin="https://x.amocrm.ru")


async def test_health_bez_tokena():
    async with TestClient(TestServer(_prilozhenie())) as kl:
        resp = await kl.get("/api/health")
        assert resp.status == 200 and (await resp.json())["ok"] is True


async def test_chaty_bez_tokena_401():
    async with TestClient(TestServer(_prilozhenie())) as kl:
        resp = await kl.get("/api/chats")
        assert resp.status == 401


async def test_chaty_s_tokenom_200():
    d = Dialog(id=1, account_id=1, channel="avito", chat_key="c")
    d.last_message_at = _T
    app = _prilozhenie(_fabrika(_Rezult(stroki=[(d, "sbsauna", "текст")])))
    async with TestClient(TestServer(app)) as kl:
        resp = await kl.get("/api/chats", headers={"Authorization": "Bearer sekret"})
        assert resp.status == 200
        data = await resp.json()
        assert data["chats"][0]["account"] == "sbsauna"


async def test_dialog_404():
    app = _prilozhenie(_fabrika(_Rezult(skalyar=None)))
    async with TestClient(TestServer(app)) as kl:
        resp = await kl.get("/api/dialog/999", headers={"Authorization": "Bearer sekret"})
        assert resp.status == 404


async def test_cors_zagolovok_na_otvete():
    async with TestClient(TestServer(_prilozhenie())) as kl:
        resp = await kl.get("/api/health")
        assert resp.headers["Access-Control-Allow-Origin"] == "https://x.amocrm.ru"


async def test_preflight_options_bez_tokena():
    async with TestClient(TestServer(_prilozhenie())) as kl:
        resp = await kl.options("/api/chats")
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "https://x.amocrm.ru"
