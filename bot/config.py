# -*- coding: utf-8 -*-
"""Конфигурация из .env.

Правило: всё, что уже используется кодом, требуем через `must()` — отсутствие
переменной роняет старт с понятным русским сообщением, а не падает потом
непонятным исключением в середине диалога. Переменные будущих этапов
(токены ботов, ключ OpenRouter, amoCRM) пока необязательные: их станет
требовать код тех этапов, когда заказчик их выдаст.
"""
import os
from dataclasses import dataclass

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
class Config:
    pg: PgConfig
    redis_url: str
    openrouter: OpenRouterConfig
    log_level: str
    # Три Telegram-бота обкатки (этап 10): код аккаунта → токен. Пустой токен →
    # бот не поднимается, соседи работают.
    telegram_tokeny: dict[str, str]

    @property
    def debug_logi(self) -> bool:
        return self.log_level == "debug"


def load_config() -> Config:
    """Собрать конфиг и проверить обязательные переменные. Звать при старте."""
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
        log_level=(os.environ.get("LOG_LEVEL") or "info").strip().lower(),
        telegram_tokeny={
            "saunamart": (os.environ.get("TG_TOKEN_SAUNAMART") or "").strip(),
            "sbsauna": (os.environ.get("TG_TOKEN_SBSAUNA") or "").strip(),
            "sbsauna_deshman": (os.environ.get("TG_TOKEN_DESHMAN") or "").strip(),
        },
    )
