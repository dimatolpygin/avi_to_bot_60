# -*- coding: utf-8 -*-
"""Конфигурация из .env.

Правило: всё, что уже используется кодом, требуем через `must()` — отсутствие
переменной роняет старт с понятным русским сообщением, а не падает потом
непонятным исключением в середине диалога. Переменные будущих этапов
(токены ботов, ключ OpenRouter, amoCRM) пока необязательные: их станет
требовать код тех этапов, когда заказчик их выдаст.
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# .env лежит в корне проекта (на уровень выше пакета bot/).
_KOREN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_KOREN, ".env"))


class OshibkaKonfiga(SystemExit):
    """Старт невозможен: не хватает переменной окружения."""


def must(name: str, *, zachem: str = "") -> str:
    """Обязательная переменная. Пусто → падаем с русским объяснением."""
    v = (os.environ.get(name) or "").strip()
    if not v:
        hvost = f" — {zachem}" if zachem else ""
        raise OshibkaKonfiga(
            f"❌ Не задана обязательная переменная окружения {name}{hvost}.\n"
            f"   Заполни её в .env (образец — .env.example) и запусти снова."
        )
    return v


def _int(name: str, po_umolchaniyu: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return po_umolchaniyu
    try:
        return int(raw)
    except ValueError:
        raise OshibkaKonfiga(
            f"❌ Переменная {name} должна быть числом, а в .env лежит {raw!r}."
        )


@dataclass(frozen=True)
class PgConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    schema: str

    @property
    def dsn(self) -> str:
        """DSN для SQLAlchemy async (драйвер asyncpg)."""
        return (f"postgresql+asyncpg://{self.user}:{self.password}"
                f"@{self.host}:{self.port}/{self.database}")

    def bez_parolya(self) -> str:
        """То же для лога — пароль не светим."""
        return f"postgresql://{self.user}@{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str          # пусто до этапа 8
    model: str
    base_url: str = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class GoogleConfig:
    """Живой источник прайса — Google-таблица заказчика (курс 31.07, этап 16).

    Пустой `creds_put` = синхронизация выключена, и это штатный режим, а не
    ошибка: бот работает из БД/файла, как раньше. Ключ сервисного аккаунта
    лежит вне git — сюда попадает только путь к нему, никогда содержимое.
    """

    creds_put: str        # путь к service-account.json; пусто → синк выключен
    tablica_id: str
    list_name: str
    interval_s: int       # период фоновой синхронизации, сек

    @property
    def vklyuchena(self) -> bool:
        return bool(self.creds_put.strip())


@dataclass(frozen=True)
class AvitoConfig:
    """OAuth-приложение Авито одного аккаунта (этап 14).

    `client_credentials`: бот сам меняет client_id/secret на access_token и
    продлевает его. `user_id` = id аккаунта Авито (тот, что в путях messenger
    API); 0 → определить лениво через `/core/v1/accounts/self`, чтобы не хранить
    его руками. Незаполненные креды = аккаунт на Авито не поднимается (как пустой
    токен Telegram), это штатный выключатель, а не ошибка.
    """

    client_id: str
    client_secret: str
    user_id: int = 0

    @property
    def zapolnen(self) -> bool:
        return bool(self.client_id.strip() and self.client_secret.strip())


@dataclass(frozen=True)
class Config:
    pg: PgConfig
    redis_url: str
    openrouter: OpenRouterConfig
    google: GoogleConfig
    log_level: str
    # Три Telegram-бота обкатки (этап 10): код аккаунта → токен. Пустой токен →
    # бот не поднимается, соседи работают.
    telegram_tokeny: dict[str, str]
    # Боевые аккаунты Авито (этап 14): код аккаунта → OAuth-приложение. Дефолт
    # пустой, чтобы обкаточные конструкторы Config (тесты) не требовали кред.
    avito: dict[str, "AvitoConfig"] = field(default_factory=dict)
    # Режим поллеров Авито: off (не поднимать) | nablyudenie (только лог) |
    # spisok (отвечать чатам из белого списка) | vse (отвечать всем — боевой,
    # 14.5, после развода с Jivo). Дефолт off: без явного включения на живой
    # аккаунт не лезем. `avito_belyy_spisok` — chat_id, которым можно отвечать.
    avito_rezhim: str = "off"
    avito_belyy_spisok: tuple[str, ...] = ()

    @property
    def debug_logi(self) -> bool:
        return self.log_level == "debug"


def load_config() -> Config:
    """Собрать конфиг и проверить обязательные переменные. Звать при старте."""
    # Дефолты id таблицы и листа живут в etl.google_prays (там же читалка) —
    # берём их оттуда, чтобы не задваивать реальный id в двух местах.
    from .etl.google_prays import LIST_PO_UMOLCHANIYU, TABLICA_ID_PO_UMOLCHANIYU

    return Config(
        pg=PgConfig(
            host=os.environ.get("PGHOST", "127.0.0.1"),
            port=_int("PGPORT", 5432),
            user=os.environ.get("PGUSER", "postgres"),
            password=must("PGPASSWORD", zachem="пароль Postgres, контейнер postgres16"),
            database=must("PGDATABASE", zachem="имя БД проекта, обычно sbavito"),
            schema=os.environ.get("PGSCHEMA", "sbavito"),
        ),
        redis_url=must("REDIS_URL", zachem="память диалога и очереди, контейнер redis"),
        openrouter=OpenRouterConfig(
            api_key=(os.environ.get("OPENROUTER_API_KEY") or "").strip(),
            model=os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5"),
        ),
        google=GoogleConfig(
            creds_put=(os.environ.get("GOOGLE_CREDS_PUT") or "").strip(),
            tablica_id=(os.environ.get("GOOGLE_TABLICA_ID") or "").strip() or TABLICA_ID_PO_UMOLCHANIYU,
            list_name=(os.environ.get("GOOGLE_LIST") or "").strip() or LIST_PO_UMOLCHANIYU,
            interval_s=_int("GOOGLE_SYNC_INTERVAL_S", 600),
        ),
        log_level=(os.environ.get("LOG_LEVEL") or "info").strip().lower(),
        telegram_tokeny={
            "saunamart": (os.environ.get("TG_TOKEN_SAUNAMART") or "").strip(),
            "sbsauna": (os.environ.get("TG_TOKEN_SBSAUNA") or "").strip(),
            "sbsauna_deshman": (os.environ.get("TG_TOKEN_DESHMAN") or "").strip(),
        },
        avito={
            "saunamart": _avito("SAUNAMART"),
            "sbsauna": _avito("SBSAUNA"),
            "sbsauna_deshman": _avito("DESHMAN"),
        },
        avito_rezhim=(os.environ.get("AVITO_REZHIM") or "off").strip().lower(),
        avito_belyy_spisok=tuple(
            c.strip() for c in (os.environ.get("AVITO_BELYY_SPISOK") or "").split(",")
            if c.strip()),
    )


def _avito(suffiks: str) -> "AvitoConfig":
    """Собрать креды Авито одного аккаунта из .env по суффиксу кода."""
    return AvitoConfig(
        client_id=(os.environ.get(f"AVITO_CLIENT_ID_{suffiks}") or "").strip(),
        client_secret=(os.environ.get(f"AVITO_CLIENT_SECRET_{suffiks}") or "").strip(),
        user_id=_int(f"AVITO_USER_ID_{suffiks}", 0),
    )
