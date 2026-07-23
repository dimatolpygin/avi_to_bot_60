# -*- coding: utf-8 -*-
"""Тесты ИИ-слоя (этап 8). Сети нет: клиент модели подменяется заглушкой.

Живой прогон — `python -m bot.ai.probe`, он ходит в OpenRouter и стоит денег.
Здесь проверяется то, что от модели не зависит и обязано работать всегда:
сборка payload для инструмента, чистка стиля и **предохранители** — та часть,
ради которой агент вообще написан кодом, а не одним промптом.
"""
import json
from decimal import Decimal

import pytest

from bot.ai import agent
from bot.ai.stil import ochistit_otvet
from bot.etl.chtenie import StrokaPraysa
from bot.etl.import_prays import _polya_pozicii
from bot.search.katalog import iz_polej
from bot.search.search import Poisk

# ── Фикстуры каталога ────────────────────────────────────────────────────────


def _stroka(nomer: int, article: str, name: str, cena: str,
            harakteristiki: str | None = None, nalichie: str = "Да") -> StrokaPraysa:
    return StrokaPraysa(article=article, name=name, characteristics=harakteristiki,
                        price_apiece=Decimal(cena), price_per_meter_fayl=None,
                        nalichie_syroe=nalichie, nomer_stroki=nomer)


STROKI = [
    _stroka(1, "176965", "Вагонка Липа сорт А 15х95 (88) L-3.0 м", "513"),
    _stroka(2, "176964", "Вагонка Липа сорт А 15х95 (88) L-2.5 м", "428"),
    _stroka(3, "119214", "Вагонка Липа сорт В 15х95 (88) L-3.0 м", "371"),
    _stroka(4, "174262", "Камни Габбро-диабаз мелкий 20кг Карелия", "600"),
    # Пустое наличие — «уточню у менеджера», а НЕ «нет»: это баг старого бота.
    _stroka(5, "118757", "Фольга алюминиевая 12 м2 80мкм", "3284", nalichie=""),
]


@pytest.fixture(scope="module")
def poisk() -> Poisk:
    return Poisk(iz_polej([_polya_pozicii(s) for s in STROKI], "тест"))


# ── Стиль ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("syroy, zhdem", [
    ("Липа — 513 рублей", "Липа, 513 рублей"),
    ("Цена 10 — 15 рублей", "Цена 10-15 рублей"),
    # Перечисление через тире: каждая новая запятая делает его нечитаемым,
    # поэтому при нескольких тире в предложении они просто выкидываются.
    ("Сорт А — без сучков, Экстра — идеальный вид, В — с сучками",
     "Сорт А без сучков, Экстра идеальный вид, В с сучками"),
    # А одиночное тире остаётся запятой даже перед связкой: слева не всегда
    # подлежащее, и без запятой выходит обрывок «мы не делаем это не наш формат».
    ("Мы не делаем — это не наш формат", "Мы не делаем, это не наш формат"),
    # Два предложения, в каждом по одному тире — это не перечисление.
    ("Липа — 513 рублей. Ольха — 924 рубля.",
     "Липа, 513 рублей. Ольха, 924 рубля."),
    ("**Вагонка** липа", "Вагонка липа"),
    ("## Заголовок\nтекст", "Заголовок\nтекст"),
    ("- вагонка\n- полок", "вагонка\nполок"),
    ("1. вагонка\n2. полок", "вагонка\nполок"),
    ("Есть 👍 вагонка", "Есть вагонка"),
    ("Первый абзац\n\n\nВторой абзац", "Первый абзац\nВторой абзац"),
])
def test_stil_chistit(syroy, zhdem):
    assert ochistit_otvet(syroy) == zhdem


def test_stil_idempotenten():
    odin = ochistit_otvet("**Липа** — 513 ₽\n\n- в наличии")
    assert ochistit_otvet(odin) == odin


def test_stil_ne_trogaet_chistyy_tekst():
    tekst = "Липа три метра есть двух сортов: А по 513 рублей, В по 371."
    assert ochistit_otvet(tekst) == tekst


# ── Предохранители ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("tekst", [
    "К сожалению, такого нет в наличии",
    "Не нашла в прайсе",
    "Мы этим не занимаемся",
    "Такой позиции нет в каталоге",
    "У нас нет сорта Б",
])
def test_otkaz_lovitsya(tekst):
    assert agent._zayavil_otkaz(tekst)


@pytest.mark.parametrize("tekst", [
    "Липа три метра есть, 513 рублей за штуку",
    "Какой сорт вас интересует?",
    "Здравствуйте, это Александра из Saunamart",
])
def test_ne_otkaz(tekst):
    assert not agent._zayavil_otkaz(tekst)


@pytest.mark.parametrize("tekst, boltovnya", [
    ("привет", True), ("спасибо!", True), ("/reset", True),
    ("нужна вагонка липа", False),
    ("здравствуйте, подскажите цену на вагонку липа сорт а три метра", False),
])
def test_boltovnya(tekst, boltovnya):
    assert agent._boltovnya(tekst) is boltovnya


# ── Payload инструмента ──────────────────────────────────────────────────────


def test_payload_nazyvaet_tsenu_i_nalichie(poisk):
    nahodki, kanal = poisk.iskat("вагонка липа сорт а 3 метра")
    p = agent.payload_poiska(nahodki, kanal, "22.07.2026")["найдено"][0]
    assert p["цена_за_штуку"] == 513
    assert p["цена_за_метр_погонный"] == 171
    assert p["наличие"] == "есть в наличии"
    assert p["артикул"] == "176965"


def test_pustoe_nalichie_eto_utochnyu_a_ne_net(poisk):
    """Главный баг старого бота: пустая ячейка прайса читалась как «нет»."""
    nahodki, kanal = poisk.iskat("фольга")
    p = agent.payload_poiska(nahodki, kanal, "")["найдено"][0]
    # «У менеджера» из уст менеджера — перевод стрелок на самого себя (24.07).
    assert p["наличие"] == "уточню на складе"
    assert "нет" not in p["наличие"]


def test_upakovka_pomechena(poisk):
    nahodki, kanal = poisk.iskat("фольга")
    p = agent.payload_poiska(nahodki, kanal, "")["найдено"][0]
    assert "делить" in p["упаковка"]


def test_nesovpavshaya_dlina_pomechena(poisk):
    """Спрошенной длины нет: товар приходит, но с прямым предупреждением."""
    nahodki, kanal = poisk.iskat("вагонка липа сорт а 1.5 метра")
    p = agent.payload_poiska(nahodki, kanal, "")["найдено"][0]
    assert "внимание" in p
    # Длины приходят С ЦЕНАМИ: с плоским списком модель объявляла цену образца
    # ценой «во всех длинах» и врала вчетверо на короткой доске (прогон 23.07).
    assert p["цена_за_штуку_по_длинам"] == {"2.5": 428, "3": 513}


def test_kvadratnyy_metr_prihodit_s_ogovorkoy(poisk):
    nahodki, kanal = poisk.iskat("вагонка липа сорт а 3 метра")
    p = agent.payload_poiska(nahodki, kanal, "")["найдено"][0]
    kv = p["цена_за_квадратный_метр"]
    # Целые рубли: 1943.18 модель озвучивает как «рубль восемнадцать копеек»,
    # а живой менеджер копейки не называет.
    assert kv["значение"] == 1943
    assert "ТОЛЬКО" in kv["как_использовать"]


def test_cena_za_metr_bez_kopeek(poisk):
    """371 / 3.0 = 123.666… — модели уходит 124, как в прайсе."""
    nahodki, kanal = poisk.iskat("вагонка липа сорт в 3 метра")
    p = agent.payload_poiska(nahodki, kanal, "")["найдено"][0]
    assert p["цена_за_метр_погонный"] == 124
    # Цена за штуку — это цена конкретной длины, и длина едет рядом с ней.
    assert p["длина_м"] == 3


def test_u_kamney_net_kvadratnogo_metra(poisk):
    """Рабочей ширины нет — пересчёт в м² запрещён, поля быть не должно."""
    nahodki, kanal = poisk.iskat("камни габбро")
    assert "цена_за_квадратный_метр" not in agent.payload_poiska(nahodki, kanal, "")["найдено"][0]


def test_pustaya_vydacha_razlichaet_kanaly(poisk):
    chuzhoy = agent.payload_poiska([], "чужой домен", "")
    net_takogo = agent.payload_poiska([], "не найдено", "")
    assert chuzhoy["найдено"] == []
    assert "не наш профиль" in chuzhoy["пометка"]
    assert "наш профиль" in net_takogo["пометка"]
    assert chuzhoy["пометка"] != net_takogo["пометка"]


# ── Agent-loop ───────────────────────────────────────────────────────────────


class FakeChat:
    """Заглушка модели: отдаёт заранее заданные ответы по очереди и запоминает,
    с каким `tool_choice` её звали, — на этом и проверяются предохранители."""

    def __init__(self, otvety: list[dict]):
        self.otvety = otvety
        self.vyzovy: list[str] = []

    async def __call__(self, cfg, messages, tools=None, tool_choice="auto"):
        self.vyzovy.append(tool_choice)
        return self.otvety[min(len(self.vyzovy) - 1, len(self.otvety) - 1)]


def _tool_call(query: str) -> dict:
    return {"content": None, "tool_calls": [{
        "id": "1", "type": "function",
        "function": {"name": "search_products",
                     "arguments": f'{{"query": "{query}"}}'}}]}


@pytest.fixture
def cfg():
    from bot.config import OpenRouterConfig
    return OpenRouterConfig(api_key="тест", model="тест")


@pytest.mark.asyncio
async def test_obychnyy_hod_zovyot_poisk(poisk, cfg, monkeypatch):
    fake = FakeChat([_tool_call("вагонка липа сорт а 3 метра"),
                     {"content": "Липа сорт А, 513 рублей за штуку.", "tool_calls": None}])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [], "почём липа три метра")
    assert r.zaprosy_poiska == ["вагонка липа сорт а 3 метра"]
    assert r.naydeno == 1
    assert "513" in r.otvet
    assert not r.forsirovan_poisk
    # История растёт на две реплики; tool-сообщения в неё не попадают.
    assert [m["role"] for m in r.istoriya] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_otkaz_bez_poiska_forsiruet_poisk(poisk, cfg, monkeypatch):
    """Предохранитель из telewin: модель отказала, не заглянув в прайс.
    Убирать его нельзя — без него был баг «то есть, то нет»."""
    fake = FakeChat([
        {"content": "Такого у нас нет в наличии.", "tool_calls": None},
        _tool_call("вагонка липа"),
        {"content": "Липа есть, 513 рублей за штуку.", "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [], "вагонка липа есть?")
    assert r.forsirovan_poisk
    # Форсируем именно поиск, а не «любой инструмент»: с появлением save_lead
    # значение "required" разрешило бы модели вместо прайса передать лид.
    assert fake.vyzovy == ["auto", agent.FORSIROVAT_POISK, "auto"]
    assert "513" in r.otvet


@pytest.mark.asyncio
async def test_utochnenie_bez_poiska_forsiruet_poisk(poisk, cfg, monkeypatch):
    """Поймано на живом прогоне: модель спросила «сорт А, В, С или эконом?»,
    хотя сортов три и никаких «С» и «эконом» не существует."""
    fake = FakeChat([
        {"content": "Какой сорт вас интересует, А, В, С или эконом?", "tool_calls": None},
        _tool_call("вагонка липа"),
        {"content": "Есть сорт А и сорт В.", "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [], "почём вагонка липа")
    assert r.forsirovan_poisk
    assert fake.vyzovy[1] == agent.FORSIROVAT_POISK


@pytest.mark.asyncio
async def test_svetskaya_replika_ne_forsiruet_poisk(poisk, cfg, monkeypatch):
    fake = FakeChat([{"content": "Здравствуйте, это Александра. Чем помочь?",
                      "tool_calls": None}])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [], "привет")
    assert not r.forsirovan_poisk
    assert fake.vyzovy == ["auto"]


@pytest.mark.asyncio
async def test_stil_chistitsya_i_v_istorii(poisk, cfg, monkeypatch):
    """Разметку снимаем и с ответа, и с истории: иначе на следующем ходу
    модель скопирует собственный прежний формат."""
    fake = FakeChat([{"content": "**Липа** — 513 рублей", "tool_calls": None}])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [], "почём липа")
    assert r.otvet == "Липа, 513 рублей"
    assert r.istoriya[-1]["content"] == "Липа, 513 рублей"


@pytest.mark.asyncio
async def test_bityy_tool_call_ne_ronyaet_dialog(poisk, cfg, monkeypatch):
    bityy = {"content": None, "tool_calls": [{
        "id": "1", "type": "function",
        "function": {"name": "search_products", "arguments": "{это не json"}}]}
    fake = FakeChat([bityy, {"content": "Уточните, пожалуйста, что нужно.",
                             "tool_calls": None}])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [], "вагонка")
    assert r.otvet


@pytest.mark.asyncio
async def test_ischerpanie_iteraciy_daet_russkiy_otvet(poisk, cfg, monkeypatch):
    """Даже упёршись в лимит, бот не сыплет системной английской фразой —
    это баг №2 старого бота («I was unable to complete the request»)."""
    fake = FakeChat([_tool_call("вагонка")])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [], "вагонка")
    assert r.otvet == "Уточните, пожалуйста, что именно нужно, я подберу по прайсу."


# ── Промпт ───────────────────────────────────────────────────────────────────


def test_assortiment_sobiraetsya_iz_kataloga(poisk):
    """Список ассортимента берётся из прайса, а не пишется руками: иначе он
    начнёт врать первым же обновлением каталога."""
    prompt = agent.sobrat_prompt(poisk.katalog)
    assert "вагонка" in prompt.lower()
    assert "камни" in prompt.lower()
    assert "{АССОРТИМЕНТ}" not in prompt
    # В списке ассортимента цен нет: за ценой модель обязана идти в инструмент.
    # В примере ответа ниже цифры есть, поэтому проверяем именно список.
    assert "513" not in agent._assortiment(poisk.katalog)
    # …а к примерам приложена оговорка, что цифры в них условные.
    assert "Цифры в них условные" in prompt


def test_prompt_soderzhit_klyuchevye_pravila(poisk):
    prompt = agent.sobrat_prompt(poisk.katalog)
    for pravilo in ("Сорт Б", "Телефон первой НЕ проси", "рабочей ширине",
                    "Александра", "квадратный метр"):
        assert pravilo in prompt, f"из промпта пропало правило: {pravilo}"


# ── Передача лида менеджеру ──────────────────────────────────────────────────


def _vyzov_lida(telefon: str, imya: str | None = None,
                vyzhimka: str = "Клиент спрашивал вагонку липа сорт А") -> dict:
    # Ключи схемы ЛАТИНСКИЕ: Anthropic отвергает кириллицу в именах свойств
    # инструмента («Property keys should match ^[a-zA-Z0-9_.-]{1,64}$») и валит
    # ЛЮБОЙ запрос с таким tools — то есть весь диалог, а не только лид.
    args = {"telefon": telefon, "vyzhimka": vyzhimka}
    if imya:
        args["imya"] = imya
    return {"content": None, "tool_calls": [{
        "id": "9", "type": "function",
        "function": {"name": "save_lead", "arguments": json.dumps(args, ensure_ascii=False)}}]}


@pytest.mark.asyncio
async def test_lead_uhodit_s_vyzhimkoy(poisk, cfg, monkeypatch):
    """Менеджеру нужен не голый номер, а о чём был разговор: переписку он
    не читал. Выжимку пишет модель тем же вызовом — диалог у неё в контексте."""
    fake = FakeChat([
        _vyzov_lida("8 900 123-45-67", "Игорь", "Нужна вагонка липа сорт А, 3 метра, парная 2х3"),
        {"content": "Спасибо, передала менеджеру, он свяжется.", "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)
    peredannye = []

    async def peredat(telefon, imya, vyzhimka):
        peredannye.append((telefon, imya, vyzhimka))

    r = await agent.otvetit(cfg, poisk, [], "мой номер 8 900 123-45-67",
                            peredat_lead=peredat)
    assert r.lead_peredan
    assert peredannye == [("8 900 123-45-67", "Игорь",
                           "Нужна вагонка липа сорт А, 3 метра, парная 2х3")]


@pytest.mark.asyncio
async def test_lead_bez_telefona_ne_peredaetsya(poisk, cfg, monkeypatch):
    """Инструмент зовут только на реальный номер: без него передавать нечего,
    а «передал» в логе означало бы лид, которого нет."""
    fake = FakeChat([
        _vyzov_lida(""),
        {"content": "Что вас интересует?", "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)
    peredannye = []

    async def peredat(telefon, imya, vyzhimka):
        peredannye.append(telefon)

    r = await agent.otvetit(cfg, poisk, [], "перезвоните мне", peredat_lead=peredat)
    assert peredannye == []
    assert r.lead_peredan is False


@pytest.mark.asyncio
async def test_padenie_bd_ne_royaet_dialog(poisk, cfg, monkeypatch):
    """Лид дороже всего, но оборвать из-за него живой диалог — хуже: клиент
    получит ответ, а ошибка уйдёт в лог целиком."""
    fake = FakeChat([
        _vyzov_lida("89001234567"),
        {"content": "Спасибо, менеджер свяжется.", "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)

    async def padaet(telefon, imya, vyzhimka):
        raise RuntimeError("Postgres лёг")

    r = await agent.otvetit(cfg, poisk, [], "мой номер 89001234567", peredat_lead=padaet)
    assert "менеджер" in r.otvet.lower()
    assert r.lead_peredan is False


def test_telefon_normalizuetsya():
    """Один человек с двумя записями номера — это два лида в amo, а не один."""
    from bot.lead import normalizovat_telefon

    assert normalizovat_telefon("8 (900) 123-45-67") == "+79001234567"
    assert normalizovat_telefon("+7 900 123 45 67") == "+79001234567"
    assert normalizovat_telefon("9001234567") == "+79001234567"
    assert normalizovat_telefon(None) is None
    # Не похоже на телефон — отдаём как есть, чтобы менеджер увидел, что написал клиент.
    assert normalizovat_telefon("напишите в вотсап") == "напишите в вотсап"


# ── Предохранитель: выпрашивание телефона ────────────────────────────────────


@pytest.mark.parametrize("syroy, zhdem", [
    # Живой диалог 24.07: посчитал заказ и тут же попросил номер.
    ("Пять штук метровой липы сорт А по 110 рублей, это 550 рублей. Оставите номер телефона?",
     "Пять штук метровой липы сорт А по 110 рублей, это 550 рублей."),
    ("Скидку уточню. Напишите ваш телефон, пожалуйста.", "Скидку уточню."),
    ("Посчитаем точно. Как с вами связаться?", "Посчитаем точно."),
    ("Нужен ваш номер телефона, чтобы подготовить варианты. Липа есть трёх сортов.",
     "Липа есть трёх сортов."),
])
def test_proshba_telefona_vyrezaetsya(syroy, zhdem):
    from bot.ai.stil import snyat_proshbu_telefona

    chistyy, vyrezano = snyat_proshbu_telefona(syroy)
    assert chistyy == zhdem
    assert vyrezano


@pytest.mark.parametrize("tekst", [
    # Подтверждение уже полученного контакта — не просьба.
    "Спасибо, записала ваш номер. Мы свяжемся с вами сегодня.",
    "Позвоним вам на этот номер после обеда.",
    "Липа сорт А есть, 513 рублей за трёхметровую штуку. Какая длина нужна?",
    # Если кроме просьбы в реплике ничего нет, пустое сообщение хуже просьбы.
    "Оставите номер телефона?",
])
def test_proshba_telefona_ne_lovit_lishnego(tekst):
    from bot.ai.stil import snyat_proshbu_telefona

    assert snyat_proshbu_telefona(tekst) == (tekst, None)


@pytest.mark.asyncio
async def test_agent_ne_vypraszivaet_telefon(poisk, cfg, monkeypatch):
    """Промпт запрещает это трижды и всё равно не удержал — держит код."""
    fake = FakeChat([
        {"content": "Пять штук по 110 рублей, это 550 рублей. Оставите номер телефона?",
         "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [{"role": "assistant", "content": "Здравствуйте."}],
                            "мне 5 штук метровой")
    assert "550 рублей" in r.otvet
    assert "телефон" not in r.otvet.lower()


@pytest.mark.asyncio
async def test_posle_lida_pro_nomer_govorit_mozhno(poisk, cfg, monkeypatch):
    """Клиент контакт уже дал: подтверждение и уточнение по номеру — не выпрашивание."""
    fake = FakeChat([
        _vyzov_lida("89001234567"),
        {"content": "Спасибо, записала ваш номер. Мы свяжемся с вами сегодня.",
         "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)

    async def peredat(telefon, imya, vyzhimka):
        return None

    r = await agent.otvetit(cfg, poisk, [], "мой номер 89001234567", peredat_lead=peredat)
    assert "записала ваш номер" in r.otvet
