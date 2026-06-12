"""Экономный eval NSFW-генерации: как модели справляются с адалт-сценой на RU.

НЕ тестирует пайплайн — только Бот-6-генерацию по моделям.
Считает реальную трату из usage. Жёсткий cap COST_CAP — стоп до перерасхода.
Вывод → docs/model_eval.md.
Запуск: .venv/bin/python scripts/eval_nsfw.py
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from narrative.prompts import ADULT, TRANSLATION

OUT = Path(__file__).resolve().parent.parent / "docs" / "model_eval.md"
MAX_TOKENS = 600
COST_CAP = 1.00  # $ — жёсткий потолок, дальше стоп (бюджет $5, тратим <$1)

# (slug, in$/M, out$/M). Дёшево/free сначала. Magnum дорогой — только EN→перевод.
RU_MODELS = [
    ("cognitivecomputations/dolphin-mistral-24b-venice-edition:free", 0.0, 0.0),
    ("z-ai/glm-4.5-air:free", 0.0, 0.0),
    ("google/gemma-4-31b-it:free", 0.0, 0.0),
    ("mistralai/mistral-nemo", 0.02, 0.03),
    ("thedrummer/cydonia-24b-v4.1", 0.30, 0.50),
    ("z-ai/glm-4.6", 0.43, 1.74),
    ("x-ai/grok-4.3", 1.25, 2.50),  # ⭐ RU + permissive
]

CARDS = (
    "ДИАНА КРАЙС, 22 — капитан, доминирует, говорит ровно без восклицаний, "
    "пирсинг на сосках, в белом. ВИВ СТЕРН, 20 — притворная покорность, "
    "скрывает цели, игривая."
)
SCENE_RU = (
    CARDS + "\n\nСцена: раздевалка клуба, вечер. Диана подавляет и контролирует, "
    "Вив притворно покорна. Напиши откровенную секс-сцену 18+ в формате "
    "визуальной новеллы (ИМЯ: реплика + (действие)/Аним:). Откровенно, "
    "доведи до кульминации. Минимум 10 реплик."
)
SCENE_EN = (
    "DIANA KRACE, 22 — captain, dominant, speaks evenly, nipple piercings, "
    "in white. VIV STERN, 20 — feigned submission, hides her motives.\n\n"
    "Scene: club locker room, evening. Diana dominates, Viv plays submissive. "
    "Write an explicit 18+ sex scene in visual-novel format (NAME: line + "
    "(action)/Anim:). Explicit, build to climax. At least 10 lines."
)

_client = None
_spent = 0.0


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.environ.get("OPENROUTER_BASE_URL",
                                    "https://openrouter.ai/api/v1"),
            api_key=os.environ["OPENROUTER_API_KEY"],
            timeout=90,
        )
    return _client


def _metrics(text: str) -> str:
    latin = len(re.findall(r"[A-Za-z]{3,}", text))
    cjk = len(re.findall(r"[　-鿿가-힯]", text))
    return f"chars={len(text)} latin3+={latin} cjk-мусор={cjk}"


def gen(model: str, in_price: float, out_price: float,
        system: str, user: str) -> tuple[str, str]:
    """Один вызов. Возвращает (текст, мета со стоимостью). Обновляет _spent."""
    global _spent
    if _spent >= COST_CAP:
        return "[ПРОПУЩЕНО: достигнут COST_CAP]", "skipped"
    start = time.time()
    try:
        resp = client().chat.completions.create(
            model=model, temperature=0.9, max_tokens=MAX_TOKENS,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        out = resp.choices[0].message.content or ""
        u = resp.usage
        cost = (u.prompt_tokens * in_price + u.completion_tokens * out_price) / 1e6
        _spent += cost
        meta = (f"{time.time() - start:.1f}s | {_metrics(out)} | "
                f"tok {u.prompt_tokens}+{u.completion_tokens} | "
                f"${cost:.4f} | всего ${_spent:.4f}")
    except Exception as exc:
        out, meta = f"[ОШИБКА] {exc}", f"{time.time() - start:.1f}s | FAILED"
    return out, meta


def flush(blocks: list[str]) -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(blocks), encoding="utf-8")


def main() -> int:
    load_dotenv()
    blocks = [
        "# Eval NSFW-генерации по моделям (RU адалт-сцена)\n",
        f"max_tokens={MAX_TOKENS}, temp=0.9, cost_cap=${COST_CAP}. "
        "Промпт — ADULT (Бот 6).\n",
        "## RU-генерация\n",
    ]
    flush(blocks)

    for model, pin, pout in RU_MODELS:
        print(f"→ {model}")
        out, meta = gen(model, pin, pout, ADULT, SCENE_RU)
        print(f"   {meta}")
        blocks.append(f"### {model}\n\n`{meta}`\n\n{out}\n")
        flush(blocks)

    # Стратегия A: Magnum пишет EN → Grok переводит RU (uncensored переводчик).
    print("→ [strategy A] Magnum EN → Grok RU")
    en, en_meta = gen("anthracite-org/magnum-v4-72b", 3.0, 5.0, ADULT, SCENE_EN)
    ru, ru_meta = gen("x-ai/grok-4.3", 1.25, 2.50, TRANSLATION,
                      f"Целевой язык: русский\n\nТекст:\n{en}")
    print(f"   EN {en_meta}\n   RU {ru_meta}")
    blocks.append("## Стратегия A: Magnum (EN) → Grok (перевод RU)\n")
    blocks.append(f"### Magnum EN-оригинал\n\n`{en_meta}`\n\n{en}\n")
    blocks.append(f"### Grok перевод RU\n\n`{ru_meta}`\n\n{ru}\n")
    flush(blocks)

    print(f"\nИТОГО потрачено: ${_spent:.4f} из ${COST_CAP} cap (бюджет $5)")
    print(f"Дамп → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
