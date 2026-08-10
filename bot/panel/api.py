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

import hashlib
import hmac
import json
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


async def dialog_adres(fabrika_sessiy, dialog_id: int) -> tuple[str, str] | None:
    """(код аккаунта, chat_key) диалога — для снятия флага оператора (14.8).

    Возвращает None, если диалога нет. `chat_key` у Авито — это `chat_id`, тот же
    ключ, по которому поллер ставит/снимает флаг перехвата в Redis."""
    async with fabrika_sessiy() as sessiya:
        stroka = (await sessiya.execute(
            select(Account.code, Dialog.chat_key)
            .join(Account, Account.id == Dialog.account_id)
            .where(Dialog.id == dialog_id)
        )).first()
    return (stroka[0], stroka[1]) if stroka else None


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
    """Всё под `/api`, кроме health и preflight, требует токен. Вебхук amoJo
    (`/amojo/…`) авторизуется своей HMAC-подписью, не bearer-токеном панели —
    его пускаем мимо (проверка подписи внутри ручки)."""
    if (request.method == "OPTIONS" or request.path == "/api/health"
            or request.path.startswith("/amojo/")):
        return await handler(request)
    if not token_veren(request, request.app["token"]):
        return web.json_response({"error": "нет доступа"}, status=401)
    return await handler(request)


# ── Вебхук amoJo (amo → клиент, 14.9-B) ──────────────────────────────────────

def podpis_amojo_verna(telo: bytes, secret: str, zagolovok: str | None) -> bool:
    """X-Signature amoCRM Chat API: HMAC-SHA1 (hex, нижний регистр) над СЫРЫМ
    телом запроса, ключ — секрет канала. Пустой секрет/заголовок → не пускаем."""
    if not secret or not zagolovok:
        return False
    nasha = hmac.new(secret.encode("utf-8"), telo, hashlib.sha1).hexdigest()
    return hmac.compare_digest(nasha, zagolovok.strip().lower())


async def _amojo_vebhuk(request: web.Request) -> web.Response:
    """`POST /amojo/{scope_id}` — событие из amoCRM (менеджер написал в карточке).

    Порядок: проверяем подпись СЫРОГО тела → логируем → отдаём обработчику в
    фоне логики. По доке Chat API канал обязан вернуть 200, а бизнес-логику
    делать асинхронно; ошибки обработчика не должны срывать 200 (иначе amoJo
    будет ретраить). Пока обработчик не задан (диагностическая фаза 14.9-B) —
    только логируем сырое тело, чтобы снять реальную форму вебхука с живого
    аккаунта (нужно поле `conversation.client_id` и проверка на эхо наших же
    импортов — риск петли)."""
    telo = await request.read()
    scope_id = request.match_info.get("scope_id", "")
    if not podpis_amojo_verna(telo, request.app.get("amojo_secret") or "",
                              request.headers.get("X-Signature")):
        log_oshibka(f"amoJo вебхук (scope {scope_id}): подпись не сошлась — отклоняю")
        return web.json_response({"error": "bad signature"}, status=403)
    try:
        dannye = json.loads(telo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        dannye = None
    logger.info("📥 amoJo вебхук (scope %s): %s", scope_id,
                telo.decode("utf-8", "replace")[:2000])
    obrabotchik = request.app.get("amojo_obrabotchik")
    if obrabotchik is not None and dannye is not None:
        try:
            await obrabotchik(dannye)
        except Exception as e:  # noqa: BLE001 — 200 отдаём всегда, иначе ретраи
            log_oshibka(f"amoJo вебхук (scope {scope_id}): обработчик упал: {e}")
    return web.json_response({"ok": True})


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
    otvet.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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
    # Пометка «чат ведёт оператор» (14.8): панель показывает бейдж и кнопку
    # возврата бота. Флаг в Redis — сбой не должен ронять список, поэтому мягко.
    operatory = request.app.get("operatory")
    if operatory is not None:
        for c in chaty:
            try:
                c["operator"] = await operatory.vedet(c["account"], c["chat_key"])
            except Exception:  # noqa: BLE001
                c["operator"] = False
    return web.json_response({"chats": chaty})


async def _vernut_bota(request: web.Request) -> web.Response:
    """`POST /api/dialog/{id}/resume` — снять перехват оператором, вернуть бота
    в чат (14.8). Кнопка в панели: менеджер закончил вести диалог вручную."""
    operatory = request.app.get("operatory")
    if operatory is None:
        return web.json_response({"error": "перехват недоступен"}, status=503)
    try:
        dialog_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "неверный id диалога"}, status=400)
    try:
        adres = await dialog_adres(request.app["fabrika"], dialog_id)
    except Exception as e:  # noqa: BLE001
        log_oshibka(f"Панель: возврат бота в диалог {dialog_id} не удался: {e}")
        return web.json_response({"error": "база недоступна"}, status=503)
    if adres is None:
        return web.json_response({"error": "диалог не найден"}, status=404)
    kod, chat_key = adres
    await operatory.snyat(kod, chat_key)
    logger.info("🙋 Панель: оператор вернул бота в чат %s:%s", kod, chat_key)
    return web.json_response({"ok": True, "dialog_id": dialog_id})


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


def sozdat_prilozhenie(fabrika_sessiy, token: str, origin: str = "*",
                       operatory=None, *, amojo_secret: str = "",
                       amojo_obrabotchik=None) -> web.Application:
    """Собрать aiohttp-приложение. Отдельно от запуска — чтобы тесты поднимали
    его на тестовом сервере без реальной БД. `operatory` (14.8) — управление
    перехватом оператором (статус чата + кнопка возврата бота). `amojo_secret`
    (14.9-B) — секрет канала для проверки подписи вебхука amoJo; `amojo_obrabotchik`
    — колбэк на разобранное событие (пусто → диагностический режим, только лог)."""
    app = web.Application(middlewares=[_cors, _avtorizaciya])
    app["fabrika"] = fabrika_sessiy
    app["token"] = token
    app["origin"] = origin
    app["operatory"] = operatory
    app["amojo_secret"] = amojo_secret
    app["amojo_obrabotchik"] = amojo_obrabotchik
    app.router.add_get("/api/health", _health)
    app.router.add_get("/api/chats", _chaty)
    app.router.add_get("/api/dialog/{id}", _dialog)
    app.router.add_post("/api/dialog/{id}/resume", _vernut_bota)
    app.router.add_post("/amojo/{scope_id}", _amojo_vebhuk)
    return app


async def zapustit(fabrika_sessiy, token: str, *, port: int, host: str = "0.0.0.0",
                   origin: str = "*", stop=None, operatory=None,
                   amojo_secret: str = "", amojo_obrabotchik=None) -> None:
    """Поднять API и держать до сигнала остановки. Токен пуст — не поднимаемся
    (проверяется вызывающим, здесь на всякий случай тоже)."""
    if not token:
        logger.info("🧩 Панель-API: токен не задан — не поднимаю")
        return
    app = sozdat_prilozhenie(fabrika_sessiy, token, origin, operatory,
                             amojo_secret=amojo_secret,
                             amojo_obrabotchik=amojo_obrabotchik)
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
