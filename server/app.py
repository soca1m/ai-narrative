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
from narrative.routing import apply_decisions
from narrative.state import Chapter

load_dotenv()
app = FastAPI(title="ai-narrative API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
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
    "structure": nodes.structure_node,
    "dialogue": nodes.dialogue_node,
    "adult": nodes.adult_node,
    "editor": nodes.editor_node,
    "translation": nodes.translation_node,
}

# Откат: какой узел «проиграть как предыдущий», чтобы next встал на нужный этап,
# и какие (не-reducer) поля очистить.
ROLLBACK = {
    "synopsis":   ("logline",    ["synopsis", "characters", "chapters", "chapter_idx", "structure_done"]),
    "characters": ("synopsis",   ["characters", "chapters", "chapter_idx", "structure_done"]),
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


RUNS: dict[str, Run] = {}


def _graph(run: Run):
    return _GRAPH_STEP if run.step_mode else _GRAPH_AUTO


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _get_run(thread_id: str) -> Run:
    run = RUNS.get(thread_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


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


def _run_worker(run: Run, graph, stream_input):
    cfg = _config(run.thread_id)
    run.status = "running"
    run.events.put({"type": "status", "status": "running"})
    try:
        for event in graph.stream(stream_input, cfg, stream_mode="values"):
            log = event.get("log", [])
            for line in log[run.printed:]:
                run.events.put({"type": "log", "line": line})
            run.printed = len(log)
            run.events.put({"type": "state", "state": _serialize(event)})

        snap = graph.get_state(cfg)
        if snap.next:
            run.status = "paused"
            run.events.put({"type": "status", "status": "paused", "next": list(snap.next)})
        else:
            run.status = "done"
            run.events.put({"type": "status", "status": "done"})
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
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


class PatchReq(BaseModel):
    patch: dict[str, Any]


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
    }
    run.worker = threading.Thread(
        target=_run_worker, args=(run, _graph(run), init), daemon=True)
    run.worker.start()
    return {"thread_id": thread_id, "step_mode": req.step_mode}


@app.get("/api/runs/{thread_id}/state")
def get_state(thread_id: str):
    run = _get_run(thread_id)
    snap = _graph(run).get_state(_config(thread_id))
    return {"status": run.status, "next": list(snap.next), "state": _serialize(snap.values or {})}


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
    run.worker = threading.Thread(
        target=_run_worker, args=(run, _graph(run), None), daemon=True)
    run.worker.start()
    return {"ok": True}


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
    run = _get_run(thread_id)
    _busy(run)
    if stage not in NODE_FUNCS:
        raise HTTPException(400, f"unknown stage {stage}")
    st = _state(thread_id)
    st["revision_feedback"] = req.feedback
    if stage in ("dialogue", "adult"):
        st["revision_target"] = stage
    if req.chapter_idx is not None:
        st["chapter_idx"] = req.chapter_idx
    result = NODE_FUNCS[stage](st)            # чистый вызов узла
    _patch(thread_id, run, result)            # merge артефакта в чекпоинт
    run.events.put({"type": "edited", "keys": list(result.keys())})
    return {"ok": True, "state": _serialize(_state(thread_id))}


# ---------- правка плана конкретной главы (структура) ----------

@app.post("/api/runs/{thread_id}/chapter/{idx}/revise")
def revise_chapter(thread_id: str, idx: int, req: ReviseReq):
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    chapters = list(st.get("chapters") or [])
    if idx < 0 or idx >= len(chapters):
        raise HTTPException(400, "bad chapter index")
    ch = chapters[idx]
    user = (
        f"Синопсис:\n{st.get('synopsis','')}\n\nКарточки:\n{st.get('characters','')}\n\n"
        f"Глава {idx + 1}: {ch.title}\nТекущий план:\n{ch.plan}\n\n"
        f"Правка нарративщика: {req.feedback}\n\n"
        "Перепиши план ТОЛЬКО этой главы с учётом правки, сохранив непрерывность "
        "с остальными. Верни только новый текст плана главы, без преамбул."
    )
    from narrative import prompts  # noqa: PLC0415
    new_plan = nodes._structural().complete(
        prompts.with_genre(prompts.STRUCTURE, st.get("genre")) + prompts.NAMES_RULE, user)
    ch.plan = new_plan
    chapters[idx] = ch
    _patch(thread_id, run, {"chapters": chapters})
    run.events.put({"type": "edited", "keys": ["chapters"]})
    return {"ok": True, "state": _serialize(_state(thread_id))}


# ---------- адалт: адаптировать главу / отключить ----------

@app.post("/api/runs/{thread_id}/chapter/{idx}/adapt_adult")
def adapt_adult(thread_id: str, idx: int):
    """Глава без почвы для адалта → Бот 5 вплетает подводку, Бот 6 генерит сцену."""
    run = _get_run(thread_id)
    _busy(run)
    st = _state(thread_id)
    chapters = list(st.get("chapters") or [])
    if idx < 0 or idx >= len(chapters):
        raise HTTPException(400, "bad chapter index")
    ch = chapters[idx]
    bridge = ch.adult_bridge_hint or (
        "добавь взаимное влечение и эротическое напряжение между подходящей парой "
        "персонажей и повод остаться наедине к финалу главы"
    )
    # 1) Бот 5 точечно адаптирует главу под сцену
    st["chapter_idx"] = idx
    st["revision_target"] = "dialogue"
    st["revision_feedback"] = (
        f"Адаптируй главу под откровенную сцену: {bridge}. "
        "Сохрани сюжет и события главы, не переписывай всё — органично вплети "
        "подводку так, чтобы финал главы естественно переходил в адалт-сцену."
    )
    res_dlg = nodes.dialogue_node(st)
    st.update(res_dlg)
    st["chapter_idx"] = idx
    # 2) Бот 6 генерит сцену по адаптированной главе (пре-чек пройдёт заново)
    res_adult = nodes.adult_node(st)
    patch = {**res_dlg, **res_adult}
    patch["log"] = (res_dlg.get("log") or []) + (res_adult.get("log") or [])
    _patch(thread_id, run, patch)
    run.events.put({"type": "edited", "keys": ["chapters"]})
    return {"ok": True, "state": _serialize(_state(thread_id))}


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

@app.post("/api/runs/{thread_id}/structure/more")
def structure_more(thread_id: str):
    """Сгенерировать следующую порцию глав (resume с structure_action=more)."""
    run = _get_run(thread_id)
    _busy(run)
    _patch(thread_id, run, {"structure_done": False, "structure_action": "more"})
    return resume_run(thread_id)


@app.post("/api/runs/{thread_id}/structure/proceed")
def structure_proceed(thread_id: str):
    """Хватит структуры — перейти к написанию глав (resume с proceed)."""
    run = _get_run(thread_id)
    _busy(run)
    _patch(thread_id, run, {"structure_action": "proceed"})
    return resume_run(thread_id)


# ---------- решения по findings редактора ----------

def _find_finding(st: dict, fid: str):
    for r in st.get("editor_reports") or []:
        for f in r.findings:
            if f.id == fid:
                return f
    return None


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
    if req.judge:  # ИИ сам решает принять/отклонить по тексту + комментарию
        f = _find_finding(st, fid)
        if f is not None:
            from narrative import prompts  # noqa: PLC0415
            verdict = nodes._editor().complete(
                "Ты — редактор. Реши, нужно ли исправлять замечание. "
                "Ответь СТРОГО одним словом: accept (исправлять) или reject (можно оставить).",
                f"Замечание [{f.severity}/{f.block}]: {f.problem}\n"
                f"Фрагмент: {f.quote}\n"
                f"Комментарий нарративщика: {d.get('comment','') or '—'}",
            ).strip().lower()
            d["status"] = "rejected" if "reject" in verdict else "accepted"
            d["judged"] = True
    decisions[fid] = d
    _patch(thread_id, run, {"finding_decisions": decisions})
    run.events.put({"type": "finding", "id": fid, "decision": d})
    return {"ok": True, "decision": d, "state": _serialize(_state(thread_id))}


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
        patch = {"chapters": chs, "chapter_idx": 0}
    elif req.stage in ("structure", "characters", "synopsis"):
        patch["chapters"] = []  # очистить главы (не-reducer)
    if req.stage in ("structure", "characters", "synopsis"):
        patch["structure_done"] = False
    _patch(thread_id, run, patch, as_node=as_node)
    run.events.put({"type": "rollback", "stage": req.stage})
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
