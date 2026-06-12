"""Общая память пайплайна (shared state).

Один экземпляр State = одна новелла (= один "чат внутри проекта" из памятки).
LangGraph checkpointer хранит это между шагами и сессиями по thread_id.
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional, TypedDict

from pydantic import BaseModel, Field

# Узлы-боты конвейера. Используется и для роутинга правок редактора.
NodeName = Literal[
    "logline",      # Бот 1
    "synopsis",     # Бот 2
    "characters",   # Бот 3
    "structure",    # Бот 4
    "dialogue",     # Бот 5
    "adult",        # Бот 6
    "editor",       # Бот 7
    "translation",  # Бот 8
]

Severity = Literal["critical", "important", "minor"]  # 🔴 / 🟡 / 🟢
FindingStatus = Literal["open", "accepted", "rejected"]


class Chapter(BaseModel):
    """Одна глава из поглавного плана (Бот 4)."""
    index: int
    title: str
    plan: str                       # текст плана главы от Бота 4
    is_adult_point: bool = True     # адалт теперь в КАЖДОЙ главе
    dialogue: Optional[str] = None  # текст главы от Бота 5
    adult_scene: Optional[str] = None  # адалт-вставка от Бота 6
    translation: Optional[str] = None  # перевод от Бота 8
    # Пре-чек адалта: глава без почвы для сцены → причина + подсказка адаптации.
    adult_block_reason: Optional[str] = None
    adult_bridge_hint: Optional[str] = None


class AdultSceneOut(BaseModel):
    """Строгий контракт ответа Бота 6: отказ — это поле, а не текст для парсинга."""
    refused: bool = Field(description="True — модель не может написать сцену")
    reason: str = Field(default="", description="Причина отказа (если refused)")
    scene: str = Field(default="", description="Полный текст адалт-сцены (если не refused)")


class AdultFeasibility(BaseModel):
    """Пре-чек Бота 6: есть ли в главе органичная почва для откровенной сцены."""
    feasible: bool = Field(description="True — сцена впишется органично")
    reason: str = Field(description="Короткое объяснение почему да/нет")
    pair: str = Field(default="", description="Кто с кем (если сцена возможна)")
    bridge: str = Field(
        default="",
        description="Как адаптировать главу, чтобы сцена стала органичной "
        "(1-2 предложения), если сейчас почвы нет",
    )


class ChapterPlan(BaseModel):
    """Одна глава в structured-выводе Бота 4 (надёжнее regex-парсинга прозы)."""
    title: str = Field(description="Название главы")
    plan: str = Field(
        description="Полный план главы одним блоком: локация, что происходит, "
        "эмоциональная дуга, точка выбора, финал."
    )


class StructurePlan(BaseModel):
    """Возврат Бота 4 — порция глав + признак завершения истории."""
    chapters: list[ChapterPlan] = Field(default_factory=list)
    story_complete: bool = Field(
        default=False,
        description="True, если на этой порции история логически завершилась "
        "и новые главы не нужны.",
    )


class FindingOut(BaseModel):
    """Замечание редактора — РОВНО то, что возвращает LLM (strict JSON-схема).

    Без UI-полей (id/status/comment), чтобы strict-схема не требовала их от модели.
    """
    severity: Severity
    block: Literal["names", "motivation", "gender", "style"]
    responsible_node: NodeName = Field(
        description="Какой бот должен переделать. Обычно dialogue/adult, "
        "реже characters/structure для проблем уровня карточек/плана."
    )
    locator: str = Field(description="Глава N, абзац/реплика — где проблема")
    quote: str = Field(
        default="",
        description="ТОЧНАЯ дословная цитата фрагмента текста главы, к которому "
        "относится замечание (для подсветки в UI). Скопируй подстроку как есть, "
        "без изменений. Пусто — если привязать к конкретному фрагменту нельзя.",
    )
    problem: str = Field(description="В чём проблема и почему")


class EditorReportOut(BaseModel):
    """Возврат LLM-редактора (strict JSON)."""
    chapter_index: int
    findings: list[FindingOut] = Field(default_factory=list)
    markdown: str = ""


class Finding(FindingOut):
    """Замечание + UI-состояние (присваивается в editor_node)."""
    id: str = ""                       # стабилен по (глава, block, locator)
    status: FindingStatus = "open"     # решение нарративщика
    user_comment: str = ""             # комментарий нарративщика к замечанию


class EditorReport(BaseModel):
    """Хранимый отчёт редактора: findings (с id/статусом) + markdown."""
    chapter_index: int
    findings: list[Finding] = Field(default_factory=list)
    markdown: str = ""  # человекочитаемый отчёт в формате из дока

    @property
    def has_blocking(self) -> bool:
        # блокируют только НЕ отклонённые критичные
        return any(
            f.severity == "critical" and f.status != "rejected"
            for f in self.findings
        )


class State(TypedDict, total=False):
    # --- вход ---
    theme: str            # тема + референсы от нарративщика
    genre: str            # особый жанр → может менять инструкции ботов
    target_language: str  # язык перевода (Бот 8)
    translation_enabled: bool   # Бот 8 вкл/выкл (перевод временно на паузе)
    chapters_per_batch: int     # сколько глав Бот 4 пишет за одну порцию

    # --- Бот 1: логлайны + выбор ---
    loglines: list[str]         # распарсенный список вариантов логлайна
    selected_logline: str       # выбранный нарративщиком (идёт в синопсис)

    # --- артефакты по шагам (накопительная общая память) ---
    logline: str          # сырой вывод Бота 1 (нумерованный список)
    synopsis: str
    characters: str       # карточки персонажей (Бот 3) — канон имён/мотиваций
    chapters: list[Chapter]
    chapter_idx: int      # текущая глава в поглавном цикле (Боты 5→6→7)

    # --- управление батчингом структуры (Бот 4 пишет порциями) ---
    structure_done: bool        # вся структура сгенерирована
    structure_action: Optional[str]  # "more" | "proceed" (команда нарративщика)

    editor_reports: Annotated[list[EditorReport], operator.add]
    # решения нарративщика по findings: id -> {"status":..., "comment":...}
    finding_decisions: dict[str, dict]

    # --- управление циклом правок (editor ↔ bot) ---
    revision_target: Optional[NodeName]  # кого переделывать
    revision_feedback: Optional[str]     # конкретный фидбек редактора
    retry_count: dict[str, int]          # счётчик попыток на (нода+глава)

    # --- лог для UI / отладки ---
    log: Annotated[list[str], operator.add]
