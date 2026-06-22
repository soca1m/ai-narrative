"""Конфиг провайдеров. Всё через переменные окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderConfig:
    """OpenAI-совместимый эндпоинт (OpenRouter, локальный сервер, vLLM...)."""
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.8
    max_tokens: int = 4096


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# Структурные боты (логлайн, синопсис, персонажи, структура, диалоги, перевод)
# → Claude через OpenRouter. БЕЗ Opus: дефолт Sonnet 4.6 ($3/$15 vs $15/$75).
def structural_provider() -> ProviderConfig:
    return ProviderConfig(
        base_url=_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=_env("OPENROUTER_API_KEY"),
        model=_env("CLAUDE_MODEL", "anthropic/claude-sonnet-4.6"),
        temperature=float(_env("STRUCTURAL_TEMPERATURE", "0.7")),
        # 0 = без лимита. Главы пишутся на ~3500 слов итеративно — жёсткий cap
        # 2048 токенов (~1500 слов) резал главу на «обрывки». Короткие боты
        # (логлайн/синопсис) и так не упираются в потолок.
        max_tokens=int(_env("STRUCTURAL_MAX_TOKENS", "0")),
    )


# Редактор (Бот 7) — самый частый вызов + ретраи. Дешёвая модель: Haiku 4.5
# ($1/$5). Проверка дешевле генерации, качества хватает.
def editor_provider() -> ProviderConfig:
    return ProviderConfig(
        base_url=_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=_env("OPENROUTER_API_KEY"),
        model=_env("EDITOR_MODEL", "anthropic/claude-haiku-4.5"),
        temperature=0.2,
        max_tokens=int(_env("EDITOR_MAX_TOKENS", "4000")),
    )


# Адалт-сцены (Бот 6) → отдельная модель без цензуры.
# Дефолт — Magnum v4 72B на OpenRouter (подтверждён живым).
# Можно подменить на локальный сервер: ADULT_BASE_URL=http://localhost:8000/v1
def adult_provider() -> ProviderConfig:
    return ProviderConfig(
        base_url=_env("ADULT_BASE_URL", _env("OPENROUTER_BASE_URL",
                      "https://openrouter.ai/api/v1")),
        api_key=_env("ADULT_API_KEY", _env("OPENROUTER_API_KEY")),
        model=_env("ADULT_MODEL", "anthracite-org/magnum-v4-72b"),
        temperature=float(_env("ADULT_TEMPERATURE", "0.9")),
        max_tokens=int(_env("ADULT_MAX_TOKENS", "3072")),
    )


# Сколько раз бот пытается исправить замечание редактора до эскалации человеку.
MAX_REVISIONS: int = int(_env("MAX_REVISIONS", "4"))

# Объём главы (слова): дефолт ~3500 (6 глав ≈ 20к слов). Можно переопределить
# на каждую главу в UI. Главы пишутся ИТЕРАТИВНО — несколько подходов с
# «продолжи с места обрыва», пока не наберём объём (борьба с обрывками).
CHAPTER_WORDS_DEFAULT: int = int(_env("CHAPTER_WORDS_DEFAULT", "3600"))
CHAPTER_MAX_PASSES: int = int(_env("CHAPTER_MAX_PASSES", "5"))
