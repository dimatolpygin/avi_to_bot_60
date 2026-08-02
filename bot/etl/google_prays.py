# -*- coding: utf-8 -*-
"""Живой источник прайса — Google-таблица заказчика (курс 31.07).

Заказчик правит прайс прямо в своей гугл-таблице, бот подтягивает изменения сам.
Читаем лист сервисным аккаунтом (доступ на чтение выдан) и отдаём тот же `Prays`,
что и файловый `chtenie.prochitat`, — дальше по общему конвейеру `import_prays`.

Границы модуля:
- gspread/google-auth импортируются ЛЕНИВО, внутри функции: на машинах без ключа
  (тесты, CI) импорт пакета `bot.etl` не должен падать из-за отсутствия библиотек.
- Разбор строк, поиск заголовка, минимум и приведение типов — не здесь, а в общем
  ядре `chtenie.sobrat_prays`: у файла и у таблицы формат строк одинаковый
  (`list[list]`), различается только способ их достать.
- Секрет (service-account.json) в код и репозиторий НЕ попадает — только путь,
  который приходит из конфигурации; сам файл лежит вне git.

⚠️ Крайний фолбэк на CSV остаётся у вызывающего (`core._katalog`): если Google лёг,
работаем на последней версии из БД, а офлайн — из файла. Этот модуль про «сходить
в таблицу», а не про стратегию отказа.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..logger import logger
from .chtenie import OshibkaPraysa, Prays, sobrat_prays

# Реальная таблица заказчика и лист товаров Saunamart (курс 31.07).
TABLICA_ID_PO_UMOLCHANIYU = "1K4SHMdGz8vv3qcgM8P5zApwtcKy1Tsye4tiYkj__udw"
LIST_PO_UMOLCHANIYU = "Saunamart"
# Читаем только на чтение — сервисному аккаунту большего и не выдано.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _syrye_stroki_google(creds_put: str, tablica_id: str, list_name: str) -> list[list]:
    """Сырые строки листа как `list[list[str]]` — та же форма, что у CSV-читалки.

    Все беды (нет библиотек, нет доступа, нет листа) заворачиваем в `OshibkaPraysa`
    с русским текстом: он идёт человеку как есть и не должен маскироваться под
    непонятный трейс gspread.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:                                   # pragma: no cover
        raise OshibkaPraysa(
            f"❌ Не установлены gspread/google-auth, читать Google-таблицу нечем: {e}"
        )

    try:
        creds = Credentials.from_service_account_file(creds_put, scopes=SCOPES)
    except OSError as e:
        raise OshibkaPraysa(
            f"❌ Не найден ключ сервисного аккаунта «{creds_put}»: {e}\n"
            f"   Он лежит вне git; проверь путь в конфигурации."
        )
    except Exception as e:  # noqa: BLE001 — битый json ключа, нечитаемые поля
        raise OshibkaPraysa(f"❌ Ключ сервисного аккаунта «{creds_put}» негоден: {e}")

    try:
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(tablica_id)
    except Exception as e:  # noqa: BLE001 — сеть, нет доступа, неверный id
        raise OshibkaPraysa(
            f"❌ Не удалось открыть Google-таблицу {tablica_id}: {e}\n"
            f"   Проверь, что таблица расшарена на сервисный аккаунт (на чтение)."
        )

    try:
        ws = sh.worksheet(list_name)
    except Exception as e:  # noqa: BLE001 — нет такого листа
        listy = ", ".join(w.title for w in sh.worksheets())
        raise OshibkaPraysa(
            f"❌ В таблице нет листа «{list_name}» ({e}). Листы: {listy}."
        )

    # get_all_values отдаёт все ячейки строками, пустые — "" (не None). Заголовок
    # так же стоит первой строкой с пустой ячейкой слева — общее ядро его найдёт.
    return ws.get_all_values()


def prochitat_google(creds_put: str,
                     tablica_id: str = TABLICA_ID_PO_UMOLCHANIYU,
                     list_name: str = LIST_PO_UMOLCHANIYU) -> Prays:
    """Прочитать прайс из Google-таблицы и проверить его целиком.

    Дата прайса — момент чтения (у таблицы нет mtime файла). Всё остальное —
    как у файлового источника, тем же ядром `sobrat_prays`.
    """
    logger.info("🔗 Читаю Google-таблицу %s, лист «%s»", tablica_id, list_name)
    syrye = _syrye_stroki_google(creds_put, tablica_id, list_name)
    data = datetime.now(timezone.utc)
    istochnik = f"Google-таблица · лист {list_name}"
    return sobrat_prays(syrye, istochnik, data, istochnik)


def main(argv: list[str]) -> int:
    """CLI-проба: `python -m bot.etl.google_prays <путь_к_creds> [id_таблицы] [лист]`.

    Ходит в сеть (читает таблицу), но в БД не пишет — только показывает, что
    источник жив и сколько строк отдаёт. Денег не тратит.
    """
    if len(argv) < 2:
        print("Использование: python -m bot.etl.google_prays "
              "<путь_к_service-account.json> [id_таблицы] [лист]")
        return 2
    creds_put = argv[1]
    tablica_id = argv[2] if len(argv) > 2 else TABLICA_ID_PO_UMOLCHANIYU
    list_name = argv[3] if len(argv) > 3 else LIST_PO_UMOLCHANIYU
    try:
        prays = prochitat_google(creds_put, tablica_id, list_name)
    except OshibkaPraysa as e:
        print(e)
        return 1
    print(f"✅ Источник: {prays.istochnik}")
    print(f"   Пригодных строк: {prays.vsego_strok}")
    print(f"   Дата чтения: {prays.data_praysa.strftime('%d.%m.%Y %H:%M')}")
    for s in prays.stroki[:3]:
        print(f"   · {s.article} | {s.name[:50]} | {s.price_apiece} ₽")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
