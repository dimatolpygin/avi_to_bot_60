# -*- coding: utf-8 -*-
"""Тесты amoCRM Chat API (этап 14, подэтап 14.3a).

Сети нет: HTTP подменяется `httpx.MockTransport`. Проверяем подпись (и как
чистую функцию вектором, и «серверной» перепроверкой того, что клиент реально
отправил), формы connect/new_message, различие входящее/исходящее, глушение
ошибок зеркала и проводку зеркала в адаптер Авито.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from types import SimpleNamespace

from bot.channels.avito import Vhodyashchee, sdelat_obrabotchik, _kanal_avito
from bot.config import AmojoConfig, Config
from bot.crm.amojo import (AmojoAPI, OshibkaAmojo, Zerkalo, payload_soobshcheniya,
                           soobshchenie_media, stroka_podpisi, x_signature)

CFG = AmojoConfig(channel_id="chan", channel_secret="secret",
                  amojo_id="acc", base_url="https://amojo.amocrm.ru")


# ── Подпись как чистая функция ───────────────────────────────────────────────

def test_stroka_podpisi_poryadok_strok():
    s = stroka_podpisi("post", "md5hex", "application/json", "DATE", "/path")
    assert s == "POST\nmd5hex\napplication/json\nDATE\n/path"    # метод в верхний регистр


def test_x_signature_vektor():
    # Фиксированный вектор: значение не должно меняться от рефакторинга.
    stroka = "POST\nabc\napplication/json\nWed, 01 Jan 2020 00:00:00 GMT\n/x"
    ozhid = hmac.new(b"secret", stroka.encode(), hashlib.sha1).hexdigest()
    assert x_signature("secret", stroka) == ozhid
    assert x_signature("secret", stroka) == x_signature("secret", stroka)  # детерминизм


# ── «Серверная» перепроверка подписи и Content-MD5 ───────────────────────────

def _proverit_podpis(request: httpx.Request, secret: str) -> None:
    """Повторяет то, что делает amoCRM: собирает строку из ПРИСЛАННЫХ заголовков
    и тела и сверяет X-Signature. Ловит рассинхрон тела и подписи."""
    telo = request.content
    assert request.headers["Content-MD5"] == hashlib.md5(telo).hexdigest()
    stroka = stroka_podpisi("POST", request.headers["Content-MD5"],
                            request.headers["Content-Type"],
                            request.headers["Date"], request.url.path)
    assert request.headers["X-Signature"] == x_signature(secret, stroka)


async def test_connect_telo_put_i_podpis():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        _proverit_podpis(request, "secret")
        zahvat["path"] = request.url.path
        zahvat["telo"] = json.loads(request.content)
        return httpx.Response(200, json={"scope_id": "chan_acc"})

    client = httpx.AsyncClient(base_url=CFG.base_url, transport=httpx.MockTransport(handler))
    async with AmojoAPI(CFG, client=client) as api:
        otvet = await api.connect("Авито SB SAUNA")

    assert otvet["scope_id"] == "chan_acc"
    assert zahvat["path"] == "/v2/origin/custom/chan/connect"      # channel_id, не scope_id
    assert zahvat["telo"] == {"account_id": "acc", "title": "Авито SB SAUNA",
                              "hook_api_version": "v2"}


async def test_new_message_idet_na_scope_id():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        _proverit_podpis(request, "secret")
        zahvat["path"] = request.url.path
        zahvat["telo"] = json.loads(request.content)
        return httpx.Response(200, json={"new_message": {"msgid": "x"}})

    client = httpx.AsyncClient(base_url=CFG.base_url, transport=httpx.MockTransport(handler))
    async with AmojoAPI(CFG, client=client) as api:
        await api.new_message(payload_soobshcheniya(
            conversation_id="sbsauna:c1", msgid="avito:m1",
            sender={"id": "avito:42", "name": "Клиент Авито"}, tekst="привет"))

    assert zahvat["path"] == "/v2/origin/custom/chan_acc"          # scope_id в пути
    assert zahvat["telo"]["event_type"] == "new_message"


async def test_oshibka_4xx_podnimaet_isklyuchenie():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = httpx.AsyncClient(base_url=CFG.base_url, transport=httpx.MockTransport(handler))
    async with AmojoAPI(CFG, client=client) as api:
        with pytest.raises(OshibkaAmojo) as e:
            await api.connect("t")
    assert e.value.status == 403


# ── payload: входящее vs исходящее ───────────────────────────────────────────

def test_vhodyashchee_bez_receiver():
    p = payload_soobshcheniya(conversation_id="d", msgid="m",
                              sender={"id": "s", "name": "n"}, tekst="t")
    assert "receiver" not in p                                     # клиент → только sender
    assert p["message"] == {"type": "text", "text": "t"}
    assert p["silent"] is False


def test_ishodyashchee_s_receiver():
    p = payload_soobshcheniya(conversation_id="d", msgid="m",
                              sender={"id": "bot", "name": "Роман"},
                              receiver={"id": "s", "name": "n"}, tekst="t")
    assert p["receiver"] == {"id": "s", "name": "n"}               # бот → sender+receiver


# ── payload вложения (media, 14.9) ───────────────────────────────────────────

def test_soobshchenie_media_shlet_tolko_zadannye_polya():
    assert soobshchenie_media("picture", "https://u/p.jpg") == {
        "type": "picture", "media": "https://u/p.jpg"}            # пустые поля не шлём
    assert soobshchenie_media("file", "https://u/f.pdf", imya="f.pdf", razmer=10) == {
        "type": "file", "media": "https://u/f.pdf",
        "file_name": "f.pdf", "file_size": 10}


def test_payload_media_vytesnyaet_tekst():
    p = payload_soobshcheniya(
        conversation_id="d", msgid="m", sender={"id": "s", "name": "n"},
        soobshchenie=soobshchenie_media("picture", "https://u/p.jpg"))
    assert p["message"] == {"type": "picture", "media": "https://u/p.jpg"}


# ── Config.bot_ref_id: per-account amojo_id менеджера (14.9-C) ────────────────

def _ref(kod, *, glob="", po_akk=None):
    """Резолвер читает только два поля — зовём метод на лёгком stand-in."""
    ns = SimpleNamespace(amo_bot_ref_id=glob, amo_bot_ref_id_po_akkauntu=po_akk or {})
    return Config.bot_ref_id(ns, kod)


def test_bot_ref_id_pusto_daet_none():
    assert _ref("saunamart") is None                       # ничего не задано → не зеркалим


def test_bot_ref_id_globalnyy_folbek():
    assert _ref("saunamart", glob="G") == "G"              # только глобальный


def test_bot_ref_id_per_account_perekryvaet():
    r = _ref("sbsauna", glob="G", po_akk={"sbsauna": "ROMAN", "saunamart": "ALEX"})
    assert r == "ROMAN"                                     # per-account важнее глобального


def test_bot_ref_id_drugoy_akkaunt_padaet_na_folbek():
    r = _ref("sbsauna_deshman", glob="G", po_akk={"saunamart": "ALEX"})
    assert r == "G"                                        # нет своего → глобальный


# ── Zerkalo: формы и глушение ошибок ─────────────────────────────────────────

def _zerkalo_zahvat(zahvat: list, *, bot_ref_id: str | None = None) -> Zerkalo:
    class _API:
        async def new_message(self, payload):
            zahvat.append(payload)
            return {}
    return Zerkalo(_API(), "sbsauna", "Роман", bot_ref_id=bot_ref_id)


async def test_zerkalo_vhodyashchee_sender_klient():
    zahvat = []
    await _zerkalo_zahvat(zahvat).vhodyashchee("c1", "m1", 42, "сколько стоит")
    p = zahvat[0]
    assert p["conversation_id"] == "sbsauna:c1"
    assert p["sender"]["id"] == "avito:42" and "receiver" not in p


async def test_zerkalo_ishodyashchee_bez_ref_id_ne_shlet():
    # Без ref_id amojo отклонил бы исходящее (error 453) — пока просто пропускаем.
    zahvat = []
    await _zerkalo_zahvat(zahvat).ishodyashchee("c1", "здравствуйте", avtor_id=42)
    assert zahvat == []


async def test_zerkalo_ishodyashchee_s_ref_id_bot_i_klient():
    zahvat = []
    await _zerkalo_zahvat(zahvat, bot_ref_id="U-REF").ishodyashchee(
        "c1", "здравствуйте", avtor_id=42)
    p = zahvat[0]
    assert p["sender"]["id"] == "bot:sbsauna" and p["sender"]["name"] == "Роман"
    assert p["sender"]["ref_id"] == "U-REF"          # помечает бота как CRM-сторону
    assert p["receiver"]["id"] == "avito:42"


async def test_zerkalo_vlozhenie_s_url_shlet_media():
    zahvat = []
    await _zerkalo_zahvat(zahvat).vhodyashchee_vlozhenie(
        "c1", "m1", 42,
        {"tip": "picture", "url": "https://u/p.jpg", "imya": "photo.jpg", "razmer": None})
    p = zahvat[0]
    assert p["conversation_id"] == "sbsauna:c1"
    assert p["sender"]["id"] == "avito:42" and "receiver" not in p   # клиент, входящее
    assert p["message"] == {"type": "picture", "media": "https://u/p.jpg",
                            "file_name": "photo.jpg"}


async def test_zerkalo_vlozhenie_bez_url_ne_shlet():
    # voice/video/file без публичного URL — зеркалить нечего, тихо пропускаем.
    zahvat = []
    await _zerkalo_zahvat(zahvat).vhodyashchee_vlozhenie(
        "c1", "m1", 42, {"tip": "voice", "url": None})
    assert zahvat == []


async def test_zerkalo_glushit_oshibku_ne_ronyaet():
    class _API:
        async def new_message(self, payload):
            raise OshibkaAmojo("amoCRM 500", status=500)
    z = Zerkalo(_API(), "sbsauna", "Роман")
    await z.vhodyashchee("c1", "m1", 42, "т")     # не должно бросить
    await z.ishodyashchee("c1", "т")              # не должно бросить


# ── Проводка зеркала в адаптер Авито ─────────────────────────────────────────

class _FakeYadro:
    def __init__(self):
        self.obrabotano = []

    def zapomnit_obyavlenie(self, kod, chat, obyavlenie):
        pass

    def obrabotat(self, kod, chat, tekst, kanal):
        self.obrabotano.append((chat, tekst, kanal))


class _FakeAPI:
    def __init__(self):
        self.otpravleno = []

    async def otpravit(self, chat_id, tekst):
        self.otpravleno.append((chat_id, tekst))


class _SpyZerkalo:
    def __init__(self):
        self.vhod = []
        self.ishod = []
        self.vlozh = []

    async def vhodyashchee(self, chat_id, msg_id, avtor_id, tekst, *, imya=None):
        self.vhod.append((chat_id, msg_id, avtor_id, tekst))

    async def vhodyashchee_vlozhenie(self, chat_id, msg_id, avtor_id, vlozhenie, *, imya=None):
        self.vlozh.append((chat_id, vlozhenie))

    async def ishodyashchee(self, chat_id, tekst, *, avtor_id=None, imya_klienta=None):
        self.ishod.append((chat_id, tekst, avtor_id))


def _vhod(chat_id="c1", msg_id="m1", author_id=42, text="привет"):
    return Vhodyashchee(chat_id=chat_id, msg_id=msg_id, author_id=author_id,
                        tekst=text, obyavlenie=None)


async def test_obrabotchik_zerkalit_vhodyashchee_pered_yadrom():
    ya, api, z = _FakeYadro(), _FakeAPI(), _SpyZerkalo()
    obr = sdelat_obrabotchik("sbsauna", api, ya, None, zerkalo=z)
    await obr(_vhod(text="сколько стоит"))

    assert z.vhod == [("c1", "m1", 42, "сколько стоит")]
    assert ya.obrabotano and ya.obrabotano[0][1] == "сколько стоит"


async def test_obrabotchik_zerkalit_vlozhenie_klienta():
    # Фото клиента (текста нет) → зеркалим в amoCRM, ядру не отдаём, просим текст.
    ya, api, z = _FakeYadro(), _FakeAPI(), _SpyZerkalo()
    obr = sdelat_obrabotchik("sbsauna", api, ya, None, zerkalo=z)
    v = Vhodyashchee(chat_id="c1", msg_id="m1", author_id=42, tekst=None,
                     obyavlenie=None,
                     vlozhenie={"tip": "picture", "url": "https://u/p.jpg",
                                "imya": "photo.jpg", "razmer": None})
    await obr(v)

    assert z.vlozh == [("c1", {"tip": "picture", "url": "https://u/p.jpg",
                               "imya": "photo.jpg", "razmer": None})]
    assert ya.obrabotano == []                                    # картинку ядру не отдаём
    assert api.otpravleno and "текстом" in api.otpravleno[0][1]   # просим написать


async def test_kanal_zerkalit_ishodyashchee_posle_otpravki():
    api, z = _FakeAPI(), _SpyZerkalo()
    kanal = _kanal_avito(api, "c1", "avito:42", zerkalo=z, avtor_id=42)
    await kanal.otpravit("здравствуйте")

    assert api.otpravleno == [("c1", "здравствуйте")]              # ушло клиенту
    assert z.ishod == [("c1", "здравствуйте", 42)]                # и в amoCRM
