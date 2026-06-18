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


class StaticShot(BaseModel):
    """Один статик/анимация для художника (машиночитаемо, без regex-парсинга)."""
    tag: str = Field(description="Код кадра: {NN}-{Локация}-{N} или {NN}-{Локация}Anim-{N}, "
                     "например 07-HeroRoom-0 или 07-HeroRoomAnim-2")
    description: str = Field(description="Описание кадра/движения для художника")


class Chapter(BaseModel):
    """Одна глава из поглавного плана (Бот 4)."""
    index: int
    title: str
    plan: str                       # текст плана главы от Бота 4
    is_adult_point: bool = False    # Бот 4 решает, где адалт органичен
    adult_note: str = ""            # кто с кем и почему сцена уместна здесь
    dialogue: Optional[str] = None  # текст главы от Бота 5
    adult_scene: Optional[str] = None  # адалт-вставка от Бота 6
    translation: Optional[str] = None  # перевод от Бота 8
    # Статики/анимации для художника (из structured-вывода Ботов 5/6).
    statics: list[StaticShot] = Field(default_factory=list)
    anims: list[StaticShot] = Field(default_factory=list)
    adult_statics: list[StaticShot] = Field(default_factory=list)
    adult_anims: list[StaticShot] = Field(default_factory=list)
    # Пре-чек адалта: глава без почвы для сцены → причина + подсказка адаптации.
    adult_block_reason: Optional[str] = None
    adult_bridge_hint: Optional[str] = None


class DialogueOut(BaseModel):
    """Строгий контракт Бота 5: текст главы + статики отдельными полями."""
    script: str = Field(description="Полный текст главы в формате визуальной новеллы. "
                        "Теги статиков/анимаций стоят в потоке текста на своих местах "
                        "(отдельной строкой перед репликами кадра).")
    statics: list[StaticShot] = Field(default_factory=list,
                                      description="Все статики главы по порядку")
    anims: list[StaticShot] = Field(default_factory=list,
                                    description="Все анимации главы по порядку")


class AdultSceneOut(BaseModel):
    """Строгий контракт ответа Бота 6: отказ — это поле, а не текст для парсинга."""
    refused: bool = Field(description="True — модель не может написать сцену")
    reason: str = Field(default="", description="Причина отказа (если refused)")
    scene: str = Field(default="", description="Полный текст адалт-сцены (если не refused), "
                       "теги статиков/анимаций в потоке текста на своих местах")
    statics: list[StaticShot] = Field(default_factory=list,
                                      description="Все статики сцены по порядку")
    anims: list[StaticShot] = Field(default_factory=list,
                                    description="Все анимации сцены по порядку")


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
    is_adult: bool = Field(
        default=True,
        description="true — в главе есть откровенная сцена. Это adult-новелла, "
        "поэтому БОЛЬШИНСТВО глав адалт, НО варьируй: 1-2 главы на историю могут "
        "быть false ради нарастания/поворота/развязки. Не делай механически "
        "каждую главу постельной и не повторяй одну и ту же формулу.",
    )
    adult_note: str = Field(
        default="",
        description="Кто участвует в сцене (можно НЕСКОЛЬКО персонажей — "
        "смешанные/групповые сцены разрешены), какая динамика и через какой "
        "повод входит сцена. Для художника и Бота 6.",
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
    judge_reason: str = ""             # вердикт ИИ при «ИИ решит» (для UI)


class ChapterCountOut(BaseModel):
    """Предложение Бота по оптимальному числу глав (юзер апрувит/меняет)."""
    count: int = Field(description="Рекомендованное число глав для этой истории")
    reason: str = Field(description="Почему столько — 1-2 предложения")


class StructureFixOut(BaseModel):
    """Возврат редактора структуры (#2): исправленный план + список фиксов."""
    chapters: list[ChapterPlan] = Field(default_factory=list)
    fixes: list[str] = Field(default_factory=list,
                             description="Что починил, по пункту на фикс")


class ChatApplyOut(BaseModel):
    """Итог обсуждения с ИИ → применить к главе (или не трогать)."""
    changed: bool = Field(description="True — внесены правки; False — главу не трогаем")
    note: str = Field(default="", description="Что изменено / почему не тронуто (1-2 строки)")
    script: str = Field(default="", description="Полный новый текст главы (если changed)")


class JudgeOut(BaseModel):
    """Вердикт ИИ для «ИИ решит» — strict JSON, надёжнее парсинга одного слова."""
    decision: Literal["accept", "reject"] = Field(
        description="accept — замечание справедливо, исправлять; "
        "reject — можно оставить как есть")
    reason: str = Field(description="Кратко почему (1-2 предложения), для нарративщика")


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
    chapters_per_batch: int     # (устар.) запас; число глав теперь target_chapters
    suggested_chapters: int     # ИИ предложил оптимальное число глав
    count_reason: str           # обоснование предложения
    target_chapters: int        # утверждённое нарративщиком число глав

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

    # --- фаза поглавной работы: сначала весь КОНТЕНТ, потом РЕДАКТОР ---
    # "content" — Бот 5 пишет все главы (диалоги+адалт) по очереди;
    # "edit" — только после всего контента Бот 7 проверяет каждую главу.
    phase: Optional[str]

    editor_reports: Annotated[list[EditorReport], operator.add]
    # решения нарративщика по findings: id -> {"status":..., "comment":...}
    finding_decisions: dict[str, dict]
    # тексты ОТКЛОНЁННЫХ нарративщиком претензий — редактор их больше не поднимает
    # (переживает смену fid между проходами, в отличие от finding_decisions)
    rejected_notes: list[str]
    # выбранная нарративщиком модель для глав (Бот 5, не-адалт) — слаг OpenRouter
    chapter_model: Optional[str]
    # что починил редактор структуры (#2) — для показа в UI
    structure_fixes: list[str]

    # --- Бот локаций (отдельный бот, как персонажи) ---
    locations: str        # карточки локаций (канон мест) — юзаются в главах

    # --- выбор провайдера по этапам + фолбэк при исчерпании лимита ---
    # этап → "subscription" | "openrouter" (пер-этапный оверрайд глобального тумблера)
    stage_providers: dict[str, str]
    # фолбэк: при исчерпании лимита подписки весь ран идёт через OpenRouter
    force_openrouter: bool
    # последнее событие лимита (для баннера UI): {provider, reset_at, stage, kind}
    limit_info: Optional[dict]

    # --- управление циклом правок (editor ↔ bot) ---
    revision_target: Optional[NodeName]  # кого переделывать
    revision_feedback: Optional[str]     # конкретный фидбек редактора
    retry_count: dict[str, int]          # счётчик попыток на (нода+глава)

    # --- лог для UI / отладки ---
    log: Annotated[list[str], operator.add]
