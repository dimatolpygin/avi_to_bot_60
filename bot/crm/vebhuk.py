# -*- coding: utf-8 -*-
"""Обработка вебхука amoJo: реплика менеджера в карточке → клиенту в Авито (14.9-B).

amoJo шлёт событие `message`, когда живой менеджер пишет в нативном чате карточки.
Мы разбираем его и:
  * находим аккаунт+чат Авито из `conversation.client_id` = «{kod}:{chat_id}»
    (его мы сами кладём при зеркалировании входящих, см. `crm/amojo.Zerkalo`);
  * **петля-предохранитель**: реагируем ТОЛЬКО на реплику живого менеджера.
    У неё `sender` без `client_id` (это CRM-пользователь); у наших же импортов
    (входящее клиента, ответ бота) `sender.client_id` заполнен — их игнорируем,
    иначе эхо вернулось бы клиенту и заглушило бота;
  * ставим флаг `Operatory` — перехват ИЗ amoCRM: бот в этом чате замолкает
    (закрывает остаток критерия 3, которого не даёт детекция на стороне Авито);
  * отправляем текст менеджера клиенту в Авито («написать первым» — вне цикла
    поллинга) и пишем реплику в журнал панели.

Вложения менеджера (media) пока НЕ пересылаем — это отдельный узел «исходящие
вложения»; флаг оператора всё равно ставим (менеджер взял диалог), а в лог кладём
пометку. Отправка id ушедшей реплики в журнал бота (`zapomnit_otpravlennoe`) держит
детекцию 14.8 честной: наш же ретранслированный текст не примут потом за «чужое
исходящее» после ручного возврата бота.
"""
from __future__ import annotations

from ..logger import log_oshibka, logger

_DEDUP_MAX = 500          # сколько id последних обработанных вебхуков помним в памяти


class PriyomAmo:
    """Обработчик события `message` из amoJo. Держит по аккаунту «отправлялку» в
    Авито (`AvitoAPI`) и журнал панели; общие — `Operatory` и дедуп в памяти.

    `apis` — {kod: AvitoAPI}; `zhurnaly` — {kod: Zhurnal} (необязательно).
    Процесс один (панель-API и поллеры в нём же), поэтому дедуп по id держим в
    памяти — этого хватает от повторной доставки того же вебхука."""

    def __init__(self, apis: dict, operatory, *, zhurnaly: dict | None = None):
        self._apis = apis
        self._operatory = operatory
        self._zhurnaly = zhurnaly or {}
        self._vidennye: list[str] = []
        self._vidennye_set: set[str] = set()

    def _novyy(self, msg_id: str) -> bool:
        if not msg_id:
            return True                       # без id не дедупим, но и не блокируем
        if msg_id in self._vidennye_set:
            return False
        self._vidennye.append(msg_id)
        self._vidennye_set.add(msg_id)
        if len(self._vidennye) > _DEDUP_MAX:
            staryy = self._vidennye.pop(0)
            self._vidennye_set.discard(staryy)
        return True

    async def __call__(self, dannye: dict) -> None:
        m = (dannye or {}).get("message") or {}
        conv = (m.get("conversation") or {}).get("client_id") or ""
        sender = m.get("sender") or {}
        inner = m.get("message") or {}
        if ":" not in conv:
            logger.info("📩 amoJo: пропускаю событие без conversation.client_id (%r)", conv)
            return
        # Петля-предохранитель: реагируем только на живого менеджера. Наши импорты
        # (клиент/бот) несут sender.client_id — это эхо, его нельзя слать обратно.
        if sender.get("client_id"):
            logger.info("📩 amoJo: событие с sender.client_id=%r — наш импорт, игнор",
                        sender.get("client_id"))
            return
        kod, chat_id = conv.split(":", 1)
        api = self._apis.get(kod)
        if api is None:
            logger.info("📩 amoJo: нет отправлялки Авито для аккаунта «%s» — пропуск", kod)
            return
        msg_id = inner.get("id")
        if not self._novyy(str(msg_id or "")):
            return
        # Перехват из amoCRM: менеджер взял диалог → бот молчит (до ручного возврата).
        await self._operatory.vzyal(kod, chat_id)
        imya = sender.get("name") or "менеджер"
        tip = inner.get("type") or "text"
        tekst = (inner.get("text") or "").strip()
        if tip != "text" or not tekst:
            logger.info("📩 amoJo→Авито: менеджер %s прислал «%s» в чат %s:%s — флаг "
                        "оператора поставлен, вложения пока не пересылаем",
                        imya, tip, kod, chat_id)
            return
        try:
            rezultat = await api.otpravit(chat_id, tekst)
        except Exception as e:  # noqa: BLE001 — сбой отправки не должен сорвать вебхук
            log_oshibka(f"amoJo→Авито: не отправил реплику менеджера в {kod}:{chat_id}: {e}")
            return
        await self._operatory.zapomnit_otpravlennoe(
            kod, chat_id, (rezultat or {}).get("id"))
        zh = self._zhurnaly.get(kod)
        if zh is not None:
            await zh.ishodyashchee(chat_id, tekst)
        logger.info("📩 amoJo→Авито: менеджер %s → клиенту в чат %s:%s (перехват): %s",
                    imya, kod, chat_id, tekst[:80])
