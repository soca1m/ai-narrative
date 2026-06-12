"""Маршрутизация правок: отчёт редактора → кому переделывать + лимит попыток.

Это «линчпин» задачи: ошибку/недостоверность ловит редактор (Бот 7),
а критичные findings гонят правку обратно ответственному боту.
Учитываются решения нарративщика: отклонённые (rejected) findings игнорятся,
комментарии нарративщика подмешиваются в фидбек.
"""
from __future__ import annotations

from .config import MAX_REVISIONS
from .state import EditorReport, Finding, NodeName

# Узлы, на которые МОЖНО маршрутизировать правку в поглавном цикле.
# characters/structure СПЕЦИАЛЬНО исключены: их регенерация пересобирает все
# главы и сбрасывает прогресс → каскад и риск бесконечного цикла.
_PER_CHAPTER: set[NodeName] = {"dialogue", "adult"}


def apply_decisions(report: EditorReport, decisions: dict[str, dict]) -> EditorReport:
    """Наложить решения нарративщика (status/comment по id) на findings отчёта.

    Возвращает КЛОН отчёта — стора не мутируем. Используется и в роутинге
    (чтобы свежие accept/reject учитывались сразу), и в сериализации для UI.
    """
    if not decisions:
        return report
    findings = []
    for f in report.findings:
        d = decisions.get(f.id)
        if d:
            f = f.model_copy(update={
                "status": d.get("status", f.status),
                "user_comment": d.get("comment", f.user_comment),
            })
        findings.append(f)
    return report.model_copy(update={"findings": findings})


def _clamp(target: NodeName | None, is_adult: bool) -> NodeName:
    if target in _PER_CHAPTER:
        return target
    return "adult" if is_adult else "dialogue"


def _active_critical(report: EditorReport) -> list[Finding]:
    """Критичные findings, которые нарративщик НЕ отклонил."""
    return [
        f for f in report.findings
        if f.severity == "critical" and f.status != "rejected"
    ]


def pick_revision_target(report: EditorReport,
                         is_adult: bool = False) -> tuple[NodeName | None, str]:
    """Берёт самый критичный НЕотклонённый finding → (нода, фидбек).

    None → блокирующих замечаний нет, идём дальше по конвейеру.
    Комментарии нарративщика к findings вплетаются в фидбек, чтобы бот их учёл.
    accepted findings помечаются как обязательные к исправлению.
    """
    blocking = _active_critical(report)
    if not blocking:
        return None, ""

    top = blocking[0]
    if is_adult:
        adult_first = [f for f in blocking if f.responsible_node == "adult"]
        if adult_first:
            top = adult_first[0]

    target = _clamp(top.responsible_node, is_adult)

    lines = []
    for f in blocking:
        mark = " [ПРИНЯТО нарративщиком — исправить обязательно]" if f.status == "accepted" else ""
        line = f"[{f.block}] {f.locator}: {f.problem}{mark}"
        if f.user_comment:
            line += f"\n  ⤷ комментарий нарративщика: {f.user_comment}"
        lines.append(line)
    issues = "\n".join(lines)

    feedback = (
        "Редактор нашёл нарушения. ГЛАВНОЕ: персонаж должен вести себя строго "
        "по своей карточке — мотивация, характер, речевая манера, скрытые цели. "
        "Не ломай образ ради сцены. НЕ переписывай всё заново, точечно исправь "
        "только это, сохранив остальное. Если у замечания есть комментарий "
        f"нарративщика — следуй ему:\n{issues}"
    )
    return target, feedback


def retry_key(node: NodeName, chapter_idx: int) -> str:
    return f"{node}:{chapter_idx}"


def can_retry(retry_count: dict[str, int], node: NodeName, chapter_idx: int) -> bool:
    return retry_count.get(retry_key(node, chapter_idx), 0) < MAX_REVISIONS
