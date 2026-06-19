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
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from .llm import LLMLimitError, _extract_json

T = TypeVar("T", bound=BaseModel)

# Дефолтная модель подписки. Слаги SDK: sonnet / opus / haiku.
DEFAULT_SUB_MODEL = "sonnet"

# Признаки исчерпания лимита подписки в тексте ошибки/ответа SDK.
_LIMIT_MARKERS = (
    "usage limit", "rate limit", "limit reached", "limit will reset",
    "quota", "out of", "too many requests", "429",
    "лимит", "превышен", "исчерпан",
)


def _sub_limit_error(text: str) -> Optional[LLMLimitError]:
    """Если текст похож на лимит подписки → LLMLimitError (иначе None).

    Пытаемся выудить время сброса: epoch-таймстамп или «through HH:MM»/«X hours».
    """
    low = (text or "").lower()
    if not any(m in low for m in _LIMIT_MARKERS):
        return None
    reset_at: Optional[float] = None
    m = re.search(r"\b(1[6-9]\d{8}|20\d{8})\b", text or "")  # epoch (сек)
    if m:
        reset_at = float(m.group(1))
    else:
        h = re.search(r"(\d+)\s*hour", low)
        if h:
            reset_at = time.time() + int(h.group(1)) * 3600
    return LLMLimitError(text[:200] or "Claude subscription limit",
                         provider="subscription", reset_at=reset_at,
                         kind="limit")


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
        try:
            out = asyncio.run(self._aquery(system, prompt))
        except Exception as exc:  # noqa: BLE001
            limit = _sub_limit_error(str(exc))
            if limit:
                raise limit from exc
            raise
        # SDK иногда не бросает, а возвращает текст «лимит исчерпан»
        limit = _sub_limit_error(out) if out and len(out) < 400 else None
        if limit:
            raise limit
        return out

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
