# -*- coding: utf-8 -*-
"""Тесты адаптера Авито (этап 14, подэтап 14.1).

Сети нет: HTTP подменяется `httpx.MockTransport`. Проверяем разбор входящего,
дедуп, ленивый токен с автопродлением и форму запроса на отправку.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from bot.channels.avito import (AvitoAPI, OshibkaAvito, Vidennye, Vhodyashchee,
                                 cikl_pollinga, izvlech_vhodyashchee)
from bot.config import AvitoConfig

CFG = AvitoConfig(client_id="cid", client_secret="sec", user_id=23598618)


def _chat_vhod(chat_id="c1", msg_id="m1", text="привет", item=None):
    chat = {"id": chat_id,
            "last_message": {"id": msg_id, "author_id": 42, "direction": "in",
                             "content": {"text": text}}}
    if item is not None:
        chat["context"] = {"type": "item", "value": item}
    return chat


def _chat_ishod(chat_id="c2", msg_id="m9"):
    return {"id": chat_id,
            "last_message": {"id": msg_id, "author_id": 23598618, "direction": "out",
                             "content": {"text": "наш ответ"}}}


# ── Разбор входящего ─────────────────────────────────────────────────────────

def test_izvlech_beret_tolko_vhodyashchee():
    assert izvlech_vhodyashchee(_chat_ishod()) is None          # исходящее — мимо
    v = izvlech_vhodyashchee(_chat_vhod())
    assert v is not None and v.chat_id == "c1" and v.tekst == "привет"


def test_izvlech_kartinka_bez_teksta_daet_none_tekst():
    chat = {"id": "c1", "last_message": {"id": "m1", "direction": "in", "content": {}}}
    v = izvlech_vhodyashchee(chat)
    assert v is not None and v.tekst is None                    # вложение — не отбрасываем


def test_izvlech_tyanet_obyavlenie_iz_konteksta():
    v = izvlech_vhodyashchee(_chat_vhod(item={"id": 8246919725,
                                              "title": "Отделка бани под ключ",
                                              "price_string": "от 74 000 ₽"}))
    assert v.obyavlenie and v.obyavlenie["title"] == "Отделка бани под ключ"


def test_izvlech_bez_id_soobshcheniya_none():
    chat = {"id": "c1", "last_message": {"direction": "in", "content": {"text": "т"}}}
    assert izvlech_vhodyashchee(chat) is None


# ── Дедуп ────────────────────────────────────────────────────────────────────

def test_vidennye_novoe_potom_povtor():
    vid = Vidennye()
    v = izvlech_vhodyashchee(_chat_vhod())
    assert vid.novoe(v)
    vid.otmetit(v)
    assert not vid.novoe(v)                                     # тот же msg_id — не новое


def test_vidennye_novyy_msg_v_tom_zhe_chate():
    vid = Vidennye()
    v1 = izvlech_vhodyashchee(_chat_vhod(msg_id="m1"))
    v2 = izvlech_vhodyashchee(_chat_vhod(msg_id="m2"))
    vid.otmetit(v1)
    assert vid.novoe(v2)                                        # клиент дописал — новое


# ── Токен ────────────────────────────────────────────────────────────────────

def _api_s_transportom(handler) -> AvitoAPI:
    client = httpx.AsyncClient(base_url="https://api.avito.ru",
                               transport=httpx.MockTransport(handler))
    return AvitoAPI(CFG, client=client)


async def test_token_zaprashivaetsya_i_keshiruetsya():
    vyzovov = {"token": 0, "chats": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            vyzovov["token"] += 1
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 86400})
        if request.url.path.endswith("/chats"):
            vyzovov["chats"] += 1
            assert request.headers["Authorization"] == "Bearer T1"
            return httpx.Response(200, json={"chats": []})
        return httpx.Response(404)

    async with _api_s_transportom(handler) as api:
        await api.chaty()
        await api.chaty()

    assert vyzovov["token"] == 1                                # токен взят один раз на два запроса
    assert vyzovov["chats"] == 2


async def test_token_oshibka_podnimaet_isklyuchenie():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid_client")

    async with _api_s_transportom(handler) as api:
        with pytest.raises(OshibkaAvito) as e:
            await api.chaty()
    assert e.value.status == 401


# ── Отправка ─────────────────────────────────────────────────────────────────

async def test_otpravit_shlet_pravilnoe_telo():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 86400})
        if request.url.path.endswith("/messages"):
            import json
            zahvat.update(json.loads(request.content))
            zahvat["uid_v_puti"] = "23598618" in request.url.path
            return httpx.Response(200, json={"id": "new"})
        return httpx.Response(404)

    async with _api_s_transportom(handler) as api:
        otvet = await api.otpravit("c1", "здравствуйте")

    assert otvet["id"] == "new"
    assert zahvat["type"] == "text"
    assert zahvat["message"]["text"] == "здравствуйте"
    assert zahvat["uid_v_puti"] is True                         # user_id из cfg попал в путь


# ── Цикл поллинга ────────────────────────────────────────────────────────────

async def test_cikl_zovet_obrabotchik_odin_raz_na_soobshchenie():
    otdano = {"chats_calls": 0}
    stop = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 86400})
        if request.url.path.endswith("/chats"):
            otdano["chats_calls"] += 1
            # Останавливаемся ПО ЧИСЛУ ОПРОСОВ (в обработчике HTTP), а не по факту
            # обработки: дедуп зовёт obrabotchik лишь раз, и вешать stop на него
            # значит зациклиться. Дать циклу крутануться минимум дважды.
            if otdano["chats_calls"] >= 2:
                stop.set()
            # Один и тот же непрочитанный чат на каждом тике — дедуп обязан не
            # дать обработать его дважды.
            return httpx.Response(200, json={"chats": [_chat_vhod()]})
        return httpx.Response(404)

    poluchennye = []

    async def obrabotchik(v: Vhodyashchee) -> None:
        poluchennye.append(v)

    async with _api_s_transportom(handler) as api:
        await asyncio.wait_for(
            cikl_pollinga(api, obrabotchik, stop, interval_s=0.01), timeout=2.0)

    assert otdano["chats_calls"] >= 2       # цикл реально крутанулся не раз
    assert len(poluchennye) == 1            # один и тот же msg → одна обработка
    assert poluchennye[0].tekst == "привет"
