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
  loglines?: string[];
  selected_logline?: string;
  logline?: string;
  synopsis?: string;
  characters?: string;
  chapters?: Chapter[];
  chapter_idx?: number;
  structure_done?: boolean;
  editor_reports?: EditorReport[];
  finding_decisions?: Record<string, { status?: string; comment?: string; judged?: boolean }>;
  retry_count?: Record<string, number>;
  log?: string[];
};

export type StartReq = {
  theme: string;
  genre?: string;
  target_language: string;
  chapters_per_batch: number;
  translation_enabled: boolean;
  step_mode: boolean;
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

export async function getState(
  threadId: string,
): Promise<{ status: string; next: string[]; state: NarrativeState }> {
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

export const structureMore = (id: string) => jpost(`/api/runs/${id}/structure/more`);
export const structureProceed = (id: string) => jpost(`/api/runs/${id}/structure/proceed`);

export const decideFinding = (
  id: string,
  fid: string,
  body: { status?: FindingStatus; comment?: string; judge?: boolean },
) => jpost(`/api/runs/${id}/findings/${fid}`, body);

export const rollback = (id: string, stage: string) =>
  jpost(`/api/runs/${id}/rollback`, { stage });

export const adaptAdult = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/adapt_adult`);

export const skipAdult = (id: string, idx: number) =>
  jpost(`/api/runs/${id}/chapter/${idx}/skip_adult`);
