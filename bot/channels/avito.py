# -*- coding: utf-8 -*-
"""Адаптер Авито: один аккаунт Авито = один поллер (этап 14, подэтап 14.1).

Тонкий слой, как `telegram.py`: принимает входящее из Avito Messenger API,
отдаёт ядру и умеет отправить реплику. Диалог, память, задержки и дробление —
в ядре и диспетчере этапа 9; здесь только транспорт.

Почему **поллинг**, а не вебхук, на первом подэтапе:
* вебхук требует публичного HTTPS-эндпоинта (`bot-admin.online`) и деплоя — это
  подэтап 14.5; поллинг же поднимается локально и ничего снаружи не открывает;
* на аккаунте уже висит ЧУЖАЯ подписка вебхука (старый Jivo, см.
  `доступы/avito_keys.md`). Подписка одна на аккаунт — регистрировать свою поверх
  Jivo нельзя, а **поллинг от подписки не зависит**: `GET .../chats` отдаёт то же
  самое, ничего у Jivo не отнимая. Это и позволяет тестировать, не трогая Jivo.

OAuth — `client_credentials`: бот сам меняет client_id/secret на access_token и
продлевает его до истечения. Токен приходит на ~сутки (`expires_in`), но полагаться
на срок нельзя — обновляем с запасом и по 401.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from ..chelovek.dispetcher import Kanal
from ..config import AvitoConfig
from ..logger import log_oshibka, logger

BASE = "https://api.avito.ru"
TAYMAUT_S = 30.0
INTERVAL_POLLINGA_S = 5.0        # как часто спрашиваем новые чаты
ZAPAS_TOKENA_S = 60.0            # обновляем токен за минуту до истечения


class OshibkaAvito(Exception):
    """Сбой обращения к Avito API. `status` — HTTP-код, если он был."""

    def __init__(self, soobshchenie: str, status: int | None = None):
        super().__init__(soobshchenie)
        self.status = status


# ── Клиент Avito Messenger API ───────────────────────────────────────────────

class AvitoAPI:
    """Обёртка над REST Авито с ленивым токеном.

    Держит свой `httpx.AsyncClient` (или принимает внешний — так тесты
    подсовывают `MockTransport` без сети). Токен и `user_id` определяются
    лениво и кешируются.
    """

    def __init__(self, cfg: AvitoConfig, *, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client
        self._own = client is None
        self._token = ""
        self._token_do = 0.0            # monotonic-время, до которого токен валиден
        self._user_id = int(cfg.user_id or 0)

    async def __aenter__(self) -> "AvitoAPI":
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=BASE, timeout=TAYMAUT_S)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()

    # — токен —

    async def _token_svezhiy(self) -> str:
        """Валидный access_token: из кеша или свежий по client_credentials."""
        if self._token and time.monotonic() < self._token_do - ZAPAS_TOKENA_S:
            return self._token
        assert self._client is not None
        otvet = await self._client.post(
            "/token",
            data={"grant_type": "client_credentials",
                  "client_id": self.cfg.client_id,
                  "client_secret": self.cfg.client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if otvet.status_code != 200:
            raise OshibkaAvito(
                f"Токен Авито не выдан: HTTP {otvet.status_code} {otvet.text[:200]}",
                status=otvet.status_code)
        dannye = otvet.json()
        self._token = dannye["access_token"]
        self._token_do = time.monotonic() + float(dannye.get("expires_in", 86400))
        logger.info("🔑 Авито: токен получен, действует ~%dс", int(dannye.get("expires_in", 0)))
        return self._token

    async def _zagolovki(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token_svezhiy()}"}

    async def _get(self, put: str, params: dict | None = None) -> dict:
        assert self._client is not None
        otvet = await self._client.get(put, params=params, headers=await self._zagolovki())
        return _telo_ili_oshibka(otvet, put)

    async def _post(self, put: str, telo: dict) -> dict:
        assert self._client is not None
        otvet = await self._client.post(put, json=telo, headers=await self._zagolovki())
        return _telo_ili_oshibka(otvet, put)

    # — данные —

    async def user_id(self) -> int:
        """id аккаунта Авито. Задан в .env → берём его, иначе спрашиваем self."""
        if self._user_id:
            return self._user_id
        dannye = await self._get("/core/v1/accounts/self")
        self._user_id = int(dannye["id"])
        logger.info("🧑 Авито: аккаунт self id=%s (%s)", self._user_id, dannye.get("name", "—"))
        return self._user_id

    async def chaty(self, *, tolko_neprochitannye: bool = True, limit: int = 100) -> list[dict]:
        """Список чатов. `unread_only` сужает до тех, где есть непрочитанное —
        именно на них надо реагировать."""
        uid = await self.user_id()
        params: dict = {"limit": limit}
        if tolko_neprochitannye:
            params["unread_only"] = "true"
        dannye = await self._get(f"/messenger/v2/accounts/{uid}/chats", params)
        return dannye.get("chats", [])

    async def otpravit(self, chat_id: str, tekst: str) -> dict:
        """Отправить текст в чат. ⚠️ Внешнее действие — доходит до живого клиента."""
        uid = await self.user_id()
        return await self._post(
            f"/messenger/v1/accounts/{uid}/chats/{chat_id}/messages",
            {"message": {"text": tekst}, "type": "text"})

    async def otmetit_prochitannym(self, chat_id: str) -> dict:
        """Пометить чат прочитанным. На поллинге НЕ обязателен и по умолчанию не
        зовётся: отметка видна клиенту и может пересечься с Jivo."""
        uid = await self.user_id()
        return await self._post(f"/messenger/v1/accounts/{uid}/chats/{chat_id}/read", {})


def _telo_ili_oshibka(otvet: httpx.Response, put: str) -> dict:
    if otvet.status_code >= 400:
        raise OshibkaAvito(
            f"Avito API {otvet.status_code} на {put}: {otvet.text[:300]}",
            status=otvet.status_code)
    return otvet.json()


# ── Разбор входящего ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Vhodyashchee:
    """Одно входящее сообщение клиента, вытащенное из чата."""

    chat_id: str
    msg_id: str
    author_id: int | None
    tekst: str | None                 # None у картинок/голоса — текста нет
    obyavlenie: dict | None           # context.value: {id,title,price_string,url,...}


def izvlech_vhodyashchee(chat: dict) -> Vhodyashchee | None:
    """Достать последнее входящее из объекта чата.

    В `chats` уже едет `last_message` с направлением — отдельный запрос за
    сообщениями не нужен (это главная экономия: не дёргаем API на каждый чат).
    Исходящие (`direction != "in"`) и служебные игнорируем.
    """
    lm = chat.get("last_message") or {}
    if lm.get("direction") != "in":
        return None
    msg_id = lm.get("id")
    if not msg_id:
        return None
    content = lm.get("content") or {}
    kontekst = chat.get("context") or {}
    obyavlenie = kontekst.get("value") if kontekst.get("type") == "item" else None
    return Vhodyashchee(
        chat_id=str(chat.get("id")),
        msg_id=str(msg_id),
        author_id=lm.get("author_id"),
        tekst=content.get("text"),
        obyavlenie=obyavlenie,
    )


class Vidennye:
    """Дедуп: какой msg_id был последним обработанным в каждом чате.

    Держится в памяти процесса — этого хватает, чтобы повторный поллинг не
    отвечал дважды на то же сообщение. Переживание рестарта (Redis) — забота
    подэтапа 14.5, где поллинг сменится вебхуком.
    """

    def __init__(self) -> None:
        self._poslednie: dict[str, str] = {}

    def novoe(self, v: Vhodyashchee) -> bool:
        return self._poslednie.get(v.chat_id) != v.msg_id

    def otmetit(self, v: Vhodyashchee) -> None:
        self._poslednie[v.chat_id] = v.msg_id


# ── Цикл поллинга ────────────────────────────────────────────────────────────

async def cikl_pollinga(api: AvitoAPI, obrabotchik, stop: asyncio.Event, *,
                        interval_s: float = INTERVAL_POLLINGA_S,
                        vidennye: Vidennye | None = None) -> None:
    """Опрашивать новые входящие и звать `obrabotchik(Vhodyashchee)` на каждое.

    Сбой одного цикла (обрыв сети, 5xx) не роняет поллер: логируем и ждём
    следующего тика. Возврат — по событию `stop`.
    """
    vid = vidennye or Vidennye()
    while not stop.is_set():
        try:
            for chat in await api.chaty(tolko_neprochitannye=True):
                v = izvlech_vhodyashchee(chat)
                if v is None or not vid.novoe(v):
                    continue
                vid.otmetit(v)
                await obrabotchik(v)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — цикл живёт дальше
            log_oshibka(f"Поллинг Авито: сбой цикла: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


async def _obrabotchik_nablyudeniya(v: Vhodyashchee) -> None:
    """Обработчик подэтапа 14.1: только логируем, ядру НЕ отдаём (это 14.2)."""
    pod = f" (под объявл. «{v.obyavlenie.get('title')}»)" if v.obyavlenie else ""
    logger.info("👤 Авито вход: чат %s, msg %s%s → %s",
                v.chat_id, v.msg_id, pod, v.tekst or "[вложение без текста]")


async def zapustit_nablyudenie(cfg: AvitoConfig, stop: asyncio.Event) -> None:
    """Поллер в режиме наблюдения (14.1): входящее пишется в лог, ответа нет."""
    async with AvitoAPI(cfg) as api:
        uid = await api.user_id()
        logger.info("📡 Авито «%s»: поллинг наблюдения запущен", uid)
        await cikl_pollinga(api, _obrabotchik_nablyudeniya, stop)


# ── Режим ответа через ядро (подэтап 14.2) ───────────────────────────────────

def _kanal_avito(api: AvitoAPI, chat_id: str, imya: str) -> Kanal:
    """Колбэки транспорта Авито для диспетчера. `pechataet=None` — у Авито
    индикатора набора нет, задержки очеловечивания работают молча."""
    async def otpravit(tekst: str) -> None:
        await api.otpravit(chat_id, tekst)

    return Kanal(otpravit=otpravit, pechataet=None, imya=imya)


_PROSBA_TEKSTOM = ("Вложение вижу, но прочитать его не могу. Напишите, пожалуйста, "
                   "текстом, что нужно.")


async def zapustit(kod: str, cfg: AvitoConfig, yadro, stop: asyncio.Event, *,
                   belyy_spisok: frozenset[str] | None = None) -> None:
    """Поллер в режиме ответа: входящее уходит в ядро, ответ шлётся в Авито.

    `belyy_spisok` — множество `chat_id`, которым РАЗРЕШЕНО отвечать. `None`
    означало бы «отвечать всем», но на живом аккаунте это столкнётся с Jivo
    (см. модульный докстринг), поэтому боевой режим включается отдельно (14.5),
    а на 14.2 список всегда задан и узок (тестовый чат).
    """
    async with AvitoAPI(cfg) as api:
        uid = await api.user_id()
        logger.info("📡 Авито «%s» (%s): ответы через ядро, белый список: %s",
                    kod, uid, ", ".join(sorted(belyy_spisok)) if belyy_spisok else "ВСЕ")
        await cikl_pollinga(api, sdelat_obrabotchik(kod, api, yadro, belyy_spisok), stop)


def sdelat_obrabotchik(kod: str, api: AvitoAPI, yadro,
                       belyy_spisok: frozenset[str] | None):
    """Обработчик входящего в режиме ответа: белый список → вложение → ядро.

    Вынесен из `zapustit`, чтобы фильтр белого списка и передачу объявления
    можно было проверить без сети (на фейковых api/yadro).
    """
    async def obrabotchik(v: Vhodyashchee) -> None:
        if belyy_spisok is not None and v.chat_id not in belyy_spisok:
            logger.info("🔇 Авито «%s»: чат %s не в белом списке — пропускаю",
                        kod, v.chat_id)
            return
        imya = f"avito:{v.author_id}" if v.author_id else f"avito:{v.chat_id}"
        if v.tekst is None:
            # Вложение без текста: отвечаем короткой просьбой напрямую, ядру
            # передавать нечего (то же, что делает адаптер Telegram).
            await api.otpravit(v.chat_id, _PROSBA_TEKSTOM)
            logger.info("👤 Авито «%s»: вложение без текста в чате %s — попросил текстом",
                        kod, v.chat_id)
            return
        # Объявление кладём ДО обработки: ядро подмешает его в промпт по ключу.
        yadro.zapomnit_obyavlenie(kod, v.chat_id, v.obyavlenie)
        yadro.obrabotat(kod, v.chat_id, v.tekst, _kanal_avito(api, v.chat_id, imya))

    return obrabotchik


# ── Смоук вручную ────────────────────────────────────────────────────────────

def _smoke() -> None:  # pragma: no cover - ручной прогон
    """`python -m bot.channels.avito <код> [chats|send <chat_id> <текст>]`.

    Читающие команды безопасны. `send` — внешнее действие, шлёт живому клиенту,
    поэтому требует явного chat_id и текста; на чужой чат его не наводить.
    """
    import sys

    from ..config import load_config

    if len(sys.argv) < 2:
        print("Использование: python -m bot.channels.avito <код_аккаунта> "
              "[self|chats|send <chat_id> <текст>]")
        raise SystemExit(2)

    kod = sys.argv[1]
    komanda = sys.argv[2] if len(sys.argv) > 2 else "chats"
    cfg = load_config().avito.get(kod)
    if cfg is None or not cfg.zapolnen:
        print(f"У аккаунта «{kod}» нет заполненных кред Авито в .env "
              f"(AVITO_CLIENT_ID_*/AVITO_CLIENT_SECRET_*).")
        raise SystemExit(1)

    async def run() -> None:
        async with AvitoAPI(cfg) as api:
            if komanda == "self":
                print("user_id:", await api.user_id())
            elif komanda == "chats":
                chaty = await api.chaty(tolko_neprochitannye=False, limit=10)
                print(f"чатов: {len(chaty)}")
                for chat in chaty:
                    v = izvlech_vhodyashchee(chat)
                    metka = "ВХОД" if v else "—"
                    lm = (chat.get("last_message") or {}).get("content", {})
                    print(f"  {chat.get('id')}: [{metka}] {str(lm.get('text'))[:60]}")
            elif komanda == "send":
                chat_id, tekst = sys.argv[3], sys.argv[4]
                print("ответ API:", await api.otpravit(chat_id, tekst))
            else:
                print("неизвестная команда:", komanda)

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    _smoke()
