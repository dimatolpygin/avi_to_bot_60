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


# Характеристики вагонки как в живой таблице: размерный зачин перепутан (спека
# полка — толщина 25, ширина 90), дальше идёт качественное описание сортов.
# По политике этапа 17 боту уходит только часть от «Сорт …».
_HAR_VAGONKA = ("Профиль толщина 25 мм, ширина 90мм. Сорт экстра без сучков, "
                "сорт А несколько сучков на метр погонный, сорт Б сучки ярко "
                "выраженны( это бюджетный вариант)")

STROKI = [
    _stroka(1, "176965", "Вагонка Липа сорт А 15х95 (88) L-3.0 м", "513", _HAR_VAGONKA),
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


def test_payload_pechi_neset_rabochiy_obem():
    """Баг заказчика 24.07: без рабочего объёма печи модель на «парная 5 на 3»
    объявляла «9 кВт подойдёт, все три подходят». Диапазон объёма обязан приходить
    в выдаче, иначе фитмент модель считает из головы. Тестовый каталог фикстуры печей
    не содержит, поэтому находку собираем вручную."""
    from decimal import Decimal
    from types import SimpleNamespace
    p = SimpleNamespace(
        name="Печь электрическая ЭКМ Терра 9 кВт", article="126672", family="печь",
        price_apiece=Decimal("26100"), availability="in", characteristics=None,
        attrs={"obem_min_m3": 9, "obem_max_m3": 14},
        length_m=None, price_per_m=None, is_package=False, working_width_mm=None)
    n = SimpleNamespace(pozitsiya=p, dlina_sovpala=True,
                        gruppa=SimpleNamespace(dliny=[], pozitsii=[p]))
    d = agent._pozitsiya_v_payload(n)
    assert d["объём_парной"]["значение"] == "от 9 до 14 м³"
    assert "НЕ подойдёт" in d["объём_парной"]["как_подбирать"]


# ── Описание товара в выдаче (этап 17) ───────────────────────────────────────


def test_opisanie_pogonazh_tolko_kachestvo_bez_razmerov():
    """C1. У вагонки/полка размеры — из названия, а размерный зачин описания
    в живой таблице перепутан (спека полка). Боту уходит только часть от «Сорт …»,
    перепутанные толщина/ширина не долетают."""
    from types import SimpleNamespace
    p = SimpleNamespace(family="вагонка", characteristics=_HAR_VAGONKA)
    opis = agent._opisanie_dlya_modeli(p)
    assert opis.startswith("Сорт")
    assert "толщина" not in opis and "25" not in opis and "90" not in opis
    assert "бюджетный" in opis


def test_opisanie_pogonazh_bez_sorta_nichego_ne_daet():
    """Нет качественной части (ни сорта, ни сучков) — отдавать один размерный
    зачин нельзя: он про другой товар. Кейс дуба: «вагонка из дуба 1 метровые
    доски» → ничего."""
    from types import SimpleNamespace
    p = SimpleNamespace(family="вагонка", characteristics="вагонка из дуба 1 метровые доски")
    assert agent._opisanie_dlya_modeli(p) is None


def test_opisanie_pogonazh_lovit_suchki_bez_slova_sort():
    """Качество бывает выражено без слова «сорт» — прямо «…с сучками». Раньше такое
    описание отбрасывалось целиком (якорь только «Сорт»), и бот отвечал «нет
    информации о сучках» при том, что она в описании есть. Живой кейс термоосины."""
    from types import SimpleNamespace
    p = SimpleNamespace(family="вагонка",
                        characteristics="вагонка термоосина 3 метровые доски с сучками")
    opis = agent._opisanie_dlya_modeli(p)
    assert opis == "с сучками"                       # размерный зачин отрезан
    assert "3 метровые" not in opis


def test_opisanie_pechi_tselikom():
    """C2. У печей/дверей описание достоверно и важно — отдаём целиком."""
    from types import SimpleNamespace
    p = SimpleNamespace(family="печь",
                        characteristics="мощность 9 кВт объем бани 9 - 14 м3")
    opis = agent._opisanie_dlya_modeli(p)
    assert opis == "мощность 9 кВт объем бани 9 - 14 м3"


def test_payload_vagonka_neset_opisanie_sortov(poisk):
    """C1 через выдачу: у вагонки в payload есть описание сортов и нет размеров."""
    nahodki, kanal = poisk.iskat("вагонка липа сорт а 3 метра")
    p = agent.payload_poiska(nahodki, kanal, "")["найдено"][0]
    assert "Сорт" in p["описание"] and "бюджетный" in p["описание"]
    assert "толщина" not in p["описание"]


def test_payload_pechi_opisanie_i_obem_iz_nepustogo_dublya():
    """C2 + устойчивость к дублю: у печи в таблице дубль строк — одна с описанием
    и объёмом, вторая пустая, и представитель группы может быть пустым. Всё это
    ОДИН товар, поэтому описание и объём берём из непустой строки."""
    from types import SimpleNamespace
    pusto = SimpleNamespace(
        name="Печь ЭКМ Терра 9 кВт", article="1", family="печь", characteristics="",
        price_apiece=Decimal("26100"), availability="in", attrs={},
        length_m=None, price_per_m=None, is_package=False, working_width_mm=None)
    polno = SimpleNamespace(
        name="Печь ЭКМ Терра 9 кВт", article="1", family="печь",
        characteristics="мощность 9 кВт объем бани 9 - 14 м3",
        price_apiece=Decimal("26100"), availability="in",
        attrs={"obem_min_m3": 9, "obem_max_m3": 14},
        length_m=None, price_per_m=None, is_package=False, working_width_mm=None)
    # Представитель (найденная позиция) — ПУСТАЯ строка дубля.
    n = SimpleNamespace(pozitsiya=pusto, dlina_sovpala=True,
                        gruppa=SimpleNamespace(dliny=[], pozitsii=[pusto, polno]))
    d = agent._pozitsiya_v_payload(n)
    assert "объем бани" in d["описание"]
    assert d["объём_парной"]["значение"] == "от 9 до 14 м³"


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


def test_kvadratnyy_metr_osnovnaya_cena(poisk):
    """Заказчик 28.07: цена за м² — то, что спрашивают, с неё и начинаем; считаем
    по общей ширине (95 мм) → 171 × 10.53 = 1800, без копеек."""
    nahodki, kanal = poisk.iskat("вагонка липа сорт а 3 метра")
    p = agent.payload_poiska(nahodki, kanal, "")["найдено"][0]
    kv = p["цена_за_квадратный_метр"]
    assert kv["значение"] == 1800
    assert "начинай" in kv["как_использовать"]


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
    """Чужой домен закрывает тему, «не найдено» — НЕ закрывает.

    Решение заказчика 24.07: банное, которого нет в прайсе, не повод отшить
    клиента. Прайс это не весь ассортимент, часть возим под заказ, поэтому
    ответ здесь — «уточню и вернусь», а не «у нас такого нет».
    """
    chuzhoy = agent.payload_poiska([], "чужой домен", "")
    net_takogo = agent.payload_poiska([], "не найдено", "")
    assert chuzhoy["найдено"] == []
    assert "не наш профиль" in chuzhoy["пометка"]
    assert "ПРО НАШ ПРОФИЛЬ" in net_takogo["пометка"]
    assert "НЕ отшивай" in net_takogo["пометка"]
    assert "уточнишь у поставщика" in net_takogo["пометка"]
    assert chuzhoy["пометка"] != net_takogo["пометка"]


def test_pustaya_vydacha_pod_obyavleniem_ne_otshivaet(poisk):
    """Под активным объявлением «не найдено» НЕ отшивает: товар в продаже,
    «уточню у поставщика»/«нет в прайсе» не говорим (жалоба заказчика про
    товары из объявлений, которых нет в каталоге — соль, трубы дымохода)."""
    pod = agent.payload_poiska([], "не найдено", "", True)
    assert pod["найдено"] == []
    assert "в продаже" in pod["пометка"]
    assert "уточню у поставщика" in pod["пометка"]   # в списке запрещённых фраз
    assert pod["пометка"] != agent.payload_poiska([], "не найдено", "")["пометка"]


def test_chuzhoy_domen_pod_obyavleniem_ne_smyagchaetsya(poisk):
    """Чужой домен под объявлением остаётся жёстким отказом: унитаз под банным
    объявлением — всё равно чужой профиль (гейт профиля выше объявления)."""
    pod = agent.payload_poiska([], "чужой домен", "", True)
    assert "не наш профиль" in pod["пометка"]
    assert pod["пометка"] == agent.payload_poiska([], "чужой домен", "")["пометка"]


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
    for pravilo in ("Сорт Б", "НЕ проси", "КВАДРАТНЫЙ МЕТР", "Александра"):
        assert pravilo in prompt, f"из промпта пропало правило: {pravilo}"
    # Контакт: спросить можно, но один раз и по делу. Обе половины правила
    # обязаны стоять рядом — без первой лид не появится, без второй бот клянчит.
    assert "СОЗРЕЛ" in prompt and "РОВНО ОДИН раз" in prompt
    assert "Второй раз\n  не спрашивай" in prompt


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


# ── Передача горячего диалога менеджеру без телефона (14.11) ──────────────────


def _vyzov_peredachi(prichina: str = "согласен на звонок",
                     vyzhimka: str = "Клиент созрел, парная 2х3, готов к замеру",
                     *, id_="7") -> dict:
    args = {"prichina": prichina, "vyzhimka": vyzhimka}
    return {"content": None, "tool_calls": [{
        "id": id_, "type": "function",
        "function": {"name": "peredat_menedzheru",
                     "arguments": json.dumps(args, ensure_ascii=False)}}]}


@pytest.mark.asyncio
async def test_peredat_menedzheru_zovyot_kolbek_bez_telefona(cfg, monkeypatch):
    """Клиент созрел без номера → инструмент отдаёт причину и выжимку колбэку.
    Аккаунт услуг (poisk=None) — телефон вообще не участвует."""
    fake = FakeChat([
        _vyzov_peredachi(),
        {"content": "Передаю коллеге, он свяжется с вами прямо здесь.", "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)
    peredannye = []

    async def peredat(prichina, vyzhimka):
        peredannye.append((prichina, vyzhimka))

    r = await agent.otvetit(cfg, None, [], "давайте созвонимся", sistemny="тест",
                            peredat_dialog=peredat)
    assert peredannye == [("согласен на звонок",
                           "Клиент созрел, парная 2х3, готов к замеру")]
    assert "колле" in r.otvet.lower()


@pytest.mark.asyncio
async def test_peredat_menedzheru_povtor_za_hod_ne_dublruet(cfg, monkeypatch):
    """Две передачи в одной реплике модели → колбэк вызывается ровно раз."""
    dva = {"content": None, "tool_calls": [
        _vyzov_peredachi(id_="a")["tool_calls"][0],
        _vyzov_peredachi(id_="b")["tool_calls"][0],
    ]}
    fake = FakeChat([dva, {"content": "Подключаю коллегу.", "tool_calls": None}])
    monkeypatch.setattr(agent, "chat", fake)
    razy = []

    async def peredat(prichina, vyzhimka):
        razy.append(1)

    await agent.otvetit(cfg, None, [], "готов брать", sistemny="тест",
                        peredat_dialog=peredat)
    assert razy == [1]


@pytest.mark.asyncio
async def test_peredat_menedzheru_sboy_ne_royaet_dialog(cfg, monkeypatch):
    """Сбой передачи в CRM не обрывает ответ клиенту — как у лида."""
    fake = FakeChat([
        _vyzov_peredachi(),
        {"content": "Подключаю коллегу, он ответит здесь.", "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)

    async def padaet(prichina, vyzhimka):
        raise RuntimeError("amoCRM лёг")

    r = await agent.otvetit(cfg, None, [], "готов к замеру", sistemny="тест",
                            peredat_dialog=padaet)
    assert "колле" in r.otvet.lower()


@pytest.mark.asyncio
async def test_peredat_menedzheru_est_u_tovarnogo_akkaunta(poisk, cfg, monkeypatch):
    """Инструмент даётся и товарному аккаунту (у него есть и поиск, и передача)."""
    zahvat = {}

    class _Fake(FakeChat):
        async def __call__(self, cfg_, messages, tools=None, tool_choice="auto"):
            zahvat["imena"] = [t["function"]["name"] for t in (tools or [])]
            return await super().__call__(cfg_, messages, tools, tool_choice)

    fake = _Fake([_vyzov_peredachi(), {"content": "Передаю коллеге.", "tool_calls": None}])
    monkeypatch.setattr(agent, "chat", fake)
    razy = []

    async def peredat(prichina, vyzhimka):
        razy.append(1)

    await agent.otvetit(cfg, poisk, [], "беру", peredat_dialog=peredat)
    assert "peredat_menedzheru" in zahvat["imena"]
    assert "search_products" in zahvat["imena"]
    assert razy == [1]


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
async def test_pervaya_proshba_kontakta_prohodit(poisk, cfg, monkeypatch):
    """Спросить один раз НУЖНО, иначе лида не будет вовсе: клиент считает,
    что уже говорит с менеджером, и телефон сам не оставит."""
    fake = FakeChat([
        {"content": "Пять штук по 110 рублей, это 550 рублей. Оставите номер, пришлю расчёт?",
         "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)
    r = await agent.otvetit(cfg, poisk, [{"role": "assistant", "content": "Здравствуйте."}],
                            "мне 5 штук метровой")
    assert "550 рублей" in r.otvet
    assert "номер" in r.otvet


@pytest.mark.asyncio
async def test_vtoraya_proshba_kontakta_vyrezaetsya(poisk, cfg, monkeypatch):
    """А второй раз — уже клянчанье: клиент промолчал, значит тема закрыта."""
    fake = FakeChat([
        {"content": "Липа сорт А есть, 513 рублей за трёхметровую. Оставите номер телефона?",
         "tool_calls": None},
    ])
    monkeypatch.setattr(agent, "chat", fake)
    istoriya = [
        {"role": "assistant", "content": "Оставьте номер, я пришлю расчёт."},
        {"role": "user", "content": "а сорт а почём"},
    ]
    r = await agent.otvetit(cfg, poisk, istoriya, "а сорт а почём")
    assert "513" in r.otvet
    assert "номер" not in r.otvet.lower()


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
