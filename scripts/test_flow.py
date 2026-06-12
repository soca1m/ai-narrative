"""Тестовый прогон с дампом всех артефактов в docs/test_output.md.

Запуск:
    .venv/bin/python scripts/test_flow.py
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from narrative import build_graph
from narrative.graph import sqlite_saver

OUT = Path(__file__).resolve().parent.parent / "docs" / "test_output.md"


def main() -> int:
    load_dotenv()
    graph = build_graph(sqlite_saver())  # персистентный state
    config = {"configurable": {"thread_id": "fulltest-gemma-001"}}

    init = {
        "theme": (
            "Закрытый теннисный клуб, лето, борьба за власть между капитаном "
            "группы и новым тренером. Лёгкая мистика. ВАЖНО: ровно 2 коротких "
            "главы, мало персонажей."
        ),
        "genre": "ровно 2 главы, короткие сцены",
        "target_language": "English",
        "adult_chapters": [1],  # 0-based: вторая глава = адалт-точка
    }

    # RESUME=1 → продолжить с чекпоинта (input=None), НЕ начинать заново.
    # Возможно благодаря SQLite-checkpointer: весь прогресс по thread_id сохранён.
    resume = os.environ.get("RESUME") == "1"
    if resume and graph.get_state(config).values:
        done = len(graph.get_state(config).values.get("log", []))
        print(f"↻ RESUME: продолжаю с чекпоинта ({done} шагов уже сделано)")
        stream_input = None
    else:
        stream_input = init

    printed = 0
    for event in graph.stream(stream_input, config, stream_mode="values"):
        log = event.get("log", [])
        for line in log[printed:]:
            print(line)
        printed = len(log)

    st = graph.get_state(config).values
    _dump(st)
    print(f"\nПолный дамп → {OUT}")
    return 0


def _dump(st: dict) -> None:
    chapters = st.get("chapters", [])
    reports = {r.chapter_index: r for r in st.get("editor_reports", [])}

    lines: list[str] = ["# Тестовый прогон — дамп артефактов\n"]
    lines.append(f"## Бот 1 — Логлайн\n\n{st.get('logline', '—')}\n")
    lines.append(f"## Бот 2 — Синопсис\n\n{st.get('synopsis', '—')}\n")
    lines.append(f"## Бот 3 — Персонажи\n\n{st.get('characters', '—')}\n")

    lines.append("## Бот 4 — Структура\n")
    for ch in chapters:
        flag = " 🔞 АДАЛТ-ТОЧКА" if ch.is_adult_point else ""
        lines.append(f"### Глава {ch.index + 1}: {ch.title}{flag}\n\n{ch.plan}\n")

    lines.append("## Поглавный контент (Боты 5/6/7/8)\n")
    for ch in chapters:
        lines.append(f"### Глава {ch.index + 1}: {ch.title}\n")
        lines.append(f"#### Бот 5 — Диалоги\n\n{ch.dialogue or '—'}\n")
        if ch.is_adult_point:
            lines.append(
                f"#### Бот 6 — Адалт (модель без цензуры)\n\n"
                f"{ch.adult_scene or '— ПУСТО (адалт не сгенерился!)'}\n"
            )
        rep = reports.get(ch.index)
        if rep:
            lines.append("#### Бот 7 — Редактор\n")
            for f in rep.findings:
                lines.append(
                    f"- **{f.severity}** [{f.block}] → `{f.responsible_node}` "
                    f"| {f.locator}: {f.problem}"
                )
            lines.append("")
        lines.append(f"#### Бот 8 — Перевод\n\n{ch.translation or '—'}\n")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
