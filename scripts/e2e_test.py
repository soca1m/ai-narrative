"""End-to-end тесты пайплайна на МОК-LLM (без сети, детерминированно).

Покрывает:
  A. Граф целиком (auto) — поглавный цикл: глава пишется → редактор →
     цикл правок → следующая глава → перевод → END.
  B. HTTP-эндпоинты (FastAPI TestClient) с реальным worker'ом и step-mode:
     старт → апрув числа глав → поглавный gate → apply_revision → export.

Запуск:
    .venv/bin/python scripts/e2e_test.py
Код возврата 0 — все тесты прошли.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # чтобы импортировался пакет server
os.environ.pop("USE_CLAUDE_SUBSCRIPTION", None)  # тесты не ходят в подписку

from narrative import nodes  # noqa: E402
from narrative.state import (  # noqa: E402
    AdultFeasibility, AdultSceneOut, ChapterCountOut, ChatApplyOut,
    DialogueOut, EditorReportOut, FindingOut, JudgeOut, ChapterPlan,
    StructureFixOut, StructurePlan,
)

N_CHAPTERS = 3


class FakeLLM:
    """Детерминированный LLM-дублёр: structured отдаёт валидную схему.

    Цикл правок: глава-черновик помечается [DRAFT] → редактор даёт critical;
    после переписи по фидбеку — [FIXED] → редактор чист. Так цикл сходится.
    """

    def __init__(self, *_a, **_k):
        class _Cfg:
            temperature = 0.7
            max_tokens = 0
        self.cfg = _Cfg()

    def complete(self, system, user, temperature=None):
        s = (system or "").lower()
        if "логлайн" in s:  # Бот 1 → нумерованный список для парсинга вариантов
            return ("1. Тайна закрытого клуба.\n2. Игра на власть.\n"
                    "3. Лето соблазна.")
        return 'ИМЯ: "реплика"\n(действие)'

    def chat(self, system, messages, temperature=None):
        return "Ответ редактора простым текстом без разметки."

    def structured(self, system, user, schema, temperature=0.2):
        name = schema.__name__
        if name == "ChapterCountOut":
            return ChapterCountOut(count=N_CHAPTERS, reason="оптимально")
        if name == "StructurePlan":
            chs = [
                ChapterPlan(title=f"Глава {i + 1}", plan=f"План главы {i + 1}.",
                            is_adult=(i % 2 == 0), adult_note="A с B")
                for i in range(N_CHAPTERS)
            ]
            return StructurePlan(chapters=chs, story_complete=True)
        if name == "StructureFixOut":
            chs = [
                ChapterPlan(title=f"Глава {i + 1}", plan=f"План главы {i + 1}.",
                            is_adult=(i % 2 == 0), adult_note="A с B")
                for i in range(N_CHAPTERS)
            ]
            return StructureFixOut(chapters=chs, fixes=[])
        if name == "DialogueOut":
            fixed = ("Редактор нашёл" in user or "[FIXED]" in user
                     or "ЗАМЕЧАНИЯ РЕДАКТОРА" in user)
            tag = "[FIXED]" if fixed else "[DRAFT]"
            return DialogueOut(script=f'ИМЯ: "реплика"\n{tag}',
                               statics=[], anims=[])
        if name == "AdultSceneOut":
            return AdultSceneOut(refused=False, reason="", scene="сцена 18+",
                                 statics=[], anims=[])
        if name == "AdultFeasibility":
            return AdultFeasibility(feasible=True, reason="ок", pair="A и B",
                                    bridge="")
        if name == "EditorReportOut":
            clean = "[FIXED]" in user
            findings = [] if clean else [FindingOut(
                severity="critical", block="style", responsible_node="dialogue",
                locator="Глава, реплика 1", quote="", problem="тест-замечание")]
            return EditorReportOut(chapter_index=0, findings=findings,
                                   markdown="отчёт")
        if name == "ChatApplyOut":
            return ChatApplyOut(changed=False, note="нечего менять", script="")
        if name == "JudgeOut":
            return JudgeOut(decision="reject", reason="несущественно")
        raise AssertionError(f"FakeLLM: неизвестная схема {name}")


def _patch_llms():
    """Подменяем фабрики LLM в nodes на фейк (вызываются в рантайме узлов)."""
    nodes._structural = lambda *a, **k: FakeLLM()
    nodes._editor = lambda *a, **k: FakeLLM()
    nodes._adult = lambda *a, **k: FakeLLM()


# ----------------------------- тест A: граф -----------------------------

def test_graph_flow() -> None:
    from langgraph.checkpoint.memory import MemorySaver
    from narrative.graph import build_graph

    g = build_graph(MemorySaver())  # auto-режим, без пауз
    cfg = {"configurable": {"thread_id": "e2e-graph"}}
    init = {
        "theme": "тест",
        "target_language": "English",
        "translation_enabled": True,
        "target_chapters": N_CHAPTERS,
    }
    final = None
    steps = 0
    for ev in g.stream(init, cfg, stream_mode="values"):
        final = ev
        steps += 1
        assert steps < 200, "граф не сходится (петля)"

    assert final is not None
    chs = final["chapters"]
    assert len(chs) == N_CHAPTERS, f"глав {len(chs)} != {N_CHAPTERS}"
    assert all(c.dialogue for c in chs), "не все главы написаны"
    assert all("[FIXED]" in c.dialogue for c in chs), \
        "не все главы прошли цикл правок до чистого состояния"
    # Бот 8: переводы кладутся в c.translations {code->текст} (Google Translate).
    # Сеть может быть недоступна в CI → проверяем мягко: либо есть переводы,
    # либо словарь пуст (фолбэк при сбое сети), но поле существует.
    assert all(isinstance(c.translations, dict) for c in chs), "нет поля translations"

    # на каждую главу — минимум 2 раунда редактора (черновик+правка)
    reports = final["editor_reports"]
    per = {}
    for r in reports:
        per[r.chapter_index] = per.get(r.chapter_index, 0) + 1
    assert all(per.get(i, 0) >= 2 for i in range(N_CHAPTERS)), \
        f"цикл правок не отработал на каждой главе: {per}"
    print(f"  A: граф ОК — {N_CHAPTERS} глав, "
          f"раунды редактора {dict(sorted(per.items()))}, шагов {steps}")


# --------------------------- тест B: эндпоинты --------------------------

def _wait_not_running(client, tid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/runs/{tid}/state").json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("worker завис в running")


def test_endpoints_flow() -> None:
    from fastapi.testclient import TestClient
    from server import app as appmod

    client = TestClient(appmod.app)
    tid = "e2e-http-" + str(int(time.time()))

    # форсим уникальный thread_id (start_run генерит свой — перехватим ответ)
    r = client.post("/api/runs", json={
        "theme": "тест", "target_language": "English",
        "translation_enabled": False, "step_mode": True,
    })
    assert r.status_code == 200, r.text
    tid = r.json()["thread_id"]

    applied_revision = False
    exported = False
    for _ in range(80):
        st = _wait_not_running(client, tid)
        status = st["status"]
        state = st["state"]
        if status == "done":
            break
        if status == "error":
            raise AssertionError(f"worker error: {st.get('error')}")
        assert status == "paused", f"неожиданный статус {status}"

        chapters = state.get("chapters") or []
        suggested = state.get("suggested_chapters") or 0
        target = state.get("target_chapters") or 0

        # этап «Объём»: апрувим число глав
        if suggested and not chapters and not target:
            rr = client.post(f"/api/runs/{tid}/chapter_count",
                             json={"count": N_CHAPTERS})
            assert rr.status_code == 200, rr.text
            continue

        # поглавный gate: у текущей главы есть отчёт с открытыми замечаниями
        idx = state.get("chapter_idx", 0)
        reports = [r for r in (state.get("editor_reports") or [])
                   if r["chapter_index"] == idx]
        last = reports[-1] if reports else None
        open_findings = ([f for f in last["findings"]
                          if f["status"] != "rejected"] if last else [])
        if open_findings:
            rr = client.post(
                f"/api/runs/{tid}/chapter/{idx}/apply_revision",
                json={"messages": [
                    {"role": "user", "content": "исправь по смыслу"}]})
            assert rr.status_code == 200, f"apply_revision: {rr.status_code} {rr.text}"
            assert rr.json().get("started"), "apply_revision должен запуститься в фоне"
            applied_revision = True
            # фоновая правка → ждём paused, затем проверяем что замечаний нет
            ns = _wait_not_running(client, tid)["state"]
            nrep = [r for r in (ns.get("editor_reports") or [])
                    if r["chapter_index"] == idx][-1]
            nopen = [f for f in nrep["findings"] if f["status"] != "rejected"]
            assert not nopen, f"после apply_revision остались замечания: {nopen}"
            # теперь глава чиста → двигаемся дальше
            client.post(f"/api/runs/{tid}/resume")
            continue

        # глава чиста / пауза не на редакторе → продолжаем
        client.post(f"/api/runs/{tid}/resume")

    final = client.get(f"/api/runs/{tid}/state").json()
    assert final["status"] == "done", f"не дошёл до done: {final['status']}"
    assert applied_revision, "цикл apply_revision не сработал ни разу"

    chs = final["state"]["chapters"]
    assert len(chs) == N_CHAPTERS
    assert all(c["dialogue"] for c in chs)

    # экспорт готового проекта
    ex = client.get(f"/api/runs/{tid}/export?fmt=txt")
    assert ex.status_code == 200, ex.text
    body = ex.json()
    assert body["chapters"] == N_CHAPTERS
    assert "Глава 1" in body["text"] and "Глава 3" in body["text"]
    exported = True

    # чат без markdown
    ch = client.post(f"/api/runs/{tid}/chat", json={
        "messages": [{"role": "user", "content": "привет"}],
        "chapter_idx": 0})
    assert ch.status_code == 200, ch.text
    reply = ch.json()["reply"]
    assert "**" not in reply and "```" not in reply, "в чате есть markdown"

    print(f"  B: эндпоинты ОК — apply_revision={applied_revision}, "
          f"export={exported}, главы={len(chs)}")


def main() -> int:
    _patch_llms()
    tests = [("граф (поглавный цикл)", test_graph_flow),
             ("HTTP-эндпоинты + apply_revision + export", test_endpoints_flow)]
    failed = 0
    for label, fn in tests:
        print(f"▶ {label}")
        try:
            fn()
            print("  ✓ PASS")
        except Exception:
            failed += 1
            print("  ✗ FAIL")
            traceback.print_exc()
    print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not failed else f"ПРОВАЛЕНО: {failed}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
