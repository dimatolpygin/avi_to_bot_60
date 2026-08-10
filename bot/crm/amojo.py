# -*- coding: utf-8 -*-
"""amoCRM Chat API (amojo): зеркало диалога Авито → карточка CRM (этап 14.3a).

Менеджер должен видеть переписку с Авито и ответы бота прямо в amoCRM, в
нативном чате карточки. Для этого каждое сообщение — и клиента, и бота —
отправляется в amojo событием `new_message`.

На 14.3a зеркало **одностороннее**: Авито → amoCRM. Обратная сторона (ответ
менеджера из amo уходит клиенту в Авито) требует публичного вебхука и деплоя —
это 14.5, туда и вынесена.

Подпись запроса (amoCRM Chat API): HMAC-SHA1 (hex, lower) над строкой
`METHOD\\nContent-MD5\\nContent-Type\\nDate\\nPath`, где `Content-MD5` — md5-хеш
ТОЧНО тех байтов тела, что уходят в запрос. Поэтому тело сериализуем один раз
и им же считаем md5 — иначе подпись не сойдётся с телом.

Сбой зеркала НЕ роняет диалог: как и лид (`bot/lead.py`), ошибка идёт в лог
целиком, а клиент получает ответ бота. Не показать переписку в CRM неприятно,
оборвать живой диалог из-за CRM — хуже.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from email.utils import formatdate

import httpx

from ..config import AmojoConfig
from ..logger import log_oshibka, logger

TAYMAUT_S = 30.0
CONTENT_TYPE = "application/json"


class OshibkaAmojo(Exception):
    """Сбой обращения к amoCRM Chat API. `status` — HTTP-код, если он был."""

    def __init__(self, soobshchenie: str, status: int | None = None):
        super().__init__(soobshchenie)
        self.status = status


# ── Подпись (чистые функции, чтобы проверять вектором без сети) ───────────────

def _md5_hex(telo: bytes) -> str:
    return hashlib.md5(telo).hexdigest()


def stroka_podpisi(metod: str, content_md5: str, content_type: str,
                   date: str, put: str) -> str:
    """Каноничная строка amoCRM Chat API для X-Signature."""
    return "\n".join([metod.upper(), content_md5, content_type, date, put])


def x_signature(secret: str, stroka: str) -> str:
    """HMAC-SHA1 (hex, нижний регистр) над канонической строкой."""
    return hmac.new(secret.encode("utf-8"), stroka.encode("utf-8"),
                    hashlib.sha1).hexdigest()


# ── Клиент amojo ─────────────────────────────────────────────────────────────

class AmojoAPI:
    """Обёртка над amoCRM Chat API с HMAC-подписью.

    Держит свой `httpx.AsyncClient` (или принимает внешний — так тесты
    подсовывают `MockTransport` без сети).
    """

    def __init__(self, cfg: AmojoConfig, *, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client
        self._own = client is None

    async def __aenter__(self) -> "AmojoAPI":
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.cfg.base_url, timeout=TAYMAUT_S)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()

    async def _post(self, put: str, telo: dict) -> dict:
        """Подписать и отправить POST. Путь `put` — без домена, с ведущим слэшем."""
        assert self._client is not None
        # Тело сериализуем один раз: этими же байтами считаем Content-MD5 и их же
        # шлём — иначе подпись не сойдётся с телом (ensure_ascii=False: кириллица
        # в UTF-8, как принимает amojo).
        syrye = json.dumps(telo, ensure_ascii=False).encode("utf-8")
        content_md5 = _md5_hex(syrye)
        date = formatdate(time.time(), usegmt=True)
        podpis = x_signature(
            self.cfg.channel_secret,
            stroka_podpisi("POST", content_md5, CONTENT_TYPE, date, put))
        zagolovki = {
            "Date": date,
            "Content-Type": CONTENT_TYPE,
            "Content-MD5": content_md5,
            "X-Signature": podpis,
        }
        otvet = await self._client.post(put, content=syrye, headers=zagolovki)
        if otvet.status_code >= 400:
            raise OshibkaAmojo(
                f"amoCRM Chat API {otvet.status_code} на {put}: {otvet.text[:300]}",
                status=otvet.status_code)
        try:
            return otvet.json()
        except ValueError:
            return {}

    async def connect(self, title: str) -> dict:
        """Подключить канал к аккаунту amoCRM. Идемпотентно: повторный connect
        уже подключённого канала не ломает связь, а возвращает её параметры.

        ⚠️ Внешнее действие: пишет в живой аккаунт amoCRM (регистрирует источник)."""
        return await self._post(
            f"/v2/origin/custom/{self.cfg.channel_id}/connect",
            {"account_id": self.cfg.amojo_id, "title": title, "hook_api_version": "v2"})

    async def new_message(self, payload: dict) -> dict:
        """Отправить событие `new_message` в канал (путь — по scope_id).

        ⚠️ Внешнее действие: сообщение появляется в карточке amoCRM."""
        return await self._post(
            f"/v2/origin/custom/{self.cfg.scope_id}",
            {"event_type": "new_message", "payload": payload})


# ── Сборка payload сообщения ─────────────────────────────────────────────────

def _uchastnik(uid: str, imya: str, telefon: str | None = None,
               ref_id: str | None = None) -> dict:
    u: dict = {"id": uid, "name": imya}
    if telefon:
        u["profile"] = {"phone": telefon}
    if ref_id:
        # ref_id помечает отправителя как пользователя amoCRM (CRM-сторона), а не
        # внешнего клиента: без него amojo считает бота вторым «origin user» и
        # отклоняет исходящее (error 453). UUID менеджера берётся через OAuth
        # amoCRM (14.4) — до него исходящие бота не зеркалим.
        u["ref_id"] = ref_id
    return u


def payload_soobshcheniya(*, conversation_id: str, msgid: str, sender: dict,
                          tekst: str, receiver: dict | None = None,
                          silent: bool = False) -> dict:
    """Тело `payload` для new_message.

    Входящее (от клиента) — только `sender`. Исходящее (бот) — `sender`+`receiver`.
    Направление в amoCRM определяется наличием `receiver`.
    """
    now = time.time()
    p: dict = {
        "timestamp": int(now),
        "msec_timestamp": int(now * 1000),
        "msgid": msgid,
        "conversation_id": conversation_id,
        "sender": sender,
        "message": {"type": "text", "text": tekst},
        "silent": silent,
    }
    if receiver is not None:
        p["receiver"] = receiver
    return p


# ── Зеркало одного аккаунта ──────────────────────────────────────────────────

class Zerkalo:
    """Одностороннее зеркало диалога Авито → amoCRM (14.3a).

    Знает имя бота (персона аккаунта) для исходящих и держит `AmojoAPI`. Каждый
    метод оборачивает сбой в лог и НЕ пробрасывает наверх: зеркало не должно
    ронять живой диалог (тот же принцип, что у `bot/lead.py`).
    """

    def __init__(self, api: AmojoAPI, kod: str, imya_bota: str, *,
                 bot_ref_id: str | None = None):
        self.api = api
        self.kod = kod
        self.imya_bota = imya_bota
        # UUID пользователя amoCRM, от чьего имени показываем реплики бота как
        # исходящие. Пусто → исходящие НЕ зеркалим (см. ishodyashchee): amojo без
        # ref_id их отклоняет. Появится в 14.4 вместе с OAuth-токеном amoCRM.
        self.bot_ref_id = bot_ref_id or None

    def _dialog(self, chat_id: str) -> str:
        # Стабильный id чата в amoCRM: один чат Авито = один чат в карточке.
        return f"{self.kod}:{chat_id}"

    def _klient(self, chat_id: str, avtor_id, imya: str | None,
                telefon: str | None = None) -> dict:
        return _uchastnik(f"avito:{avtor_id or chat_id}",
                          imya or "Клиент Авито", telefon)

    def _bot(self) -> dict:
        return _uchastnik(f"bot:{self.kod}", self.imya_bota, ref_id=self.bot_ref_id)

    async def vhodyashchee(self, chat_id: str, msg_id: str, avtor_id, tekst: str,
                           *, imya: str | None = None) -> None:
        """Сообщение клиента → входящее в amoCRM (только sender)."""
        try:
            await self.api.new_message(payload_soobshcheniya(
                conversation_id=self._dialog(chat_id),
                msgid=f"avito:{msg_id}",
                sender=self._klient(chat_id, avtor_id, imya),
                tekst=tekst))
            logger.info("📤 amoCRM ← клиент (чат %s): зеркалировано входящее", chat_id)
        except Exception as e:  # noqa: BLE001 — зеркало не роняет диалог
            log_oshibka(f"amoCRM зеркало (входящее, чат {chat_id}): {e}")

    async def ishodyashchee(self, chat_id: str, tekst: str, *, avtor_id=None,
                            imya_klienta: str | None = None) -> None:
        """Ответ бота → исходящее в amoCRM (sender=бот, receiver=клиент).

        Без `bot_ref_id` amojo отклоняет исходящее (нужен UUID пользователя
        amoCRM у отправителя), поэтому пока его нет — тихо пропускаем, чтобы не
        сыпать ошибками. Реплики бота подключатся в 14.4 с OAuth-токеном amoCRM.
        """
        if self.bot_ref_id is None:
            return
        try:
            await self.api.new_message(payload_soobshcheniya(
                conversation_id=self._dialog(chat_id),
                msgid=f"bot:{int(time.time() * 1000)}",
                sender=self._bot(),
                receiver=self._klient(chat_id, avtor_id, imya_klienta),
                tekst=tekst))
            logger.info("📤 amoCRM ← бот (чат %s): зеркалирован ответ", chat_id)
        except Exception as e:  # noqa: BLE001 — зеркало не роняет диалог
            log_oshibka(f"amoCRM зеркало (исходящее, чат {chat_id}): {e}")


# ── Смоук вручную ────────────────────────────────────────────────────────────

async def _uznat_amojo_id_menedzherov(rest) -> None:  # pragma: no cover - живой REST
    """Диагностика 14.9-C: спросить у REST amoCRM amojo-id/uuid пользователей.

    Кандидат на `sender.ref_id` для исходящих. По доке ref_id менеджера —
    amojo-идентификатор, а не plain-uuid из обычного `/users`; пробуем оба
    расширения (`amojo_id`, `uuid`) и печатаем, что вернулось.
    """
    for rasshirenie in ("amojo_id", "uuid"):
        try:
            async with httpx.AsyncClient(base_url=rest.base_url, timeout=TAYMAUT_S) as kl:
                r = await kl.get("/api/v4/users",
                                 params={"with": rasshirenie, "limit": 50},
                                 headers={"Authorization": f"Bearer {rest.access_token}"})
            print(f"\n— GET /api/v4/users?with={rasshirenie} → HTTP {r.status_code}")
            if r.status_code >= 400:
                print("  тело:", r.text[:400])
                continue
            users = (r.json().get("_embedded") or {}).get("users") or []
            for u in users:
                print(f"  id={u.get('id')} {u.get('name')!r} "
                      f"amojo_id={u.get('amojo_id')} uuid={u.get('uuid')}")
        except Exception as e:  # noqa: BLE001
            print(f"  ошибка запроса users?with={rasshirenie}: {e}")


def _smoke() -> None:  # pragma: no cover - ручной прогон, ходит в живой amoCRM
    """`python -m bot.crm.amojo connect|test|diag`.

    `connect` — идемпотентно подключает канал к аккаунту. `test` — шлёт пробную
    пару сообщений (клиент+бот) в тестовый чат: появится видимая переписка в CRM.
    `diag` (14.9-C) — шлёт ОДНО входящее в тестовый чат и печатает ПОЛНЫЙ ответ
    amojo (ищем присвоенный `ref_id` участника), затем спрашивает у REST amojo-id
    менеджеров. Не шлёт исходящее — только собирает данные для настройки
    `AMO_BOT_REF_ID`. Все три ПИШУТ в живой аккаунт amoCRM.
    """
    import asyncio
    import json as _json
    import sys

    from ..config import load_config

    komanda = sys.argv[1] if len(sys.argv) > 1 else "connect"
    polny = load_config()
    cfg = polny.amojo
    if cfg is None:
        print("Креды канала amoCRM не заданы в .env "
              "(AMOJO_CHANNEL_ID/AMOJO_CHANNEL_SECRET/AMOJO_ID).")
        raise SystemExit(1)

    async def run() -> None:
        async with AmojoAPI(cfg) as api:
            if komanda == "connect":
                print("connect →", await api.connect("Авито SB SAUNA (тест)"))
            elif komanda == "test":
                z = Zerkalo(api, "sbsauna", "Роман")
                await z.vhodyashchee("smoke-chat", "m-smoke", 999, "Тестовое от клиента",
                                     imya="Тест Клиент")
                await z.ishodyashchee("smoke-chat", "Тестовый ответ бота",
                                      avtor_id=999, imya_klienta="Тест Клиент")
                print("пара сообщений отправлена в чат smoke-chat")
            elif komanda == "diag":
                payload = payload_soobshcheniya(
                    conversation_id="sbsauna:diag-chat",
                    msgid=f"diag:{int(time.time() * 1000)}",
                    sender=_uchastnik("avito:diag-999", "Диаг Клиент"),
                    tekst="Диагностика 14.9-C: читаю ответ amojo")
                try:
                    otvet = await api.new_message(payload)
                    print("— new_message ответ amojo:")
                    print(_json.dumps(otvet, ensure_ascii=False, indent=2))
                except OshibkaAmojo as e:
                    print(f"— new_message упал: HTTP {e.status}: {e}")
                if polny.amo_rest is not None and polny.amo_rest.zapolnen:
                    await _uznat_amojo_id_menedzherov(polny.amo_rest)
                else:
                    print("\n(AMO REST не настроен — пропускаю опрос users)")
            elif komanda == "diag-out":
                ref = sys.argv[2] if len(sys.argv) > 2 else ""
                if not ref:
                    print("укажи ref_id (amojo_id менеджера): diag-out <amojo_id>")
                    return
                payload = payload_soobshcheniya(
                    conversation_id="sbsauna:diag-chat",
                    msgid=f"diag-out:{int(time.time() * 1000)}",
                    sender=_uchastnik("bot:sbsauna", "Роман", ref_id=ref),
                    receiver=_uchastnik("avito:diag-999", "Диаг Клиент"),
                    tekst="Диагностика 14.9-C: исходящее от менеджера (ref_id проверка)")
                try:
                    otvet = await api.new_message(payload)
                    print("— исходящее ПРИНЯТО amojo:")
                    print(_json.dumps(otvet, ensure_ascii=False, indent=2))
                except OshibkaAmojo as e:
                    print(f"— исходящее ОТКЛОНЕНО: HTTP {e.status}: {e}")
            else:
                print("неизвестная команда:", komanda)

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    _smoke()
