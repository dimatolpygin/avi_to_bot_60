# -*- coding: utf-8 -*-
"""Сиды справочников: `python -m bot.seed`.

Идемпотентно: повторный запуск ничего не дублирует и не затирает правки,
сделанные через панель (этап 13) — существующие строки не трогаем вовсе.
Сюда попадает только то, без чего система не стартует: три аккаунта и
заглушка базового промпта. Прайс приезжает ETL (этап 4), тексты промптов
и база знаний — этапы 8 и 11.
"""
import asyncio

from sqlalchemy import select

from .config import load_config
from .db import podklyuchit, sozdat_fabriku_sessiy, zakryt
from .logger import logger
from .models import Account, PromptBase

# Код аккаунта → (название, вид). Коды совпадают с ключами токенов в .env
# (TG_TOKEN_SAUNAMART и т.д.) и с ключом сессии `sbavito:dialog:{аккаунт}:{chat}`.
AKKAUNTY = [
    ("saunamart", "Saunamart — товары для бани", "goods"),
    ("sbsauna", "SB SAUNA — отделка под ключ (от 500 тыс)", "services"),
    ("sbsauna_deshman", "SB SAUNA Дешман — бюджетная отделка (от 350 тыс)", "services"),
]

# Заглушка: настоящий промпт пишется на этапе 8. Здесь ровно те правила,
# которые уже зафиксированы заказчиком и не поменяются от версии промпта.
PROMPT_ZAGLUSHKA = """Ты продавец компании SB Group. Отвечаешь коротко и по делу, как живой человек в переписке.

Жёсткие правила:
- Цены и наличие называешь только из результатов поиска по прайсу. Не помнишь — ищи, не нашёл — так и скажи.
- Нет позиции в прайсе — отвечай честно, что её нет. Похожее наугад не подставляешь.
- Телефон первым не просишь. Клиент сам оставил контакт или попросил перезвонить — тогда фиксируешь.
- Пишешь без длинного тире, без markdown, без списков столбиком и без эмодзи.
"""


async def zaseyat() -> int:
    """Завести недостающее. Возвращает число новых строк (0 = всё уже было)."""
    cfg = load_config()
    engine = await podklyuchit(cfg)
    Sessiya = sozdat_fabriku_sessiy(engine)
    dobavleno = 0
    try:
        async with Sessiya() as s, s.begin():
            for code, title, kind in AKKAUNTY:
                est = await s.scalar(select(Account.id).where(Account.code == code))
                if est:
                    logger.info("🌱 Аккаунт «%s» уже есть (id %s) — не трогаю", code, est)
                    continue
                s.add(Account(code=code, title=title, kind=kind))
                dobavleno += 1
                logger.info("🌱 Завожу аккаунт «%s» (%s)", code, kind)

            est_prompt = await s.scalar(
                select(PromptBase.id).where(PromptBase.is_active.is_(True)))
            if est_prompt:
                logger.info("🌱 Базовый промпт уже есть (id %s) — не перезаписываю", est_prompt)
            else:
                s.add(PromptBase(version=1, body=PROMPT_ZAGLUSHKA, updated_by="seed"))
                dobavleno += 1
                logger.info("🌱 Завожу заглушку базового промпта")

        logger.info("🌱 Сиды завершены, новых записей: %d", dobavleno)
        return dobavleno
    finally:
        await zakryt(engine)


if __name__ == "__main__":
    asyncio.run(zaseyat())
