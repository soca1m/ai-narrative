"""Узлы графа = боты. Каждый: дословный промпт + вызов LLM + запись в State.

Линейные боты (1-4) пишут общие артефакты. Боты 5-6-7 крутятся в поглавном
цикле (одна глава за раз). Бот 8 — финальный перевод (опционально, по флагу).
"""
from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache

from . import live, prompts
from .config import (CHAPTER_MAX_PASSES, CHAPTER_WORDS_DEFAULT, adult_provider,
                     editor_provider, structural_provider)
from .llm import LLM, LLMLimitError
from .routing import retry_key
from .state import (
    AdultFeasibility,
    AdultSceneOut,
    Chapter,
    StaticShot,
    ChapterCountOut,
    DialogueOut,
    EditorReport,
    EditorReportOut,
    Finding,
    State,
    StructureFixOut,
    StructurePlan,
)
from .util import parse_chapters, parse_loglines


def _subscription_on() -> bool:
    """Глобальный тумблер: все НЕ-адалт боты через подписку Claude (Agent SDK)."""
    return os.environ.get("USE_CLAUDE_SUBSCRIPTION") == "1"


def _sub_model() -> str:
    return os.environ.get("CLAUDE_SUB_MODEL", "sonnet")


def _sub_llm():
    from .claude_sub import ClaudeSubLLM  # noqa: PLC0415 — ленивый импорт SDK
    return ClaudeSubLLM(_sub_model())


def _use_subscription(state: State | None = None,
                      stage: str | None = None) -> bool:
    """Использовать ли подписку Claude для этого этапа (#5).

    Приоритет: пер-этапный оверрайд из state.stage_providers > фолбэк
    force_openrouter (лимит подписки исчерпан) > глобальный тумблер env.
    """
    if state is not None:
        if state.get("force_openrouter"):
            return False
        choice = (state.get("stage_providers") or {}).get(stage or "")
        if choice == "subscription":
            return True
        if choice == "openrouter":
            return False
    return _subscription_on()


# НЕ кэшируем структурный/редактор — чтобы тумблер подписки применялся сразу.
def _structural(state: State | None = None, stage: str | None = None):
    return (_sub_llm() if _use_subscription(state, stage)
            else LLM(structural_provider()))


def _editor(state: State | None = None, stage: str | None = None):
    return (_sub_llm() if _use_subscription(state, stage)
            else LLM(editor_provider()))


# Адалт ВСЕГДА через uncensored-модель (подписка Claude цензурит) — кэшируем.
@lru_cache(maxsize=1)
def _adult() -> LLM:
    return LLM(adult_provider())


@lru_cache(maxsize=8)
def _llm_for(model: str | None):
    """LLM с выбранной моделью для глав (#3). None → дефолтная structural.

    Префикс `claude-sub:` → Claude через ПОДПИСКУ Pro/Max (#4, Agent SDK,
    без платы за токены): claude-sub:sonnet / claude-sub:opus / claude-sub:haiku.
    """
    if not model:
        return _structural()
    if model.startswith("claude-sub:"):
        from .claude_sub import ClaudeSubLLM  # noqa: PLC0415
        return ClaudeSubLLM(model.split(":", 1)[1] or "sonnet")
    from dataclasses import replace  # noqa: PLC0415
    return LLM(replace(structural_provider(), model=model))


_MAX_CHAPTERS = 20  # хард-кап общего числа глав (страховка авто-петли структуры)

# Перевод (#3): кнопка «на русский» для нарративщика + правки RU→EN перед агентом.
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def _has_cyrillic(text: str | None) -> bool:
    return bool(_CYRILLIC.search(text or ""))


def translate(text: str, to_lang: str = "Russian",
              state: State | None = None) -> str:
    """Перевод текста на to_lang через LLM. Сохраняет формат ВН (имена,
    теги кадров NN-Loc-N, блок выбора) — переводит только прозу/реплики."""
    if not (text or "").strip():
        return text
    system = (
        "You are a professional translator. Translate the text into "
        f"{to_lang}. Keep visual-novel formatting EXACTLY: speaker labels "
        "(NAME:) keep the name; frame-tag lines like '01-Office-2 — ...' keep "
        "the tag code unchanged and translate only the description after the "
        "dash; keep the choice block. Output ONLY the translation, no notes.")
    return _structural(state).complete(system, text)


def to_english(text: str, state: State | None = None) -> str:
    """RU-правки → EN перед подачей агенту (если есть кириллица)."""
    return translate(text, "English", state) if _has_cyrillic(text) else text


def _stream_node(stage: str):
    """Декоратор: привязывает live-sink (из config графа) на время работы узла,
    чтобы LLM стримил нарастающий текст во фронт. Прямой вызов (revise из сервера)
    идёт без config → sink None → обычная генерация без стрима."""
    def deco(fn):
        def wrapped(state: State, config=None) -> dict:
            sink = live.sink_from_config(config)
            if sink is None:
                # прямой вызов (revise из сервера): НЕ трогаем уже привязанный
                # live — его мог выставить _bg_op, чтобы стримить ревизию.
                return fn(state)
            idx = state.get("chapter_idx") if stage == "dialogue" else None
            live.bind(sink, stage, idx)
            try:
                return fn(state)
            finally:
                live.clear()
        wrapped.__name__ = fn.__name__
        return wrapped
    return deco


def _genre(state: State) -> str | None:
    return state.get("genre")


def _prompt(state: State | None, stage: str | None, default: str) -> str:
    """Dev-оверрайд системного промпта шага (#4) или дефолт."""
    if state is not None and stage:
        ov = (state.get("prompt_overrides") or {}).get(stage)
        if ov and ov.strip():
            return ov
    return default


def _sys(state: State, base: str, names: bool = False,
         stage: str | None = None) -> str:
    """Системный промпт + жанр (+ правило языка/имён) + политика контента.

    CONTENT_POLICY подмешивается во ВСЕ боты (через _sys) — жёсткий фильтр
    запрещённого закладывается на этапе письма, до редактора. stage задан →
    применяется dev-оверрайд промпта этого шага.
    """
    base = _prompt(state, stage, base)
    out = prompts.with_genre(base, _genre(state))
    if names:
        out += prompts.NAMES_RULE
    out += prompts.CONTENT_POLICY
    return out


def _loc_block(state: State) -> str:
    """Блок локаций для контекста (пусто, если бот локаций не отработал)."""
    loc = (state.get("locations") or "").strip()
    return (f"\n\nЛокации (используй эти места и их CamelCase-теги "
            f"для статиков):\n{loc}") if loc else ""


def _story_context(state: State, idx: int, max_prev: int = 4) -> str:
    """Полный контекст для написания/правки главы #5/#6: синопсис + последние
    предыдущие главы (план + хвост написанного текста) — чтобы держать
    непрерывность и не повторяться."""
    parts: list[str] = []
    syn = (state.get("synopsis") or "").strip()
    if syn:
        parts.append(f"Синопсис:\n{syn}")
    chs = list(state.get("chapters") or [])
    prev = [c for c in chs if c.index < idx][-max_prev:]
    if prev:
        blocks = []
        for c in prev:
            b = f"Глава {c.index + 1}: {c.title}\nПлан:\n{c.plan}"
            if c.dialogue:
                b += f"\nКонцовка написанного: …{c.dialogue.strip()[-400:]}"
            blocks.append(b)
        parts.append("Предыдущие главы (контекст — держи непрерывность, НЕ "
                     "повторяй и НЕ переписывай их):\n\n" + "\n\n".join(blocks))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def _all_chapters_context(state: State, current_idx: int | None = None) -> str:
    """Полный контекст ВСЕХ глав (план + текст), даже пустых — для чата/помощи
    по конкретной главе. Текущая глава помечена явно, чтобы ИИ не путал, о какой
    речь."""
    chs = sorted(state.get("chapters") or [], key=lambda c: c.index)
    if not chs:
        return ""
    blocks = []
    for c in chs:
        mark = "  ◀◀ ТЕКУЩАЯ ГЛАВА (о ней идёт речь)" if c.index == current_idx else ""
        plan = (c.plan or "").strip() or "(план пуст)"
        dlg = (c.dialogue or "").strip() or "(текст ещё не написан)"
        blocks.append(
            f"=== Глава {c.index + 1}: {c.title}{mark} ===\n"
            f"ПЛАН:\n{plan}\n\nТЕКСТ:\n{dlg}"
        )
    return ("Все главы по порядку повествования (планы и тексты — для контекста; "
            "глава 1 идёт первой):\n\n" + "\n\n".join(blocks))


def gen_chapter_plan(state: State, idx: int) -> str:
    """Сгенерировать ИИ план для ОДНОЙ (обычно пустой) главы idx по контексту
    всех глав. Возвращает текст плана разделами."""
    chapters = sorted(state.get("chapters") or [], key=lambda c: c.index)
    if not (0 <= idx < len(chapters)):
        return ""
    cur = chapters[idx]
    ctx = (f"Карточки:\n{state.get('characters', '')}" + _loc_block(state)
           + "\n\n" + _all_chapters_context(state, idx)
           + f"\n\nСгенерируй план ТОЛЬКО для главы {idx + 1} "
           f"«{cur.title}» — она должна логично встать по порядку среди "
           "остальных (без дыр и повторов с соседними). Верни РОВНО ОДИН "
           "объект в массиве chapters.")
    system = _sys(state, prompts.STRUCTURE, names=True, stage="structure")
    try:
        plan = _structural(state, "structure").structured(
            system + prompts.STRUCTURE_JSON_SUFFIX, ctx, StructurePlan,
            temperature=0.7)
        c = plan.chapters[0]
        plan_text = c.plan
        if c.adult_note:
            plan_text = f"{plan_text}\n\n[ADULT POINT: {c.adult_note}]"
        return plan_text
    except LLMLimitError:
        raise
    except Exception:
        return cur.plan or ""


# ---------- Боты 1-4: линейная часть ----------

@_stream_node("logline")
def logline_node(state: State) -> dict:
    fb = state.get("revision_feedback") or ""
    user = f"Тема и референсы:\n{state['theme']}"
    if fb:
        user += f"\n\nПрошлые логлайны:\n{state.get('logline', '')}\n\n{fb}"
    user += prompts.NO_META
    out = _structural(state, "logline").complete(
        _sys(state, prompts.LOGLINE, names=True, stage="logline"), user)
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


@_stream_node("synopsis")
def synopsis_node(state: State) -> dict:
    chosen = state.get("selected_logline") or state.get("logline", "")
    fb = state.get("revision_feedback") or ""
    user = f"Логлайн:\n{chosen}"
    if fb:
        user += f"\n\nПредыдущий синопсис:\n{state.get('synopsis', '')}\n\n{fb}"
    user += prompts.NO_META
    out = _structural(state, "synopsis").complete(
        _sys(state, prompts.SYNOPSIS, names=True, stage="synopsis"), user)
    return {
        "synopsis": out,
        "revision_feedback": None,
        "revision_target": None,
        "log": ["✓ Бот 2: синопсис готов"],
    }


@_stream_node("characters")
def characters_node(state: State) -> dict:
    fb = state.get("revision_feedback") or ""
    user = f"Синопсис:\n{state['synopsis']}"
    if fb:
        user += f"\n\nПредыдущие карточки:\n{state.get('characters', '')}\n\n{fb}"
    user += prompts.NO_META
    out = _structural(state, "characters").complete(
        _sys(state, prompts.CHARACTERS, names=True, stage="characters"), user)
    return {
        "characters": out,
        "revision_feedback": None,
        "revision_target": None,
        "log": ["✓ Бот 3: карточки персонажей готовы"],
    }


@_stream_node("locations")
def locations_node(state: State) -> dict:
    """Бот локаций — карточки мест действия (канон локаций), как персонажи.

    Идёт после персонажей, до структуры. Локации потом юзаются в структуре и
    при написании глав (единые названия + CamelCase-теги статиков)."""
    fb = state.get("revision_feedback") or ""
    user = (f"Синопсис:\n{state.get('synopsis', '')}\n\n"
            f"Карточки персонажей:\n{state.get('characters', '')}")
    if fb:
        user += f"\n\nПредыдущие локации:\n{state.get('locations', '')}\n\n{fb}"
    user += prompts.NO_META
    out = _structural(state, "locations").complete(
        _sys(state, prompts.LOCATIONS, names=True, stage="locations"), user)
    return {
        "locations": out,
        "revision_feedback": None,
        "revision_target": None,
        "log": ["✓ Бот локаций: карточки локаций готовы"],
    }


def _gen_chapter_batch(state: State, start: int, n: int) -> tuple[list[Chapter], bool]:
    """Генерит порцию из n глав начиная с индекса start (0-based). → (главы, done)."""
    existing = list(state.get("chapters") or [])
    fb = state.get("revision_feedback") or ""

    ctx = (f"Синопсис:\n{state['synopsis']}\n\n"
           f"Карточки:\n{state['characters']}" + _loc_block(state))
    if existing:
        prev = "\n\n".join(
            f"Глава {c.index + 1}: {c.title}\n{c.plan}" for c in existing
        )
        ctx += f"\n\nУЖЕ ГОТОВЫЕ ГЛАВЫ (контекст, не повторять):\n{prev}"
    if fb:
        ctx += f"\n\n{fb}"

    system = _sys(state, prompts.STRUCTURE, names=True, stage="structure")
    suffix = prompts.STRUCTURE_BATCH_SUFFIX.format(n=n, start=start + 1)
    try:
        plan = _structural(state, "structure").structured(
            system + suffix + prompts.STRUCTURE_JSON_SUFFIX, ctx,
            StructurePlan, temperature=0.7,
        )
        # (title, plan, is_adult, adult_note) — адалт-точка из решения Бота 4.
        full = [(c.title, c.plan, c.is_adult, c.adult_note) for c in plan.chapters]
        story_complete = bool(plan.story_complete)
        if not full:
            raise ValueError("пустая порция")
    except Exception:  # фолбэк: проза + regex-разбор по «ГЛАВА N» (адалт гейтит пре-чек)
        out = _structural(state, "structure").complete(system + suffix, ctx)
        full = [(c.title, c.plan, True, "") for c in parse_chapters(out)]
        story_complete = False

    # ЖЁСТКО держим размер порции: модель часто игнорит «ровно N» и выдаёт всё.
    overflow = len(full) > n
    full = full[:n]
    new = []
    for i, (title, plan_text, _is_adult, note) in enumerate(full):
        # Адалт — в КАЖДОЙ главе (по умолчанию). Решение бота игнорим; пару
        # подсказывает adult_note. Вручную главу можно сделать неадалт в UI.
        if note:  # подсказку «кто с кем» кладём в план — её увидит Бот 6
            plan_text = f"{plan_text}\n\n[ADULT POINT: {note}]"
        new.append(Chapter(
            index=start + i, title=title, plan=plan_text,
            is_adult_point=True, adult_note=note or "",
        ))
    # done только если НЕ обрезали и (модель сказала «конец» ИЛИ дала меньше N).
    done = (not overflow) and (story_complete or len(new) < n)
    return new, done


# Рекомендуемое число глав по умолчанию (когда ИИ-оценка недоступна).
_DEFAULT_CHAPTERS = 6


def chapter_count_node(state: State) -> dict:
    """Перед структурой: ИИ предлагает оптимальное число глав. Пауза (step) →
    нарративщик апрувит или задаёт своё (target_chapters), потом structure."""
    if state.get("target_chapters"):  # юзер уже утвердил → ничего не делаем
        return {}
    user = (
        f"Синопсис:\n{state.get('synopsis', '')}\n\n"
        f"Карточки:\n{state.get('characters', '')}\n\n"
        "Сколько глав оптимально для этой adult-новеллы? Компактная, плотная "
        "история без воды. По умолчанию ориентир — 6 глав; больше предлагай "
        "только если сюжет реально не умещается. Адалт должен идти рано и часто."
    )
    try:
        out = _structural(state, "chapter_count").structured(
            "Ты — продюсер визуальных новелл. Оцени оптимальный объём. "
            "Не раздувай: предпочитай компактные истории (~6 глав).",
            user, ChapterCountOut, temperature=0.3)
        n = max(2, min(out.count, _MAX_CHAPTERS))
        reason = out.reason
    except Exception:
        n = _DEFAULT_CHAPTERS
        reason = f"не удалось оценить — рекомендуемый объём {n} глав"
    return {
        "suggested_chapters": n,
        "count_reason": reason,
        "log": [f"✓ Объём: ИИ предлагает {n} глав — {reason}"],
    }


# Маркеры главы-заглушки/отказа (модель вместо плана пишет «главу не нужно
# писать, история завершена»). Такую главу отбрасываем — это и был баг.
_FILLER_MARKERS = (
    "не генерируется", "не нужна", "не требуется", "story_complete",
    "историю завершил", "история завершена", "история уже завершена",
    "продолжение нарушило", "глава отсутствует", "пустая глава",
    "no chapter", "story is complete", "not generated",
)


def _is_filler_chapter(title: str, plan: str) -> bool:
    """True — глава-заглушка/отказ, а не реальный план (фильтр мусора структуры)."""
    head = f"{title}\n{plan}".lower()
    if any(m in head for m in _FILLER_MARKERS):
        return True
    # аномально длинный «заголовок» = модель впихнула объяснение в title
    if len(title) > 120:
        return True
    return False


def structure_node(state: State) -> dict:
    """Бот 4 — пишет структуру РОВНО на утверждённое число глав (target_chapters).

    Генерим всю дугу ОДНИМ заходом (STRUCTURE_FULL_SUFFIX) — модель сама
    распределяет сюжет на N глав, не оставляя глав-заглушек. Заглушки/отказы
    отфильтровываем, недобор добираем порциями, перебор обрезаем.
    """
    target = int(state.get("target_chapters")
                 or state.get("suggested_chapters") or _DEFAULT_CHAPTERS)
    target = max(2, min(target, _MAX_CHAPTERS))

    chapters: list[Chapter] = _gen_full_structure(state, target)
    chapters = [c for c in chapters
                if not _is_filler_chapter(c.title, c.plan)]

    # недобор после фильтра → добираем порциями (контекст = уже готовые главы)
    guard = 0
    while len(chapters) < target and guard < 4:
        st2 = dict(state)
        st2["chapters"] = chapters
        st2["revision_feedback"] = None
        need = target - len(chapters)
        new, _ = _gen_chapter_batch(st2, len(chapters), need)
        new = [c for c in new if not _is_filler_chapter(c.title, c.plan)]
        if not new:
            break
        chapters.extend(new)
        guard += 1

    chapters = chapters[:target]             # ровно target
    for i, ch in enumerate(chapters):        # переиндексация после фильтра/добора
        ch.index = i

    return {
        "chapters": chapters,
        "chapter_idx": 0,
        "phase": "content",
        "retry_count": state.get("retry_count") or {},
        "structure_done": True,
        "revision_feedback": None,
        "revision_target": None,
        "log": [f"✓ Бот 4: структура готова — {len(chapters)}/{target} глав"],
    }


def _gen_full_structure(state: State, target: int) -> list[Chapter]:
    """Вся структура за один structured-запрос на точное число глав."""
    ctx = (f"Синопсис:\n{state['synopsis']}\n\n"
           f"Карточки:\n{state['characters']}" + _loc_block(state))
    # #7: если план уже есть — пересобираем ПОД новое число глав, растягивая/
    # сжимая существующий сюжет, а не выдумывая с нуля (и не обрезая).
    existing = list(state.get("chapters") or [])
    if existing:
        prev = "\n\n".join(f"Глава {c.index + 1}: {c.title}\n{c.plan}"
                           for c in existing)
        ctx += (f"\n\nТЕКУЩИЙ ПЛАН ({len(existing)} глав) — пересобери его РОВНО "
                f"под {target} глав: растяни или сожми сюжет, сохрани удачные "
                f"главы/повороты и канон, не выдумывай новую историю и не "
                f"обрезай концовку:\n{prev}")
    system = _sys(state, prompts.STRUCTURE, names=True, stage="structure")
    suffix = prompts.STRUCTURE_FULL_SUFFIX.format(n=target)
    try:
        plan = _structural(state, "structure").structured(
            system + suffix + prompts.STRUCTURE_JSON_SUFFIX, ctx,
            StructurePlan, temperature=0.7,
        )
        full = [(c.title, c.plan, c.is_adult, c.adult_note)
                for c in plan.chapters]
        if not full:
            raise ValueError("пустая структура")
    except Exception:  # фолбэк: проза + regex-разбор «ГЛАВА N»
        out = _structural(state, "structure").complete(system + suffix, ctx)
        full = [(c.title, c.plan, True, "") for c in parse_chapters(out)]

    chapters: list[Chapter] = []
    for i, (title, plan_text, _is_adult, note) in enumerate(full):
        if note:  # адалт в каждой главе (по умолчанию)
            plan_text = f"{plan_text}\n\n[ADULT POINT: {note}]"
        chapters.append(Chapter(
            index=i, title=title, plan=plan_text,
            is_adult_point=True, adult_note=note or "",
        ))
    return chapters


def gen_inserted_chapter(state: State, after_idx: int) -> Chapter:
    """#7: сгенерировать ОДНУ новую главу для вставки после after_idx.

    Связывает соседние главы (плавный переход), не переписывая остальные.
    Индекс выставит вызывающий код после вставки/переиндексации.
    """
    chapters = list(state.get("chapters") or [])
    after = chapters[after_idx + 1] if after_idx + 1 < len(chapters) else None
    around = ""
    if after:
        around += f"\n\nСЛЕДУЮЩАЯ глава {after.index + 1}: {after.title}\n{after.plan}"
    # #6: новая глава видит содержимое предыдущих глав (план + хвост текста)
    ctx = (f"Карточки:\n{state.get('characters', '')}" + _loc_block(state)
           + _story_context(state, after_idx + 1)
           + around
           + "\n\nСгенерируй РОВНО ОДНУ новую главу, которая логично встаёт "
           "между предыдущей и следующей (плавный переход, без дыр и повторов). "
           "Соблюдай адалт-разнообразие. Верни массив chapters c одним объектом.")
    system = _sys(state, prompts.STRUCTURE, names=True, stage="structure")
    try:
        plan = _structural(state, "structure").structured(
            system + prompts.STRUCTURE_JSON_SUFFIX, ctx, StructurePlan,
            temperature=0.7)
        c = plan.chapters[0]
        plan_text = c.plan
        if c.adult_note:  # все главы адалт
            plan_text = f"{plan_text}\n\n[ADULT POINT: {c.adult_note}]"
        return Chapter(index=after_idx + 1, title=c.title, plan=plan_text,
                       is_adult_point=True,
                       adult_note=c.adult_note or "")
    except LLMLimitError:
        raise  # лимит → пусть обработает _bg_op (баннер выбора), не глотаем
    except Exception:
        return Chapter(index=after_idx + 1, title="Новая глава",
                       plan="(план не сгенерировался — отредактируй вручную)",
                       is_adult_point=True, adult_note="")


def structure_editor_node(state: State) -> dict:
    """Редактор СТРУКТУРЫ (#2): после завершения плана, ДО написания текстов.

    Проверяет связность/арки/канон/адалт-распределение/точки выбора и САМ
    чинит план — чтобы финальный редактор не тонул в критических ошибках.
    """
    chapters = list(state.get("chapters") or [])
    if not chapters:
        return {"log": ["· Редактор структуры: глав нет, пропуск"]}

    plan_text = "\n\n".join(
        f"Глава {c.index + 1}: {c.title}\n"
        f"[адалт: {'да' if c.is_adult_point else 'нет'}"
        f"{' — ' + c.adult_note if c.adult_note else ''}]\n{c.plan}"
        for c in chapters
    )
    user = (
        f"Синопсис:\n{state.get('synopsis', '')}\n\n"
        f"Карточки персонажей:\n{state.get('characters', '')}"
        + _loc_block(state)
        + f"\n\nПоглавный план ({len(chapters)} глав):\n{plan_text}"
    )
    try:
        out = _structural(state, "structure_editor").structured(
            _sys(state, prompts.STRUCTURE_EDITOR, names=True, stage="structure_editor")
            + prompts.STRUCTURE_EDITOR_JSON,
            user, StructureFixOut, temperature=0.4,
        )
        # количество глав должно сохраниться — иначе не доверяем фиксу
        if len(out.chapters) != len(chapters):
            raise ValueError(
                f"редактор вернул {len(out.chapters)} глав вместо {len(chapters)}")
    except Exception as exc:
        return {"log": [f"⚠ Редактор структуры: пропуск ({str(exc)[:80]})"]}

    fixed = [
        Chapter(index=i, title=c.title, plan=c.plan,
                is_adult_point=True, adult_note=c.adult_note or "")
        for i, c in enumerate(out.chapters)
    ]
    n = len(out.fixes)
    log = [f"✓ Редактор структуры: план проверен — "
           f"{'исправлений нет' if n == 0 else f'{n} исправлений'}"]
    log += [f"  · {fix}" for fix in out.fixes[:8]]
    return {"chapters": fixed, "structure_fixes": out.fixes, "log": log}


# ---------- Боты 5-6-7: поглавный цикл ----------

def _word_count(text: str | None) -> int:
    return len((text or "").split())


def _looks_truncated(text: str | None) -> bool:
    """Грубый детект обрыва: пусто, висит на полуслове, или «продолжение»."""
    t = (text or "").rstrip()
    if not t:
        return True
    low = t.lower()
    if any(m in low[-200:] for m in ("продолжение следует", "[конец части",
                                     "to be continued")):
        return True
    return t[-1] not in '.!?»"”*…)>'


# Маркеры завершённой главы — чтобы НЕ запускать лишний проход добора (он
# переписывает концовку заново → дубли). Главу в формате ВН закрывает «ВЫБОР
# ИГРОКА».
_DONE_MARKERS = ("выбор игрока", "глава завершена", "конец главы",
                 "player choice", "the end", "end of chapter", "chapter ends")


def _looks_complete(text: str | None) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _DONE_MARKERS)


# Хвостовая мета-болтовня модели (вне художественного текста) — срезаем.
_META_SUB = (
    "жди следующую", "жду главу", "жду следующую", "готов. присылай",
    "присылай сценарн", "присылай план", "глава завершена", "показанный текст",
    "структура бита", "— закрыта", "принята. финал", "принята — финал",
    "глава 01 принята", "глава 02", "следующую главу из плана",
    # английские аналоги (вывод теперь на английском)
    "waiting for the next", "send the next", "ready for the next",
    "send chapter", "next chapter when", "chapter complete", "let me know",
    "i'm ready", "ready when you", "awaiting the next", "hope this", "this scene",
)


def _strip_trailing_meta(text: str) -> str:
    """Снять хвостовые строки с мета-комментариями модели (не часть главы)."""
    lines = (text or "").rstrip().split("\n")
    while lines:
        low = lines[-1].strip().lower().strip("*# ")
        if not low:
            lines.pop()
            continue
        if any(m in low for m in _META_SUB):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def _target_words(state: State, ch: Chapter) -> int:
    return int(ch.target_words or state.get("default_words")
               or CHAPTER_WORDS_DEFAULT)


_SHOT_RE = re.compile(
    r"^\s*(\d{2}-[A-Za-z][A-Za-z0-9]*?(Anim)?-\d+)\s*[—:\-]\s*(.+?)\s*$",
    re.MULTILINE)


def _extract_shots(text: str) -> tuple[list[StaticShot], list[StaticShot]]:
    """Парсит теги кадров из текста главы → (статики, анимации) для художника."""
    statics: list[StaticShot] = []
    anims: list[StaticShot] = []
    for m in _SHOT_RE.finditer(text or ""):
        shot = StaticShot(tag=m.group(1), description=m.group(3))
        (anims if m.group(2) else statics).append(shot)
    return statics, anims


def _gen_until_words(llm, system: str, user: str, target: int,
                     max_passes: int) -> str:
    """Пишет текст ИТЕРАТИВНО: первый проход + «продолжи с обрыва», пока не
    наберём ~target слов и текст не завершён (борьба с обрывками/кусками)."""
    text = _strip_trailing_meta(llm.complete(system, user))
    passes = 1
    # добор ТОЛЬКО пока глава НЕ завершена и (оборвана или сильно коротка).
    # завершённую (есть «ВЫБОР ИГРОКА») не трогаем — иначе модель переписывает
    # концовку заново и плодит дубли.
    while passes < max_passes and not _looks_complete(text) and (
            _looks_truncated(text) or _word_count(text) < int(target * 0.85)):
        tail = (text or "")[-1500:]
        cont = _strip_trailing_meta(llm.complete(
            system,
            user + prompts.CHAPTER_CONTINUE_GUIDE.format(
                marker=prompts.ADULT_MARKER, tail=tail)))
        if not cont.strip():
            break
        text = (text or "").rstrip() + "\n\n" + cont.lstrip()
        passes += 1
    return text


def _expand_to_target(llm, system: str, ctx: str, text: str, target: int,
                      max_passes: int, keep_marker: bool = False) -> str:
    """Добивает объём РАСШИРЕНИЕМ НА МЕСТЕ (переписать подробнее, не дописывать),
    когда глава уже завершена, но короче цели. Без дублей: это полная
    обогащённая версия, а не приклеенный хвост."""
    passes = 0
    while passes < max_passes and _word_count(text) < int(target * 0.85):
        add = max(300, target - _word_count(text))
        eu = (ctx + f"\n\nТЕКУЩИЙ ТЕКСТ ГЛАВЫ:\n{text}"
              + prompts.EXPAND_GUIDE.format(add=add))
        if keep_marker:
            eu += (f"\n\nВАЖНО: сохрани строку-метку {prompts.ADULT_MARKER} "
                   "ровно один раз на её месте, НЕ раскрывай саму сцену.")
        new = _strip_trailing_meta(llm.complete(system, eu))
        if _word_count(new) <= _word_count(text) + 50:
            break  # модель не растит — выходим, чтобы не крутиться впустую
        text = new
        passes += 1
    return text


_BRACE_TAG = re.compile(
    r"\{\s*(\d{2}-[A-Za-z][A-Za-z0-9]*(?:Anim)?-\d+)\s*\}")
# грок иногда оборачивает в скобки только номер: {01}-VictorOffice-4
_BRACE_NUM = re.compile(r"\{(\d{2})\}(?=-[A-Za-z])")


def _normalize_tags(text: str) -> str:
    """Снять фигурные скобки вокруг тегов кадров (грок копирует {NN}-… из
    примера) — иначе парсер статиков их не видит, и формат расходится с Claude.
    Ловим оба варианта: {NN-Loc-N} и {NN}-Loc-N."""
    text = _BRACE_TAG.sub(r"\1", text or "")
    return _BRACE_NUM.sub(r"\1", text)


def _insert_adult(state: State, ch: Chapter, idx: int, full: str,
                  words: int) -> str:
    """Грок заполняет КАЖДУЮ метку ADULT_MARKER откровенной сценой.

    В главе может быть НЕСКОЛЬКО меток — для каждой генерим свою сцену с её
    локальным контекстом «до»/«после» (без послесловия → нет дублей). Бюджет
    слов делится между сценами.
    """
    marker = prompts.ADULT_MARKER
    n = full.count(marker)
    if n == 0:
        full = full.rstrip() + "\n\n" + marker
        n = 1
    per = max(500, words // n)               # объём на одну сцену
    pair = ch.adult_note or "см. карточки персонажей"
    system = _sys(state, prompts.ADULT, names=True, stage="adult")
    inserted = 0
    for _ in range(n):
        head, sep, tail = full.partition(marker)
        if not sep:
            break
        before = head.rstrip()[-2500:]       # подводка вплотную к сцене
        after = tail.lstrip()[:1800]         # что идёт ПОСЛЕ — не повторять
        user = (
            prompts.ADULT_INSERT.format(
                note=pair, nn=f"{idx + 1:02d}", words=per,
                before=before, after=after)
            + prompts.ADULT_CANON_GUARD.format(pair=pair))
        try:
            scene = _adult().complete(system, user)
        except LLMLimitError:
            raise
        except Exception:
            scene = ""
        if not scene.strip() or _looks_like_refusal(scene):
            full = full.replace(marker, "", 1)   # эту метку не закрыли — снимаем
            continue
        full = full.replace(marker, _normalize_tags(scene.strip()), 1)
        inserted += 1
    full = full.replace(marker, "")          # подчистить остатки меток
    ch.adult_block_reason = (None if inserted else
                             "Адалт-сцена не сгенерирована — повтори или допиши")
    return _normalize_tags(full).strip()


def write_chapter(state: State, ch: Chapter, idx: int,
                  feedback: str = "") -> Chapter:
    """Пишет ПОЛНУЮ главу по плану (новая схема, #правки):

    Claude (structural) пишет всю главу целиком и ИТЕРАТИВНО добивает объём до
    цели (~target_words) — самодостаточный эпизод, не «кусок». На адалт-главе
    Claude ставит метку [[ADULT_SCENE]], которую грок заменяет полной
    откровенной сценой (история — Claude, адалт — грок). Теги кадров парсятся
    из текста в statics/anims для художника.
    """
    target = _target_words(state, ch)
    is_adult = bool(ch.is_adult_point)
    # на адалт-главе делим бюджет: история (Claude) + сцена (грок)
    story_target = max(800, int(target * 0.6)) if is_adult else target
    adult_words = max(500, int(target * 0.4))

    base = (
        f"Карточки персонажей:\n{state['characters']}" + _loc_block(state)
        + _story_context(state, idx)
        + f"\n\nНомер этой главы для тегов статиков (NN): {idx + 1:02d}\n\n"
        f"Глава для написания:\n{ch.title}\n{ch.plan}"
    )
    if feedback:
        base += f"\n\nТекущий черновик главы:\n{ch.dialogue or ''}\n\n{feedback}"

    system = _sys(state, prompts.DIALOGUE, names=True, stage="dialogue") + prompts.STATICS_DIALOGUE
    user = base + prompts.CHAPTER_FULL_GUIDE.format(
        words=story_target, min=int(story_target * 0.8))
    if is_adult:
        user += prompts.CHAPTER_ADULT_MARKER_GUIDE.format(
            marker=prompts.ADULT_MARKER,
            pair=ch.adult_note or "см. карточки персонажей")

    # Историю всегда пишет Claude/structural (не грок) — связный сюжет.
    llm = _structural(state, "dialogue")
    full = _gen_until_words(llm, system, user, story_target, CHAPTER_MAX_PASSES)
    # завершённая, но короткая глава → добиваем объём расширением на месте
    # (без дублей), пока сцены ещё нет (метка цела)
    if _word_count(full) < int(story_target * 0.85):
        # 1 проход расширения: полный перепис Claude-подпиской медленный,
        # 2+ прохода = десятки минут на главу. 1 даёт ощутимый рост без затрат.
        full = _expand_to_target(llm, system, base, full, story_target,
                                 max_passes=1, keep_marker=is_adult)

    if is_adult:
        full = _insert_adult(state, ch, idx, full, adult_words)

    full = _normalize_tags(_strip_trailing_meta(full))
    ch.dialogue = full
    ch.statics, ch.anims = _extract_shots(full)
    ch.adult_scene = None  # адалт теперь внутри ch.dialogue
    return ch


def expand_chapter(state: State, ch: Chapter, idx: int,
                   add_words: int = 800) -> Chapter:
    """«Растянуть» текст главы: обогатить сцены/диалоги/детали на ~add_words,
    сохранив события и теги. Адалт-главу растягивает грок (без цензуры)."""
    is_adult = bool(ch.is_adult_point)
    statics_block = prompts.STATICS_ADULT if is_adult else prompts.STATICS_DIALOGUE
    system = _sys(state, prompts.DIALOGUE, names=True, stage="dialogue") + statics_block
    user = (
        f"Карточки персонажей:\n{state['characters']}" + _loc_block(state)
        + f"\n\nНомер этой главы для тегов статиков (NN): {idx + 1:02d}\n\n"
        f"ТЕКУЩИЙ ТЕКСТ ГЛАВЫ «{ch.title}»:\n{ch.dialogue or ''}"
        + prompts.EXPAND_GUIDE.format(add=add_words))
    llm = _adult() if is_adult else _structural(state, "dialogue")
    out = llm.complete(system, user)
    if out and _word_count(out) > _word_count(ch.dialogue or ""):
        ch.dialogue = out
        ch.statics, ch.anims = _extract_shots(out)
    return ch


def _open_findings_for(state: State, idx: int) -> list[Finding]:
    """Не отклонённые findings последнего отчёта редактора по главе idx."""
    from .routing import apply_decisions  # noqa: PLC0415
    reports = [r for r in (state.get("editor_reports") or [])
               if r.chapter_index == idx]
    if not reports:
        return []
    last = apply_decisions(reports[-1], state.get("finding_decisions") or {})
    return [f for f in last.findings if f.status != "rejected"]


def patch_chapter(state: State, ch: Chapter, idx: int,
                  findings: list[Finding], extra: str = "") -> Chapter:
    """ТОЧЕЧНАЯ правка главы: чинит ТОЛЬКО фрагменты из замечаний редактора,
    остальной текст возвращается дословно. Заменяет перегенерацию всей главы —
    так критичные реально устраняются, а не плодятся новые.
    """
    items = []
    for f in findings:
        q = (f.quote or "").strip()
        if q:
            items.append(f'- Фрагмент: «{q}»\n  Проблема [{f.block}]: {f.problem}')
        else:
            items.append(f"- {f.locator} [{f.block}]: {f.problem}")
    issues = "\n".join(items) if items else "(конкретных замечаний нет)"

    is_adult = bool(ch.is_adult_point)
    if is_adult:
        system = (_sys(state, prompts.DIALOGUE, names=True, stage="dialogue")
                  + prompts.DIALOGUE_ADULT_EXTRA + prompts.STATICS_ADULT)
        llm = _adult()
    else:
        system = (_sys(state, prompts.DIALOGUE, names=True, stage="dialogue")
                  + prompts.STATICS_DIALOGUE)
        llm = _structural(state, "dialogue")

    user = (
        f"Карточки персонажей (канон):\n{state['characters']}" + _loc_block(state)
        + f"\n\nНомер этой главы для тегов статиков (NN): {idx + 1:02d}\n\n"
        f"ТЕКУЩИЙ ТЕКСТ ГЛАВЫ «{ch.title}»:\n{ch.dialogue or ''}\n\n"
        f"ЗАМЕЧАНИЯ РЕДАКТОРА — исправь ТОЧЕЧНО только эти места:\n{issues}"
    )
    if extra.strip():
        user += (f"\n\nОБСУЖДЕНИЕ с нарративщиком (учти решения и формулировки):"
                 f"\n{extra}")
    if is_adult:  # #1: держать канон участников и при правке
        user += prompts.ADULT_CANON_GUARD.format(
            pair=ch.adult_note or "см. карточки персонажей")
    user += prompts.PATCH_CONTRACT

    try:
        out = llm.structured(system, user, DialogueOut, temperature=0.4)
        ch.dialogue, ch.statics, ch.anims = out.script, out.statics, out.anims
    except Exception:
        ch.dialogue = llm.complete(system, user)
        ch.statics, ch.anims = [], []  # не оставлять статики от прошлого текста
    ch.adult_scene = None
    return ch


def sync_plan_from_dialogue(state: State, ch: Chapter, idx: int) -> str:
    """Подгоняет ПЛАН главы под уже написанный ДИАЛОГ (обратная синхронизация).

    Когда нарративщик правит текст главы вручную — план обновляем, чтобы он
    отражал реальные события/повороты/адалт написанной главы.
    """
    user = (
        f"Карточки персонажей:\n{state['characters']}\n\n"
        f"Готовый текст главы {idx + 1} «{ch.title}»:\n{ch.dialogue or ''}\n\n"
        f"Текущий план главы:\n{ch.plan}\n\n"
        "Обнови план этой главы так, чтобы он ТОЧНО отражал написанный текст: "
        "локации, что происходит, эмоциональная дуга, точки выбора, адалт-точка. "
        "Верни только новый план главы, без преамбул."
    )
    return _structural(state, "dialogue").complete(
        _sys(state, prompts.STRUCTURE, names=True, stage="structure"), user)


@_stream_node("dialogue")
def dialogue_node(state: State) -> dict:
    idx = state["chapter_idx"]
    chapters = list(state["chapters"])
    ch = chapters[idx]
    is_revision = (state.get("revision_target") == "dialogue"
                   and bool(state.get("revision_feedback")))
    if is_revision:
        # ревизия → ТОЧЕЧНАЯ правка по конкретным замечаниям редактора
        findings = _open_findings_for(state, idx)
        if findings:
            ch = patch_chapter(state, ch, idx, findings)
            verb = "правка"
        else:  # нет структурных findings (напр. ручной фидбек) → обычная перепись
            ch = write_chapter(state, ch, idx, state.get("revision_feedback") or "")
            verb = "переписана"
    else:
        ch = write_chapter(state, ch, idx)
        verb = "написана"
    chapters[idx] = ch
    tag = " (с адалтом)" if ch.is_adult_point else ""
    return {
        "chapters": chapters,
        "revision_feedback": None,
        "revision_target": None,
        "log": [f"✓ Бот 5: глава {idx + 1} «{ch.title}» {verb}{tag}"],
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
        f"Номер этой главы для тегов статиков (NN): {idx + 1:02d}\n\n"
        f"Контекст сцены (глава «{ch.title}»):\n{ch.dialogue or ch.plan}"
    )

    pair_hint = ""
    if not is_revision:  # пре-чек только на первичной генерации, не на ретраях
        try:
            chk = _editor(state, "editor").structured(
                prompts.ADULT_FEASIBILITY, ctx, AdultFeasibility)
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
    # Контракт + директива глубины + плотные статики/анимации для художника.
    user += prompts.ADULT_SCENE_DIRECTIVE + prompts.STATICS_ADULT + prompts.NAMES_RULE

    # Строгий JSON-контракт: отказ — поле refused, а не текст для парсинга.
    llm = _adult()
    a_statics, a_anims = [], []
    try:
        res = llm.structured(prompts.ADULT, user + prompts.ADULT_JSON_CONTRACT,
                             AdultSceneOut, temperature=llm.cfg.temperature)
        refused, reason, scene = res.refused, res.reason, res.scene
        a_statics, a_anims = res.statics, res.anims
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
    ch.adult_statics = a_statics
    ch.adult_anims = a_anims
    ch.adult_block_reason = None
    ch.adult_bridge_hint = None
    chapters[idx] = ch
    return {
        "chapters": chapters,
        "revision_feedback": None,
        "revision_target": None,
        "log": [f"✓ Бот 6: адалт-сцена для главы {idx + 1} готова"],
    }


def _finding_id(idx: int, rnd: int, fo) -> str:
    """id замечания: глава + НОМЕР РЕВИЗИИ + block + хэш локатора.

    round обязателен: без него решение (rejected/accepted) из прошлой ревизии
    наследовалось новым finding с тем же locator → «скрывалась случайная правка».
    Семантику «не поднимать отклонённое» несёт rejected_notes (по тексту), а не
    наследование статуса.
    """
    h = hashlib.sha1(f"{fo.block}|{fo.locator}".encode()).hexdigest()[:8]
    return f"c{idx}-r{rnd}-{fo.block}-{h}"


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
    # #2: для адалт-главы характерные нюансы в сексе судим мягче — иначе цикл
    # правок застревает на спорных critical, которые grok всё равно не закроет.
    if ch.is_adult_point:
        user += prompts.EDITOR_ADULT_LENIENCY
    # #8: нарративщик уже отклонил эти претензии — НЕ поднимать их снова.
    rejected = state.get("rejected_notes") or []
    if rejected:
        joined = "\n".join(f"- {n}" for n in rejected[-30:])
        user += (
            "\n\nНарративщик УЖЕ ОТКЛОНИЛ эти претензии как несущественные — "
            f"НЕ повторяй их и ничего похожего:\n{joined}"
        )
    try:
        out: EditorReportOut = _editor(state, "editor").structured(
            _prompt(state, "editor", prompts.EDITOR), user, EditorReportOut)
    except Exception as exc:
        report = EditorReport(chapter_index=idx, findings=[],
                              markdown=f"[редактор пропущен: {exc}]")
        return {
            "editor_reports": [report],
            "log": [f"⚠ Бот 7: глава {idx + 1} — отчёт не распарсился, пропуск"],
        }

    decisions = state.get("finding_decisions") or {}
    # номер текущей ревизии главы = сколько отчётов по ней уже есть
    rnd = len([r for r in (state.get("editor_reports") or [])
               if r.chapter_index == idx])
    findings: list[Finding] = []
    for fo in out.findings:
        fid = _finding_id(idx, rnd, fo)
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
        ch.translation = _structural(state, "translation").complete(
            _prompt(state, "translation", prompts.TRANSLATION),
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
