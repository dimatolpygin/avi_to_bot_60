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
                                 _sobrat_vhodyashchie, cikl_pollinga,
                                 izvlech_vhodyashchee, sdelat_obrabotchik)
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


def _chat_kartinka(chat_id="c1", msg_id="m1"):
    return {"id": chat_id,
            "last_message": {"id": msg_id, "author_id": 42, "direction": "in",
                             "type": "image",
                             "content": {"image": {"sizes": {
                                 "140x105": "https://cdn/small.jpg",
                                 "1280x960": "https://cdn/big.jpg"}}}}}


def test_izvlech_kartinka_daet_vlozhenie_s_krupneyshim_url():
    v = izvlech_vhodyashchee(_chat_kartinka())
    assert v.tekst is None
    assert v.vlozhenie == {"tip": "picture", "url": "https://cdn/big.jpg",
                           "imya": "photo.jpg", "razmer": None}   # выбрана крупнейшая


def test_izvlech_golos_pomechaet_tip_bez_url():
    chat = {"id": "c1", "last_message": {"id": "m1", "direction": "in",
                                         "type": "voice",
                                         "content": {"voice": {"voice_id": "v"}}}}
    v = izvlech_vhodyashchee(chat)
    assert v.vlozhenie == {"tip": "voice", "url": None, "imya": None, "razmer": None}


def test_izvlech_tekst_bez_vlozheniya():
    v = izvlech_vhodyashchee(_chat_vhod(text="привет"))
    assert v.vlozhenie is None                                   # текст — не вложение


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


def test_vidennye_zalp_dva_id_oba_novye_potom_povtor():
    """Набор id на чат: два разных сообщения одного чата — оба новые (раньше один
    «последний id» второе бы затёр); повтор любого — не новое."""
    vid = Vidennye()
    v1 = izvlech_vhodyashchee(_chat_vhod(msg_id="m1"))
    v2 = izvlech_vhodyashchee(_chat_vhod(msg_id="m2"))
    assert vid.novoe(v1) and vid.novoe(v2)
    vid.otmetit(v1)
    vid.otmetit(v2)
    assert not vid.novoe(v1) and not vid.novoe(v2)              # оба запомнены


def test_vidennye_potolok_vytesnyaet_staroe():
    vid = Vidennye(potolok=2)
    for i in range(3):
        vid.otmetit(izvlech_vhodyashchee(_chat_vhod(msg_id=f"m{i}")))
    assert vid.novoe(izvlech_vhodyashchee(_chat_vhod(msg_id="m0")))  # m0 вытеснен
    assert not vid.novoe(izvlech_vhodyashchee(_chat_vhod(msg_id="m2")))


# ── Залп входящих: все сообщения за тик, не только last_message (14.10) ───────

def _msg(msg_id, *, text=None, direction="in", created=0, author_id=42, tip=None,
         content=None):
    m = {"id": msg_id, "author_id": author_id, "direction": direction,
         "created": created, "content": content if content is not None
         else ({"text": text} if text is not None else {})}
    if tip is not None:
        m["type"] = tip
    return m


class _FakeMsgApi:
    """Отдаёт заранее заданный список сообщений (или падает) на `soobshcheniya`."""

    def __init__(self, msgs, *, oshibka=False):
        self._msgs = msgs
        self._oshibka = oshibka
        self.sprosheno = []

    async def soobshcheniya(self, chat_id, *, limit=20):
        self.sprosheno.append(chat_id)
        if self._oshibka:
            raise OshibkaAvito("нет списка", status=500)
        return self._msgs


async def test_sobrat_beret_vse_vhodyashchie_v_hronologii():
    # выдача Авито «свежие первыми» — helper обязан вернуть старые первыми
    api = _FakeMsgApi([_msg("m3", text="три", created=102),
                       _msg("m2", text="два", created=101),
                       _msg("m1", text="раз", created=100)])
    vhod = await _sobrat_vhodyashchie(api, _chat_vhod(chat_id="c1"))
    assert [v.tekst for v in vhod] == ["раз", "два", "три"]      # все три, по порядку
    assert [v.msg_id for v in vhod] == ["m1", "m2", "m3"]


async def test_sobrat_otbrasyvaet_ishodyashchie():
    api = _FakeMsgApi([_msg("m1", text="вопрос", created=100),
                       _msg("o1", text="наш ответ", direction="out", created=101)])
    vhod = await _sobrat_vhodyashchie(api, _chat_vhod(chat_id="c1"))
    assert [v.msg_id for v in vhod] == ["m1"]                    # исходящее — мимо


async def test_sobrat_zalp_foto_odnoy_sekundy_vse_prihodyat():
    """3 фото группой с одинаковым `created` — не теряем ни одного."""
    api = _FakeMsgApi([_msg(f"p{i}", tip="image", created=200,
                            content={"image": {"sizes": {"1x1": f"https://cdn/{i}.jpg"}}})
                       for i in range(3)])
    vhod = await _sobrat_vhodyashchie(api, _chat_vhod(chat_id="c1"))
    assert sorted(v.msg_id for v in vhod) == ["p0", "p1", "p2"]
    assert all(v.vlozhenie and v.vlozhenie["tip"] == "picture" for v in vhod)


async def test_sobrat_folbek_na_last_message_pri_sboe():
    api = _FakeMsgApi([], oshibka=True)
    vhod = await _sobrat_vhodyashchie(api, _chat_vhod(chat_id="c1", msg_id="lm", text="хай"))
    assert [(v.msg_id, v.tekst) for v in vhod] == [("lm", "хай")]  # хотя бы последнее


async def test_sobrat_prokidyvaet_obyavlenie_v_kazhdoe():
    api = _FakeMsgApi([_msg("m1", text="а", created=1), _msg("m2", text="б", created=2)])
    chat = _chat_vhod(chat_id="c1", item={"id": 7, "title": "Отделка бани"})
    vhod = await _sobrat_vhodyashchie(api, chat)
    assert all(v.obyavlenie and v.obyavlenie["title"] == "Отделка бани" for v in vhod)


async def test_cikl_obrabatyvaet_ves_zalp():
    """Поллер зовёт обработчик на КАЖДОЕ входящее залпа, не только на последнее."""
    stop = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 86400})
        if request.url.path.endswith("/chats"):
            stop_now = _chat_vhod(chat_id="c1")
            return httpx.Response(200, json={"chats": [stop_now]})
        if request.url.path.endswith("/messages/"):
            stop.set()                                          # один тик достаточно
            return httpx.Response(200, json={"messages": [
                _msg("m1", text="раз", created=1),
                _msg("m2", text="два", created=2),
                _msg("m3", text="три", created=3)]})
        return httpx.Response(404)

    poluchennye = []

    async def obrabotchik(v: Vhodyashchee) -> None:
        poluchennye.append(v.tekst)

    async with _api_s_transportom(handler) as api:
        await asyncio.wait_for(
            cikl_pollinga(api, obrabotchik, stop, interval_s=0.01), timeout=2.0)

    assert poluchennye == ["раз", "два", "три"]                 # весь залп обработан


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


# ── Исходящие картинки (14.9): upload → messages/image ───────────────────────

async def test_zagruzit_kartinku_beret_image_id_iz_klyucha():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 86400})
        if request.url.path.endswith("/uploadImages"):
            zahvat["ctype"] = request.headers.get("content-type", "")
            zahvat["uid_v_puti"] = "23598618" in request.url.path
            # Ответ Авито: {"<image_id>": {размеры}} — id это ключ верхнего уровня.
            return httpx.Response(200, json={"98765.abcdef": {
                "1280x960": "https://cdn/big.jpg", "32x32": "https://cdn/s.jpg"}})
        return httpx.Response(404)

    async with _api_s_transportom(handler) as api:
        image_id = await api.zagruzit_kartinku(b"\xff\xd8jpegbytes", imya="ph.jpg",
                                               tip="image/jpeg")

    assert image_id == "98765.abcdef"
    assert zahvat["uid_v_puti"] is True
    assert zahvat["ctype"].startswith("multipart/form-data")   # ушло как форма


async def test_zagruzit_kartinku_pustoy_otvet_padaet():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 86400})
        return httpx.Response(200, json={})       # пусто — image_id взять неоткуда

    async with _api_s_transportom(handler) as api:
        with pytest.raises(OshibkaAvito):
            await api.zagruzit_kartinku(b"x")


async def test_otpravit_kartinku_shlet_image_id():
    zahvat = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "T1", "expires_in": 86400})
        if request.url.path.endswith("/messages/image"):
            import json
            zahvat.update(json.loads(request.content))
            zahvat["uid_v_puti"] = "23598618" in request.url.path
            return httpx.Response(200, json={"id": "img-msg-1", "type": "image"})
        return httpx.Response(404)

    async with _api_s_transportom(handler) as api:
        otvet = await api.otpravit_kartinku("c1", "98765.abcdef")

    assert otvet["id"] == "img-msg-1"
    assert zahvat["image_id"] == "98765.abcdef"
    assert zahvat["uid_v_puti"] is True


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
        if request.url.path.endswith("/messages/"):
            return httpx.Response(200, json={"messages": [
                {"id": "m1", "author_id": 42, "direction": "in",
                 "content": {"text": "привет"}}]})
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


# ── Режим ответа: белый список, вложение, объявление (14.2) ───────────────────

class _FakeYadro:
    def __init__(self):
        self.obrabotano = []      # (kod, chat, tekst)
        self.obyavleniya = []     # (kod, chat, obyavlenie)

    def zapomnit_obyavlenie(self, kod, chat, obyavlenie):
        self.obyavleniya.append((kod, chat, obyavlenie))

    def obrabotat(self, kod, chat, tekst, kanal):
        self.obrabotano.append((kod, chat, tekst))


class _FakeAPI:
    def __init__(self):
        self.otpravleno = []

    async def otpravit(self, chat_id, tekst):
        self.otpravleno.append((chat_id, tekst))


async def test_belyy_spisok_propuskaet_tolko_svoi_chaty():
    ya, api = _FakeYadro(), _FakeAPI()
    obr = sdelat_obrabotchik("sbsauna", api, ya, frozenset({"c-test"}))

    await obr(izvlech_vhodyashchee(_chat_vhod(chat_id="c-test", text="привет")))
    await obr(izvlech_vhodyashchee(_chat_vhod(chat_id="c-klient", text="здрасте")))

    assert [o[1] for o in ya.obrabotano] == ["c-test"]   # чужой чат не обработан


async def test_vlozhenie_bez_teksta_prosit_tekstom_ne_zovet_yadro():
    ya, api = _FakeYadro(), _FakeAPI()
    obr = sdelat_obrabotchik("sbsauna", api, ya, None)   # None → отвечаем всем
    chat = {"id": "c1", "last_message": {"id": "m1", "direction": "in", "content": {}}}

    await obr(izvlech_vhodyashchee(chat))

    assert ya.obrabotano == []                           # ядру вложение не отдаём
    assert api.otpravleno and "словами" in api.otpravleno[0][1]


class _FakeOperatory:
    def __init__(self, vedet=False):
        self._vedet = vedet
        self.zapomneno = []
        self.prodleno = []

    async def vedet(self, kod, chat):
        return self._vedet

    async def prodlit(self, kod, chat):
        self.prodleno.append((kod, chat))

    async def zapomnit_otpravlennoe(self, kod, chat, msg_id):
        self.zapomneno.append((chat, msg_id))


class _ApiSId:
    """Как _FakeAPI, но otpravit возвращает id — нужен журналу оператора."""

    def __init__(self):
        self.otpravleno = []

    async def otpravit(self, chat_id, tekst):
        self.otpravleno.append((chat_id, tekst))
        return {"id": "p1"}


def _chat_foto():
    return {"id": "c1", "last_message": {"id": "m1", "direction": "in", "type": "image",
            "content": {"image": {"sizes": {"1280x960": "https://u/big.jpg"}}}}}


async def test_vlozhenie_prompt_pishetsya_v_zhurnal_operatora():
    # id просьбы «напишите текстом» обязан попасть в журнал бота — иначе следующее
    # сообщение клиента детектор примет за ответ оператора и заглушит бота.
    ya, api, op = _FakeYadro(), _ApiSId(), _FakeOperatory()
    obr = sdelat_obrabotchik("sbsauna", api, ya, None, operatory=op)
    await obr(izvlech_vhodyashchee(_chat_foto()))

    assert op.zapomneno == [("c1", "p1")]                # id промпта записан
    assert api.otpravleno and "словами" in api.otpravleno[0][1]


async def test_vlozhenie_pod_operatorom_molchit():
    ya, api, op = _FakeYadro(), _ApiSId(), _FakeOperatory(vedet=True)
    obr = sdelat_obrabotchik("sbsauna", api, ya, None, operatory=op)
    await obr(izvlech_vhodyashchee(_chat_foto()))

    assert api.otpravleno == []                          # менеджера не перебиваем
    assert op.zapomneno == []


async def test_obyavlenie_zapominaetsya_pered_obrabotkoy():
    ya, api = _FakeYadro(), _FakeAPI()
    obr = sdelat_obrabotchik("sbsauna", api, ya, None)
    v = izvlech_vhodyashchee(_chat_vhod(chat_id="c1", text="сколько стоит",
                                        item={"title": "Отделка под ключ", "price_string": "от 74 000 ₽"}))
    await obr(v)

    assert ya.obyavleniya == [("sbsauna", "c1", v.obyavlenie)]
    assert ya.obrabotano == [("sbsauna", "c1", "сколько стоит")]
