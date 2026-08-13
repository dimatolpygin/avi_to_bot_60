# -*- coding: utf-8 -*-
"""Тесты обработчика вебхука amoJo → клиенту в Авито (14.9-B).

Проверяем на фейках (без сети): разбор события, маппинг из conversation.client_id,
петля-предохранитель (эхо наших импортов не пересылаем), флаг оператора, отправку
клиенту и дедуп повторной доставки. Форма события — из реального вебхука amoJo.
"""
from __future__ import annotations

from bot.crm.vebhuk import PriyomAmo


class _FakeApi:
    def __init__(self):
        self.otpravleno = []
        self.zagruzheno = []      # (байты, имя, тип)
        self.kartinki = []        # (chat_id, image_id)

    async def otpravit(self, chat_id, tekst):
        self.otpravleno.append((chat_id, tekst))
        return {"id": "avito-out-1"}

    async def zagruzit_kartinku(self, dannye, *, imya="image.jpg", tip="image/jpeg"):
        self.zagruzheno.append((dannye, imya, tip))
        return "img-999"

    async def otpravit_kartinku(self, chat_id, image_id):
        self.kartinki.append((chat_id, image_id))
        return {"id": "avito-img-1"}


class _FakeOperatory:
    def __init__(self):
        self.vzyato = []
        self.zapomneno = []

    async def vzyal(self, kod, chat):
        self.vzyato.append((kod, chat))

    async def zapomnit_otpravlennoe(self, kod, chat, msg_id):
        self.zapomneno.append((kod, chat, msg_id))


class _FakeZhurnal:
    def __init__(self):
        self.ishod = []

    async def ishodyashchee(self, chat_key, tekst, *, rid=None):
        self.ishod.append((chat_key, tekst))


def _sobytie(*, client_id="sbsauna:u2i-abc", sender=None, tekst="Здравствуйте!",
            tip="text", msg_id="m1", media=None, file_name=None):
    if sender is None:
        sender = {"id": "mgr-uuid", "name": "Денис Раневский"}   # живой менеджер
    inner = {"id": msg_id, "type": tip, "text": tekst}
    if media is not None:
        inner["media"] = media
    if file_name is not None:
        inner["file_name"] = file_name
    return {"message": {
        "receiver": {"id": "rcv", "name": "Клиент Авито", "client_id": "avito"},
        "sender": sender,
        "conversation": {"id": "skc-1", "client_id": client_id},
        "message": inner,
    }}


def _priyom(zhurnal=None, skachat=None):
    api = _FakeApi()
    op = _FakeOperatory()
    zh = {"sbsauna": zhurnal} if zhurnal is not None else None
    return PriyomAmo({"sbsauna": api}, op, zhurnaly=zh, skachat=skachat), api, op


async def test_replika_menedzhera_uhodit_klientu_i_stavit_flag():
    zh = _FakeZhurnal()
    priyom, api, op = _priyom(zhurnal=zh)
    await priyom(_sobytie(tekst="Перезвоните"))
    assert api.otpravleno == [("u2i-abc", "Перезвоните")]        # ушло клиенту
    assert op.vzyato == [("sbsauna", "u2i-abc")]                 # перехват из amoCRM
    assert op.zapomneno == [("sbsauna", "u2i-abc", "avito-out-1")]
    assert zh.ishod == [("u2i-abc", "Перезвоните")]             # в журнал панели


async def test_eho_nashego_importa_ne_pereotpravlyaetsya():
    """sender.client_id заполнен → это наш импорт (клиент/бот), не менеджер."""
    priyom, api, op = _priyom()
    await priyom(_sobytie(sender={"id": "bot:sbsauna", "name": "Роман",
                                  "client_id": "bot:sbsauna"}))
    assert api.otpravleno == [] and op.vzyato == []


async def test_bez_conversation_client_id_ignor():
    priyom, api, op = _priyom()
    await priyom(_sobytie(client_id=""))
    await priyom({"message": {"conversation": {"id": "skc"}}})   # вовсе без client_id
    assert api.otpravleno == [] and op.vzyato == []


async def test_neizvestnyy_akkaunt_ne_padaet():
    priyom, api, op = _priyom()
    await priyom(_sobytie(client_id="drugoy:chatX"))
    assert api.otpravleno == [] and op.vzyato == []


async def test_dedup_povtornoy_dostavki():
    priyom, api, op = _priyom()
    ev = _sobytie(msg_id="dup1")
    await priyom(ev)
    await priyom(ev)                                             # тот же id — повтор
    assert api.otpravleno == [("u2i-abc", "Здравствуйте!")]      # ровно один раз


async def test_vlozhenie_menedzhera_flag_stavit_no_ne_shlet():
    """Media от менеджера пока не пересылаем (отдельный узел), но флаг ставим."""
    priyom, api, op = _priyom()
    await priyom(_sobytie(tip="picture", tekst=""))
    assert api.otpravleno == []
    assert op.vzyato == [("sbsauna", "u2i-abc")]                 # перехват всё равно


async def test_pustoy_tekst_ne_shlet():
    priyom, api, op = _priyom()
    await priyom(_sobytie(tekst="   "))
    assert api.otpravleno == []
    assert op.vzyato == [("sbsauna", "u2i-abc")]


# ── Исходящие вложения (14.9): картинка менеджера → клиенту в Авито ───────────

async def test_kartinka_menedzhera_peresylaetsya_klientu():
    zh = _FakeZhurnal()
    skachano = []

    async def fake_skachat(url):
        skachano.append(url)
        return b"\xff\xd8jpeg", "image/png"

    priyom, api, op = _priyom(zhurnal=zh, skachat=fake_skachat)
    await priyom(_sobytie(tip="picture", tekst="",
                          media="https://files.amojo/ph.jpg", file_name="ph.jpg"))

    assert skachano == ["https://files.amojo/ph.jpg"]           # скачали media
    assert api.zagruzheno == [(b"\xff\xd8jpeg", "ph.jpg", "image/png")]  # тип из ответа
    assert api.kartinki == [("u2i-abc", "img-999")]             # ушла картинка клиенту
    assert op.vzyato == [("sbsauna", "u2i-abc")]                # перехват
    assert op.zapomneno == [("sbsauna", "u2i-abc", "avito-img-1")]  # id в журнал детекции
    assert zh.ishod == [("u2i-abc", "📷 фото")]                 # маркер в панель
    assert api.otpravleno == []                                 # текст не слали


async def test_kartinka_s_podpisyu_doshlet_tekst_otdelno():
    """Подпись к фото амоджо кладёт в text picture-события → шлём её отдельным
    сообщением (картинка в Авито подпись не несёт)."""
    zh = _FakeZhurnal()

    async def fake_skachat(url):
        return b"jpeg", "image/jpeg"

    priyom, api, op = _priyom(zhurnal=zh, skachat=fake_skachat)
    await priyom(_sobytie(tip="picture", tekst="тест",
                          media="https://files.amojo/ph.jpg"))

    assert api.kartinki == [("u2i-abc", "img-999")]             # фото ушло
    assert api.otpravleno == [("u2i-abc", "тест")]              # и подпись — текстом
    assert op.zapomneno == [("sbsauna", "u2i-abc", "avito-img-1"),
                            ("sbsauna", "u2i-abc", "avito-out-1")]  # оба id в журнал
    assert zh.ishod == [("u2i-abc", "📷 фото"), ("u2i-abc", "тест")]


async def test_kartinka_sboy_skachivaniya_ne_ronyaet_no_flag_stoit():
    async def fake_skachat(url):
        raise RuntimeError("404 от файлового хостинга amoJo")

    priyom, api, op = _priyom(skachat=fake_skachat)
    await priyom(_sobytie(tip="picture", tekst="", media="https://files.amojo/x.jpg"))

    assert api.kartinki == [] and api.zagruzheno == []          # ничего не ушло
    assert op.zapomneno == []                                   # нечего запоминать
    assert op.vzyato == [("sbsauna", "u2i-abc")]                # но диалог перехвачен


async def test_fayl_menedzhera_ne_peresylaetsya_avito_ne_umeet():
    """Файл (не картинка) Авито в чат не отправляет — только флаг оператора."""
    priyom, api, op = _priyom()
    await priyom(_sobytie(tip="file", tekst="",
                          media="https://files.amojo/doc.pdf", file_name="smeta.pdf"))
    assert api.kartinki == [] and api.otpravleno == []
    assert op.vzyato == [("sbsauna", "u2i-abc")]
