"""Тонкая обёртка над OpenAI-совместимым API (OpenRouter / локальный сервер).

Один клиент-класс на оба типа провайдера — отличаются только конфигом.
"""
from __future__ import annotations

import re
import time
from typing import Optional, Type, TypeVar

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel

from . import live
from .config import ProviderConfig

T = TypeVar("T", bound=BaseModel)

_STREAM_EVERY = 48  # эмитим партиал каждые ~N новых символов (плавно, но не спамит)


def _stream_text(stream) -> str:
    """Гоняет stream-ответ, шлёт нарастающий текст в live.feed, возвращает финал."""
    acc: list[str] = []
    sent = 0
    for chunk in stream:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        piece = getattr(delta, "content", None) if delta else None
        if not piece:
            continue
        acc.append(piece)
        total = sum(len(x) for x in acc)
        if total - sent >= _STREAM_EVERY:
            live.feed("".join(acc))
            sent = total
    full = "".join(acc)
    live.feed(full)
    return full


class LLMLimitError(Exception):
    """Провайдер исчерпан: кончились кредиты/лимит/квота (не транзиентно).

    Пайплайн ловит это и спрашивает нарративщика: ждать сброса или
    переключиться на другого провайдера (OpenRouter ↔ подписка Claude).
    """

    def __init__(self, message: str, provider: str = "openrouter",
                 reset_at: Optional[float] = None, kind: str = "limit"):
        super().__init__(message)
        self.provider = provider          # "openrouter" | "subscription"
        self.reset_at = reset_at          # epoch сброса (если известно)
        self.kind = kind                  # "credits" | "rate" | "limit"


# Сколько раз повторяем транзиентные сбои (таймаут/сеть/5xx/rate) до сдачи.
_MAX_TRIES = 4
_BACKOFF_BASE = 1.5


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Выудить Retry-After/reset из заголовков ответа провайдера."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    for key in ("retry-after", "Retry-After",
                "x-ratelimit-reset-requests", "x-ratelimit-reset"):
        val = headers.get(key) if hasattr(headers, "get") else None
        if not val:
            continue
        try:
            secs = float(val)
            return secs if secs < 1e6 else None
        except (TypeError, ValueError):
            continue
    return None


def _ra(exc: Exception) -> Optional[float]:
    """reset_at = now + Retry-After (если провайдер прислал заголовок)."""
    secs = _retry_after_seconds(exc)
    return (time.time() + secs) if secs else None


def _sleep(attempt: int, exc: Exception) -> None:
    """Backoff между ретраями: Retry-After, иначе экспонента с потолком."""
    hinted = _retry_after_seconds(exc)
    delay = hinted if (hinted and hinted <= 30) else _BACKOFF_BASE ** attempt
    time.sleep(min(delay, 30.0))


def _is_exhausted(exc: Exception) -> bool:
    """429/402 с признаком кончившихся кредитов/квоты (не временный rate)."""
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status == 402:  # OpenRouter: нет кредитов
        return True
    return any(s in msg for s in (
        "insufficient", "quota", "credit", "out of", "payment required",
        "exceeded your", "usage limit", "monthly limit",
    ))


class LLM:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self._client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key)

    def _create(self, **kwargs):
        """chat.completions.create с ретраями и классификацией сбоев.

        Транзиент (таймаут/сеть/5xx/временный rate) → повтор с backoff.
        Исчерпание кредитов/квоты → LLMLimitError (пайплайн спросит юзера).
        Стриминг (stream=True) тоже идёт сюда: ретраится только установка
        соединения до первого чанка.
        """
        last: Optional[Exception] = None
        for attempt in range(_MAX_TRIES):
            try:
                return self._client.chat.completions.create(**kwargs)
            except RateLimitError as exc:
                last = exc
                if _is_exhausted(exc):
                    raise LLMLimitError(
                        str(exc)[:200], provider="openrouter",
                        reset_at=_ra(exc), kind="credits") from exc
                _sleep(attempt, exc)  # временный rate → ждём и повторяем
            except (APITimeoutError, APIConnectionError,
                    InternalServerError) as exc:
                last = exc
                _sleep(attempt, exc)
            except Exception as exc:  # noqa: BLE001
                if _is_exhausted(exc):
                    raise LLMLimitError(str(exc)[:200], provider="openrouter",
                                        kind="credits") from exc
                raise
        # ретраи исчерпаны — если это был rate, трактуем как лимит
        if isinstance(last, RateLimitError):
            raise LLMLimitError(str(last)[:200], provider="openrouter",
                                reset_at=_ra(last), kind="rate") from last
        raise last if last else RuntimeError("LLM: неизвестный сбой")

    def complete(self, system: str, user: str,
                 temperature: Optional[float] = None) -> str:
        """Обычная генерация текста (используют все боты кроме редактора).

        Если активен live-sink (узел стримит во фронт) — идём через stream=True
        и шлём нарастающий текст; иначе обычный запрос.
        """
        kwargs = dict(
            model=self.cfg.model,
            temperature=self.cfg.temperature if temperature is None else temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **self._max_tokens_kwarg(),
        )
        if live.active():
            return _stream_text(self._create(stream=True, **kwargs))
        return self._create(**kwargs).choices[0].message.content or ""

    def chat(self, system: str, messages: list[dict],
             temperature: Optional[float] = None) -> str:
        """Многоходовый диалог (чат с ИИ-редактором в UI)."""
        resp = self._create(
            model=self.cfg.model,
            temperature=self.cfg.temperature if temperature is None else temperature,
            messages=[{"role": "system", "content": system}, *messages],
            **self._max_tokens_kwarg(),
        )
        return resp.choices[0].message.content or ""

    def _max_tokens_kwarg(self) -> dict:
        """max_tokens<=0 → не шлём лимит (модель пишет до своего предела).
        Нужно адалту: сцены «уровня C» длинные, жёсткий cap режет кульминацию."""
        return {"max_tokens": self.cfg.max_tokens} if self.cfg.max_tokens > 0 else {}

    def structured(self, system: str, user: str, schema: Type[T],
                   temperature: float = 0.2) -> T:
        """Строгий структурированный вывод (Бот 7 — редактор → Finding'и).

        Защита в 3 слоя (Claude через OpenRouter не enforce-ит json-формат
        нативно и оборачивает ответ в ```-ограждение):
          1. response_format=json_schema со схемой из Pydantic-модели;
          2. _extract_json — снимает markdown-ограждение, если осталось;
          3. model_validate_json — финальная валидация.
        """
        kwargs = dict(
            model=self.cfg.model,
            temperature=temperature,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": _strict_schema(schema),
                },
            },
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **self._max_tokens_kwarg(),
        )
        # стрим JSON: фронт извлекает текст «на лету» (диалоги/структура в полях)
        if live.active():
            raw = _stream_text(self._create(stream=True, **kwargs)) or "{}"
        else:
            raw = self._create(**kwargs).choices[0].message.content or "{}"
        return schema.model_validate_json(_extract_json(raw))


def _strict_schema(schema: Type[BaseModel]) -> dict:
    """JSON Schema из Pydantic, приведённая к strict-форме OpenRouter:
    на каждом объекте additionalProperties=false и required=все ключи."""
    js = schema.model_json_schema()

    def _harden(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                _harden(value)
        elif isinstance(node, list):
            for item in node:
                _harden(item)

    _harden(js)
    return js


# Управляющие символы, ломающие json.loads/model_validate_json (кроме \t\n\r).
# Модель иногда вставляет NUL () и прочее → «Invalid JSON: control char».
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _extract_json(raw: str) -> str:
    """Достаёт JSON, даже если модель обернула его в ```json-ограждение,
    и вычищает управляющие символы (иначе валидация падает на \\u0000 и т.п.)."""
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if fenced:
        return _CTRL_CHARS.sub("", fenced.group(1))
    # иначе берём от первой { до последней }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        return _CTRL_CHARS.sub("", raw[start:end + 1])
    return _CTRL_CHARS.sub("", raw)
