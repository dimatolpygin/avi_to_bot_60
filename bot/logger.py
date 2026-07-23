# -*- coding: utf-8 -*-
"""Логи на русском со сквозным request-id.

Требование проекта — логировать «каждую щель»: входящее сообщение клиента,
каждый вызов инструмента (поиск по прайсу, save_lead), каждый вызов ИИ
(модель, латентность, токены), исходящий ответ, ошибку с контекстом.
Всё это сшивается ОДНИМ request-id по всему пути обработки сообщения:
`👤 входящее → 🔧 tool → 🧠 ИИ → 🤖 ответ` с одинаковым id в скобках.

Механика (перенос из референса telewin): `contextvars` держат аккаунт и
request-id текущей asyncio-задачи — каждое входящее сообщение обрабатывается
своей задачей, значения не смешиваются между клиентами. Фильтр подмешивает их
в КАЖДУЮ запись, поэтому любой `logger.info(...)` в глубине (core → ai →
openrouter) автоматически несёт `[аккаунт rid]`, и id не надо таскать
параметром через весь тракт.

`LOG_LEVEL=debug` поднимает уровень и включает в лог промпт и ответ модели;
на `info` (по умолчанию) промпты не пишутся — незачем им течь в прод-логи.
"""
import contextlib
import contextvars
import logging
import os
import sys
import uuid

_FORMAT = "%(asctime)s  %(levelname)-5s  [%(akkaunt)s %(rid)s]  %(message)s"
_DATEFMT = "%d.%m.%Y %H:%M:%S"

# Сквозной контекст одного запроса. Прочерк — для строк вне обработки сообщения
# (старт процесса, подключение к БД, загрузка каталога).
_akkaunt: contextvars.ContextVar[str] = contextvars.ContextVar("akkaunt", default="—")
_rid: contextvars.ContextVar[str] = contextvars.ContextVar("rid", default="—")


class _KontekstFilter(logging.Filter):
    """Подмешивает аккаунт и request-id в каждую запись — их читает форматтер."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.akkaunt = _akkaunt.get()
        record.rid = _rid.get()
        return True


def _uroven() -> int:
    return logging.DEBUG if os.environ.get("LOG_LEVEL", "").lower() == "debug" else logging.INFO


def _setup() -> logging.Logger:
    lg = logging.getLogger("sbavito")
    if lg.handlers:
        return lg
    # На Windows консоль по умолчанию CP866 → кириллица кракозябрами.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    lg.setLevel(_uroven())
    h = logging.StreamHandler(sys.stdout)  # stdout — основной сток (docker logs)
    h.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    # Фильтр висит на ЛОГГЕРЕ, а не на хендлере: так аккаунт и rid попадают
    # в саму запись и видны любому, кто её читает, — включая тесты, которые
    # проверяют сквозной id, не разбирая отформатированную строку.
    lg.addFilter(_KontekstFilter())
    lg.addHandler(h)
    lg.propagate = False
    return lg


logger = _setup()

# debug-режим включает промпт и ответ ИИ в лог вызова модели
DEBUG_LOGI = _uroven() == logging.DEBUG


@contextlib.contextmanager
def nachat_zapros(akkaunt: str, rid: str | None = None):
    """Открыть контекст обработки одного входящего сообщения: зафиксировать
    аккаунт и сгенерировать request-id. Всё, что логируется внутри (включая
    await'ы той же задачи), несёт этот id. Возвращает id."""
    rid = rid or uuid.uuid4().hex[:8]
    t_ak = _akkaunt.set(akkaunt)
    t_rid = _rid.set(rid)
    try:
        yield rid
    finally:
        _akkaunt.reset(t_ak)
        _rid.reset(t_rid)


def tekushchiy_rid() -> str:
    return _rid.get()


def tekushchiy_akkaunt() -> str:
    return _akkaunt.get()


# ── Помощники логирования «каждой щели» ──────────────────────────────────────

def log_vhodyashchee(username, user_id, first_name, text: str) -> None:
    """Входящее действие клиента: сообщение, команда или нажатие кнопки."""
    logger.info(f"👤 @{username or '—'} (id:{user_id}, {first_name or ''}) → {text}")


def log_tool_call(name: str, args: dict, rezultat) -> None:
    """Вызов инструмента агентом: имя + аргументы + краткий результат.
    Для поиска `rezultat` — сколько позиций найдено (0 = честный абстейн)."""
    logger.info(f"🔧 tool {name}({args}) → {rezultat}")


def log_vyzov_ii(model: str, latency_ms: int, usage: dict | None = None,
                 *, messages=None, otvet=None) -> None:
    """Вызов ИИ: модель + латентность (+ токены, если есть usage).
    В debug — добавляет промпт и ответ модели отдельными строками."""
    hvost = ""
    if usage:
        hvost = (f" · токены {usage.get('prompt_tokens', '?')}"
                 f"+{usage.get('completion_tokens', '?')}"
                 f"={usage.get('total_tokens', '?')}")
    logger.info(f"🧠 ИИ {model} · {latency_ms}мс{hvost}")
    if DEBUG_LOGI:
        if messages is not None:
            logger.debug(f"🧠 промпт: {messages}")
        if otvet is not None:
            logger.debug(f"🧠 ответ: {otvet}")


def log_ishodyashchee(username, text: str, *, ms: int | None = None, meta: str = "") -> None:
    """Исходящий ответ бота клиенту."""
    hvost = f" за {ms}мс" if ms is not None else ""
    logger.info(f"🤖 → @{username or '—'}{hvost}: {text}{(' ' + meta) if meta else ''}")


def _soobshcheniy(n: int) -> str:
    """«2 сообщения», «5 сообщений» — лог читает человек, склонения важны."""
    hvost = n % 100
    if 11 <= hvost <= 14:
        return "сообщений"
    return {1: "сообщение", 2: "сообщения", 3: "сообщения", 4: "сообщения"}.get(n % 10, "сообщений")


def log_sklejka(skolko: int, tekst: str) -> None:
    """Дебаунс склеил несколько быстрых сообщений клиента в один вопрос."""
    logger.info(f"🧵 склейка: {skolko} {_soobshcheniy(skolko)} → один ответ | {tekst}")


def log_pauza(chto: str, sekund: float, *, meta: str = "") -> None:
    """Живая задержка перед действием. Пишем КАЖДУЮ и с точностью до десятых:
    по логу должно быть видно, что паузы плавающие, а не фиксированный таймер."""
    hvost = f" ({meta})" if meta else ""
    logger.info(f"⏳ {chto} {sekund:.1f} с{hvost}")


def log_preryvanie(otpravleno: int, vsego: int) -> None:
    """Клиент написал новое, пока мы отвечали: остаток реплик отменён.
    В историю диалога уходит только реально отправленное — иначе бот «помнит»
    несказанное и на следующем ходу ссылается на него."""
    logger.info(f"✂️ прервано новым сообщением: отправлено {otpravleno} из {vsego} реплик")


def log_oshibka(chto: str, *, zapros: str = "") -> None:
    """Ошибка с полным контекстом: traceback + аккаунт/rid (через фильтр) +
    исходный текст запроса. Звать из блока except."""
    hvost = f" | запрос: {zapros!r}" if zapros else ""
    logger.error(f"❌ {chto}{hvost}", exc_info=True)
