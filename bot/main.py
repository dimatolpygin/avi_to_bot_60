# -*- coding: utf-8 -*-
"""Точка входа процесса: `python -m bot.main`.

Этап 2 — каркас: читаем конфиг, поднимаем соединения с Postgres и Redis,
держим процесс живым и корректно гасим соединения по сигналу. Каналы
(три Telegram-бота на общем ядре) подключаются на этапе 10 — для них здесь
уже стоит супервизор: падение одного канала логируется и не роняет соседей.
"""
import asyncio
import signal

from . import SBORKA
from .config import Config, load_config
from .logger import logger, log_oshibka
from . import cache, db

_RESTART_PAUZA = 5.0   # сек между падением канала и перезапуском
_PULS_S = 300.0        # раз в 5 минут отмечаемся в логе, что процесс жив

# (код аккаунта, функция запуска канала) — заполняется на этапе 10.
KANALY: list[tuple[str, object]] = []


async def _supervise(name: str, fn, cfg: Config) -> None:
    """Канал под надзором. Штатное завершение → выход, падение → лог и
    перезапуск через паузу, отмена (shutdown) пробрасывается наверх."""
    while True:
        try:
            await fn(cfg)
            logger.info("Канал %s завершил работу", name)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Канал %s упал: %s; перезапуск через %dс",
                         name, e, int(_RESTART_PAUZA), exc_info=True)
            await asyncio.sleep(_RESTART_PAUZA)


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
    tasks: list[asyncio.Task] = []
    stop = asyncio.Event()
    try:
        engine = await db.podklyuchit(cfg)
        redis_client = await cache.podklyuchit(cfg)

        zhivye = [k for k, t in cfg.telegram_tokeny.items() if t]
        if zhivye:
            logger.info("📡 Аккаунты с токеном: %s", ", ".join(zhivye))
        else:
            logger.info("📡 Токенов Telegram в .env нет — каналы не поднимаются "
                        "(ждём три токена от заказчика, этап 10)")

        _ustanovit_signaly(stop)
        tasks = [asyncio.create_task(_supervise(name, fn, cfg), name=name)
                 for name, fn in KANALY]
        tasks.append(asyncio.create_task(_puls(stop), name="пульс"))

        logger.info("✅ Каркас поднят. Проверка логов и сквозного id: "
                    "python -m bot.proverka")
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
        await cache.zakryt(redis_client)
        await db.zakryt(engine)
        logger.info("🛑 Остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
