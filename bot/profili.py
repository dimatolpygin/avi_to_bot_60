# -*- coding: utf-8 -*-
"""Профили трёх аккаунтов: кто отвечает, чем и какими словами (этап 10).

Аккаунт — это токен бота плюс данные плюс промпт. Здесь лежит вторая часть:
персона из брифа заказчика, стартовое сообщение и системный промпт.

Товарный аккаунт (Saunamart) и аккаунты услуг устроены по-разному, и это
принципиально:

* у Saunamart есть прайс, поэтому цену называет **инструмент поиска**,
  а промпт собирается вокруг каталога (`bot/ai/agent.py`);
* у SB SAUNA и Дешмана **прайса не существует** — там не таблица, а квалификация
  и цена «от». Инструмент им не дают вовсе: с пустым каталогом он на любой вопрос
  отвечал бы «не найдено», и бот отделки говорил бы клиенту, что такой позиции нет.

Промпт услуг с этапа 11 живёт в базе знаний (`knowledge_blocks`): рантайм читает
его из БД, а `prompt` здесь — **код-фолбэк**, собранный из тех же блоков
(`bot/znaniya.py`) на случай, когда БД недоступна или ещё не засеяна.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import znaniya


@dataclass(frozen=True)
class Profil:
    """Кто отвечает от имени аккаунта и на каких данных."""

    kod: str                 # saunamart | sbsauna | sbsauna_deshman
    kompaniya: str           # как называем фирму в диалоге
    menedzher: str           # имя персоны из брифа
    tovarnyy: bool           # есть прайс → есть инструмент поиска
    privetstvie: str         # ответ на /start, он же первая реплика бота в истории
    prompt: str | None = None   # у товарного собирается из каталога, здесь None


PROFILI: dict[str, Profil] = {
    "saunamart": Profil(
        kod="saunamart",
        kompaniya="Saunamart",
        menedzher="Александра",
        tovarnyy=True,
        # Стартовое сообщение. В брифе стояло «Вас приветствует компания Saunamart…»,
        # но заказчик 28.07 попросил живое «Здравствуйте» вместо канцелярского зачина:
        # задача — чтобы читалось как ответ человека, а не автоответчика.
        privetstvie="Здравствуйте! Меня зовут Александра, Saunamart. Чем могу помочь?",
    ),
    "sbsauna": Profil(
        kod="sbsauna",
        kompaniya="SB SAUNA",
        menedzher="Роман",
        tovarnyy=False,
        privetstvie=("Вас приветствует компания SB SAUNA. Меня зовут Роман, я помогу вам "
                     "с созданием дизайн-проекта и расчётом сметы для отделки вашей парной."),
        # Код-фолбэк промпта: те же блоки, что лягут в БД сидом (bot/seed_znaniya.py).
        prompt=znaniya.prompt_iz_koda("sbsauna", "SB SAUNA"),
    ),
    "sbsauna_deshman": Profil(
        kod="sbsauna_deshman",
        kompaniya="SB SAUNA",
        # Персона для этого аккаунта в брифе не задана: заказчик заполнил два столбца,
        # Saunamart и SB SAUNA. Взят тот же Роман (фирма одна, направление другое);
        # с этапа 11 персона лежит в БД (`account_prompts`) и правится без кода.
        menedzher="Роман",
        tovarnyy=False,
        privetstvie=("Вас приветствует компания SB SAUNA. Меня зовут Роман, помогу подобрать "
                     "отделку парной под ваш бюджет и посчитать смету."),
        prompt=znaniya.prompt_iz_koda("sbsauna_deshman", "SB SAUNA"),
    ),
}


def profil(kod: str) -> Profil:
    """Профиль по коду аккаунта. Код связывает строку в `accounts`, переменную
    токена в `.env` и ключ сессии Redis — расхождение ломает все три места."""
    try:
        return PROFILI[kod]
    except KeyError:
        raise KeyError(f"Неизвестный аккаунт «{kod}». Известны: "
                       f"{', '.join(PROFILI)}") from None
