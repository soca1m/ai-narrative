"""Сравнение адалт-генерации: grok-4.3 vs grok-4.20 на одной сцене.

Один контекст (карточки + план адалт-главы) → обе модели → сравнить:
refused, длину, число реплик, статики/анимации, следование канону характера.

Запуск: .venv/bin/python scripts/test_adult_model.py
"""
from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from narrative import prompts  # noqa: E402
from narrative.config import adult_provider  # noqa: E402
from narrative.llm import LLM  # noqa: E402
from narrative.state import AdultSceneOut  # noqa: E402

MODELS = [
    "x-ai/grok-4.3",
    "deepseek/deepseek-v3.2",
    "deepseek/deepseek-v4-flash",
    "thedrummer/cydonia-24b-v4.1",
]

CHARACTERS = """\
Диана, 31 — совладелица клуба. Холодная, расчётливая. КАНОН РЕЧИ: НИКОГДА не \
угрожает прямо и не задаёт вопросов в лоб; говорит тёплыми комплиментами, \
полунамёками, ни одного лишнего слова. Скрытая цель: подчинить Эмму, не \
показав власти. Фетиш: контроль через мягкость.
Эмма, 24 — новый тренер. Гордая, прямая, легко заводится. КАНОН: говорит \
коротко и резко, не любит игры в намёки. Под напором сначала сопротивляется."""

PLAN = """\
Глава 3. Локация: личный кабинет Дианы поздним вечером после матча. \
Диана вызывает Эмму «обсудить контракт», но истинная цель — близость и \
утверждение власти. Напряжение между ними перерастает в откровенную сцену. \
[Адалт-точка: Диана и Эмма, через мягкое доминирование Дианы]"""


def run_one(model: str) -> dict:
    cfg = replace(adult_provider(), model=model)
    llm = LLM(cfg)
    user = (
        f"Карточки персонажей:\n{CHARACTERS}\n\n"
        f"Номер этой главы для тегов статиков (NN): 03\n\n"
        f"Контекст сцены (глава «Сет до конца»):\n{PLAN}"
        + prompts.ADULT_SCENE_DIRECTIVE + prompts.STATICS_ADULT
        + prompts.NAMES_RULE + prompts.ADULT_JSON_CONTRACT
    )
    t0 = time.time()
    try:
        res = llm.structured(prompts.ADULT, user, AdultSceneOut,
                             temperature=cfg.temperature)
        dt = time.time() - t0
        scene = res.scene or ""
        return {
            "model": model, "ok": True, "dt": dt, "refused": res.refused,
            "reason": res.reason, "scene_len": len(scene),
            "lines": scene.count('"') // 2,
            "statics": len(res.statics), "anims": len(res.anims),
            "head": scene[:700],
        }
    except Exception as exc:
        return {"model": model, "ok": False, "dt": time.time() - t0,
                "err": str(exc)[:200]}


def main() -> int:
    load_dotenv()
    print(f"Контекст: адалт-сцена Диана×Эмма, канон Дианы = «не угрожает "
          f"прямо, мягкие намёки»\n{'=' * 70}")
    for model in MODELS:
        print(f"\n### {model}", flush=True)
        r = run_one(model)
        if not r["ok"]:
            print(f"  ✗ ОШИБКА ({r['dt']:.0f}s): {r['err']}", flush=True)
            continue
        if r["refused"]:
            print(f"  ⚠ ОТКАЗ ({r['dt']:.0f}s): {r['reason']}", flush=True)
            continue
        print(f"  ✓ {r['dt']:.0f}s · {r['scene_len']} симв · ~{r['lines']} реплик "
              f"· статиков {r['statics']} · анимаций {r['anims']}", flush=True)
        print(f"  --- начало сцены ---\n{r['head']}\n", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
