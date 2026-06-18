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
  adult_block_reason: string | null;
  adult_bridge_hint: string | null;
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
  stage_providers?: Record<string, string>;
  force_openrouter?: boolean;
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
};

async function jpost<T = any>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(`${API_BASE}${url}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${url} failed: ${r.status}`);
  return r.json();
}

export async function startRun(req: StartReq): Promise<{ thread_id: string }> {
  return jpost("/api/runs", req);
}

export type RunSummary = {
  thread_id: string; theme: string; chapters: number;
  written: number; status: string;
};
export const listRuns = (): Promise<{ runs: RunSummary[] }> =>
  fetch(`${API_BASE}/api/runs`).then((r) => {
    if (!r.ok) throw new Error(`list runs failed: ${r.status}`);
    return r.json();
  });

export type LimitInfo = {
  provider: string; reset_at?: number | null; kind?: string; message?: string;
};

export async function getState(
  threadId: string,
): Promise<{ status: string; next: string[]; error?: string;
  limit?: LimitInfo | null; state: NarrativeState }> {
  const r = await fetch(`${API_BASE}/api/runs/${threadId}/state`);
  if (!r.ok) throw new Error(`state failed: ${r.status}`);
  return r.json();
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
// #7: добавить главу после индекса (-1 → в начало), ИИ генерит план
export const addChapter = (id: string, after_idx: number) =>
  jpost(`/api/runs/${id}/chapter/add`, { after_idx });
// #7: удалить главу
export const deleteChapter = (id: string, idx: number) =>
  fetch(`${API_BASE}/api/runs/${id}/chapter/${idx}`, { method: "DELETE" })
    .then((r) => { if (!r.ok) throw new Error(`del failed: ${r.status}`); return r.json(); });

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
  fetch(`${API_BASE}/api/claude/status`).then((r) => r.json());
export const setClaudeSubscription = (enabled: boolean, model?: string): Promise<ClaudeStatus> =>
  jpost("/api/claude/subscription", { enabled, model });
export const setClaudeToken = (token: string): Promise<ClaudeStatus> =>
  jpost("/api/claude/token", { token });
// OAuth-подключение подписки из веба: ссылка авторизации + обмен кода на токен
export const claudeAuthUrl = (): Promise<{ authorized: boolean; url: string | null }> =>
  fetch(`${API_BASE}/api/claude/auth_url`).then((r) => r.json());
export const claudeExchange = (code: string): Promise<ClaudeStatus> =>
  jpost("/api/claude/exchange", { code });

export type ChatMsg = { role: "user" | "assistant"; content: string };
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
  fetch(`${API_BASE}/api/runs/${id}/export?fmt=${fmt}`).then((r) => {
    if (!r.ok) throw new Error(`export failed: ${r.status}`);
    return r.json();
  });

export const adaptAdult = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/adapt_adult`);

export const skipAdult = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/skip_adult`);
