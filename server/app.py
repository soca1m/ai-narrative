"""FastAPI-мост к LangGraph-пайплайну для фронтенда.

Возможности:
- старт прогона (тема/жанр/язык/порция глав/перевод вкл-выкл);
- live-прогресс агентов (поллинг state + SSE);
- ручная правка артефактов на любом этапе (update_state);
- per-stage «попроси ИИ переделать» (revise) + правка плана конкретной главы;
- структура порциями: «ещё порцию» / «перейти к написанию»;
- решения по findings редактора: принять/отклонить/комментировать + авто-judge;
- откат на предыдущий этап (rollback);
- step-mode: пауза после каждого бота + resume.

Запуск: uvicorn server.app:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from narrative import nodes
from narrative.graph import STAGE_NODES, build_graph, sqlite_saver
from narrative.llm import LLMLimitError
from narrative.routing import apply_decisions
from narrative.state import Chapter, JudgeOut

load_dotenv()
app = FastAPI(title="ai-narrative API")
# Узкий CORS: фронт ходит через Next-прокси (same-origin), напрямую в браузере
# CORS не нужен. Разрешаем только локальные dev-origin'ы — это режет drive-by
# CSRF с произвольных сайтов на мутирующие/токен-эндпоинты (бэк на 127.0.0.1).
_ALLOWED = [o for o in os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3030,http://127.0.0.1:3030,"
    "http://localhost:3000,http://127.0.0.1:3000",
).split(",") if o]
app.add_middleware(
    CORSMiddleware, allow_origins=_ALLOWED,
    allow_methods=["*"], allow_headers=["*"],
)

_SAVER = sqlite_saver()
_GRAPH_AUTO = build_graph(_SAVER)
_GRAPH_STEP = build_graph(_SAVER, interrupt_after=STAGE_NODES)
_LOCK = threading.Lock()

# Прямой вызов узла (для per-stage revise / re-check) — узлы чистые (state→dict).
NODE_FUNCS = {
    "logline": nodes.logline_node,
    "synopsis": nodes.synopsis_node,
    "characters": nodes.characters_node,
    "locations": nodes.locations_node,
    "structure": nodes.structure_node,
    "dialogue": nodes.dialogue_node,
    "adult": nodes.adult_node,
    "editor": nodes.editor_node,
    "translation": nodes.translation_node,
}

# Откат: какой узел «проиграть как предыдущий», чтобы next встал на нужный этап,
# и какие (не-reducer) поля очистить.
ROLLBACK = {
    "synopsis":   ("logline",    ["synopsis", "characters", "locations", "chapters", "chapter_idx", "structure_done"]),
    "characters": ("synopsis",   ["characters", "locations", "chapters", "chapter_idx", "structure_done"]),
    "locations":  ("characters", ["locations", "chapters", "chapter_idx", "structure_done"]),
    "structure":  ("characters", ["chapters", "chapter_idx", "structure_done"]),
    "dialogue":   ("structure",  ["chapter_idx"]),  # перезапуск написания глав с 0
}


@dataclass
class Run:
    thread_id: str
    step_mode: bool
    events: queue.Queue = field(default_factory=queue.Queue)
    status: str = "idle"
    printed: int = 0
    worker: Optional[threading.Thread] = None
    error: str = ""  # текст последней ошибки воркера (для UI)
    limit: Optional[dict] = None  # инфо об исчерпании лимита (#5): баннер выбора


RUNS: dict[str, Run] = {}


def _graph(run: Run):
    return _GRAPH_STEP if run.step_mode else _GRAPH_AUTO


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _get_run(thread_id: str) -> Run:
    run = RUNS.get(thread_id)
    if run is not None:
        return run
    # Прогона нет в памяти (рестарт бэка / краш процесса), но state мог
    # сохраниться в SQLite-чекпоинте → восстанавливаем Run, чтобы продолжить
    # с места обрыва, а не терять весь прогресс.
    try:
        snap = _GRAPH_STEP.get_state(_config(thread_id))
    except Exception:
        snap = None
    if snap and (snap.values or snap.next):
        status = "done" if not snap.next else "paused"
        run = Run(thread_id=thread_id, step_mode=True, status=status)
        RUNS[thread_id] = run
        return run
    raise HTTPException(404, "run not found")


def _bg_op(run: Run, fn) -> dict:
    """Запускает ручную LLM-операцию В ФОНЕ (LLM долгий — иначе Next-прокси рвёт
    по таймауту → 500). Возвращает сразу; фронт поллит status.

    Не ломает позицию графа: по завершении/ошибке возвращаем прежний статус
    (paused/done), ошибку кладём в run.error (баннер), прогресс в чекпоинте
    цел — операцию можно повторить кнопкой, граф резюмируем."""
    prev = run.status if run.status in ("paused", "done") else "paused"

    def _worker():
        run.status = "running"
        run.error = ""
        run.events.put({"type": "status", "status": "running"})
        try:
            fn()
            run.status = prev
            run.events.put({"type": "status", "status": prev})
        except LLMLimitError as exc:  # #5: лимит → баннер выбора, прогресс цел
            run.limit = {"provider": exc.provider, "reset_at": exc.reset_at,
                         "kind": exc.kind, "message": str(exc)[:200]}
            run.status = "limit"
            run.events.put({"type": "status", "status": "limit",
                            "limit": run.limit})
        except Exception as exc:  # noqa: BLE001
            run.error = f"Операция не удалась (LLM/провайдер): {str(exc)[:200]}"
            run.status = prev
            run.events.put({"type": "status", "status": prev,
                            "error": run.error})

    run.worker = threading.Thread(target=_worker, daemon=True)
    run.worker.start()
    return {"ok": True, "started": True}


def _state(thread_id: str) -> dict:
    run = _get_run(thread_id)
    return dict(_graph(run).get_state(_config(thread_id)).values or {})


def _serialize(values: dict) -> dict:
    """State → JSON-safe dict. findings получают актуальный статус/коммент из decisions."""
    decisions = values.get("finding_decisions") or {}
    out: dict[str, Any] = {}
    for key, val in values.items():
        if key == "chapters":
            out[key] = [c.model_dump() for c in val]
        elif key == "editor_reports":
            out[key] = [apply_decisions(r, decisions).model_dump() for r in val]
        else:
            out[key] = val
    return out


def _run_worker(run: Run, _graph_unused, stream_input):
    """Гоняем step-граф (interrupt после каждой стадии) в ЦИКЛЕ. На каждой
    паузе-точке смотрим run.step_mode: ON → стоп и ждём resume; OFF → сами
    продолжаем. Так тумблер пошагового режима работает НА ЛЕТУ."""
    cfg = _config(run.thread_id)
    graph = _GRAPH_STEP
    run.status = "running"
    run.error = ""  # новый прогон → чистим прошлую ошибку
    run.events.put({"type": "status", "status": "running"})
    inp = stream_input
    try:
        while True:
            for event in graph.stream(inp, cfg, stream_mode="values"):
                log = event.get("log", [])
                for line in log[run.printed:]:
                    run.events.put({"type": "log", "line": line})
                run.printed = len(log)
                run.events.put({"type": "state", "state": _serialize(event)})

            snap = graph.get_state(cfg)
            if not snap.next:  # END
                run.status = "done"
                run.events.put({"type": "status", "status": "done"})
                return
            if run.step_mode:  # пауза-точка + пошаговый → ждём юзера
                run.status = "paused"
                run.events.put({"type": "status", "status": "paused",
                                "next": list(snap.next)})
                return
            inp = None  # авто-режим → продолжаем со следующей стадии
    except LLMLimitError as exc:  # #5: лимит провайдера исчерпан → спросить юзера
        limit = {"provider": exc.provider, "reset_at": exc.reset_at,
                 "kind": exc.kind, "message": str(exc)[:200]}
        run.limit = limit
        run.status = "limit"
        try:
            _patch(run.thread_id, run, {"limit_info": limit})
        except Exception:  # noqa: BLE001
            pass
        run.events.put({"type": "status", "status": "limit", "limit": limit})
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.error = str(exc)
        run.events.put({"type": "status", "status": "error", "error": str(exc)})


def _busy(run: Run):
    if run.worker and run.worker.is_alive():
        raise HTTPException(409, "run is busy")


def _patch(thread_id: str, run: Run, patch: dict, as_node: str | None = None):
    with _LOCK:
        if as_node:
            _graph(run).update_state(_config(thread_id), patch, as_node=as_node)
        else:
            _graph(run).update_state(_config(thread_id), patch)


# ---------- модели запросов ----------

class StartReq(BaseModel):
    theme: str
    genre: Optional[str] = None
    target_language: str = "English"
    chapters_per_batch: int = 3
    translation_enabled: bool = False  # перевод временно на паузе
    step_mode: bool = True
    chapter_model: Optional[str] = None  # модель для глав (Бот 5, не-адалт)


class PatchReq(BaseModel):
    patch: dict[str, Any]


class ChatReq(BaseModel):
    """Чат с ИИ-редактором (#1): по главе или по конкретному замечанию."""
    messages: list[dict]                 # [{role: user|assistant, content}]
    chapter_idx: Optional[int] = None    # контекст: глава
    finding_id: Optional[str] = None     # контекст: замечание


class ReviseReq(BaseModel):
    feedback: str
    chapter_idx: Optional[int] = None


class SelectLoglineReq(BaseModel):
    logline: str


class FindingReq(BaseModel):
    status: Optional[str] = None   # "accepted" | "rejected" | "open"
    comment: Optional[str] = None
    judge: bool = False            # True → ИИ сам решает принять/отклонить


class RollbackReq(BaseModel):
    stage: str


# ---------- основные эндпоинты ----------

@app.post("/api/runs")
def start_run(req: StartReq):
    thread_id = uuid.uuid4().hex[:12]
    run = Run(thread_id=thread_id, step_mode=req.step_mode)
    RUNS[thread_id] = run
    init = {
        "theme": req.theme,
        "genre": req.genre,
        "target_language": req.target_language,
        "chapters_per_batch": max(1, req.chapters_per_batch),
        "translation_enabled": req.translation_enabled,
        "chapter_model": req.chapter_model,
    }
    run.worker = threading.Thread(
        target=_run_worker, args=(run, _graph(run), init), daemon=True)
    run.worker.start()
    return {"thread_id": thread_id, "step_mode": req.step_mode}


@app.get("/api/runs/{thread_id}/state")
def get_state(thread_id: str):
    run = _get_run(thread_id)
    snap = _graph(run).get_state(_config(thread_id))
    return {"status": run.status, "next": list(snap.next),
            "error": run.error, "limit": run.limit,
            "state": _serialize(snap.values or {})}


@app.get("/api/runs")
def list_runs():
    """Список всех прогонов (новелл) из чекпоинт-БД — чтобы продолжить
    оборванную работу после рестарта/краша или скачать готовую."""
    import sqlite3
    db = os.environ.get("NARRATIVE_DB", "narrative_state.db")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True,
                               check_same_thread=False)
        rows = [r[0] for r in conn.execute(
            "SELECT thread_id FROM checkpoints GROUP BY thread_id "
            "ORDER BY MAX(rowid) DESC LIMIT 100")]
        conn.close()
    except Exception:
        rows = list(RUNS.keys())
    out = []
    for tid in rows:
        try:
            snap = _GRAPH_STEP.get_state(_config(tid))
        except Exception:
            continue
        vals = snap.values or {}
        if not vals.get("theme"):
            continue  # пустой/мусорный чекпоинт — пропускаем
        chs = vals.get("chapters") or []
        run = RUNS.get(tid)
        status = run.status if run else ("done" if not snap.next else "paused")
        out.append({
            "thread_id": tid,
            "theme": str(vals.get("theme", ""))[:100],
            "chapters": len(chs),
            "written": sum(1 for c in chs if getattr(c, "dialogue", None)),
            "status": status,
        })
    return {"runs": out}


@app.patch("/api/runs/{thread_id}/state")
def patch_state(thread_id: str, req: PatchReq):
    run = _get_run(thread_id)
    _busy(run)
    patch = dict(req.patch)
    if "chapters" in patch:
        patch["chapters"] = [Chapter(**c) for c in patch["chapters"]]
    _patch(thread_id, run, patch)
    run.events.put({"type": "edited", "keys": list(patch.keys())})
    return {"ok": True}


@app.post("/api/runs/{thread_id}/resume")
def resume_run(thread_id: str):
    run = _get_run(thread_id)
    _busy(run)
    run.limit = None  # снимаем баннер лимита при ресюме
    run.worker = threading.Thread(
        target=_run_worker, args=(run, None, None), daemon=True)
    run.worker.start()
    return {"ok": True}


# ---------- #5: провайдер по этапам + разрешение лимита ----------

_VALID_STAGES = set(STAGE_NODES) | {"locations", "chat"}


class StageProviderReq(BaseModel):
    stage: str                  # этап из STAGE_NODES (или "all")
    provider: str               # "subscription" | "openrouter" | "default"


@app.post("/api/runs/{thread_id}/stage_provider")
def set_stage_provider(thread_id: str, req: StageProviderReq):
    """Выбор провайдера (подписка/OpenRouter) для конкретного этапа (#5)."""
    run = _get_run(thread_id)
    if req.provider not in ("subscription", "openrouter", "default"):
        raise HTTPException(400, "bad provider")
    st = _state(thread_id)
    sp = dict(st.get("stage_providers") or {})
    stages = list(_VALID_STAGES) if req.stage == "all" else [req.stage]
    for s in stages:
        if req.provider == "default":
            sp.pop(s, None)
        else:
            sp[s] = req.provider
    _patch(thread_id, run, {"stage_providers": sp})
    return {"ok": True, "stage_providers": sp}


class LimitResolveReq(BaseModel):
    action: str                 # "switch" (→OpenRouter) | "wait" | "subscription"


@app.post("/api/runs/{thread_id}/limit/resolve")
def resolve_limit(thread_id: str, req: LimitResolveReq):
    """Решение нарративщика по исчерпанному лимиту (#5).

    switch — весь ран на OpenRouter и продолжить; subscription — снова на
    подписку (после сброса) и продолжить; wait — просто снять баннер.
    """
    run = _get_run(thread_id)
    _busy(run)
    if req.action == "switch":
        _patch(thread_id, run, {"force_openrouter": True, "limit_info": None})
    elif req.action == "subscription":
        _patch(thread_id, run, {"force_openrouter": False, "limit_info": None})
    elif req.action != "wait":
        raise HTTPException(400, "bad action")
    run.limit = None
    if req.action in ("switch", "subscription"):
        run.worker = threading.Thread(
            target=_run_worker, args=(run, None, None), daemon=True)
        run.worker.start()
        return {"ok": True, "resumed": True}
    return {"ok": True, "resumed": False}


class StepReq(BaseModel):
    enabled: bool


@app.post("/api/runs/{thread_id}/step")
def set_step(thread_id: str, req: StepReq):
    """Тумблер пошагового режима НА ЛЕТУ. Если выключили во время паузы —
    автоматически продолжаем (worker добежит до конца / след. паузы)."""
    run = _get_run(thread_id)
    auto_continued = False
    # под _LOCK: иначе TOCTOU между проверкой run.status/worker и стартом потока
    # (воркер параллельно читает step_mode / пишет status) → 2 воркера разом.
    with _LOCK:
        run.step_mode = req.enabled
        if not req.enabled and run.status == "paused" and not (
                run.worker and run.worker.is_alive()):
            run.worker = threading.Thread(
                target=_run_worker, args=(run, None, None), daemon=True)
            run.worker.start()
            auto_continued = True
    return {"ok": True, "step_mode": run.step_mode, "resumed": auto_continued}


# ---------- Бот 1: выбор логлайна ----------

@app.post("/api/runs/{thread_id}/select_logline")
def select_logline(thread_id: str, req: SelectLoglineReq):
    run = _get_run(thread_id)
    _busy(run)
    _patch(thread_id, run, {"selected_logline": req.logline})
    run.events.put({"type": "edited", "keys": ["selected_logline"]})
    return {"ok": True}


# ---------- per-stage revise: попросить ИИ переделать этап по фидбеку ----------

@app.post("/api/runs/{thread_id}/stage/{stage}/revise")
def revise_stage(thread_id: str, stage: str, req: ReviseReq):
    """Попросить ИИ переделать этап (ФОНОВО — LLM долгий)."""
    run = _get_run(thread_id)
    _busy(run)
    if stage not in NODE_FUNCS:
        raise HTTPException(400, f"unknown stage {stage}")

    def _op():
        st = _state(thread_id)
        st["revision_feedback"] = req.feedback
        if stage in ("dialogue", "adult"):
            st["revision_target"] = stage
        if req.chapter_idx is not None:
            st["chapter_idx"] = req.chapter_idx
        result = NODE_FUNCS[stage](st)
        _patch(thread_id, run, result)
        run.events.put({"type": "edited", "keys": list(result.keys())})

    return _bg_op(run, _op)


# ---------- правка плана конкретной главы (структура) ----------

@app.post("/api/runs/{thread_id}/chapter/{idx}/revise")
def revise_chapter(thread_id: str, idx: int, req: ReviseReq):
    """Переписать план ОДНОЙ главы по фидбеку (ФОНОВО)."""
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    if idx < 0 or idx >= len(st.get("chapters") or []):
        raise HTTPException(400, "bad chapter index")

    def _op():
        from narrative import prompts  # noqa: PLC0415
        st2 = _state(thread_id)
        chapters = list(st2.get("chapters") or [])
        ch = chapters[idx]
        user = (
            f"Синопсис:\n{st2.get('synopsis','')}\n\n"
            f"Карточки:\n{st2.get('characters','')}\n\n"
            f"Глава {idx + 1}: {ch.title}\nТекущий план:\n{ch.plan}\n\n"
            f"Правка нарративщика: {req.feedback}\n\n"
            "Перепиши план ТОЛЬКО этой главы с учётом правки, сохранив "
            "непрерывность с остальными. Верни только новый текст плана, "
            "без преамбул."
        )
        ch.plan = nodes._structural().complete(
            prompts.with_genre(prompts.STRUCTURE, st2.get("genre"))
            + prompts.NAMES_RULE, user)
        chapters[idx] = ch
        _patch(thread_id, run, {"chapters": chapters})
        run.events.put({"type": "edited", "keys": ["chapters"]})

    return _bg_op(run, _op)


@app.post("/api/runs/{thread_id}/chapter/{idx}/rewrite_dialogue")
def rewrite_dialogue(thread_id: str, idx: int):
    """Правка плана → переписать диалоги (и адалт) главы (ФОНОВО)."""
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    if idx < 0 or idx >= len(st.get("chapters") or []):
        raise HTTPException(400, "bad chapter index")

    def _op():
        st2 = _state(thread_id)
        chapters = list(st2.get("chapters") or [])
        chapters[idx] = nodes.write_chapter(st2, chapters[idx], idx)
        _patch(thread_id, run, {"chapters": chapters})
        run.events.put({"type": "edited", "keys": ["chapters"]})

    return _bg_op(run, _op)


@app.post("/api/runs/{thread_id}/chapter/{idx}/sync_plan")
def sync_plan(thread_id: str, idx: int):
    """Правка диалогов → подогнать план главы под текст (ФОНОВО)."""
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    if idx < 0 or idx >= len(st.get("chapters") or []):
        raise HTTPException(400, "bad chapter index")

    def _op():
        st2 = _state(thread_id)
        chapters = list(st2.get("chapters") or [])
        ch = chapters[idx]
        ch.plan = nodes.sync_plan_from_dialogue(st2, ch, idx)
        chapters[idx] = ch
        _patch(thread_id, run, {"chapters": chapters})
        run.events.put({"type": "edited", "keys": ["chapters"]})

    return _bg_op(run, _op)


# ---------- адалт: адаптировать главу / отключить ----------

@app.post("/api/runs/{thread_id}/chapter/{idx}/adapt_adult")
def adapt_adult(thread_id: str, idx: int):
    """Глава без почвы для адалта → Бот 5 вплетает подводку, Бот 6 генерит
    сцену (ФОНОВО — два долгих LLM-вызова, grok)."""
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    if idx < 0 or idx >= len(st.get("chapters") or []):
        raise HTTPException(400, "bad chapter index")

    def _op():
        st2 = _state(thread_id)
        chapters = list(st2.get("chapters") or [])
        ch = chapters[idx]
        bridge = ch.adult_bridge_hint or (
            "добавь взаимное влечение и эротическое напряжение между подходящей "
            "парой персонажей и повод остаться наедине к финалу главы"
        )
        st2["chapter_idx"] = idx
        st2["revision_target"] = "dialogue"
        st2["revision_feedback"] = (
            f"Адаптируй главу под откровенную сцену: {bridge}. "
            "Сохрани сюжет и события главы, не переписывай всё — органично "
            "вплети подводку так, чтобы финал естественно переходил в адалт."
        )
        res_dlg = nodes.dialogue_node(st2)
        st2.update(res_dlg)
        st2["chapter_idx"] = idx
        res_adult = nodes.adult_node(st2)
        patch = {**res_dlg, **res_adult}
        patch["log"] = (res_dlg.get("log") or []) + (res_adult.get("log") or [])
        _patch(thread_id, run, patch)
        run.events.put({"type": "edited", "keys": ["chapters"]})

    return _bg_op(run, _op)


@app.post("/api/runs/{thread_id}/chapter/{idx}/skip_adult")
def skip_adult(thread_id: str, idx: int):
    """Оставить главу без адалта: снять адалт-точку и блокировку."""
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    chapters = list(st.get("chapters") or [])
    if idx < 0 or idx >= len(chapters):
        raise HTTPException(400, "bad chapter index")
    chapters[idx].is_adult_point = False
    chapters[idx].adult_block_reason = None
    chapters[idx].adult_bridge_hint = None
    _patch(thread_id, run, {"chapters": chapters})
    run.events.put({"type": "edited", "keys": ["chapters"]})
    return {"ok": True}


# ---------- структура порциями ----------

class CountReq(BaseModel):
    count: int


@app.post("/api/runs/{thread_id}/chapter_count")
def set_chapter_count(thread_id: str, req: CountReq):
    """Утвердить число глав (апрув предложения ИИ или своё) → продолжить к структуре."""
    run = _get_run(thread_id)
    _busy(run)
    n = max(2, min(int(req.count), 30))
    _patch(thread_id, run, {"target_chapters": n})
    return resume_run(thread_id)


@app.post("/api/runs/{thread_id}/restructure")
def restructure(thread_id: str, req: CountReq):
    """#7: пересобрать структуру под НОВОЕ число глав (растянуть/сжать сюжет,
    не обрезать). Регенерит поглавный план — написанные тексты сбрасываются."""
    run = _get_run(thread_id)
    _busy(run)
    n = max(2, min(int(req.count), 30))

    def _op():
        st = _state(thread_id)
        st["target_chapters"] = n
        result = nodes.structure_node(st)  # учитывает текущий план как опору
        _patch(thread_id, run, result)
        run.events.put({"type": "edited", "keys": list(result.keys())})

    return _bg_op(run, _op)


class AddChapterReq(BaseModel):
    after_idx: int = -1   # вставить ПОСЛЕ этого индекса; -1 → в начало


@app.post("/api/runs/{thread_id}/chapter/add")
def add_chapter(thread_id: str, req: AddChapterReq):
    """#7: добавить ОДНУ главу на лету (ИИ генерит план, связывает соседей)."""
    run = _get_run(thread_id)
    _busy(run)

    def _op():
        st = _state(thread_id)
        chapters = list(st.get("chapters") or [])
        after = max(-1, min(req.after_idx, len(chapters) - 1))
        new_ch = nodes.gen_inserted_chapter(st, after)
        chapters.insert(after + 1, new_ch)
        for i, ch in enumerate(chapters):
            ch.index = i
        _patch(thread_id, run, {"chapters": chapters})
        run.events.put({"type": "edited", "keys": ["chapters"]})

    return _bg_op(run, _op)


@app.delete("/api/runs/{thread_id}/chapter/{idx}")
def delete_chapter(thread_id: str, idx: int):
    """#7: удалить главу и переиндексировать остальные."""
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    chapters = list(st.get("chapters") or [])
    if idx < 0 or idx >= len(chapters):
        raise HTTPException(400, "bad chapter index")
    if len(chapters) <= 1:
        raise HTTPException(400, "нельзя удалить последнюю главу")
    chapters.pop(idx)
    for i, ch in enumerate(chapters):
        ch.index = i
    cur = st.get("chapter_idx", 0)
    patch = {"chapters": chapters}
    if cur >= len(chapters):
        patch["chapter_idx"] = len(chapters) - 1
    _patch(thread_id, run, patch)
    run.events.put({"type": "edited", "keys": ["chapters"]})
    return {"ok": True, "chapters": len(chapters)}


# ---------- решения по findings редактора ----------

def _find_finding(st: dict, fid: str):
    for r in st.get("editor_reports") or []:
        for f in r.findings:
            if f.id == fid:
                return f
    return None


# Инструкция «без markdown»: чат-ответы ИИ должны быть простым текстом.
NO_MARKDOWN = (
    " ВАЖНО: отвечай ПРОСТЫМ текстом без markdown-разметки — не используй "
    "звёздочки (* **), решётки (#), обратные кавычки/блоки кода (```), "
    "маркированные/нумерованные списки и таблицы. Только обычные абзацы."
)


def _last_report(st: dict, idx: int):
    """Последний отчёт редактора по главе idx (с наложенными решениями) или None."""
    decisions = st.get("finding_decisions") or {}
    reports = [r for r in (st.get("editor_reports") or [])
               if r.chapter_index == idx]
    if not reports:
        return None
    return apply_decisions(reports[-1], decisions)


@app.post("/api/runs/{thread_id}/findings/{fid}")
def decide_finding(thread_id: str, fid: str, req: FindingReq):
    run = _get_run(thread_id)
    st = _state(thread_id)
    decisions = dict(st.get("finding_decisions") or {})
    d = dict(decisions.get(fid, {}))
    if req.comment is not None:
        d["comment"] = req.comment
    if req.status:
        d["status"] = req.status
    if req.judge:  # ИИ сам решает — strict JSON, вердикт с обоснованием
        f = _find_finding(st, fid)
        if f is not None:
            verdict = nodes._editor().structured(
                "Ты — главный редактор визуальной новеллы. Реши, СПРАВЕДЛИВО ли "
                "замечание и нужно ли его исправлять. Учитывай комментарий "
                "нарративщика, если он есть.",
                f"Замечание [{f.severity}/{f.block}]: {f.problem}\n"
                f"Фрагмент: «{f.quote}»\n"
                f"Комментарий нарративщика: {d.get('comment','') or '—'}",
                JudgeOut,
            )
            d["status"] = "rejected" if verdict.decision == "reject" else "accepted"
            d["judge_reason"] = verdict.reason
            d["judged"] = True
    decisions[fid] = d
    patch: dict[str, Any] = {"finding_decisions": decisions}
    # #8: отклонённое — запоминаем текст, чтобы редактор больше не поднимал
    if d.get("status") == "rejected":
        f = _find_finding(st, fid)
        if f is not None:
            notes = list(st.get("rejected_notes") or [])
            note = f.problem.strip()
            if note and note not in notes:
                notes.append(note)
                patch["rejected_notes"] = notes
    _patch(thread_id, run, patch)
    run.events.put({"type": "finding", "id": fid, "decision": d})
    return {"ok": True, "decision": d, "state": _serialize(_state(thread_id))}


# ---------- чат с ИИ-редактором (#1) ----------

@app.post("/api/runs/{thread_id}/chat")
def chat(thread_id: str, req: ChatReq):
    """Обсудить правку с ИИ: контекст — глава целиком или конкретное замечание.

    Не меняет state — только разговор. Применение правок нарративщик делает
    обычными инструментами (правка текста / «попросить ИИ» / принять finding).
    """
    _get_run(thread_id)
    st = _state(thread_id)
    chs = list(st.get("chapters") or [])

    ctx_parts = [f"Карточки персонажей:\n{st.get('characters', '')}"]
    if st.get("locations"):
        ctx_parts.append(f"Локации:\n{st.get('locations')}")
    if st.get("synopsis"):
        ctx_parts.append(f"Синопсис:\n{st.get('synopsis')}")

    # #4: общий обзор всех глав — чтобы чат понимал вопросы про соседние/
    # следующие главы и другие этапы, а не падал/терялся вне одной главы.
    if chs:
        overview = "\n".join(
            f"{c.index + 1}. {c.title}"
            f"{' [адалт]' if c.is_adult_point else ''}"
            f"{' — написана' if c.dialogue else ' — не написана'}"
            for c in chs
        )
        ctx_parts.append(f"Все главы (обзор):\n{overview}")

    if req.chapter_idx is not None and 0 <= req.chapter_idx < len(chs):
        ch = chs[req.chapter_idx]
        ctx_parts.append(
            f"ТЕКУЩАЯ глава {req.chapter_idx + 1} «{ch.title}»\n"
            f"План:\n{ch.plan}\n\nТекст:\n{ch.dialogue or '(не написан)'}"
        )
        last = _last_report(st, req.chapter_idx)
        if last and last.findings:
            joined = "\n".join(
                f"- [{f.severity}/{f.block}] {f.locator}: {f.problem}"
                f"{' (ОТКЛОНЕНО нарративщиком)' if f.status == 'rejected' else ''}"
                for f in last.findings
            )
            ctx_parts.append(
                "Замечания редактора по этой главе (текущая ревизия):\n" + joined
            )
    if req.finding_id:
        f = _find_finding(st, req.finding_id)
        if f is not None:
            ctx_parts.append(
                f"Обсуждаемое замечание редактора [{f.severity}/{f.block}]:\n"
                f"{f.problem}\nФрагмент: «{f.quote}»"
            )

    system = (
        "Ты — опытный редактор визуальных новелл 18+, собеседник нарративщика. "
        "Обсуждай по делу любой этап работы: главы, синопсис, персонажей, "
        "локации, структуру. Предлагай конкретные варианты формулировок и "
        "фиксов, объясняй коротко. Не переписывай главу целиком без просьбы. "
        "Отвечай на языке нарративщика." + NO_MARKDOWN
        + "\n\nКОНТЕКСТ:\n" + "\n\n".join(ctx_parts)
    )
    history = [
        {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
        for m in req.messages[-16:]  # окно диалога
        if m.get("role") in ("user", "assistant")
    ]
    try:
        reply = nodes._editor(st, "chat").chat(system, history)
    except LLMLimitError as exc:  # #5: лимит → 429 + инфо для баннера
        raise HTTPException(429, _limit_detail(exc))
    except Exception as exc:  # noqa: BLE001 — LLM/провайдер недоступен
        raise HTTPException(502, f"чат недоступен (LLM): {str(exc)[:160]}")
    return {"reply": reply}


def _limit_detail(exc: LLMLimitError) -> dict:
    return {"error": "limit", "provider": exc.provider,
            "reset_at": exc.reset_at, "kind": exc.kind,
            "message": str(exc)[:200]}


@app.post("/api/runs/{thread_id}/chapter/{idx}/apply_chat")
def apply_chat(thread_id: str, idx: int, req: ChatReq):
    """Применить обсуждённое в чате к главе целиком (или не трогать).

    ИИ читает всю историю чата + главу и решает: внести согласованные правки
    или оставить как есть. Адалт-глава переписывается uncensored-моделью.
    """
    from narrative.state import ChatApplyOut  # noqa: PLC0415
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    chapters = list(st.get("chapters") or [])
    if idx < 0 or idx >= len(chapters):
        raise HTTPException(400, "bad chapter index")
    ch = chapters[idx]
    convo = "\n".join(
        f"{'Нарративщик' if m.get('role') == 'user' else 'ИИ'}: {m.get('content','')}"
        for m in req.messages if m.get("role") in ("user", "assistant")
    )
    system = (
        "Ты — редактор-исполнитель визуальной новеллы 18+. На основе ОБСУЖДЕНИЯ "
        "с нарративщиком внеси в главу согласованные правки. Если из разговора "
        "НЕ следуют конкретные изменения — верни changed=false и не трогай текст. "
        "Сохрани формат ВН, теги статиков/анимаций, откровенность адалта.\n\n"
        f"Карточки:\n{st.get('characters','')}"
    )
    user = (
        f"ОБСУЖДЕНИЕ:\n{convo}\n\nТЕКУЩАЯ ГЛАВА {idx + 1} «{ch.title}»:\n"
        f"{ch.dialogue or ch.plan}\n\nПримени решения обсуждения к главе."
    )
    llm = nodes._adult() if ch.is_adult_point else nodes._structural(st, "dialogue")
    try:
        out = llm.structured(system, user, ChatApplyOut, temperature=0.6)
    except LLMLimitError as exc:
        raise HTTPException(429, _limit_detail(exc))
    except Exception as exc:
        raise HTTPException(502, f"не удалось применить (LLM): {str(exc)[:160]}")
    if out.changed and out.script.strip():
        ch.dialogue = out.script
        chapters[idx] = ch
        _patch(thread_id, run, {"chapters": chapters})
        run.events.put({"type": "edited", "keys": ["chapters"]})
    return {"changed": out.changed, "note": out.note}


# ---------- цикл ревизий главы: применить правки редактора + перепроверить ----

class RevisionReq(BaseModel):
    """Применение одной ревизии главы: чат по правкам учитывается при фиксе."""
    messages: list[dict] = []  # обсуждение этой ревизии (опционально)


def _open_findings(report) -> list:
    """Не отклонённые findings (их и исправляем)."""
    return [f for f in report.findings if f.status != "rejected"]


def _revision_worker(run: Run, idx: int, to_fix: list, convo: str):
    """ФОНОВО: точечная правка главы + перепроверка. LLM-правка адалта долгая
    (grok 30-150с), поэтому НЕ держим HTTP-запрос (иначе Next-прокси рвёт по
    таймауту → 500). Фронт поллит status: running → paused/error."""
    thread_id = run.thread_id
    run.status = "running"
    run.error = ""
    run.events.put({"type": "status", "status": "running"})
    try:
        st = _state(thread_id)
        chapters = list(st.get("chapters") or [])
        if to_fix or convo.strip():
            # 1-2) ТОЧЕЧНАЯ правка (не перегенерация)
            st["chapter_idx"] = idx
            ch = nodes.patch_chapter(st, chapters[idx], idx, to_fix, extra=convo)
            chapters[idx] = ch
            _patch(thread_id, run, {"chapters": chapters})
        # 3) перепроверить → новый раунд редактора
        st2 = _state(thread_id)
        st2["chapter_idx"] = idx
        res = nodes.editor_node(st2)
        _patch(thread_id, run, res)
        run.status = "paused"
        run.events.put({"type": "edited",
                        "keys": ["chapters", "editor_reports"]})
        run.events.put({"type": "status", "status": "paused"})
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.error = f"Не удалось применить правки (LLM/провайдер): {str(exc)[:200]}"
        run.events.put({"type": "status", "status": "error", "error": run.error})


@app.post("/api/runs/{thread_id}/chapter/{idx}/apply_revision")
def apply_revision(thread_id: str, idx: int, req: RevisionReq):
    """Запускает один шаг цикла правок главы В ФОНЕ и сразу отвечает.

    Шаг: собрать НЕотклонённые замечания + обсуждение из чата → точечно
    исправить главу → редактор перепроверяет (новый раунд). Фронт поллит
    state до status=paused. Нарративщик жмёт, пока замечания не закроются.
    """
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    chapters = list(st.get("chapters") or [])
    if idx < 0 or idx >= len(chapters):
        raise HTTPException(400, "bad chapter index")
    last = _last_report(st, idx)
    if last is None:
        raise HTTPException(400, "по этой главе ещё нет отчёта редактора")
    to_fix = _open_findings(last)
    convo = "\n".join(
        f"{'Нарративщик' if m.get('role') == 'user' else 'ИИ'}: "
        f"{m.get('content', '')}"
        for m in (req.messages or []) if m.get("role") in ("user", "assistant")
    )
    run.worker = threading.Thread(
        target=_revision_worker, args=(run, idx, to_fix, convo), daemon=True)
    run.worker.start()
    return {"ok": True, "started": True}


# ---------- откат на этап ----------

@app.post("/api/runs/{thread_id}/rollback")
def rollback(thread_id: str, req: RollbackReq):
    run = _get_run(thread_id)
    _busy(run)
    if req.stage not in ROLLBACK:
        raise HTTPException(400, f"rollback to {req.stage} не поддержан")
    as_node, clear = ROLLBACK[req.stage]
    patch: dict[str, Any] = {k: None for k in clear}
    if req.stage == "dialogue":
        # перезапуск написания: чистим тексты глав, структуру оставляем
        st = _state(thread_id)
        chs = list(st.get("chapters") or [])
        for c in chs:
            c.dialogue = c.adult_scene = c.translation = None
            c.statics, c.anims = [], []
            c.adult_statics, c.adult_anims = [], []
        # фаза заново «content»: сначала весь контент, редактор потом
        patch = {"chapters": chs, "chapter_idx": 0, "phase": "content"}
    elif req.stage in ("structure", "characters", "synopsis"):
        patch["chapters"] = []  # очистить главы (не-reducer)
    if req.stage in ("structure", "characters", "synopsis"):
        patch["structure_done"] = False
        patch["phase"] = None
    _patch(thread_id, run, patch, as_node=as_node)
    # откат снимает «done/error» — встаём на паузу на откатанном этапе, чтобы в
    # UI появилась кнопка «Продолжить», а не висел финальный экран «Проект готов».
    run.status = "paused"
    run.error = ""
    run.events.put({"type": "rollback", "stage": req.stage})
    run.events.put({"type": "status", "status": "paused"})
    return {"ok": True, "next": req.stage}


# ---------- SSE + health ----------

@app.get("/api/runs/{thread_id}/events")
async def events(thread_id: str):
    run = _get_run(thread_id)

    async def gen():
        snap = _graph(run).get_state(_config(thread_id))
        yield _sse({"type": "state", "state": _serialize(snap.values or {})})
        yield _sse({"type": "status", "status": run.status, "next": list(snap.next)})
        while True:
            try:
                yield _sse(run.events.get_nowait())
            except queue.Empty:
                await asyncio.sleep(0.2)
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- экспорт готового проекта (скачиваемый текст) ----------

@app.get("/api/runs/{thread_id}/export")
def export_project(thread_id: str, fmt: str = "txt"):
    """Собирает готовую новеллу в один документ для скачивания.

    fmt=txt — чистый текст; fmt=md — с заголовками. Включает план (кратко),
    основной текст главы (диалоги+адалт) и перевод, если он есть.
    """
    st = _state(thread_id)
    chapters = list(st.get("chapters") or [])
    theme = st.get("theme") or ""
    md = fmt == "md"

    parts: list[str] = []
    title = "Визуальная новелла 18+"
    parts.append(f"# {title}" if md else title.upper())
    if theme:
        parts.append(("> " if md else "Тема: ") + theme)
    parts.append("")

    for ch in chapters:
        head = f"Глава {ch.index + 1}. {ch.title}"
        parts.append(f"\n## {head}" if md else f"\n\n=== {head} ===")
        body = ch.dialogue or ch.plan or "(глава ещё не написана)"
        parts.append(body)
        if ch.translation:
            parts.append(("\n### Перевод" if md else "\n--- Перевод ---"))
            parts.append(ch.translation)

    text = "\n".join(parts).strip() + "\n"
    safe = "novella"
    fname = f"{safe}.{ 'md' if md else 'txt'}"
    return {"filename": fname, "text": text, "chapters": len(chapters)}


# ---------- Claude по подписке (#4 расширение): статус / тумблер / токен ----------

def _claude_login_status() -> dict:
    """Авторизован ли Claude (подписка). Источник: env-токен или credentials.json."""
    import json
    import os as _os
    import time
    if _os.environ.get("ANTHROPIC_API_KEY"):
        return {"authorized": True, "via": "api_key",
                "warn": "ANTHROPIC_API_KEY перебивает подписку — это платный API"}
    if _os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return {"authorized": True, "via": "oauth_token"}
    path = _os.path.expanduser("~/.claude/.credentials.json")
    try:
        with open(path) as fh:
            o = json.load(fh)
        o = o.get("claudeAiOauth") or o
        exp = o.get("expiresAt")
        valid = True
        when = None
        if exp:
            secs = exp / 1000 if exp > 1e12 else exp
            valid = secs > time.time()
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(secs))
        return {"authorized": valid, "via": "credentials",
                "expires": when, "sub": o.get("subscriptionType")}
    except Exception:
        return {"authorized": False, "via": None}


@app.get("/api/claude/status")
def claude_status():
    import os as _os
    st = _claude_login_status()
    st["enabled"] = _os.environ.get("USE_CLAUDE_SUBSCRIPTION") == "1"
    st["model"] = _os.environ.get("CLAUDE_SUB_MODEL", "sonnet")
    return st


class SubReq(BaseModel):
    enabled: bool
    model: Optional[str] = None


@app.post("/api/claude/subscription")
def claude_subscription(req: SubReq):
    """Тумблер: все НЕ-адалт боты через подписку Claude + выбор модели."""
    import os as _os
    _os.environ["USE_CLAUDE_SUBSCRIPTION"] = "1" if req.enabled else "0"
    if req.model:
        _os.environ["CLAUDE_SUB_MODEL"] = req.model
    return claude_status()


class TokenReq(BaseModel):
    token: str


@app.post("/api/claude/token")
def claude_token(req: TokenReq):
    """Вставить долгоживущий токен из `claude setup-token` (необязательно —
    если есть валидный логин Claude Code, токен не нужен). Пишем в процесс."""
    import os as _os
    tok = req.token.strip()
    if not tok:
        raise HTTPException(400, "пустой токен")
    _os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return claude_status()


# ---------- OAuth-подключение подписки прямо из веба ----------
# Тот же флоу, что `claude setup-token`: открыть authorize-страницу в новой
# вкладке → пользователь логинится → копирует выданный code → мы меняем его на
# токен (PKCE). Публичный client_id Claude Code.

_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CLAUDE_REDIRECT = "https://console.anthropic.com/oauth/code/callback"
_CLAUDE_SCOPE = "org:create_api_key user:profile user:inference"
_PKCE: dict[str, str] = {}  # verifier последнего запроса (один за раз)


@app.get("/api/claude/auth_url")
def claude_auth_url():
    """Сгенерировать ссылку авторизации (PKCE) — для подключения или смены
    аккаунта. Возвращаем всегда (даже если уже залогинен — чтобы можно было
    переподключить другой аккаунт)."""
    import base64
    import hashlib
    import secrets
    from urllib.parse import urlencode
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _PKCE["v"] = verifier
    params = {
        "code": "true", "client_id": _CLAUDE_CLIENT_ID,
        "response_type": "code", "redirect_uri": _CLAUDE_REDIRECT,
        "scope": _CLAUDE_SCOPE, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": verifier,
    }
    return {"authorized": False,
            "url": "https://claude.ai/oauth/authorize?" + urlencode(params)}


class CodeReq(BaseModel):
    code: str


@app.post("/api/claude/exchange")
def claude_exchange(req: CodeReq):
    """Обменять выданный code (формат code#state) на токен подписки."""
    import httpx
    raw = req.code.strip()
    if not raw:
        raise HTTPException(400, "пустой код")
    code, _, state = raw.partition("#")
    verifier = _PKCE.get("v")
    if not verifier:
        raise HTTPException(400, "сначала открой ссылку авторизации")
    try:
        r = httpx.post(
            "https://console.anthropic.com/v1/oauth/token",
            json={
                "grant_type": "authorization_code", "code": code,
                "state": state or verifier, "client_id": _CLAUDE_CLIENT_ID,
                "redirect_uri": _CLAUDE_REDIRECT, "code_verifier": verifier,
            }, timeout=30)
    except Exception as exc:
        raise HTTPException(502, f"сеть/обмен не удался: {str(exc)[:140]}")
    if r.status_code != 200:
        raise HTTPException(502, f"обмен не удался: {r.status_code} "
                            f"{r.text[:140]}")
    tok = (r.json() or {}).get("access_token")
    if not tok:
        raise HTTPException(502, "в ответе нет access_token")
    import os as _os
    _os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    _PKCE.pop("v", None)
    return claude_status()
