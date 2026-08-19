# -*- coding: utf-8 -*-
"""Лист-пульт «Рубильник» — заказчик глушит/включает ответы ботов из Google-таблицы.

Запрос заказчика 19.08: отдельная вкладка со списком всех аккаунтов и колонкой
«отвечает» (да/нет). Выключенный аккаунт по-прежнему зеркалит входящие в amoCRM
(менеджер видит чат), но САМ не отвечает. Читается фоновым синком
(`bot/sinhronizatsiya_rubilnika.py`) НЕЗАВИСИМО от синка знаний/каталога — нужен
только ключ Google (`GOOGLE_CREDS_PUT`) и флаг `GOOGLE_RUBILNIK` (по умолчанию on).

    Вкладка «Рубильник»  →  {код_аккаунта: отвечает?}  →  Yadro.ustanovit_rubilnik

Формат листа (человекочитаемый, колонки ищем по имени, не по позиции):

    | код              | бот                     | отвечает |
    | saunamart        | Saunamart (товары)      | да       |
    | sbsauna          | SB SAUNA (услуги)       | да       |
    | sbsauna_deshman  | SB SAUNA Дешман         | нет      |

Границы модуля — как у `google_prays`/`google_znaniya`: сеть только в
`prochitat_rubilnik` (через общий низкоуровневый читатель), разбор строк чистый и
тестируется без сети; секрет (service-account.json) в код/репо не попадает.
"""
from __future__ import annotations

import re

from ..logger import logger
from .google_prays import OshibkaPraysa, _syrye_stroki_google

# Вкладка по умолчанию. Заказчик может переименовать — тогда `GOOGLE_LIST_RUBILNIK`.
LIST_RUBILNIK_PO_UMOLCHANIYU = "Рубильник"

# Имена колонок (в нижнем регистре). Ключ аккаунта и состояние ищем по синонимам,
# чтобы заказчик мог назвать шапку привычно. «бот»/«название» — просто подпись.
_COL_KOD = {"код", "код_аккаунта", "аккаунт", "code"}
_COL_OTVECHAET = {"отвечает", "вкл", "бот_отвечает", "включён", "включен", "статус"}

# Ключ аккаунта — латиница/цифры/подчёркивание (как коды в системе: saunamart и т.п.).
_KOD_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

# «Отвечает?»: что считаем ВЫКЛЮЧЕННЫМ. Пусто и всё прочее — включено (мягко: забыть
# «да» безопаснее, чем случайно заглушить живого бота).
_VYKL = {"нет", "no", "0", "false", "выкл", "off", "-", "×", "x", "стоп", "молчит"}


class OshibkaRubilnika(Exception):
    """Лист-пульт не удалось прочитать/разобрать. Текст русский — идёт в лог."""


def _yacheyka(row: list[str], j: int | None) -> str:
    if j is None or j >= len(row):
        return ""
    return (row[j] or "").strip()


def _otvechaet_iz(raw: str) -> bool:
    """«да»/«нет»/пусто → bool. Пусто = отвечает (включён)."""
    return raw.strip().lower() not in _VYKL if raw.strip() else True


def razobrat_rubilnik(syrye: list[list[str]]) -> dict[str, bool]:
    """Разобрать сырые строки листа-пульта в карту {код: отвечает?}. Без сети.

    Ищем строку-шапку по колонке кода (лист может иметь сверху заголовок/инструкцию
    — их пропускаем). Колонки находим по именам. Строки ниже шапки с непустым
    латинским кодом — аккаунты; подпись/мусор отсеиваются. Дубликат кода — первый.
    """
    hdr_idx: int | None = None
    j_kod: int | None = None
    j_otv: int | None = None
    for i, row in enumerate(syrye):
        nizhnie = [(c or "").strip().lower() for c in row]
        nabor = set(nizhnie)
        if not (nabor & _COL_KOD):
            continue
        hdr_idx = i
        for j, name in enumerate(nizhnie):
            if name in _COL_KOD and j_kod is None:
                j_kod = j
            elif name in _COL_OTVECHAET and j_otv is None:
                j_otv = j
        break

    if hdr_idx is None or j_kod is None:
        raise OshibkaRubilnika(
            "❌ В листе-пульте не нашёл шапку с колонкой «код». Проверь, что строка-"
            "заголовок на месте и есть колонки «код» и «отвечает».")

    karta: dict[str, bool] = {}
    for row in syrye[hdr_idx + 1:]:
        kod = _yacheyka(row, j_kod)
        if not kod:
            continue
        if not _KOD_RE.match(kod):
            logger.warning("🔌 Лист-пульт: пропускаю строку с некорректным кодом %r "
                           "(код — латиница, напр. sbsauna)", kod)
            continue
        if kod in karta:
            logger.warning("🔌 Лист-пульт: код «%s» встречается дважды — беру первый", kod)
            continue
        karta[kod] = _otvechaet_iz(_yacheyka(row, j_otv))
    return karta


def prochitat_rubilnik(creds_put: str, tablica_id: str, list_name: str) -> dict[str, bool]:
    """Прочитать лист-пульт из Google-таблицы и разобрать в карту {код: отвечает?}.

    Сеть только здесь (через общий низкоуровневый читатель каталога). Беды сети/
    доступа приходят как `OshibkaPraysa`, разбора — как `OshibkaRubilnika`.
    """
    logger.info("🔗 Читаю лист-пульт «%s» (таблица %s)", list_name, tablica_id)
    try:
        syrye = _syrye_stroki_google(creds_put, tablica_id, list_name)
    except OshibkaPraysa:
        raise
    karta = razobrat_rubilnik(syrye)
    logger.info("🔌 Лист-пульт «%s»: аккаунтов — %d (%s)", list_name, len(karta),
                ", ".join(f"{k}={'да' if v else 'НЕТ'}" for k, v in karta.items()) or "—")
    return karta
