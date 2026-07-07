"""Быстрый перевод через бесплатный Google Translate (без ключа, без ИИ).

Используется и финальным Ботом 8 (перевод новеллы на все языки), и кнопкой
«перевести на русский» в UI, и показом замечаний редактора на русском.
"""
from __future__ import annotations

import time

import httpx

# (code — как просил нарративщик, отображаемое имя, tl-код для Google Translate).
# Первый — английский: это ЯЗЫК ОРИГИНАЛА (генерация идёт на английском), на него
# не переводим. Остальные — целевые языки перевода.
LANGUAGES: list[tuple[str, str, str]] = [
    ("en", "English (оригинал)", "en"),
    ("zh", "Китайский (упрощённый)", "zh-CN"),
    ("ru", "Русский", "ru"),
    ("es", "Испанский", "es"),
    ("pt", "Португальский", "pt"),
    ("de", "Немецкий", "de"),
    ("ja", "Японский", "ja"),
    ("fr", "Французский", "fr"),
    ("pl", "Польский", "pl"),
    ("tr", "Турецкий", "tr"),
    ("ko", "Корейский", "ko"),
    ("ua", "Украинский", "uk"),
    ("it", "Итальянский", "it"),
    ("cs", "Чешский", "cs"),
    ("hu", "Венгерский", "hu"),
    ("sv", "Шведский", "sv"),
    ("nl", "Нидерландский", "nl"),
    ("da", "Датский", "da"),
    ("fi", "Финский", "fi"),
    ("ro", "Румынский", "ro"),
    ("el", "Греческий", "el"),
    ("bg", "Болгарский", "bg"),
    ("id", "Индонезийский", "id"),
    ("et", "Эстонский", "et"),
    ("zh-HANT", "Китайский (традиционный)", "zh-TW"),
    ("th", "Тайский", "th"),
    ("vi", "Вьетнамский", "vi"),
]

# code → tl-код для Google (учёт зеркал ua→uk, zh→zh-CN, zh-HANT→zh-TW)
TL_BY_CODE: dict[str, str] = {code: tl for code, _name, tl in LANGUAGES}
NAME_BY_CODE: dict[str, str] = {code: name for code, name, _tl in LANGUAGES}
# целевые языки перевода (всё кроме оригинала-английского)
TARGET_CODES: list[str] = [code for code, _n, _t in LANGUAGES if code != "en"]


def _chunks(text: str, limit: int = 1800) -> list[str]:
    # 1800 сырых символов: после percent-encoding (кавычки-ёлочки, тире, юникод
    # ×3-9 байт) URL остаётся в пределах ~8КБ лимита прокси/балансировщиков
    """Бьём текст на куски < limit символов по границам строк (лимит запроса)."""
    out: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit and buf:
            out.append(buf)
            buf = ""
        while len(line) > limit:
            out.append(line[:limit])
            line = line[limit:]
        buf += line
    if buf:
        out.append(buf)
    return out or [text]


def google_translate(text: str, tl: str, *, tries: int = 1,
                     timeout: float = 8.0) -> str:
    """Перевести text на язык tl через endpoint Google Translate (sl=auto).

    tries=1 (дефолт) — БЫСТРО, без ретраев: для интерактивной кнопки перевода.
    Если Google недоступен/блокирует (напр. datacenter-IP удалённого сервера) —
    сразу бросаем исключение, чтобы вызывающий ушёл в фолбэк (LLM), а не висел.
    tries>1 — с бэкоффом на 429/5xx: для БАТЧ-перевода всех глав (Google троттлит).
    """
    if not (text or "").strip():
        return text
    tries = max(1, tries)
    parts: list[str] = []
    with httpx.Client(timeout=timeout) as cli:
        for chunk in _chunks(text):
            seg = ""
            for attempt in range(tries):
                try:
                    r = cli.get(
                        "https://translate.googleapis.com/translate_a/single",
                        params={"client": "gtx", "sl": "auto", "tl": tl,
                                "dt": "t", "q": chunk},
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    r.raise_for_status()
                    data = r.json()
                    seg = "".join(s[0] for s in (data[0] or []) if s and s[0])
                    break
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code in (429, 500, 502, 503) and attempt < tries - 1:
                        time.sleep(min(1.5 ** attempt, 6.0))  # backoff и повтор
                        continue
                    raise
                except (httpx.TransportError, httpx.TimeoutException):
                    if attempt < tries - 1:
                        time.sleep(min(1.5 ** attempt, 6.0))
                        continue
                    raise
            parts.append(seg)
    return "".join(parts)
