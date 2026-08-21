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


def zhivye_avito(cfg: Config) -> list[str]:
    """Аккаунты Авито с заполненными кредами. Пустые креды = аккаунт не поднят."""
    return [k for k, a in cfg.avito.items() if a.zapolnen]


def _kanal_avito(kod: str, cfg: Config, yadro: Yadro, stop: asyncio.Event,
                 operatory=None, redis=None):
    """Фабрика запуска поллера Авито по режиму `AVITO_REZHIM`.

    off сюда не доходит (отфильтрован раньше). nablyudenie — только лог,
    spisok — отвечаем чатам из белого списка, vse — отвечаем всем (боевой,
    14.5). Белый список общий на все аккаунты; на 14.2 в нём тестовый чат.
    `operatory` (14.8) — перехват оператором: бот молчит в чате, где ответил
    живой менеджер, пока его не вернут вручную из панели.
    """
    async def zapustit() -> None:
        from .channels import avito
        acc = cfg.avito[kod]
        if cfg.avito_rezhim == "nablyudenie":
            await avito.zapustit_nablyudenie(acc, stop)
            return
        spisok = None if cfg.avito_rezhim == "vse" else frozenset(cfg.avito_belyy_spisok)
        # Журнал ленты диалога в БД (14.7): пишем клиента и бота в `messages` для
        # панели-виджета. Не зависит от amoCRM и включён всегда, когда есть БД.
        from .zhurnal import Zhurnal
        zhurnal = Zhurnal(yadro.fabrika_sessiy, kod, "avito") if yadro.fabrika_sessiy else None
        # Зеркало в amoCRM (14.3): поднимаем только когда включено И есть креды
        # канала. Клиент amojo живёт ровно столько же, сколько поллер аккаунта.
        if cfg.amo_zerkalo == "on" and cfg.amojo is not None:
            from .crm.amojo import AmojoAPI, Zerkalo
            from .profili import profil
            ref = cfg.bot_ref_id(kod)   # amojo_id менеджера аккаунта (14.9-C)
            async with AmojoAPI(cfg.amojo) as amojo:
                z = Zerkalo(amojo, kod, profil(kod).menedzher, bot_ref_id=ref, redis=redis)
                logger.info("🪞 Авито «%s»: диалог зеркалируется в amoCRM%s", kod,
                            " (клиент+бот)" if ref else " (пока только входящие клиента)")
                await avito.zapustit(kod, acc, yadro, stop, belyy_spisok=spisok,
                                     zerkalo=z, zhurnal=zhurnal, operatory=operatory)
            return
        await avito.zapustit(kod, acc, yadro, stop, belyy_spisok=spisok,
                             zhurnal=zhurnal, operatory=operatory)
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

        # Аккаунты Авито (этап 14). off = не поднимаем; иначе поллер по каждому
        # аккаунту с заполненными кредами. Промпты/каталог готовим для ОБЪЕДИНЕНИЯ
        # телеграм- и авито-аккаунтов: у авито-аккаунта без TG-токена иначе не было
        # бы промпта.
        avito_kody = zhivye_avito(cfg) if cfg.avito_rezhim != "off" else []
        if avito_kody:
            logger.info("📡 Авито (%s): %s", cfg.avito_rezhim, ", ".join(avito_kody))

        gotovit = sorted(set(zhivye) | set(avito_kody))
        fabrika_sessiy = db.sozdat_fabriku_sessiy(engine)
        yadro = sozdat_yadro(cfg, redis_client)
        await yadro.podgotovit(gotovit, fabrika_sessiy)

        # Перехват оператором (14.8): общий на процесс, состояние в Redis. Один
        # объект и для поллеров Авито (ставят флаг), и для панели-API (снимает).
        from .operator import Operatory
        operatory = Operatory(redis_client)

        # Обработчик вебхука amoJo (14.9-B): реплика менеджера в карточке →
        # клиенту в Авито + флаг оператора. Нужен, только когда есть публичный
        # приёмник (panel_api_token), креды канала (amojo) и хотя бы один аккаунт
        # Авито, куда слать. Держим по аккаунту отдельный AvitoAPI-«отправлялку»
        # (поллер держит свой внутри задачи; этот — для инициативной отправки вне
        # цикла) и закрываем на остановке.
        amojo_obrabotchik = None
        amo_apis: dict = {}
        if cfg.panel_api_token and cfg.amojo is not None and avito_kody:
            from .channels.avito import AvitoAPI
            from .crm.vebhuk import PriyomAmo
            from .zhurnal import Zhurnal
            for kod in avito_kody:
                api = AvitoAPI(cfg.avito[kod])
                await api.__aenter__()
                amo_apis[kod] = api
            zhurnaly_amo = {kod: Zhurnal(fabrika_sessiy, kod, "avito")
                            for kod in avito_kody} if fabrika_sessiy else {}
            amojo_obrabotchik = PriyomAmo(amo_apis, operatory, zhurnaly=zhurnaly_amo)
            logger.info("📩 Вебхук amoJo активен: реплика менеджера → клиенту в Авито "
                        "(%s) + перехват из amoCRM", ", ".join(avito_kody))

        # Рубильник — ПЕРВЫЙ прогон СИНХРОННО, ДО подъёма поллеров (закалка гонки
        # «рестарт при заглушённом боте»). Рубильник по умолчанию считает
        # незнакомый аккаунт отвечающим, поэтому без этого на старте оставалось
        # окно ~1–3с, пока фоновый синк не проставит «нет»: за него поллер успел бы
        # вытащить висящий бэклог непрочитанных и ответить на него. Применяем
        # состояние листа заранее — заглушённый аккаунт молчит с первой секунды.
        # Сбой чтения листа безопасен: состояние не трогаем (см. модуль синка).
        if cfg.google.vklyuchena and cfg.google.rubilnik and gotovit:
            from . import sinhronizatsiya_rubilnika
            await sinhronizatsiya_rubilnika.sinhronizirovat_rubilnik_odin_raz(
                cfg, gotovit, yadro.ustanovit_rubilnik)

        _ustanovit_signaly(stop)
        tasks = [asyncio.create_task(
            _supervise(kod, _kanal_telegram(kod, cfg.telegram_tokeny[kod], yadro)), name=kod)
            for kod in zhivye]
        tasks += [asyncio.create_task(
            _supervise(f"avito-{kod}", _kanal_avito(kod, cfg, yadro, stop, operatory,
                                                    redis_client)),
            name=f"avito-{kod}")
            for kod in avito_kody]

        # Живой каталог из Google-таблицы (этап 16): фоновый синк в БД раз в
        # ~10 минут. Поднимаем только когда синк включён (задан ключ) и товарный
        # бот реально работает — каталог в память перегружать некому иначе.
        # После записи в БД синк горячо перезагружает каталог в памяти (A5).
        if cfg.google.vklyuchena and cfg.google.katalog and "saunamart" in zhivye:
            from . import sinhronizatsiya

            async def _perezagruzit() -> None:
                await yadro.perezagruzit_katalog("saunamart")

            tasks.append(asyncio.create_task(
                _supervise("синк-каталога",
                           lambda: sinhronizatsiya.cikl_sinhronizatsii(
                               cfg, fabrika_sessiy, stop, posle_zapisi=_perezagruzit)),
                name="синк-каталога"))
        elif cfg.google.vklyuchena and cfg.google.katalog:
            logger.info("🔄 Синк каталога включён, но бот saunamart не поднят — синк не запускаю")

        # Живая база знаний из Google-таблицы (этап 19): фоновый синк вкладок
        # знаний в `knowledge_blocks` раз в ~10 минут + горячая перезагрузка промпта
        # в памяти (аналог синка каталога). Шаг 1 — услуги (SB SAUNA, Дешман),
        # Шаг 2 — товарный (вкладка «Saunamart (1)»). Свой флаг `GOOGLE_ZNANIYA`
        # (по умолчанию off): включается НЕЗАВИСИМО от каталога — правку промптов
        # можно запустить, не активируя переход каталога. Синкаем только поднятые
        # аккаунты, у которых есть вкладка знаний.
        from .profili import profil
        from .etl.google_znaniya import LISTY_ZNANIY
        znaniya_up = [k for k in gotovit if k in LISTY_ZNANIY]
        if cfg.google.vklyuchena and cfg.google.znaniya and znaniya_up:
            from . import sinhronizatsiya_znaniy

            async def _perezagruzit_znaniya(kod: str) -> None:
                # Товарный и услуги перезагружаются по-разному: у товарного промпт
                # держит и каталог, и блоки, у услуг — только блоки.
                if profil(kod).tovarnyy:
                    await yadro.perezagruzit_prompt_tovarnyy(kod)
                else:
                    await yadro.perezagruzit_prompt_uslug(kod)

            tasks.append(asyncio.create_task(
                _supervise("синк-знаний",
                           lambda: sinhronizatsiya_znaniy.cikl_sinhronizatsii_znaniy(
                               cfg, fabrika_sessiy, stop, znaniya_up,
                               posle_zapisi=_perezagruzit_znaniya)),
                name="синк-знаний"))
        elif cfg.google.vklyuchena and cfg.google.znaniya and not znaniya_up:
            logger.info("📚 Синк знаний включён, но аккаунты с вкладкой знаний не подняты — "
                        "синк не запускаю")

        # Рубильник ответов из листа-пульта (запрос заказчика 19.08): НЕЗАВИСИМЫЙ
        # синк — глушит/включает ответы бота per-account с Google-листа «Рубильник».
        # Зеркало входящих в amoCRM при выключенном боте продолжает работать. Нужен
        # только ключ Google (`GOOGLE_CREDS_PUT`) и флаг `GOOGLE_RUBILNIK` (дефолт on):
        # не зависит от активации знаний/каталога. Нет листа → все отвечают (no-op).
        if cfg.google.vklyuchena and cfg.google.rubilnik and gotovit:
            from . import sinhronizatsiya_rubilnika
            tasks.append(asyncio.create_task(
                _supervise("синк-рубильника",
                           lambda: sinhronizatsiya_rubilnika.cikl_sinhronizatsii_rubilnika(
                               cfg, stop, gotovit, yadro.ustanovit_rubilnik)),
                name="синк-рубильника"))

        # Доотправка лидов, зависших с прошлого запуска (этап 14.4): amoCRM лежал —
        # строки в `leads` со status new/failed; при старте пробуем снова. Разовая
        # задача, не под супервизором: отработала — завершилась.
        if cfg.amo_rest is not None:
            from .crm.amo import otpravit_nedoslannye
            tasks.append(asyncio.create_task(
                otpravit_nedoslannye(cfg.amo_rest, redis_client, fabrika_sessiy),
                name="доотправка-лидов"))
            logger.info("📇 amoCRM REST подключён: лиды уходят в сделки, доотправка при старте")

        # HTTP-API панели-виджета (этап 14.7, кирпич 2): отдаёт ленту диалога из
        # Postgres виджету amoCRM. Поднимаем только когда задан токен доступа —
        # иначе API наружу не выставляем. Под супервизором: падение веб-сервера
        # не должно ронять ботов и наоборот.
        if cfg.panel_api_token:
            from .panel import api as panel_api
            # Секрет канала amoJo (14.9-B): тот же aiohttp-сервер принимает вебхук
            # `POST /amojo/{scope_id}` (менеджер написал в карточке). Фаза 1 —
            # диагностика: обработчик не задан, ручка только проверяет подпись и
            # логирует сырое тело, чтобы снять реальную форму вебхука с живого
            # аккаунта до включения действия (отправка клиенту + флаг оператора).
            amojo_secret = cfg.amojo.channel_secret if cfg.amojo is not None else ""
            tasks.append(asyncio.create_task(
                _supervise("панель-API", lambda: panel_api.zapustit(
                    fabrika_sessiy, cfg.panel_api_token, port=cfg.panel_api_port,
                    origin=cfg.panel_cors_origin, stop=stop, operatory=operatory,
                    amojo_secret=amojo_secret, amojo_obrabotchik=amojo_obrabotchik)),
                name="панель-API"))

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
        for api in amo_apis.values():   # «отправлялки» вебхука amoJo (14.9-B)
            try:
                await api.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        await cache.zakryt(redis_client)
        await db.zakryt(engine)
        logger.info("🛑 Остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
