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

    async def _post_files(self, put: str, files: dict) -> dict:
        """POST multipart/form-data (для загрузки картинки). `files` — как у httpx:
        {поле: (имя, байты, content_type)}."""
        assert self._client is not None
        otvet = await self._client.post(put, files=files, headers=await self._zagolovki())
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

    async def zagruzit_kartinku(self, dannye: bytes, *, imya: str = "image.jpg",
                                tip: str = "image/jpeg") -> str:
        """Загрузить картинку в Авито → `image_id` (14.9, исходящие вложения).

        Отправка картинки в Авито двухшаговая: сперва upload, потом
        `otpravit_kartinku` по полученному id. Поле формы — `uploadfile[]`; ответ
        `{"<image_id>": {размеры…}}`, где id — единственный ключ верхнего уровня."""
        uid = await self.user_id()
        otvet = await self._post_files(
            f"/messenger/v1/accounts/{uid}/uploadImages",
            {"uploadfile[]": (imya, dannye, tip)})
        if not isinstance(otvet, dict) or not otvet:
            raise OshibkaAvito(f"uploadImages вернул пусто/не словарь: {otvet!r}")
        return next(iter(otvet))

    async def otpravit_kartinku(self, chat_id: str, image_id: str) -> dict:
        """Отправить ранее загруженную картинку в чат. ⚠️ Внешнее действие —
        доходит до живого клиента."""
        uid = await self.user_id()
        return await self._post(
            f"/messenger/v1/accounts/{uid}/chats/{chat_id}/messages/image",
            {"image_id": image_id})

    async def otmetit_prochitannym(self, chat_id: str) -> dict:
        """Пометить чат прочитанным. На поллинге НЕ обязателен и по умолчанию не
        зовётся: отметка видна клиенту и может пересечься с Jivo."""
        uid = await self.user_id()
        return await self._post(f"/messenger/v1/accounts/{uid}/chats/{chat_id}/read", {})

    async def soobshcheniya(self, chat_id: str, *, limit: int = 20) -> list[dict]:
        """Последние сообщения чата — для детекции перехвата оператором (14.8).

        Нужен лишь факт «последнее исходящее отправил не бот», поэтому берём
        небольшое окно. Авито отдаёт список сообщений (в разных версиях — голым
        массивом или под ключом `messages`), нормализуем к списку."""
        uid = await self.user_id()
        dannye = await self._get(
            f"/messenger/v3/accounts/{uid}/chats/{chat_id}/messages/", {"limit": limit})
        if isinstance(dannye, dict):
            return dannye.get("messages", []) or []
        return dannye or []


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
    vlozhenie: dict | None = None     # {tip, url, imya, razmer} — фото/файл клиента (14.9)


def _krupneyshaya_kartinka(sizes: dict) -> str | None:
    """URL самой крупной версии картинки из карты Авито `{"1280x960": url, …}`.

    Ключи — «ШИРИНАxВЫСОТА»; берём максимальную площадь. Нераспознанный ключ
    (или пустая карта) не роняет — просто пропускаем."""
    luchshaya: tuple[int, str] | None = None
    for razmer, url in (sizes or {}).items():
        if not url:
            continue
        try:
            sh, vys = str(razmer).lower().split("x")
            ploshchad = int(sh) * int(vys)
        except (ValueError, AttributeError):
            ploshchad = 0
        if luchshaya is None or ploshchad > luchshaya[0]:
            luchshaya = (ploshchad, url)
    return luchshaya[1] if luchshaya else None


def _izvlech_vlozhenie(lm: dict) -> dict | None:
    """Вытащить вложение из `last_message`: `{tip, url, imya, razmer}` или None.

    Полностью зеркалим (есть публичный URL) пока только `image` — клиент шлёт
    фото товара/помещения, а его CDN-URL Авито отдаёт прямо в `content`. Прочие
    вложения (voice/video/file) требуют доп. запроса за файлом — их тип помечаем
    (`url=None`), чтобы лог и обработчик знали, ЧТО пришло, но в amoCRM пока не
    тянем. `tip` — уже в терминах amojo (`picture` для картинки)."""
    tip = lm.get("type")
    if not tip or tip == "text":
        return None
    content = lm.get("content") or {}
    if tip == "image":
        url = _krupneyshaya_kartinka((content.get("image") or {}).get("sizes") or {})
        if url:
            return {"tip": "picture", "url": url, "imya": "photo.jpg", "razmer": None}
    return {"tip": tip, "url": None, "imya": None, "razmer": None}


def _obyavlenie_chata(chat: dict) -> dict | None:
    """Объявление, под которым идёт чат: `context.value` при `type=="item"`.

    Живёт в объекте ЧАТА, а не в сообщении — поэтому при разборе из списка
    сообщений его берём один раз из чата и прокидываем в каждое входящее."""
    kontekst = chat.get("context") or {}
    return kontekst.get("value") if kontekst.get("type") == "item" else None


def _vhodyashchee_iz_soobshcheniya(chat_id: str, msg: dict,
                                   obyavlenie: dict | None) -> Vhodyashchee | None:
    """Разобрать ОДНО сообщение (из `last_message` или списка сообщений) во
    `Vhodyashchee`. Исходящие (`direction != "in"`) и без id — отбрасываем."""
    if msg.get("direction") != "in":
        return None
    msg_id = msg.get("id")
    if not msg_id:
        return None
    content = msg.get("content") or {}
    return Vhodyashchee(
        chat_id=chat_id,
        msg_id=str(msg_id),
        author_id=msg.get("author_id"),
        tekst=content.get("text"),
        obyavlenie=obyavlenie,
        vlozhenie=_izvlech_vlozhenie(msg),
    )


def izvlech_vhodyashchee(chat: dict) -> Vhodyashchee | None:
    """Достать последнее входящее из объекта чата (`last_message`).

    Фолбэк, когда список сообщений недоступен. Основной путь поллинга — через
    `_sobrat_vhodyashchie`, который берёт ВСЕ входящие чата (иначе залп сообщений
    между тиками схлопывался бы к одному `last_message`, узел 14.10).
    """
    return _vhodyashchee_iz_soobshcheniya(
        str(chat.get("id")), chat.get("last_message") or {}, _obyavlenie_chata(chat))


async def _sobrat_vhodyashchie(api: "AvitoAPI", chat: dict) -> list[Vhodyashchee]:
    """Все входящие чата в хронологическом порядке (14.10).

    Тянем список сообщений (`AvitoAPI.soobshcheniya`) и берём каждое входящее, а не
    только `last_message`: клиент может прислать несколько сообщений (напр. группу
    фото) за один интервал поллинга, и все, кроме последнего, иначе теряются.
    Объявление берём из объекта чата (в самих сообщениях его нет). Сбой запроса за
    списком → фолбэк на `last_message`, чтобы не потерять хотя бы последнее."""
    obyavlenie = _obyavlenie_chata(chat)
    chat_id = str(chat.get("id"))
    try:
        msgs = await api.soobshcheniya(chat_id)
    except Exception as e:  # noqa: BLE001 — не смогли список → хотя бы last_message
        log_oshibka(f"Поллинг Авито: список сообщений чата {chat_id}: {e}")
        v = izvlech_vhodyashchee(chat)
        return [v] if v is not None else []
    # Порядок выдачи Авито не гарантирован; сортируем по времени, старые первыми.
    # `created` — секунды, у залпа фото совпадает: reverse перед устойчивой
    # сортировкой даёт для равных времён порядок, обратный выдаче (обычно
    # свежие-первыми → станет старые-первыми).
    poryadok = sorted((m for m in reversed(msgs) if isinstance(m, dict)),
                      key=lambda m: m.get("created") or 0)
    vhod = [v for m in poryadok
            if (v := _vhodyashchee_iz_soobshcheniya(chat_id, m, obyavlenie)) is not None]
    if vhod:
        return vhod
    v = izvlech_vhodyashchee(chat)          # список без входящих — пробуем last_message
    return [v] if v is not None else []


def posledny_ishodyashchiy(soobshcheniya: list[dict]) -> dict | None:
    """Самое свежее ИСХОДЯЩЕЕ сообщение чата (`direction == "out"`) или None.

    По нему детектор перехвата (14.8) решает, отвечал ли последним бот или живой
    менеджер. Порядок выдачи Авито не гарантирован — сортируем по времени, свежее
    последним; `created` — unix-время сообщения."""
    ishod = [m for m in soobshcheniya if isinstance(m, dict) and m.get("direction") == "out"]
    if not ishod:
        return None
    ishod.sort(key=lambda m: m.get("created") or 0)
    return ishod[-1]


_VIDENNYH_NA_CHAT = 200      # сколько последних id держим на чат (защита от роста)


class Vidennye:
    """Дедуп: НАБОР обработанных msg_id в каждом чате.

    Раньше хранили один «последний id», но при залпе сообщений (клиент шлёт группу
    фото) за тик приходит несколько входящих, и один id их не различал бы — второе
    сообщение с тем же чатом ложно считалось бы «уже виденным» или, наоборот,
    затирало бы первое. Теперь помним набор id на чат (с потолком). Держится в
    памяти процесса — этого хватает, чтобы повторный поллинг не отвечал дважды.
    """

    def __init__(self, potolok: int = _VIDENNYH_NA_CHAT) -> None:
        self._potolok = potolok
        self._nabory: dict[str, set[str]] = {}
        self._poryadok: dict[str, list[str]] = {}

    def novoe(self, v: Vhodyashchee) -> bool:
        return v.msg_id not in self._nabory.get(v.chat_id, ())

    def otmetit(self, v: Vhodyashchee) -> None:
        nabor = self._nabory.setdefault(v.chat_id, set())
        if v.msg_id in nabor:
            return
        nabor.add(v.msg_id)
        poryadok = self._poryadok.setdefault(v.chat_id, [])
        poryadok.append(v.msg_id)
        if len(poryadok) > self._potolok:
            nabor.discard(poryadok.pop(0))


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
                for v in await _sobrat_vhodyashchie(api, chat):
                    if not vid.novoe(v):
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

def _kanal_avito(api: AvitoAPI, chat_id: str, imya: str, *,
                 zerkalo=None, avtor_id=None, zhurnal=None,
                 operatory=None, kod: str | None = None) -> Kanal:
    """Колбэки транспорта Авито для диспетчера. `pechataet=None` — у Авито
    индикатора набора нет, задержки очеловечивания работают молча.

    Если задано `zerkalo` (14.3), каждая отправленная реплика дублируется
    исходящим в amoCRM — так менеджер видит и ответы бота, а не только клиента.
    `zhurnal` (14.7) — та же реплика пишется в нашу БД для панели-виджета.
    `operatory` (14.8) — id отправленной реплики запоминается, чтобы отличить
    ответ бота от ответа живого менеджера при детекции перехвата.
    """
    async def otpravit(tekst: str) -> None:
        rezultat = await api.otpravit(chat_id, tekst)
        if operatory is not None and kod is not None:
            await operatory.zapomnit_otpravlennoe(
                kod, chat_id, (rezultat or {}).get("id"))
        if zerkalo is not None:
            await zerkalo.ishodyashchee(chat_id, tekst, avtor_id=avtor_id)
        if zhurnal is not None:
            await zhurnal.ishodyashchee(chat_id, tekst)

    return Kanal(otpravit=otpravit, pechataet=None, imya=imya)


# Реплика на нечитаемое вложение. НЕ говорим «не могу прочитать вложение» —
# это звучит по-роботски и выдаёт бота. Живой продавец в такой ситуации сослался бы
# на связь и попросил описать словами (решение заказчика 15.08).
_PROSBA_TEKSTOM = ("Ой, у меня сейчас интернет слабый, фото никак не подгрузится. "
                   "Опишите, пожалуйста, словами, что на нём, и я помогу.")


def _marker_vlozheniya(vlozhenie: dict | None) -> str:
    """Короткая пометка вложения для журнала-панели (менеджер видит, ЧТО пришло)."""
    tip = (vlozhenie or {}).get("tip")
    if tip == "picture":
        return "📷 фото"
    if tip:
        return f"📎 вложение ({tip})"
    return "📎 вложение"


async def zapustit(kod: str, cfg: AvitoConfig, yadro, stop: asyncio.Event, *,
                   belyy_spisok: frozenset[str] | None = None, zerkalo=None,
                   zhurnal=None, operatory=None) -> None:
    """Поллер в режиме ответа: входящее уходит в ядро, ответ шлётся в Авито.

    `belyy_spisok` — множество `chat_id`, которым РАЗРЕШЕНО отвечать. `None`
    означало бы «отвечать всем», но на живом аккаунте это столкнётся с Jivo
    (см. модульный докстринг), поэтому боевой режим включается отдельно (14.5),
    а на 14.2 список всегда задан и узок (тестовый чат).

    `zerkalo` (14.3) — если задано, диалог дублируется в amoCRM (обе стороны).
    `zhurnal` (14.7) — если задано, лента (клиент+бот) пишется в нашу БД для панели.
    `operatory` (14.8) — если задано, бот молчит в чатах, перехваченных менеджером.
    """
    async with AvitoAPI(cfg) as api:
        uid = await api.user_id()
        logger.info("📡 Авито «%s» (%s): ответы через ядро, белый список: %s%s%s%s",
                    kod, uid, ", ".join(sorted(belyy_spisok)) if belyy_spisok else "ВСЕ",
                    ", зеркало в amoCRM" if zerkalo is not None else "",
                    ", журнал в БД" if zhurnal is not None else "",
                    ", перехват оператором" if operatory is not None else "")
        await cikl_pollinga(
            api, sdelat_obrabotchik(kod, api, yadro, belyy_spisok,
                                    zerkalo=zerkalo, zhurnal=zhurnal,
                                    operatory=operatory), stop)


async def _zerkalit_operatora(zerkalo, operatory, kod: str, chat_id: str,
                              avtor_id, msgs: list[dict]) -> None:
    """Ручные ответы менеджера в Авито → исходящие в amoCRM (14.12).

    Идём по исходящим окна сообщений в хронологии: реплики самого бота уже ушли
    в amoCRM при отправке (`_kanal_avito`), их пропускаем по журналу; чужое
    исходящее — ответ живого менеджера прямо в Авито, в карточке его не было.
    Зеркалим и помечаем (`zapomnit_zerkalirovannoe`), чтобы на следующем тике не
    задублить. Текстовые только: операторские вложения без публичного url amojo
    подтянуть не может (как и у клиента, см. `vhodyashchee_vlozhenie`).

    Зовётся ТОЛЬКО когда бот в чате уже отметился (`aktiven`): на холодном старте
    журнал пуст, и всё прошлое исходящее приняли бы за операторское и разом
    залили бы историю в карточку. Дедуп и края (пустой id, сбой кеша) — в
    `Operatory.operator_zerkalen`: там перекос в «не зеркалить», чтобы не дублить."""
    ishodyashchie = sorted(
        (m for m in msgs if isinstance(m, dict) and m.get("direction") == "out"),
        key=lambda m: m.get("created") or 0)
    for m in ishodyashchie:
        mid = m.get("id")
        if await operatory.bot_otpravlyal(kod, chat_id, mid):
            continue
        if await operatory.operator_zerkalen(kod, chat_id, mid):
            continue
        tekst = ((m.get("content") or {}).get("text")) or ""
        if not tekst.strip():
            continue
        await zerkalo.ishodyashchee(chat_id, tekst, avtor_id=avtor_id,
                                    msgid=f"avito-out:{mid}")
        await operatory.zapomnit_zerkalirovannoe(kod, chat_id, mid)
        logger.info("📤 amoCRM ← оператор (чат %s): зеркалирован ручной ответ менеджера",
                    chat_id)


async def _operator_perehvatil(operatory, kod: str, api: AvitoAPI,
                               chat_id: str, *, msgs: list[dict] | None = None) -> bool:
    """Ведёт ли чат живой менеджер — бот должен молчать (14.8).

    Флаг уже стоит → молчим. Иначе смотрим последнее исходящее в чате: если его
    отправил не бот (нет в журнале реплик) — менеджер перехватил диалог, ставим
    флаг. Сбой запроса за сообщениями не должен глушить бота: не удалось
    проверить — считаем, что перехвата нет (флаг ставится только по факту).
    `msgs` — уже загруженное окно сообщений (обработчик берёт его один раз и на
    зеркалирование оператора, и сюда); None → грузим сами.

    ⚠️ Холодный старт: если журнал реплик бота в чате пуст (`aktiven` = False),
    последнее исходящее могло быть репликой самого бота ДО включения 14.8 — тогда
    глушить нельзя. Базлайним это исходящее как «виденное» и молчим только на
    СЛЕДУЮЩЕЕ чужое. Иначе на деплое бот замолкал бы во всех живых чатах разом
    (поймано на первом же прогоне 10.08)."""
    if await operatory.vedet(kod, chat_id):
        # Контакт клиента продлевает перехват: отсчёт 3 суток до авто-возврата
        # бота идёт от ПОСЛЕДНЕГО сообщения, а не от момента перехвата.
        await operatory.prodlit(kod, chat_id)
        logger.info("🙋 Авито «%s»: чат %s ведёт оператор — бот молчит", kod, chat_id)
        return True
    try:
        if msgs is None:
            msgs = await api.soobshcheniya(chat_id)
        posl = posledny_ishodyashchiy(msgs)
    except Exception as e:  # noqa: BLE001 — детекция не роняет обработку
        log_oshibka(f"Оператор: детекция перехвата в чате {chat_id}: {e}")
        return False
    if posl is None:
        return False
    if not await operatory.aktiven(kod, chat_id):
        # Холодный старт: не глушим, а фиксируем текущее исходящее как базовое.
        await operatory.zapomnit_otpravlennoe(kod, chat_id, posl.get("id"))
        logger.info("🙋 Авито «%s»: чат %s — базлайн исходящего (бот тут ещё не "
                    "говорил под 14.8), не глушу", kod, chat_id)
        return False
    if not await operatory.bot_otpravlyal(kod, chat_id, posl.get("id")):
        await operatory.vzyal(kod, chat_id)
        logger.info("🙋 Авито «%s»: в чате %s ответил живой менеджер — бот замолкает "
                    "до ручного возврата из панели", kod, chat_id)
        return True
    return False


def sdelat_obrabotchik(kod: str, api: AvitoAPI, yadro,
                       belyy_spisok: frozenset[str] | None, *, zerkalo=None,
                       zhurnal=None, operatory=None):
    """Обработчик входящего в режиме ответа: белый список → вложение → перехват → ядро.

    Вынесен из `zapustit`, чтобы фильтр белого списка, передачу объявления,
    зеркало в amoCRM, журнал в БД и перехват оператором можно было проверить без
    сети (на фейках).

    Порядок для текста: сперва зеркалим входящее в amoCRM (менеджер видит вопрос
    клиента даже если бот замолчит) и пишем в журнал, потом проверяем перехват
    оператором и только затем отдаём ядру; исходящие бота зеркалит и журналит
    обёртка `_kanal_avito`. Вложение (текста нет, 14.9) зеркалим в amoCRM как
    media и кладём маркер в журнал (менеджер видит фото); ядру не отдаём —
    прочитать картинку бот не может, поэтому просим клиента написать текстом.
    """
    async def obrabotchik(v: Vhodyashchee) -> None:
        if belyy_spisok is not None and v.chat_id not in belyy_spisok:
            logger.info("🔇 Авито «%s»: чат %s не в белом списке — пропускаю",
                        kod, v.chat_id)
            return
        imya = f"avito:{v.author_id}" if v.author_id else f"avito:{v.chat_id}"
        # Окно сообщений тянем один раз: и на зеркалирование ручных ответов
        # менеджера (14.12), и на детекцию перехвата (14.8) ниже. Ручные ответы
        # зеркалим ДО разбора входящего — менеджер видит в карточке всю переписку,
        # а не только реплики бота и клиента; только когда бот в чате уже
        # отметился (иначе холодный старт залил бы историю, см. `_zerkalit_operatora`).
        msgs: list[dict] | None = None
        if operatory is not None:
            try:
                msgs = await api.soobshcheniya(v.chat_id)
            except Exception as e:  # noqa: BLE001 — не смогли окно: без зеркала оператора
                log_oshibka(f"Оператор: окно сообщений чата {v.chat_id}: {e}")
                msgs = None
            if (zerkalo is not None and msgs is not None
                    and await operatory.aktiven(kod, v.chat_id)):
                await _zerkalit_operatora(zerkalo, operatory, kod, v.chat_id,
                                          v.author_id, msgs)
        if v.tekst is None:
            # Вложение без текста (14.9). Сперва зеркалим его в amoCRM и журнал —
            # менеджер увидит фото клиента даже если бот под оператором молчит;
            # прочитать картинку бот не может, ядру передавать нечего.
            if zerkalo is not None and v.vlozhenie:
                await zerkalo.vhodyashchee_vlozhenie(
                    v.chat_id, v.msg_id, v.author_id, v.vlozhenie, imya=imya)
            if zhurnal is not None:
                await zhurnal.vhodyashchee(
                    v.chat_id, _marker_vlozheniya(v.vlozhenie), imya=imya)
            # Под оператором не перебиваем менеджера дежурной просьбой; вложение
            # клиента — тоже контакт, продлеваем перехват до авто-возврата.
            if operatory is not None and await operatory.vedet(kod, v.chat_id):
                await operatory.prodlit(kod, v.chat_id)
                logger.info("🙋 Авито «%s»: вложение в чате %s, но ведёт оператор — молчу",
                            kod, v.chat_id)
                return
            # Просьбу текстом гоним через ТОТ ЖЕ канал, что и обычные ответы бота:
            # она уходит клиенту, зеркалится в amoCRM (менеджер видит ответ бота, а
            # не только фото) и журналится. Главное — её id пишется в журнал
            # оператора: иначе следующее сообщение клиента детектор примет за ответ
            # живого менеджера и ложно заглушит бота.
            await _kanal_avito(api, v.chat_id, imya, zerkalo=zerkalo,
                               avtor_id=v.author_id, zhurnal=zhurnal,
                               operatory=operatory, kod=kod).otpravit(_PROSBA_TEKSTOM)
            logger.info("👤 Авито «%s»: вложение (%s) без текста в чате %s — "
                        "зеркалю и прошу текстом", kod,
                        (v.vlozhenie or {}).get("tip") or "?", v.chat_id)
            return
        if zerkalo is not None:
            await zerkalo.vhodyashchee(v.chat_id, v.msg_id, v.author_id, v.tekst)
        if zhurnal is not None:
            await zhurnal.vhodyashchee(v.chat_id, v.tekst, imya=imya)
        # Перехват оператором (14.8): менеджер ответил вручную → бот молчит. Вопрос
        # клиента уже зеркалирован и в журнале — менеджер его увидит в панели и amo.
        if operatory is not None and await _operator_perehvatil(
                operatory, kod, api, v.chat_id, msgs=msgs):
            return
        # Объявление кладём ДО обработки: ядро подмешает его в промпт по ключу.
        yadro.zapomnit_obyavlenie(kod, v.chat_id, v.obyavlenie)
        yadro.obrabotat(kod, v.chat_id, v.tekst,
                        _kanal_avito(api, v.chat_id, imya,
                                     zerkalo=zerkalo, avtor_id=v.author_id,
                                     zhurnal=zhurnal, operatory=operatory, kod=kod))

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
