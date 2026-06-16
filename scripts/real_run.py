"""Реальный прогон графа на 3 главы (реальные LLM). Дёшево: перевод off.

Печатает каждый шаг лога + ловит реальные ошибки провайдеров.
Запуск: .venv/bin/python scripts/real_run.py
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from narrative.graph import build_graph  # noqa: E402


def main() -> int:
    load_dotenv()
    g = build_graph(MemorySaver())  # auto, без пауз
    cfg = {"configurable": {"thread_id": "real-3ch"}}
    init = {
        "theme": "Закрытый теннисный клуб, лето, борьба за власть. Лёгкая мистика.",
        "target_language": "Russian",
        "translation_enabled": False,
        "target_chapters": 3,
    }
    t0 = time.time()
    printed = 0
    final = None
    try:
        for ev in g.stream(init, cfg, stream_mode="values"):
            final = ev
            log = ev.get("log", [])
            for line in log[printed:]:
                print(f"[{time.time() - t0:6.1f}s] {line}", flush=True)
            printed = len(log)
    except Exception:
        print("‼ ИСКЛЮЧЕНИЕ В ГРАФЕ:", flush=True)
        traceback.print_exc()
        return 1

    print("\n===== ИТОГ =====", flush=True)
    chs = (final or {}).get("chapters", [])
    print(f"глав: {len(chs)}", flush=True)
    for c in chs:
        dlen = len(c.dialogue or "")
        adult = "🔞" if c.is_adult_point else "  "
        block = f" БЛОК:{c.adult_block_reason[:40]}" if c.adult_block_reason else ""
        print(f"  {adult} гл.{c.index + 1} «{c.title[:40]}» "
              f"диалог={dlen}симв{block}", flush=True)
    reports = (final or {}).get("editor_reports", [])
    per = {}
    last_by_ch = {}
    for r in reports:
        per[r.chapter_index] = per.get(r.chapter_index, 0) + 1
        last_by_ch[r.chapter_index] = r
    print(f"раунды редактора по главам: {dict(sorted(per.items()))}", flush=True)

    print("\n--- ПОСЛЕДНИЕ ЗАМЕЧАНИЯ РЕДАКТОРА (по главам) ---", flush=True)
    sev = {"critical": "🔴", "important": "🟡", "minor": "🟢"}
    for idx in sorted(last_by_ch):
        r = last_by_ch[idx]
        crit = sum(1 for f in r.findings if f.severity == "critical")
        print(f"\nГлава {idx + 1}: {len(r.findings)} замечаний "
              f"(критич. {crit})", flush=True)
        for f in r.findings[:12]:
            print(f"  {sev.get(f.severity, '·')} [{f.block}] {f.problem[:110]}",
                  flush=True)

    print(f"\nвсего времени: {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
