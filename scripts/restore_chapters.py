"""Восстановить стёртые главы прогона из истории чекпоинтов.

Главы, стёртые откатом (или багом), не потеряны: SQLite-checkpointer хранит
всю историю состояний. Скрипт находит последний чекпоинт с непустыми главами
и возвращает их в текущее состояние (вместе с курсором главы).

Запуск (на машине с БД; в Docker — внутри контейнера бэкенда):
    python scripts/restore_chapters.py <thread_id>          # восстановить
    python scripts/restore_chapters.py <thread_id> --dry    # только показать
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    thread_id = sys.argv[1]
    dry = "--dry" in sys.argv

    from narrative.graph import build_graph, sqlite_saver, STAGE_NODES

    graph = build_graph(sqlite_saver(), interrupt_after=STAGE_NODES)
    cfg = {"configurable": {"thread_id": thread_id}}

    cur = graph.get_state(cfg)
    if not cur or not (cur.values or cur.next):
        print(f"прогон {thread_id} не найден")
        return 1
    cur_n = len(cur.values.get("chapters") or [])
    print(f"сейчас глав: {cur_n}")

    best = None
    for snap in graph.get_state_history(cfg):
        chs = (snap.values or {}).get("chapters") or []
        # ищем самый свежий чекпоинт, где глав больше, чем сейчас
        if len(chs) > cur_n:
            best = snap
            break
    if best is None:
        print("в истории нет чекпоинта с бОльшим числом глав — восстанавливать нечего")
        return 1

    chs = best.values["chapters"]
    written = sum(1 for c in chs if c.dialogue)
    print(f"нашёл чекпоинт: глав {len(chs)} (написано {written}) — "
          f"{best.config['configurable'].get('checkpoint_id', '?')}")
    for c in chs:
        mark = "✓" if c.dialogue else "·"
        print(f"  {mark} {c.index + 1}. {c.title}")

    if dry:
        print("--dry: ничего не меняю")
        return 0

    idx = best.values.get("chapter_idx")
    idx = 0 if idx is None else max(0, min(idx, len(chs) - 1))
    graph.update_state(cfg, {"chapters": chs, "chapter_idx": idx,
                             "structure_done": True})
    print(f"восстановлено: {len(chs)} глав, курсор на главе {idx + 1}. "
          "Обнови страницу в браузере.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
