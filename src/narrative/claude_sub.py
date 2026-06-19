"""Claude через ПОДПИСКУ Pro/Max (Agent SDK / OAuth Claude Code), без API-ключа.

#4: использует логин Claude Code (~/.claude). Токены подписки, не платные.
Интерфейс совместим с narrative.llm.LLM (complete/structured/chat) — можно
подставлять вместо structural/editor провайдеров. Адалт сюда НЕ роутим
(цензура Claude), он остаётся на uncensored-модели.

Включение: USE_CLAUDE_SUBSCRIPTION=1 (+ CLAUDE_SUB_MODEL=sonnet|opus|haiku),
либо выбор «Claude подписка …» в дропдауне модели глав.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from .llm import LLMLimitError, _extract_json

T = TypeVar("T", bound=BaseModel)

# Дефолтная модель подписки. Слаги SDK: sonnet / opus / haiku.
DEFAULT_SUB_MODEL = "sonnet"

# Ретраи транзиентных сбоев SDK (CLI не запущен/соединение/таймаут).
_SUB_MAX_TRIES = 3
_SUB_BACKOFF = 1.6

# Признаки исчерпания лимита подписки в тексте ошибки/ответа SDK.
# Строковый детект — ФОЛБЭК; основной путь — типизированный RateLimitEvent.
_LIMIT_MARKERS = (
    "usage limit", "rate limit", "limit reached", "limit will reset",
    "limit resets", "quota", "out of", "too many requests", "429",
    "rate_limit", "ratelimit", "resource_exhausted",
    "лимит", "превышен", "исчерпан",
)

# Последнее состояние лимита подписки (из RateLimitEvent CLI) — для проактивного
# показа «сколько осталось / когда сброс» в UI. Обновляется на каждом событии.
_LAST_RL: Optional[dict] = None


def last_rate_limit() -> Optional[dict]:
    """Последний известный статус лимита подписки (или None, если не было)."""
    return _LAST_RL


def _record_rl(info: Any) -> Optional[dict]:
    """Сохранить RateLimitInfo в _LAST_RL (нормализованный dict). Вернуть его."""
    global _LAST_RL
    try:
        d = {
            "status": getattr(info, "status", None),
            "resets_at": getattr(info, "resets_at", None),
            "rate_limit_type": getattr(info, "rate_limit_type", None),
            "utilization": getattr(info, "utilization", None),
            "overage_status": getattr(info, "overage_status", None),
            "ts": time.time(),
        }
    except Exception:  # noqa: BLE001
        return None
    _LAST_RL = d
    return d


def _reset_from_text(text: str) -> Optional[float]:
    """Лучшая попытка выудить время сброса из текста ошибки."""
    if not text:
        return None
    m = re.search(r"\b(1[6-9]\d{8}|20\d{8})\b", text)  # unix-epoch (сек)
    if m:
        return float(m.group(1))
    h = re.search(r"(\d+)\s*hour", text.lower())
    if h:
        return time.time() + int(h.group(1)) * 3600
    mn = re.search(r"(\d+)\s*minute", text.lower())
    if mn:
        return time.time() + int(mn.group(1)) * 60
    return None


def _sub_limit_error(text: str) -> Optional[LLMLimitError]:
    """ФОЛБЭК-детект лимита по тексту ошибки/ответа (когда нет RateLimitEvent)."""
    low = (text or "").lower()
    if not any(m in low for m in _LIMIT_MARKERS):
        return None
    return LLMLimitError(text[:200] or "Claude subscription limit",
                         provider="subscription",
                         reset_at=_reset_from_text(text), kind="limit")


def _is_transient_sub(exc: Exception) -> bool:
    """Транзиентный сбой SDK (стоит повторить): соединение/таймаут/процесс."""
    name = type(exc).__name__
    if name in ("CLIConnectionError", "TimeoutError"):
        return True
    if isinstance(exc, asyncio.TimeoutError):
        return True
    low = f"{exc}".lower()
    return any(s in low for s in (
        "timeout", "timed out", "econnreset", "connection reset",
        "broken pipe", "temporarily", "503", "502", "overloaded",
    ))


class ClaudeSubLLM:
    """LLM-совместимая обёртка над claude-agent-sdk (однооборотный query)."""

    def __init__(self, model: str = DEFAULT_SUB_MODEL):
        self.model = model or DEFAULT_SUB_MODEL

        class _Cfg:  # совместимость с LLM.cfg (temperature юзается в chat())
            temperature = 0.7
            max_tokens = 0
        self.cfg = _Cfg()

    # --- внутреннее: один прогон через SDK (+ live-стрим во фронт) ---
    async def _aquery(self, system: str, prompt: str) -> str:
        from claude_agent_sdk import (  # noqa: PLC0415 — ленивый импорт
            AssistantMessage, ClaudeAgentOptions, TextBlock, query,
        )
        try:  # RateLimitEvent появился не во всех версиях SDK
            from claude_agent_sdk import RateLimitEvent  # noqa: PLC0415
        except ImportError:
            RateLimitEvent = None  # type: ignore[assignment]
        from . import live  # noqa: PLC0415

        streaming = live.active()
        opt_kwargs = dict(
            system_prompt=system,
            model=self.model,
            # запас ходов: на длинном structured-выводе SDK иногда требует >1
            # хода («Reached maximum number of turns»). Инструментов нет.
            max_turns=8,
            allowed_tools=[],  # чистая генерация, без инструментов
        )
        # партиальные сообщения → видно генерацию по мере написания
        if streaming:
            try:
                opt_kwargs["include_partial_messages"] = True
            except Exception:  # noqa: BLE001
                pass
        try:
            opts = ClaudeAgentOptions(**opt_kwargs)
        except TypeError:  # старый SDK без include_partial_messages
            opt_kwargs.pop("include_partial_messages", None)
            opts = ClaudeAgentOptions(**opt_kwargs)

        chunks: list[str] = []
        partial = ""  # текст текущего (ещё не финализированного) блока
        async for msg in query(prompt=prompt, options=opts):
            # типизированный сигнал лимита от CLI (надёжнее парсинга текста)
            if RateLimitEvent is not None and isinstance(msg, RateLimitEvent):
                info = msg.rate_limit_info
                _record_rl(info)
                if getattr(info, "status", None) == "rejected":
                    raise LLMLimitError(
                        "Лимит подписки Claude исчерпан",
                        provider="subscription",
                        reset_at=getattr(info, "resets_at", None),
                        kind=str(getattr(info, "rate_limit_type", "") or "limit"),
                    )
                continue
            # потоковые дельты (если SDK их шлёт)
            ev = getattr(msg, "event", None) or getattr(msg, "delta", None)
            if streaming and ev is not None:
                txt = getattr(ev, "text", None)
                if not txt and isinstance(ev, dict):
                    txt = (ev.get("delta") or {}).get("text") or ev.get("text")
                if txt:
                    partial += txt
                    live.feed("".join(chunks) + partial)
                    continue
            if isinstance(msg, AssistantMessage):
                partial = ""
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
                if streaming:
                    live.feed("".join(chunks))
        return "".join(chunks)

    def _run(self, system: str, prompt: str) -> str:
        # uvicorn-воркеры — обычные потоки без event loop → asyncio.run ок.
        last: Optional[Exception] = None
        for attempt in range(_SUB_MAX_TRIES):
            try:
                out = asyncio.run(self._aquery(system, prompt))
            except LLMLimitError:
                raise  # уже типизировано в _aquery — не ретраим лимит
            except Exception as exc:  # noqa: BLE001
                # ProcessError несёт exit_code/stderr — лимит часто там
                detail = " ".join(filter(None, [
                    str(exc), getattr(exc, "stderr", None) or ""]))
                limit = _sub_limit_error(detail)
                if limit:
                    raise limit from exc
                if _is_transient_sub(exc) and attempt < _SUB_MAX_TRIES - 1:
                    last = exc
                    time.sleep(min(_SUB_BACKOFF ** attempt, 20.0))
                    continue
                raise
            # SDK иногда не бросает, а возвращает текст «лимит исчерпан»
            limit = _sub_limit_error(out) if out and len(out) < 400 else None
            if limit:
                raise limit
            return out
        raise last if last else RuntimeError("ClaudeSubLLM: неизвестный сбой")

    # --- интерфейс LLM ---
    def complete(self, system: str, user: str,
                 temperature: Optional[float] = None) -> str:  # noqa: ARG002
        return self._run(system, user)

    def chat(self, system: str, messages: list[dict],
             temperature: Optional[float] = None) -> str:  # noqa: ARG002
        # SDK однооборотный → историю сворачиваем в один промпт.
        lines = []
        for m in messages:
            who = "Нарративщик" if m.get("role") == "user" else "Редактор (ты)"
            lines.append(f"{who}: {m.get('content', '')}")
        lines.append("Редактор (ты):")
        return self._run(system, "\n\n".join(lines))

    def structured(self, system: str, user: str, schema: Type[T],
                   temperature: float = 0.2) -> T:  # noqa: ARG002
        js = schema.model_json_schema()
        prompt = (
            f"{user}\n\nОтветь СТРОГО одним JSON-объектом по схеме "
            f"(без markdown-обёртки и пояснений):\n{js}"
        )
        raw = self._run(system, prompt)
        return schema.model_validate_json(_extract_json(raw))
