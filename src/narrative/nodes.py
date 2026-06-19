"""Узлы графа = боты. Каждый: дословный промпт + вызов LLM + запись в State.

Линейные боты (1-4) пишут общие артефакты. Боты 5-6-7 крутятся в поглавном
цикле (одна глава за раз). Бот 8 — финальный перевод (опционально, по флагу).
"""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache

from . import live, prompts
from .config import adult_provider, editor_provider, structural_provider
from .llm import LLM, LLMLimitError
from .routing import retry_key
from .state import (
    AdultFeasibility,
    AdultSceneOut,
    Chapter,
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


def _sys(state: State, base: str, names: bool = False) -> str:
    """Системный промпт + жанр (+ правило западных имён, где нужно)."""
    out = prompts.with_genre(base, _genre(state))
    if names:
        out += prompts.NAMES_RULE
    return out


def _loc_block(state: State) -> str:
    """Блок канон-локаций для контекста (пусто, если бот локаций не отработал)."""
    loc = (state.get("locations") or "").strip()
    return (f"\n\nЛокации (канон — используй эти места и их CamelCase-теги "
            f"для статиков):\n{loc}") if loc else ""


# ---------- Боты 1-4: линейная часть ----------

@_stream_node("logline")
def logline_node(state: State) -> dict:
    fb = state.get("revision_feedback") or ""
    user = f"Тема и референсы:\n{state['theme']}"
    if fb:
        user += f"\n\nПрошлые логлайны:\n{state.get('logline', '')}\n\n{fb}"
    user += prompts.NO_META
    out = _structural(state, "logline").complete(
        _sys(state, prompts.LOGLINE, names=True), user)
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
        _sys(state, prompts.SYNOPSIS, names=True), user)
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
        _sys(state, prompts.CHARACTERS, names=True), user)
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
        _sys(state, prompts.LOCATIONS, names=True), user)
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

    system = _sys(state, prompts.STRUCTURE, names=True)
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
            plan_text = f"{plan_text}\n\n[Адалт-точка: {note}]"
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
    system = _sys(state, prompts.STRUCTURE, names=True)
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
            plan_text = f"{plan_text}\n\n[Адалт-точка: {note}]"
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
    before = chapters[after_idx] if 0 <= after_idx < len(chapters) else None
    after = chapters[after_idx + 1] if after_idx + 1 < len(chapters) else None
    around = ""
    if before:
        around += f"\n\nПРЕДЫДУЩАЯ глава {before.index + 1}: {before.title}\n{before.plan}"
    if after:
        around += f"\n\nСЛЕДУЮЩАЯ глава {after.index + 1}: {after.title}\n{after.plan}"
    ctx = (f"Синопсис:\n{state.get('synopsis', '')}\n\n"
           f"Карточки:\n{state.get('characters', '')}" + _loc_block(state)
           + around
           + "\n\nСгенерируй РОВНО ОДНУ новую главу, которая логично встаёт "
           "между предыдущей и следующей (плавный переход, без дыр и повторов). "
           "Соблюдай адалт-разнообразие. Верни массив chapters c одним объектом.")
    system = _sys(state, prompts.STRUCTURE, names=True)
    try:
        plan = _structural(state, "structure").structured(
            system + prompts.STRUCTURE_JSON_SUFFIX, ctx, StructurePlan,
            temperature=0.7)
        c = plan.chapters[0]
        plan_text = c.plan
        if c.is_adult and c.adult_note:
            plan_text = f"{plan_text}\n\n[Адалт-точка: {c.adult_note}]"
        return Chapter(index=after_idx + 1, title=c.title, plan=plan_text,
                       is_adult_point=bool(c.is_adult),
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
            _sys(state, prompts.STRUCTURE_EDITOR, names=True)
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
                is_adult_point=bool(c.is_adult), adult_note=c.adult_note or "")
        for i, c in enumerate(out.chapters)
    ]
    n = len(out.fixes)
    log = [f"✓ Редактор структуры: план проверен — "
           f"{'исправлений нет' if n == 0 else f'{n} исправлений'}"]
    log += [f"  · {fix}" for fix in out.fixes[:8]]
    return {"chapters": fixed, "structure_fixes": out.fixes, "log": log}


# ---------- Боты 5-6-7: поглавный цикл ----------

def write_chapter(state: State, ch: Chapter, idx: int,
                  feedback: str = "") -> Chapter:
    """Пишет текст главы (диалоги + адалт внутри) по её ПЛАНУ.

    Общий код для Бота 5 (нода) и для ручной перегенерации главы из UI
    (правка плана → переписать диалог). Возвращает обновлённый ch
    (dialogue/statics/anims). Адалт-глава пишется uncensored-моделью.
    """
    user = (
        f"Карточки персонажей:\n{state['characters']}" + _loc_block(state)
        + f"\n\nНомер этой главы для тегов статиков (NN): {idx + 1:02d}\n\n"
        f"Глава для написания:\n{ch.title}\n{ch.plan}"
    )
    if feedback:
        user += f"\n\nТекущий черновик главы:\n{ch.dialogue or ''}\n\n{feedback}"
    user += (
        "\n\nВыведи ТОЛЬКО готовый текст этой главы в заданном формате "
        "визуальной новеллы. Никаких преамбул, вопросов, комментариев и "
        "пояснений. Если это правка — верни ПОЛНУЮ переписанную главу целиком."
    )
    is_adult = bool(ch.is_adult_point)
    if is_adult:
        system = (_sys(state, prompts.DIALOGUE, names=True)
                  + prompts.DIALOGUE_ADULT_EXTRA + prompts.STATICS_ADULT)
        # #1: канон-гард участников — держит характер, не тонет в карточках
        user += prompts.ADULT_CANON_GUARD.format(
            pair=ch.adult_note or "см. карточки персонажей")
        llm = _adult()
    else:
        system = _sys(state, prompts.DIALOGUE, names=True) + prompts.STATICS_DIALOGUE
        # модель = подписка Claude (если вкл) ИНАЧЕ фоллбек на OpenRouter-ключ
        llm = _structural(state, "dialogue")

    try:
        out = llm.structured(system, user, DialogueOut, temperature=0.7)
        ch.dialogue, ch.statics, ch.anims = out.script, out.statics, out.anims
    except Exception:
        # фолбэк на прозу: чистим статики/анимации — иначе остались бы от
        # прошлого (другого) текста главы и сбили бы художника/UI.
        ch.dialogue = llm.complete(system, user)
        ch.statics, ch.anims = [], []
    ch.adult_scene = None  # адалт теперь внутри ch.dialogue, не вставкой
    # #4: адалт-сцена вышла короткой → один добор объёма (grok иногда мельчит)
    if is_adult and _too_short(ch.dialogue):
        try:
            out = llm.structured(system, user + prompts.ADULT_EXPAND,
                                 DialogueOut, temperature=0.7)
            if out.script and len(out.script) > len(ch.dialogue or ""):
                ch.dialogue, ch.statics, ch.anims = out.script, out.statics, out.anims
        except Exception:
            pass
    return ch


_MIN_ADULT_CHARS = 2500  # порог «слишком короткой» адалт-сцены


def _too_short(text: str | None) -> bool:
    t = text or ""
    return len(t) < _MIN_ADULT_CHARS or t.count('"') < 24


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
        system = (_sys(state, prompts.DIALOGUE, names=True)
                  + prompts.DIALOGUE_ADULT_EXTRA + prompts.STATICS_ADULT)
        llm = _adult()
    else:
        system = (_sys(state, prompts.DIALOGUE, names=True)
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
        _sys(state, prompts.STRUCTURE, names=True), user)


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
            prompts.EDITOR, user, EditorReportOut)
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
