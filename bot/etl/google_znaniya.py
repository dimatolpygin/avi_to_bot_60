# -*- coding: utf-8 -*-
"""Живой источник БАЗЫ ЗНАНИЙ услуг — вкладки Google-таблицы заказчика (этап 19, Шаг 1).

Аккаунты услуг (SB SAUNA, Дешман) отвечают промптом, собранным из именованных
блоков знаний (`knowledge_blocks`). До этого блоки правились только в БД/панели;
теперь заказчик правит их прямо во вкладке своей таблицы — рядом с листом каталога.

Читаем вкладку тем же сервисным аккаунтом, что и каталог (`google_prays`), и
разбираем в список `BlokStroka` (ключ, заголовок, текст, вкл). Дальше — по синку
`bot/sinhronizatsiya_znaniy.py`.

Границы модуля (как у `google_prays`):
- gspread/google-auth тянутся ЛЕНИВО (внутри общего низкоуровневого читателя),
  чтобы `import bot.etl` не падал на машинах без ключа (тесты, CI);
- секрет (service-account.json) в код/репо не попадает — только путь из конфига;
- разбор строк здесь чистый (`razobrat_bloki` над `list[list[str]]`) и тестируется
  без сети: сеть — только в `prochitat_znaniya`.

⚠️ Вкладка каталога Saunamart называется «Saunamart», а вкладка ПРОМПТОВ товарного
аккаунта — «Saunamart (1)» (Шаг 2). Не перепутать: разные вкладки одной книги.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..logger import logger
from .google_prays import _syrye_stroki_google

# Вкладка знаний по коду аккаунта. Шаг 1 — только услуги; товарный «Saunamart (1)»
# подключается на Шаге 2 (сначала факты Saunamart выносятся из кода в блоки).
LISTY_ZNANIY: dict[str, str] = {
    "sbsauna": "SB SAUNA",
    "sbsauna_deshman": "SB SAUNA Дешман",
}

# Ключ блока — строго латиница (так же, как в коде и в схеме инструментов): по нему
# бот находит раздел. Кириллица/пробелы = это не строка данных (шапка, памятка).
_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

# «Вкл»: что считаем выключенным. Пусто и всё прочее — включено (мягко: заказчик
# скорее забудет поставить «да», чем случайно включит выключенное).
_VYKL = {"нет", "no", "0", "false", "выкл", "off", "-", "×", "x"}


class OshibkaZnaniy(Exception):
    """Вкладку знаний не удалось прочитать/разобрать. Текст русский — идёт в лог."""


@dataclass(frozen=True)
class BlokStroka:
    """Одна строка вкладки знаний: то, что правит заказчик."""

    key: str
    title: str
    content: str
    vkl: bool = True


def _yacheyka(row: list[str], j: int | None) -> str:
    """Значение ячейки по индексу колонки; нет колонки/за краем строки → пусто."""
    if j is None or j >= len(row):
        return ""
    return (row[j] or "").strip()


def _vkl_iz(raw: str) -> bool:
    """«да»/«нет»/пусто → bool. Пусто = включено."""
    return raw.strip().lower() not in _VYKL if raw.strip() else True


def razobrat_bloki(syrye: list[list[str]]) -> list[BlokStroka]:
    """Разобрать сырые строки вкладки в блоки знаний. Чистая функция, без сети.

    Ищем строку-шапку по колонке «ключ_блока» (лист может иметь сверху заголовок
    и строку-инструкцию — их пропускаем). Колонки находим по именам, а не по
    позиции: заказчик может вставить столбец. Строки ниже шапки с непустым
    латинским ключом — блоки; памятка справа (пустой ключ) и мусор отсеиваются.
    """
    hdr_idx: int | None = None
    cols: dict[str, int] = {}
    for i, row in enumerate(syrye):
        nizhnie = [(c or "").strip().lower() for c in row]
        if "ключ_блока" not in nizhnie:
            continue
        hdr_idx = i
        for j, name in enumerate(nizhnie):
            if name == "ключ_блока":
                cols["key"] = j
            elif name == "раздел":
                cols["title"] = j
            elif name.startswith("текст"):
                cols["content"] = j
            elif name == "вкл":
                cols["vkl"] = j
        break

    if hdr_idx is None or "key" not in cols:
        raise OshibkaZnaniy(
            "❌ Во вкладке знаний не нашёл шапку с колонкой «ключ_блока». "
            "Проверь, что строка-заголовок таблицы на месте и не переименована.")

    bloki: list[BlokStroka] = []
    vidennye: set[str] = set()
    for row in syrye[hdr_idx + 1:]:
        key = _yacheyka(row, cols["key"])
        if not key:
            continue
        if not _KEY_RE.match(key):
            logger.warning("📚 Вкладка знаний: пропускаю строку с некорректным ключом %r "
                           "(ключ — латиница, напр. dostavka)", key)
            continue
        if key in vidennye:
            logger.warning("📚 Вкладка знаний: ключ «%s» встречается дважды — беру первый", key)
            continue
        vidennye.add(key)
        title = _yacheyka(row, cols.get("title")) or key
        content = _yacheyka(row, cols.get("content"))
        vkl = _vkl_iz(_yacheyka(row, cols.get("vkl")))
        bloki.append(BlokStroka(key=key, title=title, content=content, vkl=vkl))
    return bloki


def prochitat_znaniya(creds_put: str, tablica_id: str, list_name: str) -> list[BlokStroka]:
    """Прочитать вкладку знаний из Google-таблицы и разобрать в блоки.

    Сеть только здесь (через общий низкоуровневый читатель каталога). Все беды
    заворачиваются в `OshibkaZnaniy`/`OshibkaPraysa` с русским текстом.
    """
    logger.info("🔗 Читаю вкладку знаний «%s» (таблица %s)", list_name, tablica_id)
    syrye = _syrye_stroki_google(creds_put, tablica_id, list_name)
    bloki = razobrat_bloki(syrye)
    logger.info("📚 Вкладка «%s»: разобрано блоков — %d", list_name, len(bloki))
    return bloki
