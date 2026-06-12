# Ресёрч: модели для explicit-контента и языки

Дата: 2026-06-08. Контекст: Бот 6 (адалт) нарративного пайплайна. Контент на
**русском**. Движок — OpenRouter, но адалт-эндпоинт конфигурируем (можно local).

---

## TL;DR

1. **Почти все NSFW-файнтюны — англо-first** (Magnum, Cydonia, EVA, Behemoth,
   MythoMax, Rocinante). На русском деградируют: токен-мусор, переход на
   английский, не доходят до сцены. Эмпирически подтверждено (см. таблицу).
2. **Для русского explicit три пути:**
   - **A. Англ-NSFW → перевод** (Magnum пишет EN, Claude/Grok переводит RU).
   - **B. Мультиязычная permissive/abliterated база** (Grok, GLM, abliterated
     Qwen/Gemma). — лучший баланс качества RU + откровенности.
   - **C. RU-native тюн** (Saiga/RULM) — родной русский, но слабее литературно.
3. **Рекомендация для OpenRouter-пайплайна на RU: начать с `x-ai/grok-4.3`**
   (или `grok-build-0.1`). Исходный док адалт-пример генерил именно Grok —
   сильный русский + допускает explicit + дёшев. Бэкап: GLM-4.6, dolphin-venice.
4. **Максимум качества/контроля** — self-host abliterated **Qwen 3.6** или
   **Gemma 4 Heretic** через vLLM → `ADULT_BASE_URL`. Лучший RU + ноль отказов.

---

## Почему англо-NSFW-файнтюны плохи на русском

NSFW-файнтюны тренируют на английских корпусах RP/литературы (датасеты без
рефьюзов). Файнтюн **перетирает** мультиязычность базы → модель «забывает»
русский. Чем уже и «литературнее» тюн, тем сильнее эффект.

Mistral-база «от природы» менее зацензурена и часто берётся как фундамент
([decodesfuture](https://www.decodesfuture.com/articles/top-uncensored-open-source-ai-models-2026-list),
[arsturn](https://www.arsturn.com/blog/finding-the-best-uncensored-llm-on-ollama-a-deep-dive-guide)),
но Mistral слабее в русском, чем Qwen/Gemma/GLM.

### Эмпирика (мои тесты, RU-сцена, ADULT-промпт Бота 6)

| Модель | latency | поведение на RU |
|---|---|---|
| `magnum-v4-72b` | 6-21с | **корейский+англ мусор** в тексте, не доходит до секса, soft-цензура на кульминацию |
| `cydonia-24b-v4.1` | 14с | высокий англ-bleed (latin3+=56) |
| `unslopnemo-12b` | 11с | чище (latin3+=7), но cjk-мусор |
| `mistral-nemo` | 12с | без мусора, но слабая проза |
| `dolphin-venice:free` | — | free-тариф висит/таймаут |

Вывод: **Magnum/Cydonia для русского as-is — нет.** Они для английского.

---

## Карта моделей по языку × откровенности × доступности

Легенда explicit: 🟢 пишет сам · 🟡 с джейлбрейком/few-shot · 🔴 отказывает.
RU: сила русского. OR = есть на OpenRouter.

| Модель | RU | Explicit | Где | Цена in/out (M) |
|---|---|---|---|---|
| **x-ai/grok-4.3** | 🟢 сильный | 🟡 (permissive) | OR | $1.25/$2.50 |
| **x-ai/grok-build-0.1** | 🟢 сильный | 🟡 | OR | $1.00/$2.00 |
| **z-ai/glm-4.6** | 🟢 хороший | 🟡 | OR | $0.43/$1.74 |
| **Qwen 3.6 27B abliterated/Heretic** | 🟢 сильный | 🟢 | local (HF) | self-host |
| **Gemma 4 31B Heretic** | 🟢 сильный (140+ яз) | 🟢 | local; база на OR 🔴 | $0.12/$0.36 (база) |
| dolphin-mistral-24b-venice | 🟡 средний | 🟢 | OR :free | $0.00 |
| mistralai/mistral-small-3.2-24b | 🟡 средний | 🟡 | OR | $0.07/$0.20 |
| Hermes 4 70B (Llama) | 🟡 слабее | 🟢 (RP-тюн) | OR | $0.13/$0.40 |
| anthracite-org/magnum-v4-72b | 🔴 ломается | 🟡 (EN 🟢) | OR | $3/$5 |
| thedrummer/cydonia-24b-v4.1 | 🔴 bleed | 🟢 (EN) | OR | $0.30/$0.50 |
| EVA LLaMA 3.33 70B | 🔴 (Llama) | 🟢 (EN) | local/др. | — |
| Behemoth 123B | 🔴 (на OR нет) | 🟢 (EN) | local | — |
| Saiga / RULM (IlyaGusev) | 🟢 native | 🟡 | local (HF) | self-host |

> EVA Qwen 72B и Behemoth 123B из proposal **на OpenRouter отсутствуют** —
> только локально или через спец-провайдера (Featherless/Infermatic).

---

## Три стратегии под проект

### A. Англ-NSFW → перевод (минимум инфры)
Magnum/EVA/Behemoth **на английском** сильны (там цензуры на кульминацию меньше,
мусора нет). Генерим адалт EN → Бот 8 переводит на RU.
- **+** топовая англ-проза, всё на OpenRouter, дёшево.
- **−** двойной вызов; переводчик должен быть uncensored (Claude переводит
  explicit неохотно — лучше Grok/GLM как переводчик); риск потери игры слов.

### B. Мультиязык permissive/abliterated (рекоменд. дефолт)
Модель с сильной RU-базой и снятой цензурой.
- **OpenRouter, без инфры:** `x-ai/grok-4.3`, `z-ai/glm-4.6`,
  `dolphin-mistral-24b-venice:free`. Grok — первый кандидат (док уже на нём).
- **Self-host, максимум:** abliterated **Qwen 3.6 27B** или **Gemma 4 Heretic**
  через vLLM → `ADULT_BASE_URL`. Abliteration снимает рефьюзы ценой ~1-3%
  качества, мультиязычность базы сохраняется
  ([guide](https://locallyuncensored.com/blog/abliterated-models-guide.html)).

### C. RU-native тюн
Saiga/RULM ([IlyaGusev/rulm](https://github.com/IlyaGusev/rulm)) — родной русский,
датасеты ru_saiga/gpt_roleplay_realm. Но модели меньше/старее → проза слабее
литературного уровня дока. Как переводчик/полировщик RU — годится.

---

## ⚡ Эмпирический eval (2026-06-08, `scripts/eval_nsfw.py`, трата $0.01)

RU-сцена, ADULT-промпт, max_tokens=600. Ранг по факту текста (не метрикам):

| Модель | Вердикт | RU | Explicit | Цена/сцена |
|---|---|---|---|---|
| **google/gemma-4-31b-it:free** ⭐ | **ЛУЧШИЙ**: полная сцена завязка→акт→оргазм→послевкусие, идеальный RU, в характере, формат CG/Аним | 🟢 чистый | 🟢 полный | **$0** |
| **x-ai/grok-4.3** | сильный: explicit (oral/контроль), отличный голос персонажей, обрезан на пике (cap) | 🟢 | 🟢 | $0.004 |
| thedrummer/cydonia-24b-v4.1 | ОК: чистый RU (в этот раз), в характере, но «DIана» порча имён, обрыв на cap | 🟡 | 🟡 | $0.0005 |
| z-ai/glm-4.5-air:free | начал explicit, обрезан на 600 ток (коротко) | 🟢 | 🟡? | $0 |
| mistralai/mistral-nemo | мусор: «практикуешьuju», «Кошkasantly», RU/EN каша | 🔴 | — | $0 |
| z-ai/glm-4.6 | **отказ** — пустой ответ (1 символ) | — | 🔴 | $0.001 |
| dolphin-venice:free | 429 rate-limit (Venice), free-тариф нестабилен | — | — | $0 |
| anthracite-org/magnum-v4-72b (EN) | стоп на 72 токенах — только вступление, не доходит до сцены даже на EN | (EN) | 🔴 обрыв | $0.002 |

**Перевернуло гипотезу:** Gemma 4 31B (база, не abliterated) **пошла в полный explicit** на отличном русском — и она **бесплатна** на OpenRouter. Обходит профильные NSFW-файнтюны (Magnum/Cydonia) на русском. Grok 4.3 — лучший платный. Magnum даже на английском обрывается на вступлении (нужен continuation-промптинг). GLM-4.6 (платный) отказал, а GLM-air (free) — нет.

**Перевод (стратегия A):** Grok перевёл Magnum-EN на RU идеально (latin=1) — translate-шаг рабочий, но узкое место — сам Magnum (обрыв).

## Рекомендация

**Шаг 1 (сразу, по факту eval):** `ADULT_MODEL=google/gemma-4-31b-it:free` —
лучший RU + полный explicit + **бесплатно**. Бэкап/качество: `x-ai/grok-4.3`
($0.004/сцена). Поднять `ADULT_MAX_TOKENS` до ~1500 (в eval сцены резались на
600). Magnum/Cydonia для RU — мимо.

**Шаг 2 (если нужен максимум):** поднять abliterated **Qwen 3.6 27B** на vLLM
(сервер заказчика), указать `ADULT_BASE_URL`. Лучшая комбинация RU + ноль
отказов + полный контроль/приватность.

**Шаг 3 (eval):** прогнать 8-10 эталонных RU-сцен (как в proposal) через
финалистов, оценить: беглость RU · готовность к explicit · удержание
персонажа/формата · стабильность · цена. Скрипт `scripts/eval_nsfw.py` готов,
надо лишь дополнить список моделей и долить кредиты.

> Это и есть «ресёрч-блок» из proposal. Финальная рекомендация — после eval на
> реальных сценах, не только по этим данным.

---

## Источники
- [decodesfuture — Best Uncensored AI Models 2026](https://www.decodesfuture.com/articles/best-uncensored-ai-models-2026-industry-report) / [35+ list](https://www.decodesfuture.com/articles/top-uncensored-open-source-ai-models-2026-list)
- [atlascloud — 20 Uncensored Models Ranked](https://www.atlascloud.ai/blog/guides/best-uncensored-ai-models)
- [arsturn — Best Uncensored LLM on Ollama](https://www.arsturn.com/blog/finding-the-best-uncensored-llm-on-ollama-a-deep-dive-guide)
- [Abliterated Models Guide (Qwen 3.6 / Gemma 4 Heretic)](https://locallyuncensored.com/blog/abliterated-models-guide.html) · [DEV mirror](https://dev.to/purpledoubled/abliterated-models-guide-qwen-36-gemma-4-heretic-llama-31-uncensored-download-links-1f4e)
- [noviai — Best LLMs for Roleplay 2026](https://www.noviai.ai/models-prompts/best-llm-for-roleplay/)
- [IlyaGusev/rulm — Russian LM/tuning](https://github.com/IlyaGusev/rulm)
- [DavidAU Heretic collection (HF)](https://huggingface.co/collections/DavidAU/heretic-abliterated-uncensored-unrestricted-power)
- Эмпирика: `scripts/eval_nsfw.py`, прямые прогоны через OpenRouter (2026-06-08).
