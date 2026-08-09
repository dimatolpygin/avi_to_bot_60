# -*- coding: utf-8 -*-
"""Тесты REST-интеграции amoCRM (этап 14, подэтап 14.4).

Сети нет (`httpx.MockTransport`), БД нет (фейковая сессия). Проверяем форму
запросов создания сделки/заметки/задачи, раздачу 50/50, и что оркестрация
обновляет строку лида (sent/failed) правильно.
"""
from __future__ import annotations

import json

import httpx
import pytest

from bot.config import AmoRestConfig
from bot.crm.amo import (STATUS_NERAZOBRANNOE, STATUS_PERVICHNY, VORONKA_SAUNA,
                         AmoAPI, OshibkaAmo, otpravit_lead, vybrat_menedzhera)

CFG = AmoRestConfig(base_url="https://sbcompany.amocrm.ru", access_token="T0KEN")


def _api(handler) -> AmoAPI:
    client = httpx.AsyncClient(base_url=CFG.base_url, transport=httpx.MockTransport(handler),
                               headers={"Authorization": f"Bearer {CFG.access_token}"})
    return AmoAPI(CFG, client=client)


# ── Создание сделки ──────────────────────────────────────────────────────────

async def test_sozdat_sdelku_telo_i_razbor():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer T0KEN"
        zahvat["path"] = request.url.path
        zahvat["telo"] = json.loads(request.content)
        return httpx.Response(200, json=[{"id": 555, "contact_id": 777}])

    async with _api(handler) as api:
        r = await api.sozdat_sdelku(imya_sdelki="Авито · Иван", imya_kontakta="Иван",
                                    telefon="+79001234567", status_id=STATUS_PERVICHNY,
                                    responsible_id=6783360)

    assert r == {"lead_id": 555, "contact_id": 777}
    assert zahvat["path"] == "/api/v4/leads/complex"
    lead = zahvat["telo"][0]
    assert lead["pipeline_id"] == VORONKA_SAUNA
    assert lead["status_id"] == STATUS_PERVICHNY
    assert lead["responsible_user_id"] == 6783360
    kontakt = lead["_embedded"]["contacts"][0]
    assert kontakt["name"] == "Иван"
    assert kontakt["custom_fields_values"][0]["field_code"] == "PHONE"
    assert kontakt["custom_fields_values"][0]["values"][0]["value"] == "+79001234567"


async def test_sozdat_sdelku_bez_telefona_ne_shlet_pole():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        zahvat["telo"] = json.loads(request.content)
        return httpx.Response(200, json=[{"id": 1, "contact_id": 2}])

    async with _api(handler) as api:
        await api.sozdat_sdelku(imya_sdelki="s", imya_kontakta=None, telefon=None,
                                status_id=STATUS_NERAZOBRANNOE, responsible_id=1)

    assert "custom_fields_values" not in zahvat["telo"][0]["_embedded"]["contacts"][0]


async def test_zadacha_telo():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        zahvat["path"] = request.url.path
        zahvat["telo"] = json.loads(request.content)
        return httpx.Response(200, json=[{"id": 9}])

    async with _api(handler) as api:
        await api.sozdat_zadachu(555, 6783360, "Связаться", srok_chasov=4)

    assert zahvat["path"] == "/api/v4/tasks"
    z = zahvat["telo"][0]
    assert z["entity_type"] == "leads" and z["entity_id"] == 555
    assert z["responsible_user_id"] == 6783360
    assert z["complete_till"] > 0


async def test_oshibka_4xx():
    async with _api(lambda r: httpx.Response(401, text="unauth")) as api:
        with pytest.raises(OshibkaAmo) as e:
            await api.sozdat_sdelku(imya_sdelki="s", imya_kontakta=None, telefon=None,
                                    status_id=1, responsible_id=1)
    assert e.value.status == 401


# ── Раздача 50/50 ────────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self):
        self._c = {}

    async def incr(self, key):
        self._c[key] = self._c.get(key, 0) + 1
        return self._c[key]


async def test_round_robin_cheredovanie():
    r = _FakeRedis()
    vybor = [await vybrat_menedzhera(r, "sbsauna") for _ in range(4)]
    assert vybor == [6783360, 1356618, 6783360, 1356618]     # чётко по кругу


async def test_round_robin_bez_redis_pervyy():
    assert await vybrat_menedzhera(None, "saunamart") == 2766675


async def test_round_robin_redis_upal_pervyy():
    class _Bad:
        async def incr(self, key):
            raise RuntimeError("redis down")
    assert await vybrat_menedzhera(_Bad(), "sbsauna") == 6783360


# ── Оркестрация: обновление строки лида ──────────────────────────────────────

class _Lead:
    """Заглушка строки leads (не тянем ORM/БД)."""
    def __init__(self, phone="+79990001122", name="Иван", note="о чём говорили"):
        self.id = 1
        self.phone = phone
        self.name = name
        self.note = note
        self.status = "new"
        self.amo_lead_id = None
        self.amo_contact_id = None
        self.amo_responsible_id = None
        self.error = None
        self.sent_at = None


class _Sessiya:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


async def test_otpravit_lead_uspeh_stavit_sent_i_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/complex"):
            return httpx.Response(200, json=[{"id": 900, "contact_id": 901}])
        return httpx.Response(200, json=[{"id": 1}])      # notes / tasks

    lead, sess = _Lead(), _Sessiya()
    async with _api(handler) as api:
        ok = await otpravit_lead(api, _FakeRedis(), sess, lead, "sbsauna")

    assert ok is True
    assert lead.status == "sent"
    assert lead.amo_lead_id == 900 and lead.amo_contact_id == 901
    assert lead.amo_responsible_id in (6783360, 1356618)
    assert lead.sent_at is not None


async def test_otpravit_lead_bez_telefona_status_nerazobrannoe():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/complex"):
            zahvat["telo"] = json.loads(request.content)
            return httpx.Response(200, json=[{"id": 5, "contact_id": 6}])
        return httpx.Response(200, json=[{"id": 1}])

    lead, sess = _Lead(phone=None), _Sessiya()
    async with _api(handler) as api:
        await otpravit_lead(api, None, sess, lead, "sbsauna")

    assert zahvat["telo"][0]["status_id"] == STATUS_NERAZOBRANNOE


async def test_otpravit_lead_sboy_stavit_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="amo down")

    lead, sess = _Lead(), _Sessiya()
    async with _api(handler) as api:
        ok = await otpravit_lead(api, None, sess, lead, "sbsauna")

    assert ok is False
    assert lead.status == "failed"
    assert lead.error and lead.amo_lead_id is None
