# -*- coding: utf-8 -*-
"""HTTP-API панели: лента диалога из Postgres для виджета amoCRM (14.7, кирпич 2).

Только чтение и только из `dialogs`/`messages` (их наполняет журнал кирпича 1).
Три ручки:

* `GET /api/health`          — жив ли сервис (без токена, для nginx/проверок);
* `GET /api/chats`           — список чатов, свежие сверху, с превью последней реплики;
* `GET /api/dialog/{id}`     — вся переписка одного диалога.

Доступ — bearer-токен `PANEL_API_TOKEN`: виджет живёт в браузере на `amocrm.ru`
и ходит сюда через HTTPS (nginx проксирует `/api` в контейнер). Токен пуст →
API не поднимается вовсе (штатный выключатель, как пустой токен бота).

Чтение вынесено в чистые async-функции (`spisok_chatov`, `soobshcheniya_dialoga`):
их гоняют тесты на фейковой сессии без БД, а aiohttp-ручки — тонкие обёртки.
Ошибку БД не проглатываем (в отличие от журнала): панель должна честно показать
503, а не пустую ленту, иначе менеджер решит, что переписки нет.
"""
from __future__ import annotations

from datetime import datetime

from aiohttp import web
from sqlalchemy import select

from ..logger import logger, log_oshibka
from ..models import Account, Dialog, Message

# Крайняя длина списка чатов: панель показывает недавние, вся история — по клику
# в конкретный диалог. Ограничение и защита от «отдать всю таблицу разом».
_LIMIT_CHATOV = 200


def _vremya(dt: datetime | None) -> str | None:
    """datetime → ISO-строка для JSON (или None). Панель форматирует сама."""
    return dt.isoformat() if dt is not None else None


# ── Чтение из БД (тестируется без сети и без aiohttp) ─────────────────────────

async def spisok_chatov(fabrika_sessiy, *, limit: int = _LIMIT_CHATOV) -> list[dict]:
    """Диалоги, свежие сверху, с превью последней реплики. Пусто → []."""
    posledn = (
        select(Message.body)
        .where(Message.dialog_id == Dialog.id)
        .order_by(Message.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        select(Dialog, Account.code, posledn.label("posledn"))
        .join(Account, Account.id == Dialog.account_id)
        .order_by(Dialog.last_message_at.desc())
        .limit(limit)
    )
    async with fabrika_sessiy() as sessiya:
        stroki = (await sessiya.execute(stmt)).all()
    return [
        {
            "dialog_id": d.id,
            "account": kod,
            "channel": d.channel,
            "chat_key": d.chat_key,
            "client_name": d.client_name,
            "client_username": d.client_username,
            "last_message_at": _vremya(d.last_message_at),
            "preview": posledn_body,
        }
        for d, kod, posledn_body in stroki
    ]


async def soobshcheniya_dialoga(fabrika_sessiy, dialog_id: int) -> list[dict] | None:
    """Вся переписка диалога по возрастанию времени. Диалога нет → None
    (ручка отдаст 404), пустой (заведён, реплик нет) → []."""
    async with fabrika_sessiy() as sessiya:
        dialog = (await sessiya.execute(
            select(Dialog).where(Dialog.id == dialog_id)
        )).scalar_one_or_none()
        if dialog is None:
            return None
        soobshcheniya = (await sessiya.execute(
            select(Message)
            .where(Message.dialog_id == dialog_id)
            .order_by(Message.created_at)
        )).scalars().all()
    return [
        {
            "id": m.id,
            "role": m.role,          # client | bot | system | tool
            "body": m.body,
            "request_id": m.request_id,
            "created_at": _vremya(m.created_at),
        }
        for m in soobshcheniya
    ]


# ── Авторизация ──────────────────────────────────────────────────────────────

def token_veren(request: web.Request, token: str) -> bool:
    """Bearer-токен из заголовка совпал с настроенным. Токен канала — секрет,
    сравнение прямое (не по префиксу): лишний пробел не должен пускать."""
    zagolovok = (request.headers.get("Authorization") or "").strip()
    if zagolovok.lower().startswith("bearer "):
        predlozhen = zagolovok[7:].strip()
    else:
        predlozhen = zagolovok
    return bool(token) and predlozhen == token


@web.middleware
async def _avtorizaciya(request: web.Request, handler):
    """Всё под `/api`, кроме health и preflight, требует токен."""
    if request.method == "OPTIONS" or request.path == "/api/health":
        return await handler(request)
    if not token_veren(request, request.app["token"]):
        return web.json_response({"error": "нет доступа"}, status=401)
    return await handler(request)


@web.middleware
async def _cors(request: web.Request, handler):
    """Виджет крутится в браузере на amocrm.ru и ходит сюда кросс-доменно.
    Origin настраивается (`PANEL_CORS_ORIGIN`), по умолчанию — любой amoCRM."""
    if request.method == "OPTIONS":
        otvet = web.Response(status=204)
    else:
        otvet = await handler(request)
    otvet.headers["Access-Control-Allow-Origin"] = request.app["origin"]
    otvet.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    otvet.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return otvet


# ── Ручки ────────────────────────────────────────────────────────────────────

async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _chaty(request: web.Request) -> web.Response:
    try:
        chaty = await spisok_chatov(request.app["fabrika"])
    except Exception as e:  # noqa: BLE001 — БД легла: 503, а не пустая лента
        log_oshibka(f"Панель: список чатов не отдан: {e}")
        return web.json_response({"error": "база недоступна"}, status=503)
    return web.json_response({"chats": chaty})


async def _dialog(request: web.Request) -> web.Response:
    try:
        dialog_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "неверный id диалога"}, status=400)
    try:
        soobshcheniya = await soobshcheniya_dialoga(request.app["fabrika"], dialog_id)
    except Exception as e:  # noqa: BLE001
        log_oshibka(f"Панель: диалог {dialog_id} не отдан: {e}")
        return web.json_response({"error": "база недоступна"}, status=503)
    if soobshcheniya is None:
        return web.json_response({"error": "диалог не найден"}, status=404)
    return web.json_response({"dialog_id": dialog_id, "messages": soobshcheniya})


def sozdat_prilozhenie(fabrika_sessiy, token: str, origin: str = "*") -> web.Application:
    """Собрать aiohttp-приложение. Отдельно от запуска — чтобы тесты поднимали
    его на тестовом сервере без реальной БД."""
    app = web.Application(middlewares=[_cors, _avtorizaciya])
    app["fabrika"] = fabrika_sessiy
    app["token"] = token
    app["origin"] = origin
    app.router.add_get("/api/health", _health)
    app.router.add_get("/api/chats", _chaty)
    app.router.add_get("/api/dialog/{id}", _dialog)
    return app


async def zapustit(fabrika_sessiy, token: str, *, port: int, host: str = "0.0.0.0",
                   origin: str = "*", stop=None) -> None:
    """Поднять API и держать до сигнала остановки. Токен пуст — не поднимаемся
    (проверяется вызывающим, здесь на всякий случай тоже)."""
    if not token:
        logger.info("🧩 Панель-API: токен не задан — не поднимаю")
        return
    app = sozdat_prilozhenie(fabrika_sessiy, token, origin)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("🧩 Панель-API слушает %s:%d (Origin %s), ручки /api/chats, /api/dialog/{id}",
                host, port, origin)
    try:
        if stop is not None:
            await stop.wait()
        else:  # без события остановки просто живём, пока не отменят задачу
            import asyncio
            await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        logger.info("🧩 Панель-API остановлен")
