# -*- coding: utf-8 -*-
"""Тесты журнала диалога в Postgres (этап 14.7, кирпич 1).

БД нет — фейковая сессия/фабрика (как в test_amo). Проверяем: маппинг ролей
client/bot, upsert диалога (новый vs существующий), best-effort (ошибка глотается,
диалог не падает), no-op на пустом тексте/без фабрики, и проводку в адаптер Авито.
"""
from __future__ import annotations

from bot.models import Account, Dialog, Message
from bot.zhurnal import Zhurnal


# ── Фейковые сессия/фабрика ───────────────────────────────────────────────────

class _Rezult:
    def __init__(self, val):
        self._val = val

    def scalar_one_or_none(self):
        return self._val


class _Sessiya:
    """Первый execute → Account, второй → Dialog (как в Zhurnal._zapisat)."""

    def __init__(self, akk, dialog):
        self._akk = akk
        self._dialog = dialog
        self._n = 0
        self.added: list = []
        self.committed = False

    async def execute(self, _stmt):
        self._n += 1
        return _Rezult(self._akk if self._n == 1 else self._dialog)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for o in self.added:                       # эмулируем присвоение id диалогу
            if isinstance(o, Dialog) and o.id is None:
                o.id = 77

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


def _fabrika(sess):
    return lambda: sess


AKK = Account(id=1, code="sbsauna")


def _soobshcheniya(sess):
    return [o for o in sess.added if isinstance(o, Message)]


# ── Запись ───────────────────────────────────────────────────────────────────

async def test_vhodyashchee_novyy_dialog_rol_client():
    sess = _Sessiya(AKK, None)               # диалога ещё нет → заведём
    zh = Zhurnal(_fabrika(sess), "sbsauna", "avito")
    await zh.vhodyashchee("u2i-abc", "почём отделка", imya="avito:42")

    dialogi = [o for o in sess.added if isinstance(o, Dialog)]
    msgs = _soobshcheniya(sess)
    assert len(dialogi) == 1 and dialogi[0].channel == "avito"
    assert dialogi[0].chat_key == "u2i-abc" and dialogi[0].client_username == "avito:42"
    assert len(msgs) == 1 and msgs[0].role == "client"
    assert msgs[0].body == "почём отделка" and msgs[0].dialog_id == 77
    assert sess.committed is True


async def test_ishodyashchee_sushchestvuyushchiy_dialog_rol_bot():
    dialog = Dialog(id=5, account_id=1, channel="avito", chat_key="u2i-abc")
    sess = _Sessiya(AKK, dialog)
    zh = Zhurnal(_fabrika(sess), "sbsauna", "avito")
    await zh.ishodyashchee("u2i-abc", "Здравствуйте, это Роман")

    assert not [o for o in sess.added if isinstance(o, Dialog)]   # новый не заводим
    msgs = _soobshcheniya(sess)
    assert len(msgs) == 1 and msgs[0].role == "bot" and msgs[0].dialog_id == 5
    assert sess.committed is True


async def test_pustoy_tekst_ne_pishet():
    sess = _Sessiya(AKK, None)
    zh = Zhurnal(_fabrika(sess), "sbsauna", "avito")
    await zh.vhodyashchee("u2i-abc", "   ")
    assert sess.added == [] and sess.committed is False


async def test_bez_fabriki_noop():
    zh = Zhurnal(None, "sbsauna", "avito")
    await zh.ishodyashchee("u2i-abc", "текст")     # не должно падать


async def test_net_akkaunta_ne_pishet_soobshchenie():
    sess = _Sessiya(None, None)                    # Account не найден
    zh = Zhurnal(_fabrika(sess), "sbsauna", "avito")
    await zh.vhodyashchee("u2i-abc", "текст")
    assert _soobshcheniya(sess) == [] and sess.committed is False


async def test_oshibka_bd_glotaetsya():
    def bad():
        raise RuntimeError("БД легла")
    zh = Zhurnal(bad, "sbsauna", "avito")
    await zh.vhodyashchee("u2i-abc", "текст")      # не должно пробросить исключение


# ── Проводка в адаптер Авито ──────────────────────────────────────────────────

class _ZhurnalShpion:
    def __init__(self):
        self.vhod: list = []
        self.ishod: list = []

    async def vhodyashchee(self, chat, tekst, *, imya=None, rid=None):
        self.vhod.append((chat, tekst, imya))

    async def ishodyashchee(self, chat, tekst, *, rid=None):
        self.ishod.append((chat, tekst))


class _ApiShpion:
    async def otpravit(self, chat_id, tekst):
        return {"ok": True}


async def test_kanal_avito_zhurnalit_ishodyashchee():
    from bot.channels.avito import _kanal_avito
    zh = _ZhurnalShpion()
    kanal = _kanal_avito(_ApiShpion(), "u2i-abc", "avito:42", zhurnal=zh)
    await kanal.otpravit("ответ бота")
    assert zh.ishod == [("u2i-abc", "ответ бота")]


async def test_obrabotchik_zhurnalit_vhodyashchee():
    from bot.channels.avito import Vhodyashchee, sdelat_obrabotchik
    zh = _ZhurnalShpion()

    class _Yadro:
        def zapomnit_obyavlenie(self, *a): pass
        def obrabotat(self, *a): return None
        def otvechaet(self, kod): return True

    obr = sdelat_obrabotchik("sbsauna", _ApiShpion(), _Yadro(),
                             frozenset({"u2i-abc"}), zhurnal=zh)
    await obr(Vhodyashchee(chat_id="u2i-abc", msg_id="m1", author_id=42,
                           tekst="вопрос", obyavlenie=None))
    assert zh.vhod == [("u2i-abc", "вопрос", "avito:42")]
