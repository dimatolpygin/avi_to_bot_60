# -*- coding: utf-8 -*-
"""Точка входа процесса: `python -m bot.main`.

Поднимает соединения, собирает ядро диалога и запускает по одному Telegram-боту
на каждый аккаунт Авито, у которого в `.env` есть токен (этап 10). Каждый канал
живёт под супервизором: падение одного логируется и перезапускается через паузу,
соседи продолжают отвечать — три аккаунта не должны зависеть друг от друга.

Пустой токен — это не ошибка, а способ выключить конкретного бота: аккаунт
просто не поднимается, остальные работают.
"""
import asyncio
import signal

from . import SBORKA
from .config import Config, load_config
from .core import Yadro, sozdat_yadro
from .logger import logger, log_oshibka
from . import cache, db

_RESTART_PAUZA = 5.0   # сек между падением канала и перезапуском
_PULS_S = 300.0        # раз в 5 минут отмечаемся в логе, что процесс жив


async def _supervise(name: str, zapustit) -> None:
    """Канал под надзором. Штатное завершение → выход, падение → лог и
    перезапуск через паузу, отмена (shutdown) пробрасывается наверх."""
    while True:
        try:
            await zapustit()
            logger.info("Канал %s завершил работу", name)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Канал %s упал: %s; перезапуск через %dс",
                         name, e, int(_RESTART_PAUZA), exc_info=True)
            await asyncio.sleep(_RESTART_PAUZA)


def zhivye_akkaunty(cfg: Config) -> tuple[list[str], list[str]]:
    """(с токеном, без токена). Пустой токен — не ошибка, а выключатель бота:
    аккаунт просто не поднимается, соседи работают."""
    zhivye = [k for k, t in cfg.telegram_tokeny.items() if t.strip()]
    vyklyuchennye = [k for k, t in cfg.telegram_tokeny.items() if not t.strip()]
    return zhivye, vyklyuchennye


def _kanal_telegram(kod: str, token: str, yadro: Yadro):
    """Фабрика запуска канала — импорт внутри, чтобы aiogram не тянулся в CLI
    (ETL, замеры, разбор запроса), которым транспорт не нужен."""
    async def zapustit() -> None:
        from .channels import telegram
        await telegram.zapustit(kod, token, yadro)
    return zapustit


async def _puls(stop: asyncio.Event) -> None:
    """Пока каналов нет, процесс всё равно должен жить: под ним проверяется
    автоперезагрузка при правке кода и держатся соединения."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_PULS_S)
        except asyncio.TimeoutError:
            logger.info("⏳ Процесс жив, жду подключения каналов (этап 10)")


def _ustanovit_signaly(stop: asyncio.Event) -> None:
    """SIGINT/SIGTERM → событие остановки. На Windows `add_signal_handler`
    недоступен — там срабатывает KeyboardInterrupt в `__main__`."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda *_: stop.set())
            except (ValueError, OSError):
                pass


async def main() -> None:
    cfg = load_config()
    logger.info("🚀 SBAvito: старт процесса · сборка %s · уровень логов %s",
                SBORKA, cfg.log_level)

    engine = None
    redis_client = None
    yadro: Yadro | None = None
    tasks: list[asyncio.Task] = []
    stop = asyncio.Event()
    try:
        engine = await db.podklyuchit(cfg)
        redis_client = await cache.podklyuchit(cfg)

        zhivye, vyklyuchennye = zhivye_akkaunty(cfg)
        if zhivye:
            logger.info("📡 Аккаунты с токеном: %s", ", ".join(zhivye))
        if vyklyuchennye:
            logger.info("📡 Без токена (боты не поднимаются): %s", ", ".join(vyklyuchennye))
        if not zhivye:
            logger.info("📡 Токенов Telegram в .env нет — каналы не поднимаются")

        fabrika_sessiy = db.sozdat_fabriku_sessiy(engine)
        yadro = sozdat_yadro(cfg, redis_client)
        await yadro.podgotovit(zhivye, fabrika_sessiy)

        _ustanovit_signaly(stop)
        tasks = [asyncio.create_task(
            _supervise(kod, _kanal_telegram(kod, cfg.telegram_tokeny[kod], yadro)), name=kod)
            for kod in zhivye]

        # Живой каталог из Google-таблицы (этап 16): фоновый синк в БД раз в
        # ~10 минут. Поднимаем только когда синк включён (задан ключ) и товарный
        # бот реально работает — каталог в память перегружать некому иначе.
        # После записи в БД синк горячо перезагружает каталог в памяти (A5).
        if cfg.google.vklyuchena and "saunamart" in zhivye:
            from . import sinhronizatsiya

            async def _perezagruzit() -> None:
                await yadro.perezagruzit_katalog("saunamart")

            tasks.append(asyncio.create_task(
                _supervise("синк-каталога",
                           lambda: sinhronizatsiya.cikl_sinhronizatsii(
                               cfg, fabrika_sessiy, stop, posle_zapisi=_perezagruzit)),
                name="синк-каталога"))
        elif cfg.google.vklyuchena:
            logger.info("🔄 Синк каталога включён, но бот saunamart не поднят — синк не запускаю")

        if not tasks:
            tasks.append(asyncio.create_task(_puls(stop), name="пульс"))

        logger.info("✅ Поднято ботов: %d. Логи диалогов идут сюда же.", len(zhivye))
        await stop.wait()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        log_oshibka(f"Процесс упал при старте: {e}")
        raise
    finally:
        logger.info("🛑 Останавливаюсь — закрываю каналы и соединения…")
        stop.set()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if yadro is not None:
            # Гасим недоговорённые диалоги: незаконченный ответ лучше оборвать,
            # чем оставить висеть задачу при закрытых соединениях.
            await yadro.ostanovit()
        await cache.zakryt(redis_client)
        await db.zakryt(engine)
        logger.info("🛑 Остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
