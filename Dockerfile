# Бэкенд: FastAPI-мост к LangGraph-пайплайну.
FROM python:3.12-slim

# git иногда нужен зависимостям; чистим кэш для лёгкого образа.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости отдельным слоем — кэшируются, пока requirements.txt не менялся.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Код пайплайна и сервера.
COPY src/ ./src/
COPY server/ ./server/

# src/narrative импортируется как пакет narrative.
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# Стейт прогонов (SQLite-чекпоинт) — переживает рестарт через том.
VOLUME ["/app/data"]
ENV NARRATIVE_DB=/app/data/narrative_state.db

EXPOSE 8000
# 0.0.0.0 — внутри контейнера (доступ из фронт-контейнера по docker-сети).
# Наружу порт НЕ публикуется (см. docker-compose) — ходит только фронт-прокси.
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
