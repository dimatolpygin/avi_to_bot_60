# -*- coding: utf-8 -*-
"""Тесты очеловечивания (этап 9): дробление, задержки, дебаунс, прерываемость.

Сеть и ИИ здесь не нужны — отвечающий подменён функцией. Темп сжат до
миллисекунд тем же кодом, что работает в бою: `Tempo` для того и вынесен
объектом, чтобы тест не спал реальные двадцать секунд.
"""
from __future__ import annotations

import asyncio
import logging
import re

import pytest

from bot.chelovek.dispetcher import Dispetcher, Kanal, PamyatVPamyati
from bot.chelovek.razbivka import MAKS_REPLIK, Tempo, razbit

# Боевые интервалы, сжатые до миллисекунд. Окно склейки нарочно оставлено
# заметно больше остальных пауз: тест шлёт сообщения с интервалом 20 мс,
# и они обязаны попасть в одно окно даже с грубым таймером Windows.
BYSTRO = Tempo(
    chtenie=(0.01, 0.02),
    nabor_baza=(0.01, 0.02),
    nabor_za_simvol=0.0,
    nabor_potolok=0.05,
    mezhdu_replikami=(0.01, 0.02),
    okno_debounsa=(0.15, 0.2),
    potolok_debounsa=1.0,
    obnovlenie_pechataet=0.01,
)

DLINNYY = (
    "Липа три метра есть двух сортов: А по 513 рублей за штуку и В по 371. "
    "Разница только в сучках, на прочность она не влияет. "
    "Сорт А идёт почти без сучков, его чаще берут на стены на виду. "
    "Сорт В подешевле и выглядит живее, многие берут именно его. "
    "Какой из них вам посчитать?"
)


# ── Дробление ────────────────────────────────────────────────────────────────

def test_korotkiy_otvet_uhodit_odnoy_replikoy():
    t = "Да, липа три метра есть, 513 рублей за штуку."
    assert razbit(t) == [t]


def test_dlinnyy_otvet_drobitsya():
    repliki = razbit(DLINNYY)
    assert 2 <= len(repliki) <= MAKS_REPLIK


def test_rez_tolko_po_granice_predlozheniya():
    """Разрез внутри предложения разорвал бы цену или размер пополам.

    Поэтому каждая реплика, кроме последней, обязана кончаться знаком конца
    предложения, а склейка всех реплик — совпадать с исходным текстом.
    """
    for _ in range(30):                       # порог разреза случайный, гоняем много раз
        repliki = razbit(DLINNYY)
        for r in repliki[:-1]:
            assert r[-1] in ".!?…", r
        assert " ".join(repliki) == DLINNYY.strip()


def test_ne_bolshe_treh_replik():
    """Четыре сообщения подряд от продавца читаются как рассылка, а не как человек."""
    ochen_dlinnyy = " ".join(f"Предложение номер {i} про вагонку липу." for i in range(40))
    assert len(razbit(ochen_dlinnyy)) <= MAKS_REPLIK


def test_pustoy_otvet_ne_daet_replik():
    assert razbit("") == []
    assert razbit("   \n  ") == []


def test_perenos_stroki_daet_otdelnuyu_repliku():
    assert razbit("Здравствуйте, это Александра.\nЧто подбираете?") == [
        "Здравствуйте, это Александра.", "Что подбираете?"]


# ── Задержки ─────────────────────────────────────────────────────────────────

def test_pauzy_plavayushchie():
    """Фиксированная задержка выдаёт таймер вернее, чем любой стиль текста."""
    t = Tempo()
    znacheniya = {round(t.pauza_chteniya(), 3) for _ in range(20)}
    assert len(znacheniya) > 5
    assert all(t.chtenie[0] <= z <= t.chtenie[1] for z in znacheniya)


def test_nabor_zavisit_ot_dliny_i_ne_prevyshaet_potolok():
    t = Tempo()
    korotkaya = min(t.pauza_nabora("Есть, 513 рублей.") for _ in range(50))
    dlinnaya = min(t.pauza_nabora("x" * 400) for _ in range(50))
    assert dlinnaya > korotkaya
    assert t.pauza_nabora("x" * 10_000) <= t.nabor_potolok


def test_uskorit_szhimaet_vse_intervaly():
    t = Tempo().uskorit(0.1)
    assert t.chtenie == (0.2, 0.5)
    assert t.potolok_debounsa == pytest.approx(1.2)
    assert t.pauza_nabora("x" * 100) <= t.nabor_potolok == pytest.approx(2.6)


# ── Стенд ────────────────────────────────────────────────────────────────────

class Stend:
    """Диспетчер с подменённым отвечающим и каналом-запоминалкой."""

    def __init__(self, otvet="Есть, 513 рублей за штуку.", *, zaderzhka=0.0,
                 padat=False, tempo=BYSTRO):
        self.otvety = otvet if isinstance(otvet, list) else [otvet]
        self.zaderzhka = zaderzhka
        self.padat = padat
        self.voprosy: list[str] = []
        self.istorii: list[list[dict]] = []
        self.sobytiya: list[tuple[str, str]] = []   # («реплика»|«печатает», текст)
        self.pamyat = PamyatVPamyati()
        self.dispetcher = Dispetcher(self._otvetit, tempo=tempo, pamyat=self.pamyat)
        self.pervaya_replika = asyncio.Event()

    async def _otvetit(self, vopros: str, istoriya: list[dict]) -> str:
        self.voprosy.append(vopros)
        self.istorii.append(istoriya)
        if self.zaderzhka:
            await asyncio.sleep(self.zaderzhka)
        if self.padat:
            raise RuntimeError("модель недоступна")
        return self.otvety[min(len(self.voprosy) - 1, len(self.otvety) - 1)]

    def kanal(self) -> Kanal:
        async def otpravit(t: str) -> None:
            self.sobytiya.append(("реплика", t))
            self.pervaya_replika.set()

        async def pechataet() -> None:
            self.sobytiya.append(("печатает", ""))

        return Kanal(otpravit=otpravit, pechataet=pechataet, imya="klient")

    def prinyat(self, tekst: str, *, akkaunt="saunamart", chat=101):
        return self.dispetcher.prinyat(akkaunt, chat, tekst, self.kanal())

    @property
    def repliki(self) -> list[str]:
        return [t for vid, t in self.sobytiya if vid == "реплика"]

    async def istoriya(self, akkaunt="saunamart", chat=101) -> list[dict]:
        return await self.pamyat.istoriya(Dispetcher.klyuch(akkaunt, chat))


# ── Дебаунс ──────────────────────────────────────────────────────────────────

async def test_tri_soobshcheniya_podryad_daut_odin_otvet():
    """Клиент дописывает мысль тремя сообщениями — отвечаем один раз на всё."""
    s = Stend()
    s.prinyat("привет")
    await asyncio.sleep(0.02)
    s.prinyat("нужна вагонка липа")
    await asyncio.sleep(0.02)
    s.prinyat("три метра")
    await s.dispetcher.dozhdatsya()

    assert len(s.voprosy) == 1
    assert s.voprosy[0] == "привет\nнужна вагонка липа\nтри метра"
    assert len(s.repliki) == 1


async def test_potolok_debounsa_ne_daet_zhdat_vechno():
    """Человек, который печатает без остановки, всё равно получает ответ."""
    s = Stend(tempo=BYSTRO)
    zadacha = None
    for _ in range(12):                       # 12 × 0.1 с = 1.2 с > потолка в 1.0 с
        zadacha = s.prinyat("ещё вопрос")
        await asyncio.sleep(0.1)
    await asyncio.wait_for(s.dispetcher.dozhdatsya(), timeout=5)
    assert zadacha is not None
    assert len(s.voprosy) >= 1


async def test_soobshchenie_posle_otveta_nachinaet_novuyu_pachku():
    s = Stend()
    s.prinyat("липа три метра")
    await s.dispetcher.dozhdatsya()
    s.prinyat("а сорт б почём")
    await s.dispetcher.dozhdatsya()
    assert s.voprosy == ["липа три метра", "а сорт б почём"]


# ── Отправка ─────────────────────────────────────────────────────────────────

async def test_dlinnyy_otvet_prihodit_neskolkimi_replikami():
    s = Stend(otvet=DLINNYY)
    s.prinyat("расскажите про липу")
    await s.dispetcher.dozhdatsya()
    assert 2 <= len(s.repliki) <= MAKS_REPLIK
    assert " ".join(s.repliki) == DLINNYY.strip()


async def test_pered_kazhdoy_replikoy_status_pechataet():
    s = Stend(otvet=DLINNYY)
    s.prinyat("расскажите про липу")
    await s.dispetcher.dozhdatsya()
    bylo_pechataet = False
    for vid, _ in s.sobytiya:
        if vid == "печатает":
            bylo_pechataet = True
        else:
            assert bylo_pechataet, "реплика ушла без индикатора набора"
            bylo_pechataet = False


async def test_kanal_bez_indikatora_rabotaet():
    """У Авито индикатора набора нет — слой обязан работать и без него."""
    s = Stend()
    kanal = Kanal(otpravit=lambda t: _zapisat(s, t), pechataet=None, imya="avito")
    s.dispetcher.prinyat("saunamart", 7, "липа три метра", kanal)
    await s.dispetcher.dozhdatsya()
    assert s.repliki == ["Есть, 513 рублей за штуку."]


async def _zapisat(s: Stend, t: str) -> None:
    s.sobytiya.append(("реплика", t))


# ── Прерываемость ────────────────────────────────────────────────────────────

async def test_preryvanie_vo_vremya_generacii_ne_pishet_otvet_v_istoriyu():
    """Ответ не успел уйти в чат — значит его не было, и помнить его нельзя."""
    s = Stend(zaderzhka=1.0)
    s.prinyat("липа три метра")
    await asyncio.sleep(0.4)                  # окно склейки прошло, идёт генерация
    s.prinyat("хотя нет, покажите полок")
    await s.dispetcher.dozhdatsya()

    assert s.repliki == ["Есть, 513 рублей за штуку."]   # один ответ, на второй вопрос
    istoriya = await s.istoriya()
    assert [z["role"] for z in istoriya] == ["user", "assistant"]
    # Первый вопрос не потерян: он склеен со вторым в одну реплику клиента.
    assert "липа три метра" in istoriya[0]["content"]
    assert "покажите полок" in istoriya[0]["content"]


async def test_preryvanie_vo_vremya_otpravki_hranit_tolko_ushedshee():
    medlenno = Tempo(
        chtenie=(0.01, 0.01), nabor_baza=(0.3, 0.3), nabor_za_simvol=0.0,
        nabor_potolok=1.0, mezhdu_replikami=(0.01, 0.01),
        okno_debounsa=(0.05, 0.05), potolok_debounsa=1.0, obnovlenie_pechataet=0.05,
    )
    s = Stend(otvet=DLINNYY, tempo=medlenno)
    s.prinyat("расскажите про липу")
    await asyncio.wait_for(s.pervaya_replika.wait(), timeout=5)
    s.prinyat("а полок есть?")               # перебиваем после первой реплики
    await s.dispetcher.dozhdatsya()

    istoriya = await s.istoriya()
    otvety_bota = [z["content"] for z in istoriya if z["role"] == "assistant"]
    # В памяти — ровно то, что клиент увидел: недоотправленный хвост не сохранён.
    assert otvety_bota[0] in DLINNYY
    assert len(otvety_bota[0]) < len(DLINNYY)


async def test_istoriya_dohodit_do_otvechayushchego():
    s = Stend(otvet=["Есть, 513 рублей.", "Сорт В по 371."])
    s.prinyat("липа три метра")
    await s.dispetcher.dozhdatsya()
    s.prinyat("а сорт б почём")
    await s.dispetcher.dozhdatsya()

    assert s.istorii[0] == []
    assert [z["role"] for z in s.istorii[1]] == ["user", "assistant"]
    assert s.istorii[1][1]["content"] == "Есть, 513 рублей."


async def test_dva_chata_ne_smeshivayut_istorii():
    """Один и тот же chat_id в разных ботах — разные диалоги (этап 10)."""
    s = Stend()
    s.prinyat("вопрос саунамарта", akkaunt="saunamart", chat=55)
    s.prinyat("вопрос сбсауны", akkaunt="sbsauna", chat=55)
    await s.dispetcher.dozhdatsya()

    a = await s.istoriya("saunamart", 55)
    b = await s.istoriya("sbsauna", 55)
    assert a[0]["content"] == "вопрос саунамарта"
    assert b[0]["content"] == "вопрос сбсауны"


async def test_sbros_zabyvaet_dialog():
    s = Stend()
    s.prinyat("липа три метра")
    await s.dispetcher.dozhdatsya()
    await s.dispetcher.sbros("saunamart", 101)
    assert await s.istoriya() == []


# ── Ошибки ───────────────────────────────────────────────────────────────────

async def test_oshibka_otvechayushchego_daet_russkiy_folbek():
    """Модель недоступна — клиент получает человеческую фразу, а не молчание."""
    s = Stend(padat=True)
    s.prinyat("липа три метра")
    await s.dispetcher.dozhdatsya()
    assert len(s.repliki) == 1
    assert re.search(r"[а-яё]", s.repliki[0], re.I)     # по-русски, а не «I was unable»
    istoriya = await s.istoriya()
    # Фолбэк лёг в историю: иначе там останутся два `user` подряд.
    assert [z["role"] for z in istoriya] == ["user", "assistant"]


async def test_posle_oshibki_dialog_zhivoy():
    s = Stend(padat=True)
    s.prinyat("липа три метра")
    await s.dispetcher.dozhdatsya()
    s.padat = False
    s.prinyat("а полок есть?")
    await s.dispetcher.dozhdatsya()
    assert s.repliki[-1] == "Есть, 513 рублей за штуку."


async def test_dve_repliki_odnoy_roli_skleivayutsya_v_pamyati():
    """Часть провайдеров падает на двух `user` подряд — склеиваем на записи."""
    p = PamyatVPamyati()
    await p.dopisat("k", "user", "первое")
    await p.dopisat("k", "user", "второе")
    assert await p.istoriya("k") == [{"role": "user", "content": "первое\nвторое"}]


# ── Логи ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def logi(caplog):
    """Наш логгер не пропагирует записи вверх — на время теста включаем."""
    from bot.logger import logger
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="sbavito")
    yield caplog
    logger.propagate = False


async def test_v_loge_vidny_sklejka_pauzy_i_repliki(logi):
    s = Stend(otvet=DLINNYY)
    s.prinyat("нужна липа")
    await asyncio.sleep(0.02)
    s.prinyat("три метра")
    await s.dispetcher.dozhdatsya()

    stroki = [r.getMessage() for r in logi.records]
    assert any("склейка: 2 сообщения" in t for t in stroki)
    assert any("читаю сообщение" in t for t in stroki)
    assert any("набираю реплику" in t for t in stroki)
    assert sum("реплика 1/" in t for t in stroki) == 1


async def test_u_vsey_pachki_odin_request_id(logi):
    """Два склеенных сообщения и ответ на них сшиты одним id — иначе по логу
    не собрать, что именно бот отвечал этому клиенту."""
    s = Stend()
    s.prinyat("нужна липа")
    await asyncio.sleep(0.02)
    s.prinyat("три метра")
    await s.dispetcher.dozhdatsya()

    ridy = {r.rid for r in logi.records if "👤" in r.getMessage() or "🤖" in r.getMessage()}
    assert len(ridy) == 1
    assert "—" not in ridy
    assert {r.akkaunt for r in logi.records if "🤖" in r.getMessage()} == {"saunamart"}


async def test_preryvanie_vidno_v_loge(logi):
    s = Stend(zaderzhka=1.0)
    s.prinyat("липа три метра")
    await asyncio.sleep(0.4)
    s.prinyat("хотя нет, полок")
    await s.dispetcher.dozhdatsya()
    assert any("прервано новым сообщением" in r.getMessage() for r in logi.records)


async def test_pauzy_v_loge_raznye(logi):
    """Критерий приёмки: по логу видно, что задержки плавающие."""
    # Паузы в логе печатаются с точностью до десятых, поэтому разброс тут задан
    # в этих же десятых: на боевом темпе (набор 9–13 с) он куда заметнее.
    s = Stend(tempo=Tempo(
        chtenie=(0.01, 0.02), nabor_baza=(0.1, 0.5), nabor_za_simvol=0.0,
        nabor_potolok=1.0, mezhdu_replikami=(0.01, 0.02),
        okno_debounsa=(0.01, 0.02), potolok_debounsa=1.0, obnovlenie_pechataet=0.05))
    for i in range(5):
        s.prinyat(f"вопрос {i}")
        await s.dispetcher.dozhdatsya()

    nabory = {r.getMessage() for r in logi.records if "набираю реплику" in r.getMessage()}
    assert len(nabory) > 1
