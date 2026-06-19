"""Живой стрим генерации: узел пишет → партиалы летят во фронт.

Sink держим в threading.local — узел графа и его LLM-вызов идут в ОДНОМ потоке,
поэтому потоколокальная привязка надёжна (в отличие от ContextVar через пул).
Узел вызывает bind() (sink берётся из config графа), LLM дёргает feed() по мере
накопления текста, узел — clear() в finally.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

# sink(stage: str, idx: int|None, full_text: str) — отдаёт накопленный текст.
Sink = Callable[[str, Optional[int], str], None]

_local = threading.local()


def bind(sink: Optional[Sink], stage: str, idx: Optional[int] = None) -> None:
    _local.sink = sink
    _local.stage = stage
    _local.idx = idx


def clear() -> None:
    _local.sink = None


def active() -> bool:
    return getattr(_local, "sink", None) is not None


def feed(full_text: str) -> None:
    sink = getattr(_local, "sink", None)
    if sink:
        try:
            sink(getattr(_local, "stage", ""), getattr(_local, "idx", None), full_text)
        except Exception:  # noqa: BLE001 — стрим не должен ронять генерацию
            pass


def sink_from_config(config) -> Optional[Sink]:
    """Достаёт on_delta из config графа (configurable.on_delta)."""
    try:
        return (config or {}).get("configurable", {}).get("on_delta")
    except Exception:  # noqa: BLE001
        return None
