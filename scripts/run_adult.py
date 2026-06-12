"""Разовый прогон ТОЛЬКО Бота 6 (адалт) на конкретной порно-сцене.

Контекст захардкожен (Диана/Вив, раздевалка) — сцена с реальным
романтическим/сексуальным сетапом, как в эталоне заказчика. Цель —
изолированно оценить адалт-модель на нормальном explicit-запросе.

Запуск: .venv/bin/python scripts/run_adult.py
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _out_path(model: str) -> Path:
    slug = model.split("/")[-1].replace(":", "-")
    return DOCS / f"adult_only_{slug}.md"

# --- захардкоженный контекст: карточки + сцена ---
CHARACTERS = """\
ДИАНА КРАЙС, 22 — капитан команды. Доминирует, привыкла к власти и контролю.
Говорит ровно, без восклицаний, холодно-уверенно. Любит подчинять. Внешность:
высокая, спортивная, светлые волосы, пирсинг на сосках, носит белое.
Скрытая цель: сломать маску Вив, заставить её признать поражение.

ВИВ СТЕРН, 20 — новенькая. Притворная покорность, играет в подчинение, но
скрывает истинные мотивы (метит на место капитана, связана с Максом из Милана).
Игривая, дерзкая под маской. Внешность: гибкая, тёмные волосы, насмешливый взгляд."""

SCENE = """\
Локация: раздевалка теннисного клуба, вечер, пусто. После матча.
Что происходит: Диана подавляет и контролирует, Вив притворно покорна, но
наслаждается игрой и подыгрывает. Между ними напряжение власти и желания.
Эмоциональная дуга: от провокации и подчинения — к взаимной кульминации.
Динамика строго по карточкам: Диана сверху и доминирует, Вив отдаёт контроль,
скрывая, что это часть её плана."""

def main() -> int:
    load_dotenv()
    from narrative import prompts
    from narrative.nodes import _adult

    # та же директива глубины, что в проде (nodes.adult_node) — единый источник
    user = (
        f"Карточки персонажей:\n{CHARACTERS}\n\n"
        f"Контекст сцены:\n{SCENE}"
        f"{prompts.ADULT_SCENE_DIRECTIVE}"
    )

    llm = _adult()
    cap = llm.cfg.max_tokens or "без лимита"
    print(f"модель: {llm.cfg.model} | max_tokens={cap} | temp={llm.cfg.temperature}")
    t = time.time()
    out = llm.complete(prompts.ADULT, user)
    dt = time.time() - t

    latin = len(re.findall(r"[A-Za-z]{3,}", out))
    cjk = len(re.findall(r"[　-鿿가-힯]", out))
    meta = f"{dt:.1f}s | chars={len(out)} | latin3+={latin} | cjk={cjk}"
    print(meta)

    out_path = _out_path(llm.cfg.model)
    out_path.write_text(
        f"# Бот 6 (адалт) — изолированный прогон (Диана/Вив)\n\n"
        f"`модель: {llm.cfg.model} | {meta}`\n\n{out}\n",
        encoding="utf-8",
    )
    print(f"\ndump -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
