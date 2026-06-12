"""Сборка LangGraph: конвейер ботов + цикл правок через редактора.

Поток:
  logline → synopsis → characters → structure → [поглавный цикл] → translation

Поглавный цикл (на каждую главу):
  dialogue → adult → editor → (роутинг)
    • есть критичный finding и попытки не исчерпаны → bump_retry → к боту-виновнику
    • иначе → следующая глава, либо выход в translation

Редактор есть всегда. Возврат «тому, кто ошибся» — через responsible_node.
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from . import nodes
from .routing import apply_decisions, can_retry, pick_revision_target
from .state import State


def _is_adult_chapter(state: State) -> bool:
    return bool(state["chapters"][state["chapter_idx"]].is_adult_point)


def _critical_count(report) -> int:
    # отклонённые нарративщиком не считаем «открытыми»
    return sum(
        1 for f in report.findings
        if f.severity == "critical" and getattr(f, "status", "open") != "rejected"
    )


def _after_editor(state: State) -> str:
    """Условное ребро после редактора: правка / следующая глава / перевод."""
    idx = state["chapter_idx"]
    reports = state["editor_reports"]
    decisions = state.get("finding_decisions") or {}
    last = apply_decisions(reports[-1], decisions)
    target, _ = pick_revision_target(last, _is_adult_chapter(state))

    # Блокирующих замечаний нет → двигаемся дальше.
    if target is None:
        return "advance"

    # Лимит попыток исчерпан → не зацикливаемся, отдаём человеку.
    if not can_retry(state.get("retry_count", {}), target, idx):
        return "advance"

    # No-progress guard: если эту главу уже правили и число критичных НЕ упало —
    # значит застряли на творческом суждении, дальше дожимать бессмысленно.
    # Стоп раньше лимита, эскалация человеку (замечания в editor_reports).
    retried = any(
        key.endswith(f":{idx}") and cnt > 0
        for key, cnt in state.get("retry_count", {}).items()
    )
    if retried and len(reports) >= 2:
        prev = apply_decisions(reports[-2], decisions)
        if _critical_count(last) >= _critical_count(prev):
            return "advance"

    return f"revise:{target}"


def _after_structure(state: State) -> str:
    """После порции структуры: ещё порция или переход к написанию глав.

    structure_done выставляется, когда история завершена (story_complete),
    порция вышла короче запрошенной, или достигнут хард-кап глав.
    В step-mode interrupt_after['structure'] ставит паузу после КАЖДОЙ порции —
    нарративщик ревьюит/правит и командует «ещё» или «перейти к написанию».
    """
    return "dialogue" if state.get("structure_done") else "structure"


def _advance_or_finish(state: State) -> str:
    """После успешной главы: следующая глава или перевод."""
    if state["chapter_idx"] + 1 < len(state["chapters"]):
        return "next_chapter"
    return "translation"


def _set_revision(target: str):
    """Фабрика узла-сеттера: проставляет цель и фидбек правки в state."""
    def _node(state: State) -> dict:
        decisions = state.get("finding_decisions") or {}
        report = apply_decisions(state["editor_reports"][-1], decisions)
        _, feedback = pick_revision_target(report, _is_adult_chapter(state))
        return {"revision_target": target, "revision_feedback": feedback}
    return _node


def _next_chapter(state: State) -> dict:
    return {"chapter_idx": state["chapter_idx"] + 1}


def sqlite_saver(path: str = "narrative_state.db") -> SqliteSaver:
    """Персистентный checkpointer: state переживает краши и сессии.

    Это «общая память между агентами и сессиями» из требований. При обрыве
    (напр. 402 на редакторе) пайплайн резюмится с последнего шага — адалт и
    прочее не перегенерируются.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn)


# Стадии, после которых можно вставать на паузу (step-mode для ручных правок).
STAGE_NODES = [
    "logline", "synopsis", "characters", "structure",
    "dialogue", "adult", "editor", "translation",
]


def build_graph(checkpointer=None, interrupt_after=None):
    g = StateGraph(State)

    # узлы-боты
    g.add_node("logline", nodes.logline_node)
    g.add_node("synopsis", nodes.synopsis_node)
    g.add_node("characters", nodes.characters_node)
    g.add_node("structure", nodes.structure_node)
    g.add_node("dialogue", nodes.dialogue_node)
    g.add_node("adult", nodes.adult_node)
    g.add_node("editor", nodes.editor_node)
    g.add_node("translation", nodes.translation_node)
    g.add_node("bump_retry", nodes.bump_retry_node)

    # узлы-сеттеры правки (по одному на возможную цель)
    for tgt in ("dialogue", "adult", "characters", "structure"):
        g.add_node(f"revise_to_{tgt}", _set_revision(tgt))
    g.add_node("next_chapter", _next_chapter)

    # линейная часть
    g.add_edge(START, "logline")
    g.add_edge("logline", "synopsis")
    g.add_edge("synopsis", "characters")
    g.add_edge("characters", "structure")
    # структура порциями: петля structure→structure, пока не structure_done
    g.add_conditional_edges("structure", _after_structure, {
        "structure": "structure",
        "dialogue": "dialogue",
    })

    # поглавный цикл
    g.add_edge("dialogue", "adult")
    g.add_edge("adult", "editor")

    g.add_conditional_edges("editor", _after_editor, {
        "advance": "advance_router",
        "revise:dialogue": "revise_to_dialogue",
        "revise:adult": "revise_to_adult",
        "revise:characters": "revise_to_characters",
        "revise:structure": "revise_to_structure",
    })

    # после сеттера правки — учёт попытки, затем к боту-виновнику
    for tgt in ("dialogue", "adult", "characters", "structure"):
        g.add_edge(f"revise_to_{tgt}", "bump_retry")
    g.add_conditional_edges("bump_retry", lambda s: s["revision_target"], {
        "dialogue": "dialogue",
        "adult": "adult",
        "characters": "characters",   # правка карточки → пересборка ниже
        "structure": "structure",
    })

    # роутер «дальше»: следующая глава или перевод
    g.add_node("advance_router", lambda s: {})
    g.add_conditional_edges("advance_router", _advance_or_finish, {
        "next_chapter": "next_chapter",
        "translation": "translation",
    })
    g.add_edge("next_chapter", "dialogue")
    g.add_edge("translation", END)

    return g.compile(
        checkpointer=checkpointer or MemorySaver(),
        interrupt_after=interrupt_after or [],
    )
