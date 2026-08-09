# -*- coding: utf-8 -*-
"""Тесты этапа 10: профили аккаунтов, ядро, память в Redis, адаптер Telegram.

Сети нет нигде: модель подменена, Redis подменён списками в памяти, Telegram
только собирается (токен фиктивный, поллинг не запускается).
"""
from __future__ import annotations

import json
import os

import pytest

from bot.etl.import_prays import FAYL_PO_UMOLCHANIYU


def _skip_bez_praysa() -> None:
    """Клиентский прайс `материалы/прайс/*.csv` — вне git (публичный репо, CI).
    Ядро с каталогом из файла тогда не собрать → тест пропускаем, а не роняем."""
    if not os.path.exists(FAYL_PO_UMOLCHANIYU):
        pytest.skip("прайс материалы/прайс/*.csv вне git — тест на живом каталоге пропущен")

from bot import core
from bot.ai import agent
from bot.chelovek.dispetcher import Dispetcher, Kanal
from bot.chelovek.razbivka import Tempo
from bot.config import Config, GoogleConfig, OpenRouterConfig, PgConfig
from bot.core import Yadro
from bot.pamyat import PamyatRedis
from bot.profili import PROFILI, profil
from bot.search.katalog import iz_fayla_praysa
from bot.search.search import Poisk
from bot.seed import AKKAUNTY

BYSTRO = Tempo(
    chtenie=(0.01, 0.01), nabor_baza=(0.01, 0.01), nabor_za_simvol=0.0,
    nabor_potolok=0.05, mezhdu_replikami=(0.01, 0.01),
    okno_debounsa=(0.05, 0.06), potolok_debounsa=1.0, obnovlenie_pechataet=0.05,
)


# ── Профили ──────────────────────────────────────────────────────────────────

def test_kody_profiley_sovpadayut_s_akkauntami_bd():
    """Код связывает строку в `accounts`, токен в `.env` и ключ сессии Redis.
    Разъедутся — сломаются все три места разом."""
    assert set(PROFILI) == {kod for kod, _, _ in AKKAUNTY}


def test_u_uslug_prompt_est_u_tovarnogo_sobiraetsya_iz_kataloga():
    assert profil("saunamart").prompt is None      # собирается из живого каталога
    for kod in ("sbsauna", "sbsauna_deshman"):
        assert profil(kod).prompt, kod


@pytest.mark.parametrize("kod", ["sbsauna", "sbsauna_deshman"])
def test_prompt_uslug_neset_pravila_zakazchika(kod):
    """Правила стиля общие для всех трёх аккаунтов — их нельзя потерять
    при копировании промпта."""
    p = profil(kod).prompt
    assert "Никаких длинных тире" in p
    assert "НЕ проси" in p and "РОВНО ОДИН раз" in p
    assert "Без эмодзи" in p


@pytest.mark.parametrize("kod", ["sbsauna", "sbsauna_deshman"])
def test_prompt_uslug_neset_stop_list_i_geo(kod):
    """Стоп-лист заказчика: отдельные работы не берём, с чужим материалом
    не работаем, с нуля не строим. Обещать это — прямой убыток."""
    p = profil(kod).prompt
    assert "только печь" in p and "только дверь" in p
    assert "чужим материалом" in p
    assert "Строительство с нуля" in p or "строительство с нуля" in p
    assert "ЮФО" in p and "Крым" in p


def test_orientiry_cen_raznye_u_dvuh_akkauntov_uslug():
    assert "500 тысяч" in profil("sbsauna").prompt
    assert "350 тысяч" in profil("sbsauna_deshman").prompt
    # Дешман готов продать материалы, основной аккаунт — нет: разные правила.
    assert "не продаём" in profil("sbsauna").prompt
    assert "МАТЕРИАЛЫ ОТДЕЛЬНО мы продать можем" in profil("sbsauna_deshman").prompt


def test_uslugam_zapreshcheno_schitat_obemy_i_sroki():
    """Главный дефект прогона 23.07: бот считал вслух то, чего не знает.

    Обещал «40-45 кубометров вагонки на 480-675 тысяч» при реальных 0.35 м³ —
    ошибка в сто раз, и сумма выше, чем вся парная под ключ у него же.
    """
    for kod in ("sbsauna", "sbsauna_deshman"):
        p = profil(kod).prompt
        assert "ЧЕГО ТЫ НЕ ЗНАЕШЬ" in p, kod
        assert "кубометры" in p, kod
        assert "сколько дней, недель или месяцев" in p, kod
        # Ориентир «от» нельзя пересчитывать под размеры клиента.
        assert "не умножай на его размеры" in p, kod


def test_privetstvie_est_u_vseh_i_nazyvaet_personu():
    for kod, prof in PROFILI.items():
        assert prof.menedzher in prof.privetstvie, kod
        assert prof.kompaniya in prof.privetstvie, kod


# ── Агент без прайса ─────────────────────────────────────────────────────────

class FakeChat:
    """Подменённый клиент модели: записывает вызовы, отдаёт заготовки."""

    def __init__(self, otvety: list[str]):
        self.otvety = list(otvety)
        self.vyzovy: list[dict] = []

    async def __call__(self, cfg, messages, tools=None, tool_choice="auto"):
        self.vyzovy.append({"messages": messages, "tools": tools,
                            "tool_choice": tool_choice})
        return {"content": self.otvety.pop(0), "tool_calls": None}


CFG = OpenRouterConfig(api_key="x", model="test/model")


async def test_agent_bez_poiska_ne_daet_instrument(monkeypatch):
    """У аккаунтов услуг прайса нет: инструмент с пустым каталогом отвечал бы
    «не найдено» на любой вопрос про отделку."""
    fake = FakeChat(["Отделка парной под ключ от 500 тысяч, зависит от размера."])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(CFG, None, [], "сколько стоит отделка парной",
                            sistemny=profil("sbsauna").prompt)
    assert "500" in r.otvet
    # Поиска нет, а передача лида есть: прайса у аккаунта не существует,
    # контакт клиента — существует, и для отделки он даже важнее.
    imena = [i["function"]["name"] for i in fake.vyzovy[0]["tools"]]
    assert imena == ["save_lead"]
    assert r.zaprosy_poiska == []


async def test_agent_bez_poiska_ne_forsiruet_predohranitel(monkeypatch):
    """Предохранители про «отказал, не заглянув в прайс». Прайса нет — форсить
    нечего, иначе агент зациклится на несуществующем инструменте."""
    fake = FakeChat(["Отдельную установку печи мы не делаем, только отделку целиком."])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(CFG, None, [], "поставьте мне только печь",
                            sistemny=profil("sbsauna").prompt)
    assert len(fake.vyzovy) == 1          # ровно один вызов, без форс-повтора
    assert r.forsirovan_poisk is False


async def test_agent_bez_promta_i_bez_poiska_padaet_ponyatno():
    with pytest.raises(ValueError, match="системный промпт обязателен"):
        await agent.otvetit(CFG, None, [], "привет")


async def test_povtornoe_privetstvie_snimaetsya_kodom(monkeypatch):
    """Баг живого прогона 23.07: на /start клиент получает стартовое сообщение
    из брифа, а на первый же вопрос бот здоровается второй раз. Промптом это
    не лечится — модель не узнаёт своё приветствие в чужой формулировке."""
    fake = FakeChat(["Здравствуйте, это Александра из Saunamart. Есть вагонка липа и полок."])
    monkeypatch.setattr(agent, "chat", fake)
    istoriya = [{"role": "assistant",
                 "content": "Вас приветствует компания Saunamart. Меня зовут Александра."}]
    r = await agent.otvetit(CFG, None, istoriya, "что есть у вас?",
                            sistemny="тестовый промпт")
    assert r.otvet == "Есть вагонка липа и полок."


async def test_pervoe_privetstvie_ostaetsya(monkeypatch):
    """В пустом диалоге здороваться нужно — на Авито первый контакт идёт
    не через /start, а сразу с вопроса клиента."""
    fake = FakeChat(["Здравствуйте, это Александра из Saunamart. Есть вагонка липа."])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(CFG, None, [], "что есть?", sistemny="тестовый промпт")
    assert r.otvet.startswith("Здравствуйте")


def test_snyatie_privetstviya_ne_treplet_smysl():
    from bot.ai.stil import snyat_privetstvie

    # Приветствие только в начале реплики и только первым предложением.
    assert snyat_privetstvie("Добрый день! Липа есть.") == "Липа есть."
    assert snyat_privetstvie("Липа есть, 513 рублей.") == "Липа есть, 513 рублей."
    assert (snyat_privetstvie("Менеджер перезвонит и скажет: здравствуйте, это Saunamart.")
            == "Менеджер перезвонит и скажет: здравствуйте, это Saunamart.")
    # Если кроме приветствия ничего нет, пустое сообщение хуже лишнего «здравствуйте».
    assert snyat_privetstvie("Здравствуйте!") == "Здравствуйте!"


def test_prompt_saunamart_ne_izmenilsya_posle_vynosa_stilya():
    """Правила стиля вынесены в общую функцию — текст промпта Saunamart
    обязан остаться прежним, иначе этап 8 надо перепроверять заново."""
    stil = agent.pravila_stilya("Александра", "Saunamart")
    assert "Здравствуйте, это\n  Александра из Saunamart" in stil
    assert "не больше двух позиций" in stil
    # У услуг блок про позиции прайса выключается.
    assert "не больше двух позиций" not in agent.pravila_stilya(
        "Роман", "SB SAUNA", zhenskiy_rod=False, pro_pozicii=False)


def test_rod_beretsya_parametrom_a_ne_ugadyvaetsya():
    zhen = agent.pravila_stilya("Александра", "Saunamart")
    muzh = agent.pravila_stilya("Роман", "SB SAUNA", zhenskiy_rod=False)
    assert "уже здоровалась" in zhen and "первой НЕ проси" in zhen
    assert "уже здоровался" in muzh and "первым НЕ проси" in muzh


# ── Память в Redis ───────────────────────────────────────────────────────────

class FakeRedis:
    """Минимальный Redis на списках: только те команды, что нужны памяти."""

    def __init__(self, padat: bool = False):
        self.dannye: dict[str, list[str]] = {}
        self.ttl: dict[str, int] = {}
        self.padat = padat

    def _p(self):
        if self.padat:
            raise ConnectionError("Redis недоступен")

    async def lrange(self, k, a, b):
        self._p()
        spisok = self.dannye.get(k, [])
        return spisok[a:] if b == -1 else spisok[a:b + 1]

    async def lindex(self, k, i):
        self._p()
        spisok = self.dannye.get(k, [])
        return spisok[i] if spisok else None

    async def lset(self, k, i, v):
        self._p()
        self.dannye[k][i] = v

    async def rpush(self, k, v):
        self._p()
        self.dannye.setdefault(k, []).append(v)

    async def ltrim(self, k, a, b):
        self._p()
        self.dannye[k] = self.dannye.get(k, [])[a:] if b == -1 else self.dannye[k][a:b + 1]

    async def expire(self, k, s):
        self._p()
        self.ttl[k] = s

    async def delete(self, k):
        self._p()
        self.dannye.pop(k, None)


async def test_klyuch_pamyati_soderzhit_akkaunt():
    """Один chat_id приходит в три бота — без аккаунта в ключе истории смешаются."""
    a = PamyatRedis.redis_klyuch(Dispetcher.klyuch("saunamart", 42))
    b = PamyatRedis.redis_klyuch(Dispetcher.klyuch("sbsauna", 42))
    assert a != b
    assert a == "sbavito:dialog:saunamart:42"


async def test_pamyat_pishet_chitaet_i_stavit_ttl():
    r = FakeRedis()
    p = PamyatRedis(r, ttl_s=1800)
    await p.dopisat("saunamart:1", "user", "липа три метра")
    await p.dopisat("saunamart:1", "assistant", "513 рублей")
    assert await p.istoriya("saunamart:1") == [
        {"role": "user", "content": "липа три метра"},
        {"role": "assistant", "content": "513 рублей"}]
    assert r.ttl["sbavito:dialog:saunamart:1"] == 1800


async def test_pamyat_skleivaet_odinakovye_roli():
    r = FakeRedis()
    p = PamyatRedis(r)
    await p.dopisat("k", "user", "первое")
    await p.dopisat("k", "user", "второе")
    assert await p.istoriya("k") == [{"role": "user", "content": "первое\nвторое"}]


async def test_pamyat_hranit_tolko_poslednie_repliki():
    r = FakeRedis()
    p = PamyatRedis(r, maks_replik=4)
    for i in range(10):
        await p.dopisat("k", "user" if i % 2 == 0 else "assistant", f"реплика {i}")
    istoriya = await p.istoriya("k")
    assert len(istoriya) == 4
    assert istoriya[-1]["content"] == "реплика 9"


async def test_sboy_redis_ne_ronyaet_dialog():
    """Ответить без памяти лучше, чем не ответить: моргание кеша не должно
    выглядеть для клиента как поломка бота."""
    p = PamyatRedis(FakeRedis(padat=True))
    assert await p.istoriya("k") == []
    await p.dopisat("k", "user", "вопрос")     # не бросает
    await p.ochistit("k")


async def test_bitaya_zapis_ne_ronyaet_istoriyu():
    r = FakeRedis()
    r.dannye["sbavito:dialog:k"] = ["не json", json.dumps({"role": "user", "content": "ок"})]
    assert await PamyatRedis(r).istoriya("k") == [{"role": "user", "content": "ок"}]


# ── Ядро ─────────────────────────────────────────────────────────────────────

def _cfg() -> Config:
    return Config(
        pg=PgConfig(host="h", port=1, user="u", password="p", database="d", schema="s"),
        redis_url="redis://x", openrouter=CFG, log_level="info",
        google=GoogleConfig(creds_put="", tablica_id="t", list_name="l", interval_s=600),
        telegram_tokeny={"saunamart": "", "sbsauna": "", "sbsauna_deshman": ""},
    )


class SborKanala:
    def __init__(self):
        self.repliki: list[str] = []

    def kanal(self) -> Kanal:
        async def otpravit(t: str) -> None:
            self.repliki.append(t)
        return Kanal(otpravit=otpravit, pechataet=None, imya="test")


async def _yadro(monkeypatch, otvety: list[str]) -> tuple[Yadro, FakeChat, PamyatRedis]:
    _skip_bez_praysa()
    fake = FakeChat(otvety)
    monkeypatch.setattr(agent, "chat", fake)
    pamyat = PamyatRedis(FakeRedis())
    yadro = Yadro(_cfg(), pamyat, tempo=BYSTRO)
    await yadro.podgotovit(["saunamart", "sbsauna", "sbsauna_deshman"])
    return yadro, fake, pamyat


async def test_tovarnyy_akkaunt_poluchaet_poisk_a_uslugi_net(monkeypatch):
    yadro, _, _ = await _yadro(monkeypatch, [])
    assert yadro._poiski.get("saunamart") is not None
    assert yadro._poiski.get("sbsauna") is None
    # Промпт товарного собран из живого каталога — ассортимент в нём есть.
    assert "вагонка" in yadro._prompty["saunamart"].lower()


async def test_raznye_akkaunty_otvechayut_svoim_promptom(monkeypatch):
    """Критерий этапа: на один и тот же вопрос боты отвечают своими данными."""
    yadro, fake, _ = await _yadro(monkeypatch, ["ответ товарного", "ответ услуг"])
    sbor = SborKanala()
    await yadro.obrabotat("saunamart", 1, "сколько стоит отделка", sbor.kanal())
    await yadro.obrabotat("sbsauna", 1, "сколько стоит отделка", sbor.kanal())

    promt_tovarnyy = fake.vyzovy[0]["messages"][0]["content"]
    promt_uslugi = fake.vyzovy[1]["messages"][0]["content"]
    assert "Александра" in promt_tovarnyy and "Роман" not in promt_tovarnyy
    assert "Роман" in promt_uslugi and "500 тысяч" in promt_uslugi
    imena = lambda i: [t["function"]["name"] for t in fake.vyzovy[i]["tools"]]
    assert imena(0) == ["search_products", "save_lead"]   # у товарного оба
    assert imena(1) == ["save_lead"]                      # у услуг только лид


async def test_odin_chat_v_raznyh_botah_ne_smeshivaet_istorii(monkeypatch):
    yadro, _, pamyat = await _yadro(monkeypatch, ["ответ один", "ответ два"])
    sbor = SborKanala()
    await yadro.obrabotat("saunamart", 77, "вопрос товарному", sbor.kanal())
    await yadro.obrabotat("sbsauna", 77, "вопрос по отделке", sbor.kanal())

    a = await pamyat.istoriya("saunamart:77")
    b = await pamyat.istoriya("sbsauna:77")
    assert a[0]["content"] == "вопрос товарному"
    assert b[0]["content"] == "вопрос по отделке"


async def test_reset_chistit_tolko_svoy_akkaunt(monkeypatch):
    yadro, _, pamyat = await _yadro(monkeypatch, ["один", "два"])
    sbor = SborKanala()
    await yadro.obrabotat("saunamart", 5, "первый вопрос", sbor.kanal())
    await yadro.obrabotat("sbsauna", 5, "второй вопрос", sbor.kanal())
    await yadro.sbros("saunamart", 5)

    assert await pamyat.istoriya("saunamart:5") == []
    assert await pamyat.istoriya("sbsauna:5") != []


async def test_privetstvie_lozhitsya_v_istoriyu(monkeypatch):
    """Иначе на первый же вопрос бот представится второй раз: приветствие
    уходит в обход модели, и она о нём не знает."""
    yadro, _, pamyat = await _yadro(monkeypatch, [])
    tekst = await yadro.zapomnit_privetstvie("saunamart", 9)
    istoriya = await pamyat.istoriya("saunamart:9")
    assert istoriya == [{"role": "assistant", "content": tekst}]
    assert "Александра" in tekst


async def test_neizvestnyy_akkaunt_padaet_ponyatno(monkeypatch):
    yadro, _, _ = await _yadro(monkeypatch, [])
    with pytest.raises(KeyError, match="Неизвестный аккаунт"):
        yadro.dispetcher("avito_next_year")


# ── Горячая перезагрузка каталога (этап 16, A5) ──────────────────────────────

class _FakeSessiya:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


def _fabrika_sessiy():
    return _FakeSessiya()


async def _yadro_dlya_perezagruzki() -> Yadro:
    """Ядро с каталогом из файла (podgotovit без фабрики) + фабрика сессий,
    подставленная руками: перезагрузку кормит подменённый `zagruzit_iz_bd`,
    в реальную БД не ходим."""
    _skip_bez_praysa()
    yadro = Yadro(_cfg(), PamyatRedis(FakeRedis()), tempo=BYSTRO)
    await yadro.podgotovit(["saunamart"])
    yadro._fabrika_sessiy = _fabrika_sessiy
    return yadro


async def test_perezagruzka_menyaet_poisk_i_prompt(monkeypatch):
    """Свежий каталог из БД без рестарта заменяет поиск и промпт целиком."""
    yadro = await _yadro_dlya_perezagruzki()

    async def _zagruzit(sessiya, kod):
        return iz_fayla_praysa()
    monkeypatch.setattr(core, "zagruzit_iz_bd", _zagruzit)

    yadro._poiski["saunamart"] = "СТАРЫЙ"
    yadro._prompty["saunamart"] = "старый промпт"
    ok = await yadro.perezagruzit_katalog("saunamart")
    assert ok is True
    assert isinstance(yadro._poiski["saunamart"], Poisk)
    assert "вагонка" in yadro._prompty["saunamart"].lower()


async def test_bityy_sink_ne_zatiraet_rabochiy_katalog(monkeypatch):
    """Синк в БД прошёл, а чтение назад упало — рабочий каталог остаётся."""
    yadro = await _yadro_dlya_perezagruzki()

    async def _padaet(sessiya, kod):
        raise RuntimeError("соединение с БД потеряно")
    monkeypatch.setattr(core, "zagruzit_iz_bd", _padaet)

    yadro._poiski["saunamart"] = "РАБОЧИЙ"
    ok = await yadro.perezagruzit_katalog("saunamart")
    assert ok is False and yadro._poiski["saunamart"] == "РАБОЧИЙ"


async def test_pustoy_katalog_ne_zatiraet_rabochiy(monkeypatch):
    """Пустой каталог (гонка, кривой прайс) не должен обнулить бота."""
    import types
    yadro = await _yadro_dlya_perezagruzki()

    async def _pusto(sessiya, kod):
        return types.SimpleNamespace(gruppy=[])
    monkeypatch.setattr(core, "zagruzit_iz_bd", _pusto)

    yadro._poiski["saunamart"] = "РАБОЧИЙ"
    ok = await yadro.perezagruzit_katalog("saunamart")
    assert ok is False and yadro._poiski["saunamart"] == "РАБОЧИЙ"


async def test_perezagruzka_uslug_nichego_ne_delaet(monkeypatch):
    """У аккаунтов услуг каталога нет — перезагружать нечего, в БД не идём."""
    yadro = await _yadro_dlya_perezagruzki()
    hodili = False

    async def _zagruzit(sessiya, kod):
        nonlocal hodili
        hodili = True
        return iz_fayla_praysa()
    monkeypatch.setattr(core, "zagruzit_iz_bd", _zagruzit)

    ok = await yadro.perezagruzit_katalog("sbsauna")
    assert ok is False and hodili is False


async def test_perezagruzka_bez_fabriki_sessiy_ne_padaet():
    """Ядро поднято без БД (файловый каталог) — перезагрузка просто пропускается."""
    _skip_bez_praysa()
    yadro = Yadro(_cfg(), PamyatRedis(FakeRedis()), tempo=BYSTRO)
    await yadro.podgotovit(["saunamart"])       # fabrika=None
    ok = await yadro.perezagruzit_katalog("saunamart")
    assert ok is False


# ── Адаптер Telegram ─────────────────────────────────────────────────────────

def test_bot_sobiraetsya_so_vsemi_hendlerami(monkeypatch):
    """Собираем на фиктивном токене: сети это не требует, а регистрацию
    хендлеров проверяет (текст, /start, /reset, вложения)."""
    from bot.channels import telegram

    class ZaglushkaYadro:
        pass

    bot, dp = telegram.sobrat_bota("saunamart", "123456:TESTTESTTESTTESTTESTTESTTEST",
                                   ZaglushkaYadro())
    assert len(dp.message.handlers) == 4


# ── Супервизор ───────────────────────────────────────────────────────────────

def test_pustoy_token_vyklyuchaet_tolko_svoego_bota():
    from bot.main import zhivye_akkaunty

    cfg = _cfg()
    cfg.telegram_tokeny.update({"saunamart": "1:AA", "sbsauna": "  ",
                                "sbsauna_deshman": "3:CC"})
    zhivye, vyklyuchennye = zhivye_akkaunty(cfg)
    assert zhivye == ["saunamart", "sbsauna_deshman"]
    assert vyklyuchennye == ["sbsauna"]


async def test_supervizor_perezapuskaet_upavshiy_kanal(monkeypatch):
    """Падение бота — это сеть или Telegram, а не приговор: канал поднимается снова."""
    from bot import main

    monkeypatch.setattr(main, "_RESTART_PAUZA", 0.01)
    popytki = []

    async def kanal():
        popytki.append(1)
        if len(popytki) < 3:
            raise RuntimeError("Telegram отвалился")

    await main._supervise("saunamart", kanal)
    assert len(popytki) == 3     # два падения, третий запуск завершился штатно


async def test_padenie_odnogo_kanala_ne_ronyaet_sosedey(monkeypatch):
    """Критерий этапа: три аккаунта независимы."""
    import asyncio

    from bot import main

    monkeypatch.setattr(main, "_RESTART_PAUZA", 0.01)
    zhiv = asyncio.Event()

    async def padayushchiy():
        raise RuntimeError("этот бот сломан")

    async def zdorovyy():
        zhiv.set()
        await asyncio.sleep(3600)

    zadachi = [asyncio.create_task(main._supervise("плохой", padayushchiy)),
               asyncio.create_task(main._supervise("хороший", zdorovyy))]
    await asyncio.wait_for(zhiv.wait(), timeout=2)
    await asyncio.sleep(0.05)                  # даём плохому упасть пару раз
    assert not zadachi[1].done()               # сосед жив
    for z in zadachi:
        z.cancel()
    await asyncio.gather(*zadachi, return_exceptions=True)


async def test_indikator_nabora_ne_ronyaet_otpravku():
    """Индикатор — украшение. Его падение не должно ронять ответ клиенту."""
    from bot.channels import telegram

    otpravleno: list[str] = []

    class BotZaglushka:
        async def send_message(self, chat_id, tekst):
            otpravleno.append(tekst)

        async def send_chat_action(self, chat_id, action):
            raise RuntimeError("Telegram молчит")

    kanal = telegram._kanal(BotZaglushka(), 1, "klient")
    await kanal.pechataet()          # не бросает
    await kanal.otpravit("привет")
    assert otpravleno == ["привет"]
