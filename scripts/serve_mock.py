"""Поднимает реальный FastAPI-бэк, но с МОК-LLM (без сети/денег).

Для ручного/Playwright-прогона фронта по всем кнопкам. Граф мгновенный.
Запуск: .venv/bin/python scripts/serve_mock.py  (порт 8000)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from e2e_test import _patch_llms  # noqa: E402

_patch_llms()  # ДО импорта app: узлы зовут nodes._structural() в рантайме

import uvicorn  # noqa: E402

from server.app import app  # noqa: E402

if __name__ == "__main__":
    print("MOCK backend on :8000 (FakeLLM, без сети)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
