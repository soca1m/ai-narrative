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
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from .llm import _extract_json

T = TypeVar("T", bound=BaseModel)

# Дефолтная модель подписки. Слаги SDK: sonnet / opus / haiku.
DEFAULT_SUB_MODEL = "sonnet"


class ClaudeSubLLM:
    """LLM-совместимая обёртка над claude-agent-sdk (однооборотный query)."""

    def __init__(self, model: str = DEFAULT_SUB_MODEL):
        self.model = model or DEFAULT_SUB_MODEL

        class _Cfg:  # совместимость с LLM.cfg (temperature юзается в chat())
            temperature = 0.7
            max_tokens = 0
        self.cfg = _Cfg()

    # --- внутреннее: один прогон через SDK ---
    async def _aquery(self, system: str, prompt: str) -> str:
        from claude_agent_sdk import (  # noqa: PLC0415 — ленивый импорт
            AssistantMessage, ClaudeAgentOptions, TextBlock, query,
        )
        opts = ClaudeAgentOptions(
            system_prompt=system,
            model=self.model,
            # запас ходов: на длинном structured-выводе SDK иногда требует >1
            # хода («Reached maximum number of turns»). Инструментов нет.
            max_turns=8,
            allowed_tools=[],  # чистая генерация, без инструментов
        )
        chunks: list[str] = []
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
        return "".join(chunks)

    def _run(self, system: str, prompt: str) -> str:
        # uvicorn-воркеры — обычные потоки без event loop → asyncio.run ок.
        return asyncio.run(self._aquery(system, prompt))

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
