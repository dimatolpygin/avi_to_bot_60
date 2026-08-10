# -*- coding: utf-8 -*-
"""Тесты перехвата оператором (этап 14.8).

Сети нет: Redis подменён структурами в памяти, Avito API — фейком. Проверяем
флаг перехвата и его мягкую деградацию, детекцию чужого исходящего, молчание
бота под оператором и возврат бота кнопкой панели.
"""
from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from bot.channels.avito import (Vhodyashchee, _kanal_avito, izvlech_vhodyashchee,
                                 posledny_ishodyashchiy, sdelat_obrabotchik)
from bot.operator import Operatory
from bot.panel import api as panel_api


# ── Фейки ─────────────────────────────────────────────────────────────────────

class FakeRedis:
    """Минимальный Redis: строки (флаг) + списки (журнал реплик бота)."""

    def __init__(self, padat: bool = False):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.padat = padat

    def _p(self):
        if self.padat:
            raise ConnectionError("Redis недоступен")

    async def set(self, k, v):
        self._p(); self.kv[k] = v

    async def get(self, k):
        self._p(); return self.kv.get(k)

    async def delete(self, k):
        self._p(); self.kv.pop(k, None); self.lists.pop(k, None)

    async def rpush(self, k, v):
        self._p(); self.lists.setdefault(k, []).append(v)

    async def ltrim(self, k, a, b):
        self._p()
        sp = self.lists.get(k, [])
        self.lists[k] = sp[a:] if b == -1 else sp[a:b + 1]

    async def lrange(self, k, a, b):
        self._p()
        sp = self.lists.get(k, [])
        return sp[a:] if b == -1 else sp[a:b + 1]

    async def llen(self, k):
        self._p(); return len(self.lists.get(k, []))


class _FakeYadro:
    def __init__(self):
        self.obrabotano = []
        self.obyavleniya = []

    def zapomnit_obyavlenie(self, kod, chat, obyavlenie):
        self.obyavleniya.append((kod, chat, obyavlenie))

    def obrabotat(self, kod, chat, tekst, kanal):
        self.obrabotano.append((kod, chat, tekst))


class _FakeAPI:
    def __init__(self, soobshcheniya=None, otvet_id="new"):
        self._soobshcheniya = soobshcheniya or []
        self._otvet_id = otvet_id
        self.otpravleno = []

    async def otpravit(self, chat_id, tekst):
        self.otpravleno.append((chat_id, tekst))
        return {"id": self._otvet_id}

    async def soobshcheniya(self, chat_id, *, limit=20):
        return list(self._soobshcheniya)


def _vhod(chat_id="c1", msg_id="m1", text="привет"):
    return izvlech_vhodyashchee({
        "id": chat_id,
        "last_message": {"id": msg_id, "author_id": 42, "direction": "in",
                         "content": {"text": text}}})


# ── Флаг перехвата ────────────────────────────────────────────────────────────

async def test_flag_stavitsya_chitaetsya_snimaetsya():
    op = Operatory(FakeRedis())
    assert await op.vedet("saunamart", "c1") is False
    await op.vzyal("saunamart", "c1")
    assert await op.vedet("saunamart", "c1") is True
    await op.snyat("saunamart", "c1")
    assert await op.vedet("saunamart", "c1") is False


async def test_flag_bez_redis_rabotaet_v_pamyati():
    op = Operatory(None)
    await op.vzyal("sbsauna", "c9")
    assert await op.vedet("sbsauna", "c9") is True
    assert await op.vedet("sbsauna", "drugoy") is False


async def test_flag_raznyh_akkauntov_ne_smeshivayutsya():
    op = Operatory(FakeRedis())
    await op.vzyal("saunamart", "c1")
    assert await op.vedet("sbsauna", "c1") is False   # тот же chat, другой аккаунт


async def test_sboy_redis_ne_morozit_bota():
    """Redis лёг: флаг не прочитать → считаем, что оператора нет (бот отвечает),
    а не морозим диалог навсегда."""
    op = Operatory(FakeRedis(padat=True))
    assert await op.vedet("saunamart", "c1") is False
    await op.vzyal("saunamart", "c1")     # не бросает
    await op.snyat("saunamart", "c1")     # не бросает


# ── Журнал реплик бота ────────────────────────────────────────────────────────

async def test_bot_otpravlyal_pomnit_svoi_id():
    op = Operatory(FakeRedis())
    await op.zapomnit_otpravlennoe("saunamart", "c1", "bot-1")
    assert await op.bot_otpravlyal("saunamart", "c1", "bot-1") is True
    assert await op.bot_otpravlyal("saunamart", "c1", "chuzhoy") is False


async def test_pustoy_id_ne_bot():
    op = Operatory(FakeRedis())
    assert await op.bot_otpravlyal("saunamart", "c1", None) is False


async def test_sboy_redis_v_zhurnale_schitaet_bot():
    """Не прочитать журнал = не глушить ложно: считаем сообщение своим."""
    op = Operatory(FakeRedis(padat=True))
    assert await op.bot_otpravlyal("saunamart", "c1", "любой") is True


# ── Детектор последнего исходящего ────────────────────────────────────────────

def test_posledny_ishodyashchiy_beret_svezhee_out():
    msgs = [
        {"id": "a", "direction": "out", "created": 100},
        {"id": "b", "direction": "in", "created": 150},
        {"id": "c", "direction": "out", "created": 200},
    ]
    assert posledny_ishodyashchiy(msgs)["id"] == "c"


def test_posledny_ishodyashchiy_net_out():
    assert posledny_ishodyashchiy([{"id": "b", "direction": "in", "created": 1}]) is None
    assert posledny_ishodyashchiy([]) is None


# ── Обработчик под оператором ─────────────────────────────────────────────────

async def test_pod_operatorom_bot_molchit():
    op = Operatory(FakeRedis())
    await op.vzyal("saunamart", "c1")
    ya, api = _FakeYadro(), _FakeAPI()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, operatory=op)

    await obr(_vhod(chat_id="c1", text="почём липа"))

    assert ya.obrabotano == []            # ядру не отдаём — ведёт менеджер


async def test_chuzhoe_ishodyashchee_stavit_flag_i_glushit():
    """Бот в чате уже говорил (журнал непуст), а последнее исходящее — не его
    (ответил менеджер): ставим флаг, бот в этом сообщении молчит."""
    op = Operatory(FakeRedis())
    await op.zapomnit_otpravlennoe("saunamart", "c1", "bot-old")   # бот тут говорил
    api = _FakeAPI(soobshcheniya=[{"id": "mgr-1", "direction": "out", "created": 100}])
    ya = _FakeYadro()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, operatory=op)

    await obr(_vhod(chat_id="c1", text="а доставка?"))

    assert ya.obrabotano == []
    assert await op.vedet("saunamart", "c1") is True


async def test_holodny_start_ne_glushit_a_bazlainit():
    """Журнал пуст (бот тут ещё не говорил под 14.8): чужое-на-вид исходящее —
    это могла быть реплика самого бота ДО 14.8. Не глушим, а запоминаем её как
    базлайн; бот отвечает на входящее."""
    op = Operatory(FakeRedis())
    api = _FakeAPI(soobshcheniya=[{"id": "staraya", "direction": "out", "created": 100}])
    ya = _FakeYadro()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, operatory=op)

    await obr(_vhod(chat_id="c1", text="почём липа"))

    assert ya.obrabotano == [("saunamart", "c1", "почём липа")]     # бот ответил
    assert await op.vedet("saunamart", "c1") is False              # не заглушён
    assert await op.bot_otpravlyal("saunamart", "c1", "staraya")   # базлайн записан


async def test_posle_bazlaina_novoe_chuzhoe_glushit():
    """После базлайна холодного старта СЛЕДУЮЩЕЕ новое чужое исходящее уже глушит."""
    op = Operatory(FakeRedis())
    ya = _FakeYadro()
    # 1-е входящее: базлайним «staraya», бот отвечает
    api1 = _FakeAPI(soobshcheniya=[{"id": "staraya", "direction": "out", "created": 100}])
    await sdelat_obrabotchik("saunamart", api1, ya, None, operatory=op)(
        _vhod(chat_id="c1", text="привет"))
    assert await op.vedet("saunamart", "c1") is False
    # 2-е входящее: менеджер ответил «mgr-2» (новее) — теперь глушим
    api2 = _FakeAPI(soobshcheniya=[
        {"id": "staraya", "direction": "out", "created": 100},
        {"id": "mgr-2", "direction": "out", "created": 200}])
    ya.obrabotano.clear()
    await sdelat_obrabotchik("saunamart", api2, ya, None, operatory=op)(
        _vhod(chat_id="c1", text="ещё вопрос"))
    assert ya.obrabotano == []
    assert await op.vedet("saunamart", "c1") is True


async def test_svoe_ishodyashchee_ne_glushit():
    """Последнее исходящее отправил сам бот — перехвата нет, отвечаем."""
    op = Operatory(FakeRedis())
    await op.zapomnit_otpravlennoe("saunamart", "c1", "bot-7")
    api = _FakeAPI(soobshcheniya=[{"id": "bot-7", "direction": "out", "created": 100}])
    ya = _FakeYadro()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, operatory=op)

    await obr(_vhod(chat_id="c1", text="ещё вопрос"))

    assert ya.obrabotano == [("saunamart", "c1", "ещё вопрос")]
    assert await op.vedet("saunamart", "c1") is False


async def test_bez_ishodyashchih_bot_otvechaet():
    op = Operatory(FakeRedis())
    api = _FakeAPI(soobshcheniya=[{"id": "m1", "direction": "in", "created": 1}])
    ya = _FakeYadro()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, operatory=op)

    await obr(_vhod(chat_id="c1", text="здравствуйте"))

    assert ya.obrabotano == [("saunamart", "c1", "здравствуйте")]


async def test_vlozhenie_pod_operatorom_molchit():
    """Под оператором даже на вложение бот не отвечает дежурной просьбой."""
    op = Operatory(FakeRedis())
    await op.vzyal("saunamart", "c1")
    api = _FakeAPI()
    ya = _FakeYadro()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, operatory=op)
    chat = {"id": "c1", "last_message": {"id": "m1", "direction": "in", "content": {}}}

    await obr(izvlech_vhodyashchee(chat))

    assert api.otpravleno == []           # молчим, не перебиваем менеджера


async def test_bez_operatory_povedenie_prezhnee():
    """operatory=None → старый путь: бот отвечает, детекции нет."""
    api = _FakeAPI()
    ya = _FakeYadro()
    obr = sdelat_obrabotchik("saunamart", api, ya, None)

    await obr(_vhod(chat_id="c1", text="привет"))

    assert ya.obrabotano == [("saunamart", "c1", "привет")]


# ── Запись id реплики бота через канал ────────────────────────────────────────

async def test_kanal_zapominaet_otpravlennoe():
    op = Operatory(FakeRedis())
    api = _FakeAPI(otvet_id="bot-42")
    kanal = _kanal_avito(api, "c1", "avito:42", operatory=op, kod="saunamart")

    await kanal.otpravit("Здравствуйте, это Александра.")

    assert api.otpravleno == [("c1", "Здравствуйте, это Александра.")]
    assert await op.bot_otpravlyal("saunamart", "c1", "bot-42") is True


# ── Панель: статус оператора и возврат бота ───────────────────────────────────

class _Rezult:
    def __init__(self, stroki=None, first=None):
        self._stroki = stroki or []
        self._first = first

    def all(self):
        return self._stroki

    def first(self):
        return self._first


class _Sessiya:
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


async def test_chaty_pokazyvayut_status_operatora():
    from bot.models import Dialog
    from datetime import datetime, timezone
    op = Operatory(FakeRedis())
    await op.vzyal("saunamart", "c1")
    d = Dialog(id=1, account_id=1, channel="avito", chat_key="c1")
    d.last_message_at = datetime(2026, 8, 10, tzinfo=timezone.utc)
    app = panel_api.sozdat_prilozhenie(
        _fabrika(_Rezult(stroki=[(d, "saunamart", "текст")])),
        token="sekret", origin="*", operatory=op)
    async with TestClient(TestServer(app)) as kl:
        resp = await kl.get("/api/chats", headers={"Authorization": "Bearer sekret"})
        data = await resp.json()
        assert data["chats"][0]["operator"] is True


async def test_resume_snimaet_flag():
    op = Operatory(FakeRedis())
    await op.vzyal("saunamart", "c1")
    # dialog_adres → (код аккаунта, chat_key)
    app = panel_api.sozdat_prilozhenie(
        _fabrika(_Rezult(first=("saunamart", "c1"))),
        token="sekret", origin="*", operatory=op)
    async with TestClient(TestServer(app)) as kl:
        resp = await kl.post("/api/dialog/5/resume",
                             headers={"Authorization": "Bearer sekret"})
        assert resp.status == 200
    assert await op.vedet("saunamart", "c1") is False


async def test_resume_bez_tokena_401():
    op = Operatory(FakeRedis())
    app = panel_api.sozdat_prilozhenie(_fabrika(_Rezult(first=("saunamart", "c1"))),
                                       token="sekret", origin="*", operatory=op)
    async with TestClient(TestServer(app)) as kl:
        resp = await kl.post("/api/dialog/5/resume")
        assert resp.status == 401


async def test_resume_net_dialoga_404():
    op = Operatory(FakeRedis())
    app = panel_api.sozdat_prilozhenie(_fabrika(_Rezult(first=None)),
                                       token="sekret", origin="*", operatory=op)
    async with TestClient(TestServer(app)) as kl:
        resp = await kl.post("/api/dialog/999/resume",
                             headers={"Authorization": "Bearer sekret"})
        assert resp.status == 404
