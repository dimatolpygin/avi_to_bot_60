# -*- coding: utf-8 -*-
"""amoCRM REST API v4: лид → контакт + сделка + задача менеджеру (этап 14.4).

Клиент сам оставил контакт (`bot/lead.py` уже положил строку в `leads` со
статусом `new`) — здесь строка уезжает в amoCRM: создаётся сделка с контактом
в воронке «Сауна», к ней прикрепляется саммари диалога заметкой и задача
ответственному менеджеру. Ответственный выбирается раздачей 50/50 внутри
аккаунта (Redis-счётчик).

Порядок именно такой — **сначала БД, потом CRM**: падение amoCRM не теряет
контакт. Успех → `status=sent` + id сделки/контакта; сбой → `status=failed` +
текст ошибки, и `otpravit_nedoslannye` повторит позже. Живой диалог отправка
в CRM не роняет: всё обёрнуто, ошибка идёт в лог.

Токен — долгосрочный, живёт в `.env`. Маппинг воронки/статусов/менеджеров —
не секрет, стоит здесь константами (`доступы/amo_keys.md` — первоисточник).
"""
from __future__ import annotations

import time

import httpx
from sqlalchemy import select

from ..cache import PREFIKS
from ..config import AmoRestConfig
from ..logger import log_oshibka, logger
from ..models import Account, Dialog, Lead
from ..profili import istochnik_akkaunta

TAYMAUT_S = 30.0

# ── Маппинг amoCRM (доступы/amo_keys.md) ─────────────────────────────────────
VORONKA_SAUNA = 76279
STATUS_PERVICHNY = 10133215        # есть телефон → первичный контакт
STATUS_NERAZOBRANNOE = 26019208    # нет телефона (у нас лид всегда с телефоном)

# Ответственные по аккаунту для раздачи 50/50. Дешман — те же менеджеры услуг,
# что и SB SAUNA (одна бригада услуг), пока заказчик не задал отдельных.
MENEDZHERY: dict[str, tuple[int, ...]] = {
    "sbsauna": (6783360, 1356618),          # Тейхриб, Гусев
    "sbsauna_deshman": (6783360, 1356618),  # те же (услуги SB SAUNA)
    "saunamart": (2766675, 10916125),       # Кирьянова, Колесникова
}


class OshibkaAmo(Exception):
    """Сбой REST API amoCRM. `status` — HTTP-код, если он был."""

    def __init__(self, soobshchenie: str, status: int | None = None):
        super().__init__(soobshchenie)
        self.status = status


# ── Клиент REST API ──────────────────────────────────────────────────────────

class AmoAPI:
    """Обёртка над amoCRM API v4 с Bearer-токеном.

    Держит свой `httpx.AsyncClient` (или принимает внешний — так тесты
    подсовывают `MockTransport` без сети).
    """

    def __init__(self, cfg: AmoRestConfig, *, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = client
        self._own = client is None

    async def __aenter__(self) -> "AmoAPI":
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.base_url, timeout=TAYMAUT_S,
                headers={"Authorization": f"Bearer {self.cfg.access_token}"})
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()

    async def _post(self, put: str, telo) -> dict:
        assert self._client is not None
        otvet = await self._client.post(put, json=telo)
        if otvet.status_code >= 400:
            raise OshibkaAmo(f"amoCRM {otvet.status_code} на {put}: {otvet.text[:300]}",
                             status=otvet.status_code)
        try:
            return otvet.json()
        except ValueError:
            return {}

    async def _get(self, put: str, params=None):
        assert self._client is not None
        otvet = await self._client.get(put, params=params)
        if otvet.status_code == 204:      # amoCRM отдаёт 204 на «ничего не найдено»
            return {}
        if otvet.status_code >= 400:
            raise OshibkaAmo(f"amoCRM {otvet.status_code} на {put}: {otvet.text[:300]}",
                             status=otvet.status_code)
        try:
            return otvet.json()
        except ValueError:
            return {}

    async def _patch(self, put: str, telo) -> dict:
        assert self._client is not None
        otvet = await self._client.patch(put, json=telo)
        if otvet.status_code >= 400:
            raise OshibkaAmo(f"amoCRM {otvet.status_code} на {put}: {otvet.text[:300]}",
                             status=otvet.status_code)
        try:
            return otvet.json()
        except ValueError:
            return {}

    async def sozdat_sdelku(self, *, imya_sdelki: str, imya_kontakta: str | None,
                            telefon: str | None, status_id: int,
                            responsible_id: int) -> dict:
        """Комплексное создание: контакт + сделка одним запросом. Возвращает
        `{lead_id, contact_id}` первого элемента ответа."""
        kontakt: dict = {"name": imya_kontakta or imya_sdelki}
        if telefon:
            kontakt["custom_fields_values"] = [
                {"field_code": "PHONE", "values": [{"value": telefon}]}]
        telo = [{
            "name": imya_sdelki,
            "pipeline_id": VORONKA_SAUNA,
            "status_id": status_id,
            "responsible_user_id": responsible_id,
            "_embedded": {"contacts": [kontakt]},
        }]
        dannye = await self._post("/api/v4/leads/complex", telo)
        pervyy = dannye[0] if isinstance(dannye, list) and dannye else {}
        return {"lead_id": pervyy.get("id"), "contact_id": pervyy.get("contact_id")}

    async def dobavit_primechanie(self, lead_id: int, tekst: str) -> None:
        """Заметка (саммари диалога) на сделке."""
        await self._post(f"/api/v4/leads/{lead_id}/notes",
                         [{"note_type": "common", "params": {"text": tekst}}])

    async def sozdat_zadachu(self, lead_id: int, responsible_id: int, tekst: str,
                             *, srok_chasov: int = 4) -> None:
        """Задача ответственному по сделке — «связаться с клиентом»."""
        await self._post("/api/v4/tasks", [{
            "text": tekst,
            "complete_till": int(time.time()) + srok_chasov * 3600,
            "entity_id": lead_id,
            "entity_type": "leads",
            "responsible_user_id": responsible_id,
        }])

    async def nayti_sdelku_po_chatu(self, conv_uuid: str) -> dict | None:
        """По amojo-UUID чата → сделка воронки «Сауна» этого чата или None.

        Нужна для передачи диалога менеджеру БЕЗ телефона (14.11): amoCRM сам
        завёл сделку в «Неразобранном» на первое входящее, и её надо двигать, а
        не плодить дубль. Прямого «чат → сделка» amoCRM не даёт, поэтому идём
        цепочкой (проверено живьём 15.08):
          chat → `contacts/chats` → contact_id → `contacts/{id}?with=leads` → сделки.
        Возвращает `{lead_id, status_id, responsible_id}` — приоритет у сделки в
        «Неразобранном» (её и двигаем); если такой нет, отдаём любую сделку «Сауны»
        (уже в работе — тогда только добавим задачу). Ничего не нашли → None (звонящий
        создаст новую сделку). `conv_uuid` = `new_message.conversation_id` из ответа
        amojo, его кладёт в Redis зеркало.
        """
        chats = await self._get("/api/v4/contacts/chats", {"chat_id[]": conv_uuid})
        spisok = (chats.get("_embedded", {}).get("chats", [])
                  if isinstance(chats, dict) else [])
        contact_id = spisok[0].get("contact_id") if spisok else None
        if not contact_id:
            return None
        kont = await self._get(f"/api/v4/contacts/{contact_id}", {"with": "leads"})
        imya = kont.get("name") if isinstance(kont, dict) else None
        lead_ids = [l.get("id") for l in
                    (kont.get("_embedded", {}).get("leads", []) if isinstance(kont, dict) else [])
                    if l.get("id")]
        kandidat: dict | None = None
        for lid in lead_ids:
            lead = await self._get(f"/api/v4/leads/{lid}")
            if not isinstance(lead, dict) or lead.get("pipeline_id") != VORONKA_SAUNA:
                continue
            info = {"lead_id": lid, "status_id": lead.get("status_id"),
                    "responsible_id": lead.get("responsible_user_id") or 0,
                    "imya": imya, "contact_id": contact_id}
            if lead.get("status_id") == STATUS_NERAZOBRANNOE:
                return info                    # идеальная цель — её и двигаем
            kandidat = kandidat or info        # уже в работе — запасной вариант
        return kandidat

    async def dobavit_telefon_kontaktu(self, contact_id: int, telefon: str) -> None:
        """Дописать телефон существующему контакту (PATCH).

        Нужно, когда двигаем сделку чата из «Неразобранного» вместо создания
        дубля (баг заказчика 23.08): контакт этого чата уже есть — его завёл
        amojo на первое входящее, — а номера у него нет, клиент дал его только
        сейчас. Пишем в стандартное поле PHONE (первый номер контакта)."""
        await self._patch(f"/api/v4/contacts/{contact_id}", {
            "custom_fields_values": [
                {"field_code": "PHONE", "values": [{"value": telefon}]}]})

    async def dvinut_sdelku(self, lead_id: int, *, status_id: int | None = None,
                            responsible_id: int | None = None,
                            imya_sdelki: str | None = None) -> None:
        """Сдвинуть сделку по этапу, назначить ответственного и/или переименовать (PATCH).

        Частичное обновление: шлём только заданные поля. Пусто — не ходим в CRM.
        `imya_sdelki` нужен, чтобы у перенесённой из «Неразобранного» авто-сделки
        было понятное имя «Авито · …», а не дефолтное «Сделка #id» (14.11)."""
        telo: dict = {}
        if status_id is not None:
            telo["status_id"] = status_id
        if responsible_id is not None:
            telo["responsible_user_id"] = responsible_id
        if imya_sdelki:
            telo["name"] = imya_sdelki
        if not telo:
            return
        await self._patch(f"/api/v4/leads/{lead_id}", telo)


# ── Раздача 50/50 ────────────────────────────────────────────────────────────

async def vybrat_menedzhera(redis, kod: str) -> int:
    """ID ответственного по кругу внутри аккаунта (Redis-счётчик).

    Два менеджера на аккаунт → чётный/нечётный INCR. Redis лёг → берём первого
    (лид важнее равномерности; при сбое кеша не роняем отправку)."""
    spisok = MENEDZHERY.get(kod) or ()
    if not spisok:
        raise OshibkaAmo(f"Нет менеджеров для аккаунта «{kod}» в MENEDZHERY")
    if len(spisok) == 1 or redis is None:
        return spisok[0]
    try:
        n = await redis.incr(f"{PREFIKS}:amo:rr:{kod}")
    except Exception as e:  # noqa: BLE001 — кеш не должен ронять отправку лида
        log_oshibka(f"Round-robin «{kod}»: Redis недоступен ({e}) — беру первого менеджера")
        return spisok[0]
    return spisok[(int(n) - 1) % len(spisok)]


# ── Оркестрация: строка leads → сделка в amoCRM ──────────────────────────────

# Служебные имена, которые amojo даёт контакту, когда реальное имя клиента не
# пришло. Не тащим их в имя сделки — иначе выходит «Авито · Авито Клиент».
_SLUZHEBNYE_IMENA = {"клиент", "клиент авито", "авито клиент"}


def _imya_sdelki(kod: str, imya: str | None, tema: str | None = None) -> str:
    """Имя сделки с источником аккаунта: «Авито · <источник> · <тема|клиент>».

    Три аккаунта Авито льются в один канал amoCRM — без метки менеджер не поймёт,
    чей лид и по какому направлению отвечать (Saunamart товары ≠ SB SAUNA услуги ≠
    Дешман бюджет). Метку берём из общей карты `profili.istochnik_akkaunta`, чтобы
    имя сделки и бейдж панели не разъехались.

    Хвост имени (запрос заказчика 23.08 — «Авито · Saunamart · Заказ вагонка»):
    короткая ТЕМА разговора, которую даёт модель, если есть; иначе имя клиента;
    иначе «клиент». Тему подрезаем — имя сделки не должно быть простынёй."""
    tem = (tema or "").strip()
    kto = tem or (imya or "").strip()
    if not kto or kto.lower() in _SLUZHEBNYE_IMENA:
        kto = "клиент"
    if len(kto) > 40:
        kto = kto[:40].rsplit(" ", 1)[0].rstrip(" ,;.-") or kto[:40]
    return f"Авито · {istochnik_akkaunta(kod)} · {kto}"


async def _nayti_sdelku_chata_lida(api: "AmoAPI", redis, sessiya, lead: Lead,
                                   kod: str) -> dict | None:
    """Существующая сделка чата этого лида (её завёл amojo с ПЕРЕПИСКОЙ) или None.

    Клиент дал телефон в чате, который amoCRM уже завёл в «Неразобранном» на первое
    входящее — с полной перепиской. Раньше `otpravit_lead` на это создавал ВТОРУЮ
    карточку (пустую, без истории) — жалоба заказчика 23.08: сделка «Первичный
    контакт» без чата, а переписка отдельно в «Неразобранном». Находим ту сделку и
    двигаем её (как 14.11 для случая без телефона), а не плодим дубль.

    Цепочка: chat_key лида → conv-UUID из Redis (его положило зеркало) →
    `nayti_sdelku_po_chatu`. Нет Redis/маппинга/диалога → None (звонящий создаст
    новую сделку — мягкий фолбэк, клиента не теряем)."""
    if redis is None or lead.dialog_id is None:
        return None
    chat_key = (await sessiya.execute(
        select(Dialog.chat_key).where(Dialog.id == lead.dialog_id))).scalar_one_or_none()
    if not chat_key:
        return None
    from .amojo import Zerkalo
    try:
        conv_uuid = await redis.get(Zerkalo.klyuch_conv(kod, chat_key))
    except Exception as e:  # noqa: BLE001 — кеш не роняет отправку лида
        log_oshibka(f"amoCRM: не прочитал conv-UUID лида #{lead.id}: {e}")
        return None
    if not conv_uuid:
        return None
    if isinstance(conv_uuid, bytes):
        conv_uuid = conv_uuid.decode()
    return await api.nayti_sdelku_po_chatu(conv_uuid)


async def otpravit_lead(api: AmoAPI, redis, sessiya, lead: Lead, kod: str,
                        *, tema: str | None = None) -> bool:
    """Отправить лид в amoCRM и обновить строку `leads`.

    Клиент дал телефон. Если у чата УЖЕ есть сделка (её завёл amojo с перепиской) —
    двигаем её и дописываем телефон на существующий контакт, а НЕ создаём дубль без
    истории (баг заказчика 23.08). Не нашли — создаём сделку+контакт как раньше.
    Затем в обоих случаях — заметка (саммари) + задача менеджеру.

    Возвращает True при успехе. Любой сбой → `status=failed` + текст ошибки
    (для повтора), исключение не пробрасывается: отправка лида не роняет процесс.
    """
    try:
        responsible = await vybrat_menedzhera(redis, kod)
        imya_sdelki = _imya_sdelki(kod, lead.name, tema)
        target = await _nayti_sdelku_chata_lida(api, redis, sessiya, lead, kod)
        if target:
            lead_id = target["lead_id"]
            contact_id = target.get("contact_id")
            if target["status_id"] == STATUS_NERAZOBRANNOE:
                await api.dvinut_sdelku(lead_id, status_id=STATUS_PERVICHNY,
                                        responsible_id=responsible, imya_sdelki=imya_sdelki)
            else:
                # Уже в работе — назад не двигаем; уважим текущего ответственного
                # (ему и задача) и лишь дадим сделке осмысленное имя.
                responsible = target["responsible_id"] or responsible
                await api.dvinut_sdelku(lead_id, imya_sdelki=imya_sdelki)
            if lead.phone and contact_id:
                await api.dobavit_telefon_kontaktu(contact_id, lead.phone)
            logger.info("📇 amoCRM: лид #%s → существующая сделка %s чата (аккаунт %s), "
                        "дубль не создан", lead.id, lead_id, kod)
        else:
            status_id = STATUS_PERVICHNY if lead.phone else STATUS_NERAZOBRANNOE
            sozd = await api.sozdat_sdelku(
                imya_sdelki=imya_sdelki, imya_kontakta=lead.name,
                telefon=lead.phone, status_id=status_id, responsible_id=responsible)
            lead_id = sozd["lead_id"]
            if not lead_id:
                raise OshibkaAmo("amoCRM не вернул id сделки")
            contact_id = sozd.get("contact_id")
        if lead.note:
            await api.dobavit_primechanie(lead_id, lead.note)
        zadacha = "Связаться с клиентом с Авито"
        if lead.phone:
            zadacha += f" · тел. {lead.phone}"
        await api.sozdat_zadachu(lead_id, responsible, zadacha)

        lead.amo_lead_id = lead_id
        lead.amo_contact_id = contact_id
        lead.amo_responsible_id = responsible
        lead.status = "sent"
        lead.error = None
        from datetime import datetime, timezone
        lead.sent_at = datetime.now(timezone.utc)
        await sessiya.commit()
        logger.info("📇 amoCRM: сделка %s создана по лиду #%s (аккаунт %s, ответственный %s)",
                    lead_id, lead.id, kod, responsible)
        return True
    except Exception as e:  # noqa: BLE001 — лид уже в БД, помечаем failed для повтора
        log_oshibka(f"amoCRM: лид #{lead.id} не отправлен: {e}")
        try:
            lead.status = "failed"
            lead.error = str(e)[:500]
            await sessiya.commit()
        except Exception as e2:  # noqa: BLE001
            log_oshibka(f"amoCRM: не смог пометить лид #{lead.id} failed: {e2}")
        return False


async def otpravit_lead_po_id(cfg: AmoRestConfig, redis, fabrika_sessiy,
                              lead_id: int, *, tema: str | None = None) -> bool:
    """Загрузить строку лида по id и отправить в amoCRM. Точка входа из ядра.

    `tema` — короткая тема разговора от модели (в имя сделки, Task C). Токен/фабрика/
    лид отсутствуют → тихо выходим (лид остаётся в БД со `new`, подхватит
    `otpravit_nedoslannye`)."""
    if cfg is None or not cfg.zapolnen or fabrika_sessiy is None:
        return False
    try:
        async with fabrika_sessiy() as sessiya:
            lead = (await sessiya.execute(
                select(Lead).where(Lead.id == lead_id))).scalar_one_or_none()
            if lead is None:
                log_oshibka(f"amoCRM: лид #{lead_id} не найден в БД")
                return False
            kod = (await sessiya.execute(
                select(Account.code).where(Account.id == lead.account_id))).scalar_one_or_none()
            if not kod:
                log_oshibka(f"amoCRM: у лида #{lead_id} нет аккаунта")
                return False
            async with AmoAPI(cfg) as api:
                return await otpravit_lead(api, redis, sessiya, lead, kod, tema=tema)
    except Exception as e:  # noqa: BLE001
        log_oshibka(f"amoCRM: отправка лида #{lead_id} упала: {e}")
        return False


async def peredat_dialog_menedzheru(cfg: AmoRestConfig, redis, *, kod: str,
                                    conv_uuid: str | None, vyzhimka: str,
                                    tema: str | None = None) -> bool:
    """Передать ГОРЯЧИЙ диалог менеджеру без телефона (14.11): двигаем сделку
    из «Неразобранного» на «Первичный контакт», назначаем ответственного 50/50
    и ставим задачу. Точка входа из ядра.

    Клиент созрел на разговор (согласился на созвон, «беру», «выезжайте на замер»),
    но номер не оставил — на Авито это норма: менеджер продолжит прямо в чате
    (исходящие уже умеем, 14.9). Раньше такой чат навсегда висел в «Неразобранном»
    ничей — это и чиним.

    Порядок: найти сделку чата по `conv_uuid` (его положило зеркало в Redis) и
    двинуть её. Не нашли (маппинга нет — старый чат, или Redis лёг) → создаём новую
    сделку на «Первичном контакте», чтобы менеджер клиента всё равно получил. Всё
    обёрнуто: передача не роняет диалог, сбой идёт в лог (как у `otpravit_lead`).
    """
    if cfg is None or not cfg.zapolnen:
        return False
    try:
        async with AmoAPI(cfg) as api:
            responsible = await vybrat_menedzhera(redis, kod)
            target = await api.nayti_sdelku_po_chatu(conv_uuid) if conv_uuid else None
            if target:
                lead_id = target["lead_id"]
                if target["status_id"] == STATUS_NERAZOBRANNOE:
                    # Заодно даём понятное имя: авто-сделка «Неразобранного» звалась
                    # «Сделка #id» — по ней не видно, что клиент с Авито.
                    await api.dvinut_sdelku(lead_id, status_id=STATUS_PERVICHNY,
                                            responsible_id=responsible,
                                            imya_sdelki=_imya_sdelki(kod, target.get("imya"), tema))
                    logger.info("📇 amoCRM: сделка %s (аккаунт %s) из «Неразобранного» → "
                                "«Первичный контакт», ответственный %s", lead_id, kod, responsible)
                elif not target["responsible_id"]:
                    await api.dvinut_sdelku(lead_id, responsible_id=responsible)
                    logger.info("📇 amoCRM: сделке %s (аккаунт %s) назначен ответственный %s "
                                "(уже вне «Неразобранного»)", lead_id, kod, responsible)
                else:
                    responsible = target["responsible_id"]   # уважим текущего — ему и задача
                    logger.info("📇 amoCRM: сделка %s (аккаунт %s) уже в работе у %s — "
                                "добавляю задачу", lead_id, kod, responsible)
            else:
                sozd = await api.sozdat_sdelku(
                    imya_sdelki=_imya_sdelki(kod, None, tema), imya_kontakta=None,
                    telefon=None, status_id=STATUS_PERVICHNY, responsible_id=responsible)
                lead_id = sozd["lead_id"]
                if not lead_id:
                    raise OshibkaAmo("amoCRM не вернул id сделки")
                logger.info("📇 amoCRM: у чата нет сделки в «Неразобранном» — создал новую %s "
                            "(аккаунт %s, ответственный %s)", lead_id, kod, responsible)
            if vyzhimka:
                await api.dobavit_primechanie(lead_id, vyzhimka)
            await api.sozdat_zadachu(
                lead_id, responsible,
                "Клиент с Авито готов к разговору — свяжитесь и ответьте в чате Авито")
            return True
    except Exception as e:  # noqa: BLE001 — передача не роняет диалог
        log_oshibka(f"amoCRM: передача диалога менеджеру (аккаунт {kod}, "
                    f"чат {conv_uuid}) не удалась: {e}")
        return False


async def otpravit_nedoslannye(cfg: AmoRestConfig, redis, fabrika_sessiy,
                               *, limit: int = 50) -> int:
    """Повторить отправку лидов со `status in (new, failed)`. Возвращает число
    успешно доотправленных. Зовётся на старте и/или периодически (14.5)."""
    if cfg is None or not cfg.zapolnen or fabrika_sessiy is None:
        return 0
    otpravleno = 0
    try:
        async with fabrika_sessiy() as sessiya:
            lidy = (await sessiya.execute(
                select(Lead).where(Lead.status.in_(("new", "failed")))
                .order_by(Lead.created_at).limit(limit))).scalars().all()
            if not lidy:
                return 0
            logger.info("📇 amoCRM: доотправляю %d лид(ов) со статусом new/failed", len(lidy))
            async with AmoAPI(cfg) as api:
                for lead in lidy:
                    kod = (await sessiya.execute(
                        select(Account.code).where(Account.id == lead.account_id))).scalar_one_or_none()
                    if kod and await otpravit_lead(api, redis, sessiya, lead, kod):
                        otpravleno += 1
    except Exception as e:  # noqa: BLE001
        log_oshibka(f"amoCRM: доотправка лидов упала: {e}")
    return otpravleno


# ── Смоук вручную ────────────────────────────────────────────────────────────

def _smoke() -> None:  # pragma: no cover - ручной прогон, пишет в живой amoCRM
    """`python -m bot.crm.amo <аккаунт> [телефон] [имя]` — создать ТЕСТОВУЮ сделку.

    ⚠️ Пишет в живой amoCRM: появится настоящая сделка с задачей. Для проверки."""
    import asyncio
    import sys

    from ..config import load_config

    kod = sys.argv[1] if len(sys.argv) > 1 else "sbsauna"
    telefon = sys.argv[2] if len(sys.argv) > 2 else "+79001234567"
    imya = sys.argv[3] if len(sys.argv) > 3 else "Тест Авито"
    cfg = load_config().amo_rest
    if cfg is None:
        print("AMO_ACCESS_TOKEN/AMO_BASE не заданы в .env")
        raise SystemExit(1)

    async def run() -> None:
        responsible = MENEDZHERY[kod][0]
        async with AmoAPI(cfg) as api:
            sozd = await api.sozdat_sdelku(
                imya_sdelki=_imya_sdelki(kod, imya), imya_kontakta=imya,
                telefon=telefon, status_id=STATUS_PERVICHNY, responsible_id=responsible)
            print("создана сделка:", sozd)
            if sozd["lead_id"]:
                await api.dobavit_primechanie(sozd["lead_id"], "Саммари: тестовая проверка 14.4")
                await api.sozdat_zadachu(sozd["lead_id"], responsible,
                                         f"Связаться с клиентом · тел. {telefon}")
                print("заметка и задача добавлены")

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    _smoke()
