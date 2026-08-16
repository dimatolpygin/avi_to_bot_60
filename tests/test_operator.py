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
        self.ex: dict[str, int | None] = {}
        self.padat = padat

    def _p(self):
        if self.padat:
            raise ConnectionError("Redis недоступен")

    async def set(self, k, v, ex=None):
        self._p(); self.kv[k] = v; self.ex[k] = ex

    async def get(self, k):
        self._p(); return self.kv.get(k)

    async def expire(self, k, ttl):
        self._p()
        if k in self.kv:
            self.ex[k] = ttl

    async def delete(self, k):
        self._p(); self.kv.pop(k, None); self.lists.pop(k, None); self.ex.pop(k, None)

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


# ── Авто-возврат бота по 3-суточной тишине (решение 11.08) ────────────────────

class _Chasy:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self):
        return self.t


async def test_ttl_avtovozvrat_v_pamyati():
    """Перехват сам истекает после ttl тишины — бот возвращается без панели."""
    ch = _Chasy()
    op = Operatory(None, ttl_s=100, chasy=ch)
    await op.vzyal("saunamart", "c1")
    assert await op.vedet("saunamart", "c1") is True
    ch.t += 99
    assert await op.vedet("saunamart", "c1") is True      # ещё держит
    ch.t += 2                                             # прошло 101 > 100
    assert await op.vedet("saunamart", "c1") is False     # бот вернулся сам


async def test_prodlit_otodvigaet_vozvrat():
    """Новый контакт продлевает перехват: отсчёт идёт от последнего сообщения."""
    ch = _Chasy()
    op = Operatory(None, ttl_s=100, chasy=ch)
    await op.vzyal("saunamart", "c1")
    ch.t += 90
    await op.prodlit("saunamart", "c1")                   # контакт на 90-й секунде
    ch.t += 90                                            # 90 после продления < 100
    assert await op.vedet("saunamart", "c1") is True
    ch.t += 20                                            # 110 от продления > 100
    assert await op.vedet("saunamart", "c1") is False


async def test_prodlit_ne_ozhivlyaet_snyatyy():
    op = Operatory(None, ttl_s=100)
    await op.prodlit("saunamart", "c1")                   # флага нет
    assert await op.vedet("saunamart", "c1") is False


async def test_vzyal_stavit_ttl_v_redis():
    r = FakeRedis()
    await Operatory(r, ttl_s=259200).vzyal("saunamart", "c1")
    assert r.ex["sbavito:operator:saunamart:c1"] == 259200


async def test_prodlit_expire_v_redis():
    r = FakeRedis()
    op = Operatory(r, ttl_s=100)
    await op.vzyal("saunamart", "c1")
    r.ex["sbavito:operator:saunamart:c1"] = 5             # как будто TTL утёк
    await op.prodlit("saunamart", "c1")
    assert r.ex["sbavito:operator:saunamart:c1"] == 100   # заново продлён


async def test_prodlit_bez_flaga_redis_noop():
    r = FakeRedis()
    await Operatory(r, ttl_s=100).prodlit("saunamart", "c1")   # ключа нет
    assert "sbavito:operator:saunamart:c1" not in r.ex


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


# ── Зеркалирование ручных ответов менеджера в amoCRM (14.12) ──────────────────

class _FakeZerkalo:
    """Ловит вызовы зеркала amoCRM (без сети)."""

    def __init__(self):
        self.vhodyashchie = []       # (chat, tekst)
        self.ishodyashchie = []      # (chat, tekst, msgid)

    async def vhodyashchee(self, chat_id, msg_id, avtor_id, tekst, *, imya=None):
        self.vhodyashchie.append((chat_id, tekst))

    async def vhodyashchee_vlozhenie(self, chat_id, msg_id, avtor_id, vlozhenie, *, imya=None):
        pass

    async def ishodyashchee(self, chat_id, tekst, *, avtor_id=None, imya_klienta=None, msgid=None):
        self.ishodyashchie.append((chat_id, tekst, msgid))


def _out(mid, text, created=100):
    return {"id": mid, "direction": "out", "created": created, "content": {"text": text}}


async def test_operatorskiy_otvet_zerkalitsya_v_amo():
    """Ручной ответ менеджера в Авито (чужое исходящее) уходит в карточку amoCRM."""
    op = Operatory(FakeRedis())
    await op.zapomnit_otpravlennoe("saunamart", "c1", "bot-old")   # бот тут говорил
    api = _FakeAPI(soobshcheniya=[_out("mgr-1", "Оставьте номер, перезвоним")])
    ya, zerk = _FakeYadro(), _FakeZerkalo()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, zerkalo=zerk, operatory=op)

    await obr(_vhod(chat_id="c1", text="а доставка?"))

    assert zerk.ishodyashchie == [("c1", "Оставьте номер, перезвоним", "avito-out:mgr-1")]
    assert ya.obrabotano == []                              # перехват заглушил бота
    assert await op.vedet("saunamart", "c1") is True


async def test_operatorskiy_otvet_ne_dublitsya_na_sleduyushchem_tike():
    op = Operatory(FakeRedis())
    await op.zapomnit_otpravlennoe("saunamart", "c1", "bot-old")
    api = _FakeAPI(soobshcheniya=[_out("mgr-1", "Оставьте номер")])
    ya, zerk = _FakeYadro(), _FakeZerkalo()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, zerkalo=zerk, operatory=op)

    await obr(_vhod(chat_id="c1", text="раз"))
    await obr(_vhod(chat_id="c1", msg_id="m2", text="два"))     # то же окно, тот же mgr-1

    assert len(zerk.ishodyashchie) == 1                     # зеркалировано ровно раз


async def test_novyy_operatorskiy_otvet_zerkalitsya_pod_flagom():
    """Флаг уже стоит, а менеджер написал ещё — новая реплика тоже уходит в amoCRM."""
    op = Operatory(FakeRedis())
    await op.zapomnit_otpravlennoe("saunamart", "c1", "bot-old")
    ya, zerk = _FakeYadro(), _FakeZerkalo()
    # 1-й тик: mgr-1
    api1 = _FakeAPI(soobshcheniya=[_out("mgr-1", "Первый ответ", 100)])
    await sdelat_obrabotchik("saunamart", api1, ya, None, zerkalo=zerk, operatory=op)(
        _vhod(chat_id="c1", text="раз"))
    # 2-й тик: менеджер добавил mgr-2 (флаг перехвата уже стоит)
    api2 = _FakeAPI(soobshcheniya=[_out("mgr-1", "Первый ответ", 100),
                                   _out("mgr-2", "Второй ответ", 200)])
    await sdelat_obrabotchik("saunamart", api2, ya, None, zerkalo=zerk, operatory=op)(
        _vhod(chat_id="c1", msg_id="m2", text="два"))

    assert [t for _, t, _ in zerk.ishodyashchie] == ["Первый ответ", "Второй ответ"]


async def test_svoi_repliki_bota_ne_zerkalyatsya_povtorno():
    """Исходящее самого бота уже ушло в amoCRM при отправке — второй раз не шлём."""
    op = Operatory(FakeRedis())
    await op.zapomnit_otpravlennoe("saunamart", "c1", "bot-7")
    api = _FakeAPI(soobshcheniya=[_out("bot-7", "Ответ бота")])
    ya, zerk = _FakeYadro(), _FakeZerkalo()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, zerkalo=zerk, operatory=op)

    await obr(_vhod(chat_id="c1", text="ещё"))

    assert zerk.ishodyashchie == []                         # своё не дублируем
    assert ya.obrabotano == [("saunamart", "c1", "ещё")]    # бот отвечает


async def test_holodny_start_ne_zalivaet_istoriyu_v_amo():
    """Журнал пуст (aktiven=False): прошлые исходящие в amoCRM разом не льём."""
    op = Operatory(FakeRedis())
    api = _FakeAPI(soobshcheniya=[_out("staraya-1", "старое 1", 90),
                                  _out("staraya-2", "старое 2", 100)])
    ya, zerk = _FakeYadro(), _FakeZerkalo()
    obr = sdelat_obrabotchik("saunamart", api, ya, None, zerkalo=zerk, operatory=op)

    await obr(_vhod(chat_id="c1", text="привет"))

    assert zerk.ishodyashchie == []                         # историю не зеркалим


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
