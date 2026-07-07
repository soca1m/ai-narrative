// Клиент API. Пусто = same-origin: браузер ходит на Next (:3000), а Next
// проксирует /api/* на бэкенд (см. next.config.mjs rewrites).
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export type FindingStatus = "open" | "accepted" | "rejected";

export type Finding = {
  id: string;
  severity: "critical" | "important" | "minor";
  block: "names" | "motivation" | "gender" | "style";
  responsible_node: string;
  locator: string;
  quote: string;
  problem: string;
  status: FindingStatus;
  user_comment: string;
  judge_reason: string;
};

export type EditorReport = {
  chapter_index: number;
  findings: Finding[];
  markdown: string;
};

export type Chapter = {
  index: number;
  title: string;
  plan: string;
  is_adult_point: boolean;
  dialogue: string | null;
  adult_scene: string | null;
  translation: string | null;
  translations?: Record<string, string>;
  adult_block_reason: string | null;
  adult_bridge_hint: string | null;
  target_words?: number | null;
};

export type NarrativeState = {
  theme?: string;
  genre?: string | null;
  target_language?: string;
  translation_enabled?: boolean;
  chapters_per_batch?: number;
  chapter_model?: string | null;
  loglines?: string[];
  selected_logline?: string;
  logline?: string;
  synopsis?: string;
  characters?: string;
  locations?: string;
  default_words?: number;
  stage_providers?: Record<string, string>;
  force_openrouter?: boolean;
  prompt_overrides?: Record<string, string>;
  limit_info?: LimitInfo | null;
  chapters?: Chapter[];
  chapter_idx?: number;
  phase?: string;
  suggested_chapters?: number;
  count_reason?: string;
  target_chapters?: number;
  structure_done?: boolean;
  structure_fixes?: string[];
  editor_reports?: EditorReport[];
  finding_decisions?: Record<string, { status?: string; comment?: string; judged?: boolean }>;
  retry_count?: Record<string, number>;
  log?: string[];
};

export type StartReq = {
  theme: string;
  genre?: string;
  target_language: string;
  translation_enabled: boolean;
  step_mode: boolean;
  chapter_model?: string;
  default_words?: number;   // цель слов/главу (0/пусто → дефолт ~3500)
  prompt_overrides?: Record<string, string>;  // dev-оверрайды промптов до старта
};

// Безопасный разбор ответа: не падаем с «Unexpected token» на не-JSON
// (напр. бэкенд отдал «Internal Server Error» или прокси вернул HTML).
async function parseJsonSafe<T = any>(r: Response, url: string): Promise<T> {
  const text = await r.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* не JSON */ }
  if (!r.ok) {
    const detail = (data && (data.detail?.message || data.detail || data.error))
      || (text ? text.slice(0, 160) : "");
    throw new Error(`${url}: ${r.status}${detail ? ` — ${detail}` : ""}`);
  }
  if (data === null && text) {
    throw new Error(`${url}: некорректный ответ сервера (не JSON)`);
  }
  return data as T;
}

async function jget<T = any>(url: string): Promise<T> {
  const r = await fetch(`${API_BASE}${url}`);
  return parseJsonSafe<T>(r, url);
}

async function jpost<T = any>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${url}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parseJsonSafe<T>(r, url);
}

export async function startRun(req: StartReq): Promise<{ thread_id: string }> {
  return jpost("/api/runs", req);
}

export type RunSummary = {
  thread_id: string; theme: string; chapters: number;
  written: number; status: string;
};
export const listRuns = (): Promise<{ runs: RunSummary[] }> =>
  jget("/api/runs");

export type LimitInfo = {
  provider: string; reset_at?: number | null; kind?: string; message?: string;
};

export async function getState(
  threadId: string,
): Promise<{ status: string; next: string[]; error?: string;
  limit?: LimitInfo | null;
  gen?: { stage: string; idx: number | null; text: string } | null;
  structure_dirty?: boolean;
  state: NarrativeState }> {
  return jget(`/api/runs/${threadId}/state`);
}

export async function patchState(
  threadId: string,
  patch: Record<string, unknown>,
): Promise<void> {
  const r = await fetch(`${API_BASE}/api/runs/${threadId}/state`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patch }),
  });
  if (!r.ok) throw new Error(`patch failed: ${r.status}`);
}

export const resumeRun = (id: string) => jpost(`/api/runs/${id}/resume`);
// остановить генерацию на любом этапе (мягкая пауза, прогресс сохранён)
export const stopRun = (id: string) => jpost(`/api/runs/${id}/stop`);
// проверка структуры (бот) после ручных правок глав / пропустить проверку
export const checkStructure = (id: string) => jpost(`/api/runs/${id}/structure/check`);
export const skipStructureCheck = (id: string) => jpost(`/api/runs/${id}/structure/skip_check`);
// пропустить ЭТАП проверки структуры в пайплайне → сразу к написанию глав
export const skipStructureStage = (id: string) => jpost(`/api/runs/${id}/structure/skip_stage`);

export const selectLogline = (id: string, logline: string) =>
  jpost(`/api/runs/${id}/select_logline`, { logline });

export const reviseStage = (id: string, stage: string, feedback: string, chapter_idx?: number) =>
  jpost(`/api/runs/${id}/stage/${stage}/revise`, { feedback, chapter_idx });

export const reviseChapter = (id: string, idx: number, feedback: string) =>
  jpost(`/api/runs/${id}/chapter/${idx}/revise`, { feedback });

// план → переписать диалоги главы; диалоги → подогнать план
export const rewriteDialogue = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/rewrite_dialogue`);
export const syncPlan = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/sync_plan`);

export const setChapterCount = (id: string, count: number) =>
  jpost(`/api/runs/${id}/chapter_count`, { count });

// #7: пересобрать структуру под новое число глав (растянуть/сжать)
export const restructure = (id: string, count: number) =>
  jpost(`/api/runs/${id}/restructure`, { count });
// добавить главу после индекса (-1 → в начало). generate=false → пустой
// черновик (план пишут вручную/кнопкой); generate=true → ИИ сразу пишет план
export const addChapter = (id: string, after_idx: number, is_adult = true, generate = false) =>
  jpost(`/api/runs/${id}/chapter/add`, { after_idx, is_adult, generate });
// сгенерировать ИИ план для (пустой) главы — видит все главы
export const genChapterPlan = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/gen_plan`);
// пропустить выбор объёма/структуру → создать один пустой черновик
export const manualStructure = (id: string) =>
  jpost(`/api/runs/${id}/structure/manual`);
// #7: удалить главу (через parseJsonSafe — единый разбор ошибок/пустых ответов)
export const deleteChapter = async (id: string, idx: number) => {
  const url = `/api/runs/${id}/chapter/${idx}`;
  const r = await fetch(`${API_BASE}${url}`, { method: "DELETE" });
  return parseJsonSafe(r, url);
};
// #6: двигать главу вверх/вниз (меняет порядок и переиндексирует)
export const moveChapter = (id: string, idx: number, dir: "up" | "down") =>
  jpost(`/api/runs/${id}/chapter/${idx}/move`, { dir });
// drag-and-drop: новый порядок глав целиком (перестановка старых индексов)
export const reorderChapters = (id: string, order: number[]) =>
  jpost(`/api/runs/${id}/chapter/reorder`, { order });

// объём главы (слова): оверрайд цели на конкретную главу (0 → дефолт прогона)
export const setChapterWords = (id: string, idx: number, words: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/words`, { words });
// «растянуть» главу на ~add_words (ФОНОВО)
export const expandChapter = (id: string, idx: number, add_words = 800) =>
  jpost(`/api/runs/${id}/chapter/${idx}/expand`, { add_words });
// «растянуть» текстовый блок-этап (synopsis/characters/locations)
export const expandStage = (id: string, stage: string, add_words = 400) =>
  jpost(`/api/runs/${id}/stage/${stage}/expand`, { add_words });

// #5: провайдер (подписка/OpenRouter) для этапа ("all" → все)
export const setStageProvider = (id: string, stage: string, provider: string) =>
  jpost(`/api/runs/${id}/stage_provider`, { stage, provider });
// #5: решение по исчерпанному лимиту: switch | wait | subscription
export const resolveLimit = (id: string, action: "switch" | "wait" | "subscription") =>
  jpost(`/api/runs/${id}/limit/resolve`, { action });
export const setStepMode = (id: string, enabled: boolean) =>
  jpost(`/api/runs/${id}/step`, { enabled });

export const decideFinding = (
  id: string,
  fid: string,
  body: { status?: FindingStatus; comment?: string; judge?: boolean },
) => jpost(`/api/runs/${id}/findings/${fid}`, body);

export const rollback = (id: string, stage: string) =>
  jpost(`/api/runs/${id}/rollback`, { stage });

export type ClaudeStatus = {
  authorized: boolean; via: string | null; enabled: boolean; model: string;
  expires?: string | null; sub?: string | null; warn?: string;
};
export const claudeStatus = (): Promise<ClaudeStatus> =>
  jget("/api/claude/status");

export type ClaudeRateLimit = {
  status?: "allowed" | "allowed_warning" | "rejected" | null;
  resets_at?: number | null;
  rate_limit_type?: string | null;
  utilization?: number | null;
  overage_status?: string | null;
};
export const claudeUsage = (): Promise<{ rate_limit: ClaudeRateLimit | null }> =>
  jget("/api/claude/usage");
export const setClaudeSubscription = (enabled: boolean, model?: string): Promise<ClaudeStatus> =>
  jpost("/api/claude/subscription", { enabled, model });
export const setClaudeToken = (token: string): Promise<ClaudeStatus> =>
  jpost("/api/claude/token", { token });
// OAuth-подключение подписки из веба: ссылка авторизации + обмен кода на токен
export const claudeAuthUrl = (): Promise<{ authorized: boolean; url: string | null }> =>
  jget("/api/claude/auth_url");
export const claudeExchange = (code: string): Promise<ClaudeStatus> =>
  jpost("/api/claude/exchange", { code });

export type ChatMsg = { role: "user" | "assistant"; content: string };

// Ассистент по всему проекту (отдельная вкладка): свободный чат + применение
// правок к одному результату бота (не трогает пайплайн/порядок).
export type ApplyTarget =
  | "synopsis" | "characters" | "locations" | "logline"
  | "chapter_plan" | "chapter_dialogue";
export const projectChat = (id: string, messages: ChatMsg[]): Promise<{ reply: string }> =>
  jpost(`/api/runs/${id}/project_chat`, { messages });
export const projectApply = (
  id: string, target: ApplyTarget, messages: ChatMsg[], chapter_idx?: number,
): Promise<{ ok?: boolean; started?: boolean }> =>
  jpost(`/api/runs/${id}/project_apply`, { target, messages, chapter_idx });
// ассистент сам решает, какой результат бота изменить
export const projectApplyAuto = (
  id: string, messages: ChatMsg[],
): Promise<{ ok?: boolean; started?: boolean }> =>
  jpost(`/api/runs/${id}/project_apply_auto`, { messages });
export const chatWithEditor = (
  id: string,
  messages: ChatMsg[],
  ctx: { chapter_idx?: number; finding_id?: string },
): Promise<{ reply: string }> =>
  jpost(`/api/runs/${id}/chat`, { messages, ...ctx });

// применить обсуждённое в чате к главе целиком (ИИ решает: править/не трогать)
export const applyChatToChapter = (
  id: string, idx: number, messages: ChatMsg[],
): Promise<{ changed: boolean; note: string }> =>
  jpost(`/api/runs/${id}/chapter/${idx}/apply_chat`, { messages, chapter_idx: idx });

// один шаг цикла правок: запускает правку+перепроверку В ФОНЕ (LLM долгий),
// сразу отвечает; фронт поллит status до paused.
export const applyRevision = (
  id: string, idx: number, messages: ChatMsg[] = [],
): Promise<{ ok: boolean; started: boolean }> =>
  jpost(`/api/runs/${id}/chapter/${idx}/apply_revision`, { messages });

// собрать готовый проект для скачивания
export const exportProject = (
  id: string, fmt: "txt" | "md" = "txt",
): Promise<{ filename: string; text: string; chapters: number }> =>
  jget(`/api/runs/${id}/export?fmt=${fmt}`);

// скачать .docx (бинарь) — фетчим blob и триггерим загрузку в браузере
export async function downloadDocx(id: string): Promise<void> {
  const r = await fetch(`${API_BASE}/api/runs/${id}/export.docx`);
  if (!r.ok) {
    // вытащить понятный detail из FastAPI-ошибки (напр. «python-docx не установлен»)
    let detail = "";
    try {
      const j = await r.json();
      detail = (j && (j.detail?.message || j.detail || j.error)) || "";
    } catch { /* не JSON */ }
    throw new Error(detail || `Скачивание .docx не удалось (HTTP ${r.status})`);
  }
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "novella.docx";
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

// #3: перевод произвольного текста (английский вывод → русский для чтения)
export const translateText = (text: string, to: "ru" | "en" = "ru"): Promise<{ text: string }> =>
  jpost("/api/translate", { text, to });

// #4 (dev): дефолтные промпты по шагам + оверрайд на прогон
export const getPrompts = (): Promise<{ defaults: Record<string, string> }> =>
  jget("/api/prompts");
export const setPromptOverride = (id: string, stage: string, text: string) =>
  jpost(`/api/runs/${id}/prompt_override`, { stage, text });

export const adaptAdult = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/adapt_adult`);

export const skipAdult = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/skip_adult`);
