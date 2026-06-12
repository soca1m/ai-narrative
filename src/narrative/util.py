"""Утилиты."""
from __future__ import annotations

import re

from .state import Chapter

# «1. ...», «1) ...», «- ...», «• ...» — строки нумерованного списка логлайнов.
_LOGLINE_RE = re.compile(r"^\s*(?:\d+[\.\)]|[-–—•*])\s+(.*\S)", re.MULTILINE)


def parse_loglines(text: str) -> list[str]:
    """Разбивает вывод Бота 1 на отдельные логлайны для выбора в UI.

    Бот возвращает пронумерованный список. Если разметки нет — каждый непустой
    абзац считаем отдельным вариантом; на крайний случай — весь текст одним.
    """
    items = [m.group(1).strip() for m in _LOGLINE_RE.finditer(text)]
    if items:
        return items
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    return paras or ([text.strip()] if text.strip() else [])

# «ГЛАВА 1. «Название»» или «Глава 1: Название» — режем поглавный план Бота 4.
_CHAPTER_RE = re.compile(r"^\s*ГЛАВА\s+(\d+)[\.\:]?\s*[«\"]?(.*?)[»\"]?\s*$",
                         re.IGNORECASE | re.MULTILINE)


def parse_chapters(structure_text: str) -> list[Chapter]:
    """Разбивает текст структуры на главы.

    Эвристика для скелета. На проде лучше попросить Бота 4 вернуть JSON.
    Если разметка не нашлась — вся структура = одна глава.
    """
    matches = list(_CHAPTER_RE.finditer(structure_text))
    if not matches:
        return [Chapter(index=0, title="Глава 1", plan=structure_text.strip())]

    chapters: list[Chapter] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(structure_text)
        title = m.group(2).strip() or f"Глава {m.group(1)}"
        chapters.append(Chapter(
            index=i,
            title=title,
            plan=structure_text[start:end].strip(),
        ))
    return chapters
