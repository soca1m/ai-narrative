"""Запуск конвейера на одну новеллу.

Пример:
    python scripts/run.py --theme "Летний теннисный клуб, мистика, власть" \
        --genre "комедийная драма" --lang English --thread korona-001
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from narrative import build_graph
from narrative.graph import sqlite_saver


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", required=True, help="тема + референсы")
    ap.add_argument("--genre", default=None, help="особый жанр/требования")
    ap.add_argument("--lang", default="English", help="язык перевода (Бот 8)")
    ap.add_argument("--thread", default="novel-1",
                    help="id новеллы = «новый чат на проект» из памятки")
    ap.add_argument("--adult", default="",
                    help="номера глав с адалт-точкой через запятую, 1-based "
                         "(напр. '1,3'). НД проставляет вручную.")
    args = ap.parse_args()

    # 1-based от пользователя → 0-based индексы глав
    adult_chapters = [int(x) - 1 for x in args.adult.split(",") if x.strip()]

    graph = build_graph(sqlite_saver())  # персистентный state
    config = {"configurable": {"thread_id": args.thread}}

    init: dict = {
        "theme": args.theme,
        "genre": args.genre,
        "target_language": args.lang,
        "adult_chapters": adult_chapters,
    }

    # stream — видно работу каждого бота; печатаем только новые строки лога.
    printed = 0
    for event in graph.stream(init, config, stream_mode="values"):
        log = event.get("log", [])
        for line in log[printed:]:
            print(line)
        printed = len(log)

    final = graph.get_state(config).values
    print("\n=== ГОТОВО ===")
    print(f"Глав: {len(final.get('chapters', []))}")
    print(f"Отчётов редактора: {len(final.get('editor_reports', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
