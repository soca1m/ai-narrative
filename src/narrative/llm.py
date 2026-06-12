"""Тонкая обёртка над OpenAI-совместимым API (OpenRouter / локальный сервер).

Один клиент-класс на оба типа провайдера — отличаются только конфигом.
"""
from __future__ import annotations

import re
from typing import Optional, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from .config import ProviderConfig

T = TypeVar("T", bound=BaseModel)


class LLM:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self._client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key)

    def complete(self, system: str, user: str,
                 temperature: Optional[float] = None) -> str:
        """Обычная генерация текста (используют все боты кроме редактора)."""
        resp = self._client.chat.completions.create(
            model=self.cfg.model,
            temperature=self.cfg.temperature if temperature is None else temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
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
        resp = self._client.chat.completions.create(
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
        raw = resp.choices[0].message.content or "{}"
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


def _extract_json(raw: str) -> str:
    """Достаёт JSON, даже если модель обернула его в ```json-ограждение."""
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if fenced:
        return fenced.group(1)
    # иначе берём от первой { до последней }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        return raw[start:end + 1]
    return raw
