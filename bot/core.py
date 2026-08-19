# -*- coding: utf-8 -*-
"""Ядро диалога: доменная логика без транспорта (этап 10).

Транспорт (Telegram сейчас, Авито на этапе 14) знает про ядро ровно две вещи:
`obrabotat(...)` — пришло сообщение, и `sbros(...)` — клиент нажал /reset.
Всё остальное — чей это аккаунт, где искать, что помнить, как отвечать
по-человечески — живёт здесь.

Замка `asyncio.Lock` на `(аккаунт, чат)`, который был в плане этапа, **нет
намеренно**: диспетчер этапа 9 держит на диалог ровно одну задачу и новое
сообщение ОТМЕНЯЕТ старую. Замок дал бы обратное поведение — второй вопрос
дождался бы, пока бот договорит первый ответ, хотя клиент уже передумал.

Аккаунты неодинаковы: у Saunamart есть прайс и инструмент поиска, у двух
аккаунтов услуг прайса не существует (там квалификация и цена «от»), поэтому
инструмент им не даётся вовсе — см. `bot/profili.py`.
"""
from __future__ import annotations

import asyncio

from .ai.agent import otvetit, sobrat_prompt
from .chelovek.dispetcher import Dispetcher, Kanal, Pamyat
from .chelovek.razbivka import Tempo
from .config import Config
from .lead import DannyeLida, sohranit_lead
from .logger import logger
from .pamyat import PamyatRedis
from .profili import Profil, profil
from .search.katalog import Katalog, iz_fayla_praysa, zagruzit_iz_bd
from .search.pokrytie import proverit_pokrytie
from .search.search import Poisk
from .znaniya import prompt_iz_bd
from .znaniya_tovar import KEY_PRIVETSTVIE
from .znaniya_tovar import bloki_iz_bd as bloki_tovar_iz_bd


class Yadro:
    """Один экземпляр на процесс: каталоги, память и диспетчеры всех аккаунтов."""

    def __init__(self, cfg: Config, pamyat: Pamyat, *, tempo: Tempo | None = None,
                 redis=None) -> None:
        self.cfg = cfg
        self.pamyat = pamyat
        self.tempo = tempo or Tempo()
        # Сырой Redis для раздачи лидов 50/50 (round-robin, этап 14.4). Может быть
        # None (тесты/без кеша) — тогда отправка берёт первого менеджера.
        self._redis = redis
        # Фоновые задачи (отправка лида в amoCRM): держим ссылки, чтобы их не
        # собрал GC, и снимаем по завершении. Отправка не должна тормозить ответ.
        self._fon: set[asyncio.Task] = set()
        # Транспорт нужен только затем, чтобы лид знал, из какого канала пришёл
        # клиент: на этапе 14 здесь станет «avito», и менеджер в amo увидит разницу.
        self.kanal_transporta = "telegram"
        self._fabrika_sessiy = None
        self._dispetchery: dict[str, Dispetcher] = {}
        self._poiski: dict[str, Poisk] = {}
        self._prompty: dict[str, str] = {}
        self._data_praysa: dict[str, str] = {}
        # Стартовое приветствие товарного аккаунта: дефолт из профиля, но блок
        # `privetstvie` во вкладке «Saunamart (1)» его перекрывает (этап 19 Шаг 2).
        self._privetstviya: dict[str, str] = {}
        # Объявление, под которым клиент пишет (Авито, этап 14): ключ диалога →
        # готовый факт для промпта. Заполняет адаптер Авито перед `obrabotat`,
        # читает замыкание `_otvechat`. У Telegram объявлений нет — словарь пуст.
        self._obyavleniya: dict[str, str] = {}

    # ── Подготовка ───────────────────────────────────────────────────────────

    async def podgotovit(self, kody: list[str], fabrika_sessiy=None) -> None:
        """Загрузить каталоги и собрать промпты — один раз при старте процесса.

        Каталог товарного аккаунта берётся из `products`, а если БД недоступна
        (или фабрика сессий не передана) — прямо из выгрузки прайса. Второй путь
        нужен, чтобы бот поднимался и без Postgres: без каталога он не сможет
        назвать ни одной цены, а это худший из отказов.
        """
        self._fabrika_sessiy = fabrika_sessiy
        for kod in kody:
            prof = profil(kod)
            if not prof.tovarnyy:
                self._prompty[kod] = await self._prompt_uslug(prof, fabrika_sessiy)
                logger.info("🧩 Аккаунт «%s»: услуги, прайса нет, поиск не подключаем", kod)
                continue
            katalog = await self._katalog(kod, fabrika_sessiy)
            poisk = Poisk(katalog)
            self._poiski[kod] = poisk
            # Промпт собирается один раз: из каталога (ассортимент) и блоков знаний
            # из БД (правки заказчика во вкладке «Saunamart (1)», этап 19 Шаг 2).
            # Пустая/недоступная БД → код-фолбэк = прежний монолит.
            self._prompty[kod] = await self._sobrat_tovar(kod, katalog)
            self._data_praysa[kod] = _data_praysa()
            logger.info("🧩 Аккаунт «%s»: каталог %d товаров, поиск подключён",
                        kod, len(katalog.gruppy))
            # Сигнал о словах каталога, не покрытых словарями (этап 16, B3):
            # новое семейство/порода из таблицы иначе молча ломают поиск.
            proverit_pokrytie(katalog, poisk.sl)

    async def _katalog(self, kod: str, fabrika_sessiy) -> Katalog:
        if fabrika_sessiy is not None:
            try:
                async with fabrika_sessiy() as sessiya:
                    return await zagruzit_iz_bd(sessiya, kod)
            except Exception as e:  # noqa: BLE001
                logger.warning("🧩 Каталог «%s» из БД не поднялся (%s) — беру файл прайса",
                               kod, e)
        return iz_fayla_praysa()

    async def _sobrat_tovar(self, kod: str, katalog: Katalog) -> str:
        """Промпт товарного аккаунта: скелет-механика + блоки знаний из БД + каталог.

        Блоки правит заказчик во вкладке «Saunamart (1)» (этап 19 Шаг 2); отсюда
        же берётся стартовое приветствие (`privetstvie`). Пустая или недоступная
        база знаний → чистый код-фолбэк (`sobrat_prompt` без блоков), равный
        монолиту до Шага 2: бот обязан подняться и без засеянной БД. Заодно
        кладёт приветствие в кеш `_privetstviya` (его читает `zapomnit_privetstvie`).
        """
        bloki = None
        if self._fabrika_sessiy is not None:
            try:
                async with self._fabrika_sessiy() as sessiya:
                    bloki = await bloki_tovar_iz_bd(sessiya, kod)
            except Exception as e:  # noqa: BLE001 — БД лежит, бот всё равно поднимается
                logger.warning("🧩 Блоки товарного «%s» из БД не поднялись (%s) — код-фолбэк",
                               kod, e)
        if bloki:
            self._privetstviya[kod] = bloki.get(KEY_PRIVETSTVIE) or profil(kod).privetstvie
            logger.info("🧩 Аккаунт «%s»: промпт собран из базы знаний (блоков в БД: %d)",
                        kod, len(bloki))
            return sobrat_prompt(katalog, bloki)
        self._privetstviya[kod] = profil(kod).privetstvie
        logger.info("🧩 Аккаунт «%s»: база знаний пуста — промпт из код-фолбэка "
                    "(запусти `python -m bot.seed_znaniya`)", kod)
        return sobrat_prompt(katalog)

    async def perezagruzit_katalog(self, kod: str) -> bool:
        """Пересобрать поиск и промпт товарного аккаунта из БД БЕЗ рестарта.

        Зовётся после успешного фонового синка (этап 16, A5): `Yadro` строит
        `_poiski[kod]` и промпт один раз при старте и держит до рестарта, поэтому
        свежий каталог в БД для живого бота бесполезен без этой перезагрузки.

        Подмена **атомарна**: сначала целиком собираем новый `Poisk` и промпт,
        и только потом одной привязкой меняем ссылку в словаре — живой диалог
        видит либо старую версию целиком, либо новую, но не полусобранную.

        Под защитой: пустой или не поднявшийся каталог **не затирает** рабочий.
        Синк в БД мог пройти, а чтение назад — упасть (обрыв соединения) или
        вернуть пусто (гонка, кривой прайс). Оставить бота без каталога хуже,
        чем ответить по чуть устаревшему. Возвращает True, если подмена была.
        """
        prof = profil(kod)
        if not prof.tovarnyy:
            return False   # у аккаунтов услуг каталога нет — перезагружать нечего
        if self._fabrika_sessiy is None:
            logger.warning("🔄 Перезагрузка каталога «%s»: нет фабрики сессий — пропускаю", kod)
            return False
        try:
            async with self._fabrika_sessiy() as sessiya:
                katalog = await zagruzit_iz_bd(sessiya, kod)
        except Exception as e:  # noqa: BLE001 — обрыв БД, откат — не роняем бота
            logger.error("🔄 Перезагрузка каталога «%s» из БД не удалась (%s) — "
                         "оставляю прежний", kod, e)
            return False
        if not katalog.gruppy:
            logger.warning("🔄 Перезагрузка каталога «%s»: БД вернула пустой каталог — "
                           "оставляю прежний", kod)
            return False

        novyy_poisk = Poisk(katalog)
        # Промпт пересобираем из блоков БД (как на старте): свежий каталог даёт
        # ассортимент, блоки — правки заказчика. Пусто/сбой → код-фолбэк.
        novyy_prompt = await self._sobrat_tovar(kod, katalog)
        self._poiski[kod] = novyy_poisk
        self._prompty[kod] = novyy_prompt
        logger.info("🔄 Каталог «%s» перезагружен без рестарта: %d товаров, "
                    "поиск и промпт пересобраны", kod, len(katalog.gruppy))
        # Свежая таблица могла принести новое семейство/породу — сигналим (B3).
        proverit_pokrytie(katalog, novyy_poisk.sl)
        return True

    async def perezagruzit_prompt_uslug(self, kod: str) -> bool:
        """Пересобрать промпт аккаунта услуг из базы знаний БД БЕЗ рестарта.

        Зовётся после успешного фонового синка знаний (этап 19, Шаг 1): `Yadro`
        собирает промпт услуг один раз при старте (`podgotovit` → `_prompty[kod]`)
        и держит до рестарта, поэтому свежие блоки в `knowledge_blocks` для живого
        бота бесполезны без этой перезагрузки.

        Под защитой, как перезагрузка каталога: пустая или не поднявшаяся база
        знаний **не затирает** рабочий промпт. Синк в БД мог пройти, а чтение
        назад — упасть (обрыв) или вернуть пусто (гонка) — тогда оставляем прежний
        промпт: ответить по чуть устаревшему лучше, чем без промпта. В отличие от
        `_prompt_uslug`, на пустоту НЕ откатываемся на код-фолбэк — при живой БД
        это была бы подмена свежих правок кодовой заглушкой. Возвращает True,
        если подмена была.
        """
        prof = profil(kod)
        if prof.tovarnyy:
            return False   # у товарного промпт из каталога — см. perezagruzit_katalog
        if self._fabrika_sessiy is None:
            logger.warning("🔄 Перезагрузка промпта «%s»: нет фабрики сессий — пропускаю", kod)
            return False
        try:
            async with self._fabrika_sessiy() as sessiya:
                novyy = await prompt_iz_bd(sessiya, prof.kod, prof.kompaniya)
        except Exception as e:  # noqa: BLE001 — обрыв БД, откат — не роняем бота
            logger.error("🔄 Перезагрузка промпта «%s» из БД не удалась (%s) — "
                         "оставляю прежний", kod, e)
            return False
        if not novyy:
            logger.warning("🔄 Перезагрузка промпта «%s»: база знаний в БД пуста — "
                           "оставляю прежний", kod)
            return False
        self._prompty[kod] = novyy
        logger.info("🔄 Промпт услуг «%s» перезагружен без рестарта из базы знаний", kod)
        return True

    async def perezagruzit_prompt_tovarnyy(self, kod: str) -> bool:
        """Пересобрать промпт ТОВАРНОГО аккаунта из блоков БД БЕЗ рестарта.

        Зовётся после синка знаний товарного (этап 19 Шаг 2), когда правку блока
        во вкладке «Saunamart (1)» надо применить, не пересобирая поиск и не трогая
        каталог. Каталог берём из живого `Poisk` (`poisk.katalog`) — он не менялся.

        Под защитой, как перезагрузка услуг: пустая или не поднявшаяся база знаний
        **не затирает** рабочий промпт (в отличие от старта, на код-фолбэк тут НЕ
        откатываемся — при живой БД это была бы подмена свежих правок заглушкой).
        Возвращает True, если подмена была.
        """
        prof = profil(kod)
        if not prof.tovarnyy:
            return False   # у услуг свой путь — perezagruzit_prompt_uslug
        poisk = self._poiski.get(kod)
        if poisk is None:
            logger.warning("🔄 Перезагрузка промпта товарного «%s»: поиск не поднят — пропускаю", kod)
            return False
        if self._fabrika_sessiy is None:
            logger.warning("🔄 Перезагрузка промпта товарного «%s»: нет фабрики сессий — пропускаю", kod)
            return False
        try:
            async with self._fabrika_sessiy() as sessiya:
                bloki = await bloki_tovar_iz_bd(sessiya, kod)
        except Exception as e:  # noqa: BLE001 — обрыв БД, откат — не роняем бота
            logger.error("🔄 Перезагрузка промпта товарного «%s» из БД не удалась (%s) — "
                         "оставляю прежний", kod, e)
            return False
        if not bloki:
            logger.warning("🔄 Перезагрузка промпта товарного «%s»: база знаний пуста — "
                           "оставляю прежний", kod)
            return False
        self._prompty[kod] = sobrat_prompt(poisk.katalog, bloki)
        self._privetstviya[kod] = bloki.get(KEY_PRIVETSTVIE) or prof.privetstvie
        logger.info("🔄 Промпт товарного «%s» перезагружен без рестарта из базы знаний", kod)
        return True

    async def _prompt_uslug(self, prof: Profil, fabrika_sessiy) -> str:
        """Промпт аккаунта услуг: из базы знаний в БД, а нет её — код-фолбэк.

        Та же страховка, что у каталога товарного аккаунта: правка блока
        в `knowledge_blocks` меняет ответ (после рестарта, как обновление
        каталога), а пустая или недоступная БД не оставляет бота без промпта.
        """
        if fabrika_sessiy is not None:
            try:
                async with fabrika_sessiy() as sessiya:
                    iz_bd = await prompt_iz_bd(sessiya, prof.kod, prof.kompaniya)
                if iz_bd:
                    logger.info("🧩 Аккаунт «%s»: промпт услуг собран из базы знаний БД", prof.kod)
                    return iz_bd
                logger.warning("🧩 Аккаунт «%s»: база знаний в БД пуста — беру код-фолбэк "
                               "(запусти `python -m bot.seed_znaniya`)", prof.kod)
            except Exception as e:  # noqa: BLE001
                logger.warning("🧩 База знаний «%s» из БД не поднялась (%s) — беру код-фолбэк",
                               prof.kod, e)
        return prof.prompt or ""

    @property
    def fabrika_sessiy(self):
        """Фабрика async-сессий БД (задана в `podgotovit`). Нужна адаптеру Авито
        для журнала диалога (этап 14.7); None, если процесс поднят без Postgres."""
        return self._fabrika_sessiy

    # ── Работа ───────────────────────────────────────────────────────────────

    def dispetcher(self, kod: str) -> Dispetcher:
        """Диспетчер аккаунта. Свой на каждый: у них разные отвечающие."""
        if kod not in self._dispetchery:
            self._dispetchery[kod] = Dispetcher(
                self._otvetchik(profil(kod)), tempo=self.tempo, pamyat=self.pamyat)
        return self._dispetchery[kod]

    def _otvetchik(self, prof: Profil):
        """Замыкание «вопрос + история → текст ответа» под конкретный аккаунт."""
        async def otvechat(vopros: str, istoriya: list[dict], klyuch: str) -> str:
            rezultat = await otvetit(
                self.cfg.openrouter,
                self._poiski.get(prof.kod),           # None у аккаунтов услуг
                istoriya, vopros,
                data_praysa=self._data_praysa.get(prof.kod, ""),
                sistemny=self._sistemny(prof.kod, klyuch),
                peredat_lead=self._peredat_lead(prof.kod, klyuch),
                peredat_dialog=self._peredat_dialog(prof.kod, klyuch),
            )
            return rezultat.otvet
        return otvechat

    def _sistemny(self, kod: str, klyuch: str) -> str | None:
        """Системный промпт аккаунта плюс факт объявления, если он есть.

        Объявление — это отдельный факт под конкретный чат (Авито), а не часть
        промпта аккаунта: у соседнего чата оно другое. Поэтому подмешивается
        здесь, по ключу диалога, а не зашивается в общий промпт.
        """
        baza = self._prompty.get(kod) or None
        fakt = self._obyavleniya.get(klyuch)
        if not fakt:
            return baza
        return f"{baza}\n\n{fakt}" if baza else fakt

    def zapomnit_obyavlenie(self, kod: str, chat: str | int, obyavlenie: dict | None) -> None:
        """Запомнить/забыть объявление, под которым идёт диалог (зовёт адаптер Авито).

        Пусто/None → факт снимается: чат мог отвязаться, и старое объявление
        подмешивать нельзя. Telegram эту функцию не зовёт вовсе.
        """
        klyuch = Dispetcher.klyuch(kod, chat)
        fakt = _fakt_obyavleniya(obyavlenie) if obyavlenie else ""
        if fakt:
            self._obyavleniya[klyuch] = fakt
        else:
            self._obyavleniya.pop(klyuch, None)

    def _peredat_lead(self, kod: str, klyuch: str):
        """Куда уходит контакт, который клиент оставил сам.

        Чат достаём из ключа диалога (`аккаунт:чат`): по нему менеджер найдёт
        переписку. Сначала лид ложится в БД (не теряем контакт), затем в ФОНЕ
        уходит в amoCRM (сделка+задача, этап 14.4) — фон, чтобы ответ клиенту
        не ждал трёх запросов в CRM.
        """
        _, _, chat = klyuch.partition(":")

        async def peredat(telefon: str, imya: str | None, vyzhimka: str) -> None:
            lead_id = await sohranit_lead(self._fabrika_sessiy, DannyeLida(
                kod_akkaunta=kod, chat=chat, kanal=self.kanal_transporta,
                telefon=telefon, imya=imya, vyzhimka=vyzhimka))
            if lead_id and self.cfg.amo_rest is not None:
                from .crm.amo import otpravit_lead_po_id
                self._v_fone(otpravit_lead_po_id(
                    self.cfg.amo_rest, self._redis, self._fabrika_sessiy, lead_id))
        return peredat

    def _peredat_dialog(self, kod: str, klyuch: str):
        """Куда уходит ГОРЯЧИЙ диалог, когда клиент созрел, но телефона не оставил (14.11).

        В отличие от лида, тут нет контакта для записи в `leads` — передаём в amoCRM
        напрямую: двигаем сделку чата из «Неразобранного» на «Первичный контакт»,
        назначаем менеджера 50/50 и ставим задачу. Сделку находим по amojo-UUID чата,
        который зеркало (`Zerkalo`) положило в Redis при первом входящем. В ФОНЕ —
        ответ клиенту не должен ждать похода в CRM.

        Нет amoCRM или Redis (Telegram-обкатка, тесты) → тихо выходим: механики
        передачи там нет, а ронять ответ клиенту нельзя.
        """
        _, _, chat = klyuch.partition(":")

        async def peredat(prichina: str, vyzhimka: str) -> None:
            if self.cfg.amo_rest is None:
                logger.info("🤝 Передача менеджеру пропущена: amoCRM не подключён (%s)", prichina)
                return
            conv_uuid = None
            if self._redis is not None:
                from .crm.amojo import Zerkalo
                try:
                    conv_uuid = await self._redis.get(Zerkalo.klyuch_conv(kod, chat))
                except Exception as e:  # noqa: BLE001 — кеш не роняет передачу
                    logger.warning("🤝 Не прочитал conv-UUID чата %s:%s (%s) — "
                                   "передача создаст новую сделку", kod, chat, e)
            from .crm.amo import peredat_dialog_menedzheru
            self._v_fone(peredat_dialog_menedzheru(
                self.cfg.amo_rest, self._redis, kod=kod,
                conv_uuid=conv_uuid, vyzhimka=vyzhimka))
        return peredat

    def _v_fone(self, coro) -> None:
        """Запустить корутину в фоне, удержав ссылку и сняв её по завершении."""
        t = asyncio.ensure_future(coro)
        self._fon.add(t)
        t.add_done_callback(self._fon.discard)

    def obrabotat(self, kod: str, chat: str | int, tekst: str, kanal: Kanal) -> asyncio.Task:
        """Входящее сообщение клиента. Возвращает задачу ответа (нужна тестам)."""
        return self.dispetcher(kod).prinyat(kod, chat, tekst, kanal)

    async def sbros(self, kod: str, chat: str | int) -> None:
        """`/reset`: забыть диалог ЭТОГО аккаунта, соседние не трогая."""
        await self.dispetcher(kod).sbros(kod, chat)
        logger.info("🧹 История диалога очищена по команде клиента")

    async def zapomnit_privetstvie(self, kod: str, chat: str | int) -> str:
        """Положить стартовое сообщение в историю как первую реплику бота.

        Иначе на первый же вопрос модель представится второй раз: она не видит
        того, что транспорт отправил в обход неё.

        Текст берём из кеша `_privetstviya` (у товарного он из блока `privetstvie`
        вкладки «Saunamart (1)»), фолбэк — стартовое сообщение профиля.
        """
        privet = self._privetstviya.get(kod) or profil(kod).privetstvie
        await self.pamyat.dopisat(Dispetcher.klyuch(kod, chat), "assistant", privet)
        return privet

    async def ostanovit(self) -> None:
        for d in self._dispetchery.values():
            await d.ostanovit()
        # Даём фоновой отправке лидов доработать, пока соединения ещё открыты
        # (лид уже в БД; при обрыве подхватит otpravit_nedoslannye при старте).
        if self._fon:
            await asyncio.gather(*self._fon, return_exceptions=True)


def _fakt_obyavleniya(o: dict) -> str:
    """Короткий факт для промпта из объявления Авито (`context.value`).

    Не пересказ прайса, а подсказка «клиент смотрит вот это». Оговорки — те же,
    что всюду: объёмы по объявлению не считаем, гейт «не наш профиль» остаётся
    выше. Смысл — чтобы на пустую выдачу поиска бот ответил про объявление,
    а не «нет такой позиции» (баг старого бота под объявлением «Доска обрезная»).
    """
    title = (o.get("title") or "").strip()
    if not title:
        return ""
    price = (o.get("price_string") or "").strip()
    hvost = f", цена {price}" if price else ""
    return (
        f"ФАКТ: клиент пишет под объявлением «{title}»{hvost}. Это подсказка о том, "
        f"что он смотрит, а не прайс — объёмы по объявлению не считай, гейт «не наш "
        f"профиль» остаётся выше. Если по прайсу товар не нашёлся, но вопрос про это "
        f"объявление — ответь про него (что это и цена), а не «нет такой позиции»."
    )


def _data_praysa() -> str:
    """Дата актуальности прайса для ответа инструмента — из времени файла выгрузки.

    Справочное поле: клиент иногда спрашивает, насколько цена свежая. Файла нет —
    пусто, и модель просто не упомянет дату (выдумывать ей нечего).
    """
    import os
    from datetime import datetime

    from .etl.import_prays import FAYL_PO_UMOLCHANIYU
    try:
        return datetime.fromtimestamp(os.path.getmtime(FAYL_PO_UMOLCHANIYU)).strftime("%d.%m.%Y")
    except OSError:
        return ""


def sozdat_yadro(cfg: Config, redis_client, *, tempo: Tempo | None = None) -> Yadro:
    return Yadro(cfg, PamyatRedis(redis_client), tempo=tempo, redis=redis_client)
