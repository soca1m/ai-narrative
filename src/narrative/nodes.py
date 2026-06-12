"""Узлы графа = боты. Каждый: дословный промпт + вызов LLM + запись в State.

Линейные боты (1-4) пишут общие артефакты. Боты 5-6-7 крутятся в поглавном
цикле (одна глава за раз). Бот 8 — финальный перевод (опционально, по флагу).
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache

from . import prompts
from .config import adult_provider, editor_provider, structural_provider
from .llm import LLM
from .routing import retry_key
from .state import (
    AdultFeasibility,
    AdultSceneOut,
    Chapter,
    EditorReport,
    EditorReportOut,
    Finding,
    State,
    StructurePlan,
)
from .util import parse_chapters, parse_loglines


# Ленивая инициализация: клиенты создаются при первом вызове, ПОСЛЕ load_dotenv.
@lru_cache(maxsize=1)
def _structural() -> LLM:
    return LLM(structural_provider())


@lru_cache(maxsize=1)
def _adult() -> LLM:
    return LLM(adult_provider())


@lru_cache(maxsize=1)
def _editor() -> LLM:
    return LLM(editor_provider())


_MAX_CHAPTERS = 20  # хард-кап общего числа глав (страховка авто-петли структуры)


def _genre(state: State) -> str | None:
    return state.get("genre")


def _sys(state: State, base: str, names: bool = False) -> str:
    """Системный промпт + жанр (+ правило западных имён, где нужно)."""
    out = prompts.with_genre(base, _genre(state))
    if names:
        out += prompts.NAMES_RULE
    return out


# ---------- Боты 1-4: линейная часть ----------

def logline_node(state: State) -> dict:
    fb = state.get("revision_feedback") or ""
    user = f"Тема и референсы:\n{state['theme']}"
    if fb:
        user += f"\n\nПрошлые логлайны:\n{state.get('logline', '')}\n\n{fb}"
    user += prompts.NO_META
    out = _structural().complete(_sys(state, prompts.LOGLINE, names=True), user)
    loglines = parse_loglines(out)
    return {
        "logline": out,
        "loglines": loglines,
        # авто-режим: если выбор ещё не сделан — берём первый вариант
        "selected_logline": state.get("selected_logline") or (loglines[0] if loglines else out),
        "revision_feedback": None,
        "revision_target": None,
        "log": [f"✓ Бот 1: {len(loglines)} логлайнов готовы"],
    }


def synopsis_node(state: State) -> dict:
    chosen = state.get("selected_logline") or state.get("logline", "")
    fb = state.get("revision_feedback") or ""
    user = f"Логлайн:\n{chosen}"
    if fb:
        user += f"\n\nПредыдущий синопсис:\n{state.get('synopsis', '')}\n\n{fb}"
    user += prompts.NO_META
    out = _structural().complete(_sys(state, prompts.SYNOPSIS, names=True), user)
    return {
        "synopsis": out,
        "revision_feedback": None,
        "revision_target": None,
        "log": ["✓ Бот 2: синопсис готов"],
    }


def characters_node(state: State) -> dict:
    fb = state.get("revision_feedback") or ""
    user = f"Синопсис:\n{state['synopsis']}"
    if fb:
        user += f"\n\nПредыдущие карточки:\n{state.get('characters', '')}\n\n{fb}"
    user += prompts.NO_META
    out = _structural().complete(_sys(state, prompts.CHARACTERS, names=True), user)
    return {
        "characters": out,
        "revision_feedback": None,
        "revision_target": None,
        "log": ["✓ Бот 3: карточки персонажей готовы"],
    }


def _gen_chapter_batch(state: State, start: int, n: int) -> tuple[list[Chapter], bool]:
    """Генерит порцию из n глав начиная с индекса start (0-based). → (главы, done)."""
    existing = list(state.get("chapters") or [])
    fb = state.get("revision_feedback") or ""

    ctx = f"Синопсис:\n{state['synopsis']}\n\nКарточки:\n{state['characters']}"
    if existing:
        prev = "\n\n".join(
            f"Глава {c.index + 1}: {c.title}\n{c.plan}" for c in existing
        )
        ctx += f"\n\nУЖЕ ГОТОВЫЕ ГЛАВЫ (контекст, не повторять):\n{prev}"
    if fb:
        ctx += f"\n\n{fb}"

    system = _sys(state, prompts.STRUCTURE, names=True)
    suffix = prompts.STRUCTURE_BATCH_SUFFIX.format(n=n, start=start + 1)
    try:
        plan = _structural().structured(
            system + suffix + prompts.STRUCTURE_JSON_SUFFIX, ctx,
            StructurePlan, temperature=0.7,
        )
        full = [(c.title, c.plan) for c in plan.chapters]
        story_complete = bool(plan.story_complete)
        if not full:
            raise ValueError("пустая порция")
    except Exception:  # фолбэк: проза + regex-разбор по «ГЛАВА N»
        out = _structural().complete(system + suffix, ctx)
        full = [(c.title, c.plan) for c in parse_chapters(out)]
        story_complete = False

    # ЖЁСТКО держим размер порции: модель часто игнорит «ровно N» и выдаёт всё.
    overflow = len(full) > n
    full = full[:n]
    new = [
        Chapter(index=start + i, title=t, plan=p, is_adult_point=True)
        for i, (t, p) in enumerate(full)
    ]
    # done только если НЕ обрезали и (модель сказала «конец» ИЛИ дала меньше N).
    done = (not overflow) and (story_complete or len(new) < n)
    return new, done


def structure_node(state: State) -> dict:
    """Бот 4 — пишет структуру ПОРЦИЯМИ по chapters_per_batch глав за раз.

    Команда нарративщика (structure_action):
      "proceed" — хватит структуры, переходим к написанию глав;
      иначе — генерим следующую порцию.
    Адалт-точка ставится на КАЖДУЮ главу.
    """
    action = state.get("structure_action")
    existing = list(state.get("chapters") or [])

    # Нарративщик сказал «достаточно» → завершаем структуру без новой порции.
    if action == "proceed" and existing:
        return {
            "structure_done": True,
            "structure_action": None,
            "chapter_idx": 0,
            "log": [f"✓ Бот 4: структура завершена нарративщиком ({len(existing)} глав)"],
        }

    n = int(state.get("chapters_per_batch") or 3)
    start = len(existing)
    new, done = _gen_chapter_batch(state, start, n)
    chapters = existing + new
    if len(chapters) >= _MAX_CHAPTERS:  # хард-кап от бесконечной петли в авто-режиме
        done = True

    return {
        "chapters": chapters,
        "chapter_idx": 0,
        "retry_count": state.get("retry_count") or {},
        "structure_done": done,
        "structure_action": None,
        "revision_feedback": None,
        "revision_target": None,
        "log": [
            f"✓ Бот 4: порция глав {start + 1}–{start + len(new)} готова "
            f"(всего {len(chapters)}{', история завершена' if done else ''})"
        ],
    }


# ---------- Боты 5-6-7: поглавный цикл ----------

def dialogue_node(state: State) -> dict:
    idx = state["chapter_idx"]
    chapters = list(state["chapters"])
    ch = chapters[idx]

    fb = state.get("revision_feedback") or ""
    user = (
        f"Карточки персонажей:\n{state['characters']}\n\n"
        f"Глава для написания:\n{ch.title}\n{ch.plan}"
    )
    if fb and state.get("revision_target") == "dialogue":
        user += f"\n\nТекущий черновик главы:\n{ch.dialogue or ''}\n\n{fb}"
    user += (
        "\n\nВыведи ТОЛЬКО готовый текст этой главы в заданном формате "
        "визуальной новеллы. Никаких преамбул, вопросов, комментариев и "
        "пояснений. Если это правка — верни ПОЛНУЮ переписанную главу целиком."
    )

    ch.dialogue = _structural().complete(_sys(state, prompts.DIALOGUE, names=True), user)
    chapters[idx] = ch
    return {
        "chapters": chapters,
        "revision_feedback": None,
        "revision_target": None,
        "log": [f"✓ Бот 5: глава {idx + 1} «{ch.title}» написана"],
    }


# Маркеры отказа модели (начало ответа). Узкие фразы — чтобы не словить
# реплику персонажа вида «не могу больше ждать».
_REFUSAL_MARKERS = (
    "не могу написать", "не могу создать", "не могу выполнить",
    "не буду этого делать", "не стану писать", "я не могу помочь",
    "i can't write", "i cannot write", "i can't fulfill", "i cannot fulfill",
    "i can't create", "i won't", "as an ai", "против моих правил",
)


def _looks_like_refusal(text: str) -> bool:
    head = text.strip().lower()[:300]
    return any(m in head for m in _REFUSAL_MARKERS)


def adult_node(state: State) -> dict:
    """Бот 6 — адалт-сцена на каждой главе, с пре-чеком почвы.

    Пре-чек (дешёвая модель): есть ли в главе органичная почва для сцены.
    Нет почвы / модель отказалась → adult_block_reason на главе, UI предлагает
    «адаптировать главу» или «оставить без адалта». Мусор-отказ в сцену не пишем.
    """
    idx = state["chapter_idx"]
    chapters = list(state["chapters"])
    ch = chapters[idx]
    if not ch.is_adult_point:
        return {"log": [f"· Бот 6: глава {idx + 1} без адалт-точки, пропуск"]}

    fb = state.get("revision_feedback") or ""
    is_revision = bool(fb) and state.get("revision_target") == "adult"
    ctx = (
        f"Карточки персонажей:\n{state['characters']}\n\n"
        f"Контекст сцены (глава «{ch.title}»):\n{ch.dialogue or ch.plan}"
    )

    pair_hint = ""
    if not is_revision:  # пре-чек только на первичной генерации, не на ретраях
        try:
            chk = _editor().structured(prompts.ADULT_FEASIBILITY, ctx, AdultFeasibility)
        except Exception:
            chk = None  # пре-чек упал → не блокируем, пробуем генерить
        if chk and not chk.feasible:
            ch.adult_scene = None
            ch.adult_block_reason = chk.reason
            ch.adult_bridge_hint = chk.bridge or None
            chapters[idx] = ch
            return {
                "chapters": chapters,
                "revision_feedback": None,
                "revision_target": None,
                "log": [f"⚠ Бот 6: глава {idx + 1} — нет почвы для адалта. "
                        "Адаптируй главу или отключи адалт (кнопки в карточке главы)."],
            }
        if chk and chk.pair:
            pair_hint = f"\n\nУчастники сцены (логичны по главе): {chk.pair}."

    user = ctx
    if is_revision:
        user += f"\n\nТекущая адалт-сцена:\n{ch.adult_scene or ''}\n\n{fb}"
    user += pair_hint
    # Контракт + директива глубины: сцена всегда раскрывается на «уровень C».
    user += prompts.ADULT_SCENE_DIRECTIVE + prompts.NAMES_RULE

    # Строгий JSON-контракт: отказ — поле refused, а не текст для парсинга.
    llm = _adult()
    try:
        res = llm.structured(prompts.ADULT, user + prompts.ADULT_JSON_CONTRACT,
                             AdultSceneOut, temperature=llm.cfg.temperature)
        refused, reason, scene = res.refused, res.reason, res.scene
    except Exception:  # модель/провайдер не вытянул JSON → фолбэк на маркеры
        raw = llm.complete(prompts.ADULT, user)
        refused = _looks_like_refusal(raw)
        reason = raw.strip()[:180] if refused else ""
        scene = "" if refused else raw

    if refused or not scene.strip():
        ch.adult_scene = None
        ch.adult_block_reason = f"Модель отказалась: {reason[:180] or 'без причины'}"
        ch.adult_bridge_hint = ch.adult_bridge_hint or (
            "Вплети в главу взаимное влечение/напряжение между подходящей парой "
            "и повод остаться наедине."
        )
        chapters[idx] = ch
        return {
            "chapters": chapters,
            "revision_feedback": None,
            "revision_target": None,
            "log": [f"⚠ Бот 6: глава {idx + 1} — модель отказалась писать сцену. "
                    "Адаптируй главу или отключи адалт."],
        }

    ch.adult_scene = scene
    ch.adult_block_reason = None
    ch.adult_bridge_hint = None
    chapters[idx] = ch
    return {
        "chapters": chapters,
        "revision_feedback": None,
        "revision_target": None,
        "log": [f"✓ Бот 6: адалт-сцена для главы {idx + 1} готова"],
    }


def _finding_id(idx: int, fo) -> str:
    """Стабильный id замечания: глава + block + хэш локатора (переживает раунды)."""
    h = hashlib.sha1(f"{fo.block}|{fo.locator}".encode()).hexdigest()[:8]
    return f"c{idx}-{fo.block}-{h}"


def editor_node(state: State) -> dict:
    """Бот 7 — структурированная проверка текущей главы.

    findings получают стабильный id и наследуют решения нарративщика
    (accepted/rejected/комментарий) из finding_decisions.
    """
    idx = state["chapter_idx"]
    ch = state["chapters"][idx]

    full_text = ch.dialogue or ""
    if ch.adult_scene:
        full_text += f"\n\n--- АДАЛТ-ВСТАВКА ---\n{ch.adult_scene}"

    user = (
        f"Карточки персонажей (канон):\n{state['characters']}\n\n"
        f"Номер главы: {idx}\nНазвание: {ch.title}\n\n"
        f"Текст главы для проверки:\n{full_text}"
    )
    try:
        out: EditorReportOut = _editor().structured(prompts.EDITOR, user, EditorReportOut)
    except Exception as exc:
        report = EditorReport(chapter_index=idx, findings=[],
                              markdown=f"[редактор пропущен: {exc}]")
        return {
            "editor_reports": [report],
            "log": [f"⚠ Бот 7: глава {idx + 1} — отчёт не распарсился, пропуск"],
        }

    decisions = state.get("finding_decisions") or {}
    findings: list[Finding] = []
    for fo in out.findings:
        fid = _finding_id(idx, fo)
        d = decisions.get(fid, {})
        findings.append(Finding(
            id=fid, severity=fo.severity, block=fo.block,
            responsible_node=fo.responsible_node, locator=fo.locator,
            quote=fo.quote, problem=fo.problem,
            status=d.get("status", "open"), user_comment=d.get("comment", ""),
        ))
    report = EditorReport(chapter_index=idx, findings=findings, markdown=out.markdown)

    n = len(findings)
    return {
        "editor_reports": [report],
        "log": [f"✓ Бот 7: глава {idx + 1} — {n} замечаний"
                f"{' (есть критичные)' if report.has_blocking else ''}"],
    }


def translation_node(state: State) -> dict:
    """Бот 8 — перевод всех готовых глав. Гейтится флагом translation_enabled."""
    if not state.get("translation_enabled", True):
        return {"log": ["· Бот 8: перевод отключён (на паузе)"]}
    lang = state.get("target_language") or "English"
    chapters = list(state["chapters"])
    for i, ch in enumerate(chapters):
        text = ch.dialogue or ""
        if ch.adult_scene:
            text += f"\n\n--- АДАЛТ ---\n{ch.adult_scene}"
        ch.translation = _structural().complete(
            prompts.TRANSLATION,
            f"Целевой язык: {lang}\n\nТекст:\n{text}",
        )
        chapters[i] = ch
    return {"chapters": chapters, "log": [f"✓ Бот 8: перевод на {lang} готов"]}


# ---------- служебный узел: учёт попытки правки ----------

def bump_retry_node(state: State) -> dict:
    """Инкремент счётчика попыток перед возвратом к боту-виновнику."""
    target = state.get("revision_target")
    idx = state.get("chapter_idx", 0)
    rc = dict(state.get("retry_count", {}))
    if target:
        key = retry_key(target, idx)
        rc[key] = rc.get(key, 0) + 1
    return {"retry_count": rc}
