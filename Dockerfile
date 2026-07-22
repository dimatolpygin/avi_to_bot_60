# Образ приложения. python-slim достаточно: asyncpg/httpx/aiogram ставятся
# из wheel'ов, компилятор не нужен.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# Зависимости отдельным слоем: ставим editable по пустому пакету, чтобы слой
# кешировался, пока не менялся pyproject.toml, а код приезжал следующим слоем
# (и подменялся volume-монтированием в dev без пересборки).
# Ставим и dev-экстра: watchmedo даёт автоперезагрузку, pytest — тесты.
COPY pyproject.toml ./
RUN mkdir -p bot && touch bot/__init__.py \
    && pip install --no-cache-dir -e ".[dev]"

COPY bot ./bot
COPY tests ./tests

# non-root
RUN useradd --create-home app && chown -R app:app /app
USER app

CMD ["python", "-m", "bot.main"]
