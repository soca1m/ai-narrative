"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Play, Plus, FolderOpen, Download, RotateCw, Undo2, Check, X,
  ChevronDown, ChevronRight, MessageSquare, Sparkles, Wrench, Link2,
  ScanSearch, SendHorizontal, AlertTriangle, Loader2, BookOpenText,
  CircleCheckBig, RefreshCw, Wand2, ArrowRight, Circle, Minus, Pause,
  Maximize2, Minimize2, Languages, ArrowUp, ArrowDown, Trash2, HelpCircle,
  GripVertical,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import {
  Chapter,
  ChatMsg,
  EditorReport,
  Finding,
  FindingStatus,
  ClaudeStatus,
  NarrativeState,
  LimitInfo,
  projectChat,
  projectApplyAuto,
  chatWithEditor,
  applyChatToChapter,
  applyRevision,
  exportProject,
  downloadDocx,
  restructure,
  addChapter,
  genChapterPlan,
  manualStructure,
  deleteChapter,
  moveChapter,
  reorderChapters,
  setChapterWords,
  expandChapter,
  translateText,
  getPrompts,
  setPromptOverride,
  resolveLimit,
  RunSummary,
  listRuns,
  claudeStatus,
  claudeUsage,
  ClaudeRateLimit,
  claudeAuthUrl,
  claudeExchange,
  setClaudeSubscription,
  setClaudeToken,
  adaptAdult,
  decideFinding,
  getState,
  patchState,
  resumeRun,
  stopRun,
  checkStructure,
  skipStructureCheck,
  skipStructureStage,
  reviseChapter,
  rewriteDialogue,
  syncPlan,
  reviseStage,
  rollback,
  selectLogline,
  skipAdult,
  startRun,
  setChapterCount,
  setStepMode as apiSetStep,
} from "../lib/api";

const STAGES: [string, string, string][] = [
  ["logline", "Логлайн", "01"],
  ["synopsis", "Синопсис", "02"],
  ["characters", "Персонажи", "03"],
  ["locations", "Локации", "04"],
  ["chapter_count", "Объём", "05"],
  ["structure", "Структура", "06"],
  ["structure_editor", "Ред. структуры", "07"],
  ["dialogue", "Диалоги + Адалт", "08"],
  ["editor", "Редактор", "09"],
  ["translation", "Перевод", "10"],
];

// что сейчас сгенерируется — для ghost-превью следующего этапа
const STAGE_DESC: Record<string, string> = {
  logline: "ИИ придумает несколько коротких идей истории — выберешь одну.",
  synopsis: "Подробный пересказ всей истории на основе выбранной идеи.",
  characters: "Описания героев: характер, чего хотят, их роль в истории.",
  locations: "Места, где будет происходить действие.",
  chapter_count: "ИИ предложит, сколько глав лучше сделать.",
  structure: "План истории по главам: что происходит в каждой.",
  structure_editor: "ИИ проверит план и поправит нестыковки перед написанием.",
  dialogue: "ИИ напишет текст глав — реплики героев.",
  editor: "ИИ проверит готовые главы и предложит правки.",
  translation: "Перевод глав на нужный язык.",
};

const BOT_NAME: Record<number, string> = {
  1: "Логлайн", 2: "Синопсис", 3: "Персонажи", 4: "Структура",
  5: "Диалоги", 6: "Адалт", 7: "Редактор", 8: "Перевод",
};

// #3: простые подсказки по каждому боту — человеческим языком, без технических деталей.
const BOT_HINTS: Record<string, string> = {
  logline: "Это короткая идея истории в одном-двух предложениях. Выбери ту, что "
    + "нравится больше — на ней построится вся новелла. Не та? Попроси другие варианты.",
  synopsis: "Краткий пересказ всей истории. Здесь удобно задать настроение и "
    + "главные повороты. Если что-то не так — поправь текст сам или попроси ИИ.",
  characters: "Описания героев: какие они по характеру, чего хотят, какую роль "
    + "играют. Можно дописать или попросить ИИ изменить.",
  locations: "Места, где происходит действие. Можно поправить вручную или "
    + "попросить ИИ переписать.",
  structure: "План истории по главам: что происходит в каждой и где будут "
    + "горячие сцены. Главы можно добавлять, удалять и менять местами.",
  dialogue: "Готовый текст главы — реплики героев. Правь прямо в поле или попроси "
    + "ИИ переписать своими словами.",
};

// #3: строка-подсказка под шапкой бота
function BotHint({ stage }: { stage: string }) {
  const t = BOT_HINTS[stage];
  if (!t) return null;
  return (
    <div className="bot-hint" title="Как работать с этим ботом">
      <HelpCircle size={13} /> <span>{t}</span>
    </div>
  );
}

// человекочитаемое имя следующего шага (узлы графа, включая служебные) —
// чтобы кнопка «Продолжить» не показывала технический id вроде structure_editor
const NODE_LABEL: Record<string, string> = {
  logline: "Логлайн", synopsis: "Синопсис", characters: "Персонажи",
  chapter_count: "Объём истории", structure: "Структуру",
  structure_editor: "Проверку структуры", dialogue: "Написание глав",
  editor: "Проверку главы редактором", translation: "Перевод",
  content_next: "Следующую главу", edit_start: "Проверку редактором",
  edit_next: "Следующую главу", edit_router: "Следующий шаг",
  revise_to_dialogue: "Правку главы", bump_retry: "Правку главы",
};
function nextLabel(next: string[]): string {
  const n = next[0];
  return (n && NODE_LABEL[n]) || "далее";
}

// скачать текст файлом на стороне браузера
function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

// live-текст: complete()-этапы шлют чистый текст, structured()-этапы (диалоги) —
// сырой JSON. Вытаскиваем поле script на лету, чтобы показывать читаемый текст.
function cleanLive(t: string): string {
  if (!t) return "";
  const m = t.indexOf('"script":"');
  if (m < 0) return t;
  let s = t.slice(m + '"script":"'.length);
  const tail = s.search(/","(statics|anims)"/);
  if (tail >= 0) s = s.slice(0, tail);
  return s.replace(/\\n/g, "\n").replace(/\\"/g, '"').replace(/\\t/g, "  ").replace(/\\\\/g, "\\");
}

function wordCount(s: string | null | undefined): number {
  const t = (s ?? "").trim();
  return t ? t.split(/\s+/).length : 0;
}

// #3: кнопка «перевести на русский» для нарративщиков, кто не знает англ
function TranslateBox({ text }: { text: string | null | undefined }) {
  const [ru, setRu] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  if (!text || !text.trim()) return null;
  async function go() {
    if (ru !== null) { setRu(null); return; }
    setBusy(true); setErr(false);
    try { const r = await translateText(text!, "ru"); setRu(r.text); }
    catch { setErr(true); } finally { setBusy(false); }
  }
  return (
    <div className="trbox">
      <button className="small ghost" onClick={go} disabled={busy}
        title="Перевести этот текст на русский (через ИИ)">
        {busy ? <Loader2 size={14} className="spin" /> : <Languages size={14} />}
        {ru !== null ? "Скрыть перевод" : "Перевести на русский"}
      </button>
      {err && <span className="tr-err">перевод не удался</span>}
      {ru !== null && <div className="tr-out">{ru}</div>}
    </div>
  );
}

type Ev = { i: number; kind: string; bot: number; chapter: number | null; text: string };

function classifyLog(lines: string[]): Ev[] {
  const writes: Record<string, number> = {};
  return lines.map((line, i) => {
    const bm = line.match(/Бот (\d)/);
    const bot = bm ? +bm[1] : 0;
    const cm = line.match(/глав[а-яё]*\s+(\d+)/i);
    const chapter = cm ? +cm[1] : null;
    let kind = "bot";
    if (line.startsWith("⚠")) kind = "escalate";
    else if (line.includes("пропуск") || line.includes("отключён")) kind = "skip";
    else if (bot === 7) kind = line.includes("критич") ? "revise" : "approve";
    else if ((bot === 5 || bot === 6) && chapter != null) {
      const key = `${bot}:${chapter}`;
      writes[key] = (writes[key] ?? 0) + 1;
      kind = writes[key] > 1 ? "rollback" : "bot";
    }
    return { i, kind, bot, chapter, text: line };
  });
}

const EV_ICON: Record<string, LucideIcon> = {
  approve: Check, rollback: Undo2, revise: Wand2,
  escalate: AlertTriangle, skip: Minus, bot: Circle,
};

function critCount(r: EditorReport) {
  return r.findings.filter((f) => f.severity === "critical" && f.status !== "rejected").length;
}
function impCount(r: EditorReport) {
  return r.findings.filter((f) => f.severity === "important").length;
}
function minCount(r: EditorReport) {
  return r.findings.filter((f) => f.severity === "minor").length;
}

// ---------- подсветка цитат редактора в тексте (как GitHub/Google Docs) ----------
function Highlighted({
  text, findings, active, onPick,
}: {
  text: string; findings: Finding[]; active: string | null; onPick: (id: string) => void;
}) {
  const segs = useMemo(() => {
    const marks: { start: number; end: number; f: Finding }[] = [];
    for (const f of findings) {
      if (!f.quote) continue;
      const at = text.indexOf(f.quote);
      if (at < 0) continue;
      marks.push({ start: at, end: at + f.quote.length, f });
    }
    marks.sort((a, b) => a.start - b.start);
    const out: { txt: string; f: Finding | null }[] = [];
    let pos = 0;
    for (const m of marks) {
      if (m.start < pos) continue; // пропускаем перекрытия
      if (m.start > pos) out.push({ txt: text.slice(pos, m.start), f: null });
      out.push({ txt: text.slice(m.start, m.end), f: m.f });
      pos = m.end;
    }
    if (pos < text.length) out.push({ txt: text.slice(pos), f: null });
    return out;
  }, [text, findings]);

  if (!findings.some((f) => f.quote && text.includes(f.quote))) return null;

  return (
    <div className="highlighted">
      {segs.map((s, i) =>
        s.f ? (
          <mark
            key={i}
            id={`mark-${s.f.id}`}
            className={`hl ${s.f.severity} ${active === s.f.id ? "on" : ""} ${s.f.status}`}
            onClick={() => onPick(s.f!.id)}
            title={s.f.problem}
          >
            {s.txt}
          </mark>
        ) : (
          <span key={i}>{s.txt}</span>
        ),
      )}
    </div>
  );
}

export default function Page() {
  const [theme, setTheme] = useState(
    "Закрытый теннисный клуб, лето, борьба за власть. Лёгкая мистика.",
  );
  const [genre, setGenre] = useState("");
  const lang = "English";  // генерация всегда на английском (поле языка убрано)
  const [translation, setTranslation] = useState(true);
  const [stepMode, setStepMode] = useState(true);

  const [threadId, setThreadId] = useState<string | null>(null);
  const [status, setStatus] = useState("idle");
  const [next, setNext] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [limit, setLimit] = useState<LimitInfo | null>(null);
  const [applyingRev, setApplyingRev] = useState(false);
  const [st, setSt] = useState<NarrativeState>({});
  const [gen, setGen] = useState<{ stage: string; idx: number | null; text: string } | null>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollGenRef = useRef(0);  // поколение цикла поллинга (гонка при смене работ)

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState<string | null>(null);
  const [countInput, setCountInput] = useState<number | "">("");
  const [reCount, setReCount] = useState<number | "">("");  // #7 пересборка
  const [tab, setTab] = useState<"pipeline" | "assistant">("pipeline");
  // dev-оверрайды промптов ДО старта (применятся с первого бота)
  const [preOverrides, setPreOverrides] = useState<Record<string, string>>({});
  const [structureDirty, setStructureDirty] = useState(false);
  // индикатор скачивания (сборка docx/txt на бэке может занять секунды)
  const [dl, setDl] = useState<null | "txt" | "md" | "docx">(null);

  // список прошлых работ (продолжить оборванную / скачать)
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [showRuns, setShowRuns] = useState(false);
  async function loadRuns() {
    try { const r = await listRuns(); setRuns(r.runs); } catch {}
  }
  function openRun(tid: string) {
    if (pollRef.current) clearTimeout(pollRef.current);
    setErr(null); setDirty(new Set()); setDrafts({}); setShowRuns(false);
    setThreadId(tid); poll(tid);
  }

  const events = useMemo(() => classifyLog(st.log ?? []), [st.log]);
  const rollbackTick = useMemo(
    () => events.filter((e) => e.kind === "rollback").length,
    [events],
  );

  // poll() пересоздаёт tick() один раз и реschedule'ит сам себя — он держит
  // СТАРОЕ замыкание dirty. Через ref читаем актуальный dirty, иначе синк затрёт
  // правку, которую юзер начал после старта поллинга.
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  function syncDrafts(s: NarrativeState) {
    setDrafts((prev) => {
      const d = { ...prev };
      for (const k of ["synopsis", "characters", "locations"] as const) {
        if (dirtyRef.current.has(k)) continue;
        // строка из state → в черновик; пусто/None (после отката) → очищаем,
        // иначе блок не исчезает (EditableCard скрывается при пустом value).
        d[k] = typeof s[k] === "string" ? (s[k] as string) : "";
      }
      return d;
    });
  }

  async function onStart() {
    setSt({});
    setDirty(new Set());
    const { thread_id } = await startRun({
      theme, genre: genre || undefined, target_language: lang,
      translation_enabled: translation, step_mode: stepMode,
      prompt_overrides: Object.keys(preOverrides).length ? preOverrides : undefined,
    });
    setThreadId(thread_id);
    poll(thread_id);
  }

  function poll(id: string) {
    if (pollRef.current) clearTimeout(pollRef.current);
    // поколение поллинга: тик, начатый ДО переключения работы/перезапуска poll,
    // после await не должен ни применять чужое состояние, ни перепланировать
    // себя (иначе два вечных цикла и мигание между работами)
    const gen0 = ++pollGenRef.current;
    let fails = 0;
    const tick = async () => {
      try {
        const r = await getState(id);
        if (gen0 !== pollGenRef.current) return;  // устаревший цикл — умираем
        fails = 0;
        setStatus(r.status);
        setNext(r.next);
        setErr(r.error || null);
        setLimit(r.limit || null);
        setGen(r.gen || null);
        setStructureDirty(!!r.structure_dirty);
        setSt(r.state);
        syncDrafts(r.state);
        if (r.status === "running" || r.status === "idle")
          // во время генерации поллим часто → видно как ИИ пишет текст
          pollRef.current = setTimeout(tick, r.gen ? 400 : 1200);
      } catch {
        if (gen0 !== pollGenRef.current) return;
        fails += 1;
        if (fails >= 3) setErr("Нет связи с сервером — продолжаю попытки…");
        pollRef.current = setTimeout(tick, 2000);
      }
    };
    tick();
  }

  useEffect(() => () => {
    if (pollRef.current) clearTimeout(pollRef.current);
  }, []);

  async function onResume() {
    if (!threadId) return;
    await resumeRun(threadId);
    setStatus("running");
    poll(threadId);
  }
  async function onStop() {
    if (!threadId) return;
    try { await stopRun(threadId); poll(threadId); }
    catch (e) { setErr(e instanceof Error ? e.message : "Не удалось остановить"); }
  }
  async function onCheckStructure() {
    if (!threadId) return;
    try { setErr(null); await checkStructure(threadId); setStatus("running"); poll(threadId); }
    catch (e) { setErr(e instanceof Error ? e.message : "Не удалось проверить структуру"); }
  }
  async function onSkipStructureCheck() {
    if (!threadId) return;
    try { await skipStructureCheck(threadId); setStructureDirty(false); refresh(); }
    catch (e) { setErr(e instanceof Error ? e.message : "Ошибка"); }
  }
  async function onSkipStructureStage() {
    if (!threadId) return;
    try { setErr(null); await skipStructureStage(threadId); setStatus("running"); poll(threadId); }
    catch (e) { setErr(e instanceof Error ? e.message : "Не удалось пропустить проверку"); }
  }

  function refresh() { if (threadId) poll(threadId); }

  // #7: пересобрать структуру под новое число глав / добавить / удалить главу
  async function onRestructure() {
    if (!threadId || !reCount) return;
    if (!confirm(`Пересобрать структуру под ${reCount} глав? Сюжет растянется/`
      + `сожмётся, написанные тексты глав сбросятся.`)) return;
    try {
      setErr(null);
      await restructure(threadId, +reCount);
      setReCount(""); setStatus("running"); poll(threadId);
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось пересобрать"); }
  }
  async function onAddChapter(after: number, isAdult = true, generate = false) {
    if (!threadId) return;
    try {
      setErr(null);
      await addChapter(threadId, after, isAdult, generate);
      if (generate) { setStatus("running"); poll(threadId); } else { refresh(); }
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось добавить главу"); }
  }
  async function onGenChapterPlan(idx: number) {
    if (!threadId) return;
    try {
      setErr(null);
      await genChapterPlan(threadId, idx);
      setStatus("running"); poll(threadId);
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось сгенерировать план"); }
  }
  async function onManualStructure() {
    if (!threadId) return;
    try {
      setErr(null);
      await manualStructure(threadId); refresh();
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось создать черновик"); }
  }
  async function onDeleteChapter(idx: number) {
    if (!threadId) return;
    if (!confirm(`Удалить главу ${idx + 1}?`)) return;
    try {
      setErr(null);
      await deleteChapter(threadId, idx); refresh();
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось удалить главу"); }
  }
  async function onMoveChapter(idx: number, dir: "up" | "down") {
    if (!threadId) return;
    try {
      setErr(null);
      await moveChapter(threadId, idx, dir); refresh();
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось переместить главу"); }
  }
  async function onReorderChapters(order: number[]) {
    if (!threadId) return;
    try {
      setErr(null);
      await reorderChapters(threadId, order); refresh();
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось изменить порядок"); }
  }
  async function onSetWords(idx: number, words: number) {
    if (!threadId) return;
    try {
      setErr(null);
      await setChapterWords(threadId, idx, words); refresh();
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось задать объём"); }
  }
  async function onExpandChapter(idx: number) {
    if (!threadId) return;
    try {
      setErr(null);
      await expandChapter(threadId, idx);
      setStatus("running"); poll(threadId);
    } catch (e) { setErr(e instanceof Error ? e.message : "Не удалось растянуть главу"); }
  }

  // #5: решение по исчерпанному лимиту провайдера
  async function onLimit(action: "switch" | "wait" | "subscription") {
    if (!threadId) return;
    try {
      setErr(null);
      await resolveLimit(threadId, action);
      setLimit(null);
      if (action !== "wait") setStatus("running");
      poll(threadId);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Не удалось применить решение по лимиту");
    }
  }

  function edit(key: string, val: string) {
    setDrafts((d) => ({ ...d, [key]: val }));
    setDirty((s) => new Set(s).add(key));
  }
  async function save(key: string) {
    if (!threadId) return;
    await patchState(threadId, { [key]: drafts[key] });
    setDirty((s) => { const n = new Set(s); n.delete(key); return n; });
    setSaved(key);
    setTimeout(() => setSaved(null), 1500);
    refresh();
  }

  const has: Record<string, boolean> = {
    logline: !!st.loglines?.length,
    synopsis: !!st.synopsis,
    characters: !!st.characters,
    locations: !!st.locations,
    chapter_count: (st.suggested_chapters ?? 0) > 0 || (st.target_chapters ?? 0) > 0,
    structure: !!st.chapters?.length,
    structure_editor: Array.isArray(st.structure_fixes),
    dialogue: !!st.chapters?.some((c) => c.dialogue),
    adult: !!st.chapters?.some((c) => c.adult_scene),
    editor: !!st.editor_reports?.length,
    translation: !!st.chapters?.some((c) => c.translation),
  };
  const activeStage = next[0] ?? null;
  // этап сейчас генерится и его артефакт ещё не готов → показать блок-плейсхолдер
  // gen.stage (что РЕАЛЬНО генерится сейчас, вкл. ревизии) приоритетнее
  // activeStage (next-узел графа) — иначе ревизия логлайна покажет «синопсис».
  const showGen = (stage: string) =>
    status === "running" && !has[stage]
    && (gen ? gen.stage === stage : activeStage === stage);
  // человекочитаемое имя того, что генерится сейчас (для индикатора)
  const STAGE_RU: Record<string, string> = {
    logline: "логлайн", synopsis: "синопсис", characters: "персонажи",
    locations: "локации", structure: "структуру", dialogue: "главу", chat: "ответ",
  };
  // live-текст для этапа (нарастающий вывод модели), если он сейчас этот
  const liveFor = (stage: string) =>
    gen?.stage === stage ? cleanLive(gen.text) : undefined;
  // активная глава пишется → live-скелет в её карточке
  const writingIdx = gen?.stage === "dialogue" ? gen.idx
    : (status === "running" && activeStage === "dialogue" ? (st.chapter_idx ?? 0) : null);
  const writingText = gen?.stage === "dialogue" ? cleanLive(gen.text) : undefined;
  // апрув числа глав: chapter_count предложил, структуры ещё нет
  const pausedAtCount = status === "paused"
    && (st.suggested_chapters ?? 0) > 0 && !st.chapters?.length;

  // Текущий этап = последний, у которого есть результат. Кнопка «Откат»
  // показывается ТОЛЬКО на нём: удаляет его результат и ставит пайплайн на
  // регенерацию этого этапа (зависимые этапы очищаются на бэке).
  const currentStage: string | null = has.dialogue
    ? "dialogue"
    : has.structure
    ? "structure"
    : has.locations
    ? "locations"
    : has.characters
    ? "characters"
    : has.synopsis
    ? "synopsis"
    : has.logline
    ? "logline"
    : null;

  const totalRevisions = Object.values(st.retry_count ?? {}).reduce((a, b) => a + b, 0);
  const reports = st.editor_reports ?? [];
  const openCrit = (st.chapters ?? []).reduce((sum, c) => {
    const rs = reports.filter((r) => r.chapter_index === c.index);
    return sum + (rs.length ? critCount(rs[rs.length - 1]) : 0);
  }, 0);
  const botsRun = new Set(events.filter((e) => e.bot).map((e) => e.bot)).size;
  const busy = status === "running";

  // --- поглавный review-gate: текущая глава должна быть исправлена ПОЛНОСТЬЮ ---
  const curIdx = st.chapter_idx ?? 0;
  const curReport = [...reports].reverse().find((r) => r.chapter_index === curIdx);
  // глава проверена редактором (есть отчёт по ней)
  const curEdited = !!curReport;
  // все НЕотклонённые замечания (любой важности) — пока есть, дальше нельзя
  const curOpenAll = curReport
    ? curReport.findings.filter((f) => f.status !== "rejected").length
    : 0;
  const curOpenCrit = curReport
    ? curReport.findings.filter((f) => f.severity === "critical" && f.status !== "rejected").length
    : 0;
  // gate: на паузе текущая глава проверена и остались открытые замечания.
  // Переход к следующей главе блокируется, пока их не закрыть (исправить/отклонить).
  const gating = status === "paused" && curEdited && curOpenAll > 0;

  // проект готов: граф дошёл до конца (перевод/END). Все главы написаны →
  // можно скачивать в любой момент. Готовность снимает «ощущение бесконечности».
  const allWritten = (st.chapters?.length ?? 0) > 0
    && (st.chapters ?? []).every((c) => !!c.dialogue);
  const projectDone = status === "done";

  async function onDownload(fmt: "txt" | "md" | "docx") {
    if (!threadId || dl) return;
    setDl(fmt);
    try {
      if (fmt === "docx") { await downloadDocx(threadId); return; }
      const r = await exportProject(threadId, fmt);
      downloadText(r.filename, r.text);
    } catch (e) {
      setErr(e instanceof Error && e.message
        ? `Не удалось собрать проект: ${e.message}`
        : "Не удалось собрать проект для скачивания");
    } finally { setDl(null); }
  }

  // единый перехват ошибок действий: показываем в баннере, НЕ роняем UI.
  // Долгие операции теперь фоновые → refresh запускает поллинг (running→paused).
  async function guard(fn: () => Promise<void>) {
    try {
      setErr(null);
      await fn();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Ошибка операции");
    }
  }

  return (
    <div className="shell">
      {/* RAIL */}
      <aside className="rail">
        <div className="brand"><span className="dot" /> ai-narrative</div>
        <div className="tagline">КОМНАТА СЦЕНАРИСТОВ · АДАЛТ В ДИАЛОГАХ · 18+</div>

        <label>Тема и референсы</label>
        <textarea rows={4} value={theme} onChange={(e) => setTheme(e.target.value)} />
        <label>Особый жанр</label>
        <input type="text" value={genre} onChange={(e) => setGenre(e.target.value)}
          placeholder="напр. комедийная драма" />
        <div className="check">
          <input id="tr" type="checkbox" checked={translation}
            onChange={(e) => setTranslation(e.target.checked)} />
          <label htmlFor="tr" className="inline">Перевод (Бот 8) — на все языки через Google (финальный шаг)</label>
        </div>
        <div className="check">
          <input id="step" type="checkbox" checked={stepMode}
            onChange={(e) => {
              const v = e.target.checked;
              setStepMode(v);
              if (threadId) apiSetStep(threadId, v).then(() => poll(threadId));
            }} />
          <label htmlFor="step" className="inline">Пошаговый режим — пауза после каждого бота (можно менять на лету)</label>
        </div>
        <ClaudeSubPanel />

        {!threadId && (
          <PromptDevPanel overrides={preOverrides}
            onLocalChange={(s, t) => setPreOverrides((o) => {
              const n = { ...o }; if (t) n[s] = t; else delete n[s]; return n;
            })} />
        )}

        <button className="wide" onClick={onStart} disabled={busy}>
          {threadId ? <><Plus size={16} /> Новая работа</>
                    : <><Play size={16} /> Запустить пайплайн</>}
        </button>

        <button className="wide secondary" style={{ marginTop: 8 }}
          onClick={() => { const v = !showRuns; setShowRuns(v); if (v) loadRuns(); }}>
          <FolderOpen size={16} /> Мои работы{runs.length ? ` (${runs.length})` : ""}
        </button>
        {showRuns && (
          <div className="runs">
            {runs.length === 0 && <div className="runs-empty">пусто — запусти первую</div>}
            {runs.map((r) => (
              <div key={r.thread_id} className={`run-item ${r.thread_id === threadId ? "cur" : ""}`}>
                <div className="run-meta" onClick={() => openRun(r.thread_id)} title="Открыть / продолжить">
                  <div className="run-theme">{r.theme || "(без темы)"}</div>
                  <div className="run-sub">
                    {r.written}/{r.chapters} глав · {r.status}
                    {r.thread_id === threadId ? " · открыта" : ""}
                  </div>
                </div>
                <button className="xs ghost icon" title="Скачать .txt"
                  onClick={async () => {
                    try {
                      const ex = await exportProject(r.thread_id, "txt");
                      downloadText(ex.filename, ex.text);
                    } catch { setErr("Не удалось выгрузить работу"); }
                  }}><Download size={14} /></button>
              </div>
            ))}
          </div>
        )}

        {threadId && (
          <>
            <div className="statusbar">
              <span className={`tally ${status}`}>{status}</span>
              {next.length > 0 && <span className="next-chip">→ {nextLabel(next)}</span>}
            </div>
            {err && (
              <div className="errbox">
                <div className="errt"><AlertTriangle size={15} /> Ошибка</div>
                <div className="errm">{err}</div>
                {status === "error"
                  ? <button className="wide" onClick={() => { setErr(null); onResume(); }}><RotateCw size={15} /> Повторить</button>
                  : <button className="wide ghost" onClick={() => setErr(null)}>Закрыть</button>}
              </div>
            )}
            {limit && (
              <div className="limitbox">
                <div className="errt">
                  <AlertTriangle size={15} /> Лимит{" "}
                  {limit.provider === "subscription" ? "подписки Claude" : "OpenRouter"}{" "}
                  исчерпан
                </div>
                <div className="errm">
                  {limit.message || "Провайдер временно недоступен по лимиту."}
                  {limit.reset_at
                    ? <><br />Сброс: {new Date(limit.reset_at * 1000).toLocaleString()}</>
                    : <><br />Остаток/время сброса провайдер не сообщил.</>}
                </div>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  <button className="small" onClick={() => onLimit("switch")}>
                    <RefreshCw size={14} /> Перейти на OpenRouter
                  </button>
                  <button className="small secondary" onClick={() => onLimit("subscription")}>
                    <RotateCw size={14} /> Снова подписка
                  </button>
                  <button className="small ghost" onClick={() => onLimit("wait")}>
                    Подождать (скрыть)
                  </button>
                </div>
              </div>
            )}
            {busy && (
              <div className="working">
                <Loader2 size={15} className="spin" />
                ИИ работает… {gen
                  ? (STAGE_RU[gen.stage] ?? gen.stage)
                  + (gen.stage === "dialogue" && gen.idx != null ? ` ${gen.idx + 1}` : "")
                  : curEdited
                  ? `правка главы ${curIdx + 1}`
                  : nextLabel(next).toLowerCase()}
                <button className="small ghost" style={{ marginLeft: "auto" }}
                  onClick={onStop} title="Остановить генерацию (прогресс сохранится)">
                  <Pause size={14} /> Стоп
                </button>
              </div>
            )}
            {status === "paused" && !pausedAtCount && !gating && (
              <button className="wide" onClick={onResume}>
                {curEdited
                  ? <><Check size={16} /> Глава {curIdx + 1} готова — следующая</>
                  : <><ArrowRight size={16} /> Продолжить · {nextLabel(next)}</>}
              </button>
            )}
            {status === "paused" && !pausedAtCount && !gating && next[0] === "structure_editor" && (
              <button className="wide secondary" style={{ marginTop: 8 }}
                onClick={onSkipStructureStage}
                title="Не запускать проверку структуры — сразу перейти к написанию глав">
                Пропустить проверку — к написанию глав
              </button>
            )}
            {gating && (
              <div className="gate">
                <div className="gate-t"><ScanSearch size={15} /> Редактор · глава {curIdx + 1}</div>
                <div className="gate-h">
                  {curOpenAll} незакрыт{curOpenAll === 1 ? "ое замечание" : "ых замечаний"}
                  {curOpenCrit > 0 ? ` (из них ${curOpenCrit} критич.)` : ""}.
                  Исправь или отклони каждое — пока глава не доведена, к следующей
                  не перейти.
                </div>
                <button className="wide" disabled={applyingRev || busy}
                  onClick={async () => {
                    if (!threadId) return;
                    setApplyingRev(true);
                    try {
                      setErr(null);
                      // учитываем обсуждение из чата ТЕКУЩЕЙ ревизии (если был)
                      const round = reports.filter((r) => r.chapter_index === curIdx).length;
                      let msgs: ChatMsg[] = [];
                      try {
                        const v = localStorage.getItem(`chat:${threadId}:c${curIdx}:r${round}`);
                        if (v) msgs = JSON.parse(v);
                      } catch {}
                      await applyRevision(threadId, curIdx, msgs);
                      refresh();  // poll подхватит status=running
                    } catch (e) {
                      setErr(e instanceof Error ? e.message : "Ошибка применения правок");
                    } finally {
                      setApplyingRev(false);
                    }
                  }}>
                  {applyingRev
                    ? <><Loader2 size={15} className="spin" /> Запускаю правку…</>
                    : <><Check size={16} /> Исправить всё и перепроверить</>}
                </button>
              </div>
            )}
            {pausedAtCount && (
              <div className="batch-ctl">
                <div className="hint">
                  <BookOpenText size={14} style={{ verticalAlign: "-2px" }} /> ИИ предлагает <b>{st.suggested_chapters}</b> глав.
                  {st.count_reason ? <><br />{st.count_reason}</> : null}
                </div>
                <div className="row" style={{ gap: 8 }}>
                  <input type="number" min={2} max={30}
                    placeholder={String(st.suggested_chapters ?? "")}
                    value={countInput}
                    onChange={(e) => setCountInput(e.target.value === "" ? "" : Math.max(2, +e.target.value))} />
                </div>
                <button className="wide" onClick={async () => {
                  const n = countInput || st.suggested_chapters || 6;
                  await setChapterCount(threadId, n);
                  setStatus("running"); setCountInput(""); poll(threadId);
                }}>
                  <Check size={16} /> Писать {countInput || st.suggested_chapters} глав
                </button>
                <button className="wide secondary" style={{ marginTop: 8 }}
                  title="Не задавать объём: создать один пустой черновик главы. Дальше добавишь сколько нужно и напишешь/сгенерируешь планы сам."
                  onClick={onManualStructure}>
                  <Plus size={16} /> Пропустить — пустой черновик
                </button>
              </div>
            )}


            <h2 className="section">Лента событий</h2>
            <div className="feed">
              <AnimatePresence initial={false}>
                {[...events].reverse().map((e) => (
                  <motion.div key={e.i} className={`ev ${e.kind}`} layout
                    initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
                    <span className="ic">{(() => { const EI = EV_ICON[e.kind] ?? Circle; return <EI size={13} />; })()}</span>
                    <div className="body"><div className="t">{e.text.replace(/^[✓·⚠]\s*/, "")}</div></div>
                  </motion.div>
                ))}
              </AnimatePresence>
              {!events.length && <div className="chip">ожидание…</div>}
            </div>
            <PromptDevPanel threadId={threadId}
              overrides={st.prompt_overrides ?? {}} onChanged={refresh} />
            <div className="thread">thread · {threadId}</div>
          </>
        )}
      </aside>

      {/* STAGE */}
      <main className="stage-area">
        {threadId && (
          <div className="tabbar">
            <button className={`tab ${tab === "pipeline" ? "active" : ""}`}
              onClick={() => setTab("pipeline")}>Конвейер</button>
            <button className={`tab ${tab === "assistant" ? "active" : ""}`}
              onClick={() => setTab("assistant")}>
              <MessageSquare size={14} /> Ассистент
            </button>
          </div>
        )}

        {threadId && (
          <div style={{ display: tab === "assistant" ? "block" : "none" }}>
            <AssistantTab threadId={threadId} busy={busy} onRefresh={refresh} />
          </div>
        )}

        {tab === "pipeline" && (<>
        {!threadId && (
          <div className="empty">
            Задай тему слева и запусти конвейер. Правь артефакты, проси ИИ переделать
            этап, откатывайся, выбирай логлайн, принимай/отклоняй правки редактора.
          </div>
        )}

        {threadId && (
          <>
            {projectDone && (
              <motion.div className="proj-done" initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}>
                <div className="done-t"><CircleCheckBig size={20} /> Проект готов</div>
                <div className="done-d">
                  Все {st.chapters?.length ?? 0} глав написаны и проверены
                  редактором. Можно скачать готовый текст новеллы.
                </div>
                <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
                  <button disabled={dl !== null} onClick={() => onDownload("docx")}>
                    {dl === "docx" ? <><Loader2 size={16} className="spin" /> Собираю…</> : <><Download size={16} /> Скачать .docx</>}
                  </button>
                  <button className="secondary" disabled={dl !== null} onClick={() => onDownload("txt")}>
                    {dl === "txt" ? <><Loader2 size={16} className="spin" /> Собираю…</> : <><Download size={16} /> Скачать .txt</>}
                  </button>
                  <button className="secondary" disabled={dl !== null} onClick={() => onDownload("md")}>
                    {dl === "md" ? <><Loader2 size={16} className="spin" /> Собираю…</> : <><Download size={16} /> Скачать .md</>}
                  </button>
                </div>
              </motion.div>
            )}

            <div className="pipeline">
              {STAGES.map(([k, label, ix]) => {
                const off = k === "translation" && !st.translation_enabled;
                const cls = activeStage === k ? "active" : has[k] ? "done" : "";
                return (
                  <div key={k} className={`node ${cls} ${off ? "off" : ""}`}>
                    <span className="ix">{ix}</span>{label}{off && <Pause size={9} style={{ marginLeft: 4 }} />}
                  </div>
                );
              })}
            </div>

            {/* ghost-превью оставляем ТОЛЬКО для этапов без своего GenCard
                (число глав / редактор структуры / редактор) — у остальных
                live-блок показывает сам процесс */}
            {activeStage && STAGE_DESC[activeStage] && !has[activeStage]
              && ["chapter_count", "structure_editor", "editor"].includes(activeStage) && (
              <motion.div className="preview" layout
                initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}>
                <span className={`ghost-dot ${status === "running" ? "live" : ""}`} />
                <div>
                  <div className="ghost-t">
                    {status === "running" ? "Сейчас работает" : "Далее"}:{" "}
                    {STAGES.find((s) => s[0] === activeStage)?.[1] ?? activeStage}
                  </div>
                  <div className="ghost-d">{STAGE_DESC[activeStage]}</div>
                </div>
              </motion.div>
            )}

            {(st.chapters?.length ?? 0) > 0 && !projectDone && (
              <div className={`phasebar ${gating || curOpenCrit > 0 ? "edit" : "content"}`}>
                <span className="ph-dot" />
                {`Глава ${curIdx + 1} из ${st.chapters!.length} · `}
                {curEdited
                  ? (gating ? "правки редактора — довести до конца" : "проверена")
                  : "написание (диалоги + адалт), затем проверка редактором"}
              </div>
            )}

            {(st.structure_fixes?.length ?? 0) > 0 && (
              <div className="sfixes">
                <div className="sft"><Wrench size={14} /> Редактор структуры исправил план ({st.structure_fixes!.length}):</div>
                {st.structure_fixes!.map((fx, i) => <div key={i} className="sfi">· {fx}</div>)}
              </div>
            )}

            {/* #7: управление числом глав — пересобрать / добавить */}
            {(st.chapters?.length ?? 0) > 0 && !projectDone && (
              <div className="chapctl">
                <span className="cc-lbl">Глав: <b>{st.chapters!.length}</b></span>
                <input type="number" min={2} max={30} placeholder="новое число"
                  value={reCount}
                  onChange={(e) => setReCount(e.target.value === "" ? "" : Math.max(2, +e.target.value))} />
                <button className="small" disabled={busy || !reCount}
                  onClick={onRestructure} title="Пересобрать сюжет под новое число глав">
                  <RefreshCw size={14} /> Пересобрать
                </button>
              </div>
            )}

            {/* Порядок убывания: свежие результаты сверху (главы → персонажи → синопсис → логлайн) */}
            {showGen("structure") && <GenCard bot="06" title="Структура · поглавный план" rows={5} live={liveFor("structure")} />}

            {structureDirty && !busy && (
              <div className="struct-check">
                <div className="sc-t"><ScanSearch size={15} /> Структура изменилась</div>
                <div className="sc-d">Главы добавлены/удалены. Запустить проверку структуры (ИИ выровняет план и связки) или пропустить?</div>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  <button className="small" onClick={onCheckStructure}><ScanSearch size={14} /> Проверить структуру</button>
                  <button className="small ghost" onClick={onSkipStructureCheck}>Пропустить</button>
                </div>
              </div>
            )}

            <Chapters
              key={threadId}  /* ремоунт при смене работы: локальные правки не утекают в другой проект */
              onAddChapter={onAddChapter}
              onGenChapterPlan={onGenChapterPlan}
              onDeleteChapter={onDeleteChapter}
              onMoveChapter={onMoveChapter}
              onReorderChapters={onReorderChapters}
              onSetWords={onSetWords}
              onExpandChapter={onExpandChapter}
              threadId={threadId}
              phase={st.phase}
              activeIdx={curIdx}
              writingIdx={writingIdx}
              writingText={writingText}
              onRefresh={refresh}
              busy={busy}
              canDownload={allWritten}
              downloading={dl !== null}
              onDownload={onDownload}
              chapters={st.chapters ?? []}
              reports={reports}
              onSaveAll={(chs) => guard(async () => { if (threadId) { await patchState(threadId, { chapters: chs }); refresh(); } })}
              onReviseChapter={(i, fb) => guard(async () => { if (threadId) { await reviseChapter(threadId, i, fb); setStatus("running"); poll(threadId); } })}
              onReviseDialogue={(i, fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "dialogue", fb, i); setStatus("running"); poll(threadId); } })}
              onRewriteDialogue={(i) => guard(async () => { if (threadId) { await rewriteDialogue(threadId, i); refresh(); } })}
              onSyncPlan={(i) => guard(async () => { if (threadId) { await syncPlan(threadId, i); refresh(); } })}
              rollbackStage={currentStage === "dialogue" ? "dialogue" : currentStage === "structure" ? "structure" : null}
              onRollbackWriting={() => guard(async () => { if (threadId && confirm("Откатить этап написания: удалить все тексты глав (диалоги/адалт/перевод) и сгенерировать заново? План глав сохранится.")) { await rollback(threadId, "dialogue"); refresh(); } })}
              onRollbackStructure={() => guard(async () => { if (threadId && confirm("Откатить этап структуры: удалить план глав и сгенерировать заново?")) { await rollback(threadId, "structure"); refresh(); } })}
              onDecide={(fid, body) => guard(async () => { if (threadId) { await decideFinding(threadId, fid, body); refresh(); } })}
              onAdaptAdult={(i) => guard(async () => { if (threadId) { await adaptAdult(threadId, i); refresh(); } })}
              onSkipAdult={(i) => guard(async () => { if (threadId) { await skipAdult(threadId, i); refresh(); } })}
            />

            {/* Локации (генерятся ПОСЛЕ персонажей → выше них: свежее сверху) */}
            {showGen("locations") && <GenCard bot="04" title="Локации · места действия" rows={4} live={liveFor("locations")} />}
            <EditableCard
              title="Локации · места действия" bot="04" rows={10} valKey="locations" busy={busy} hint={BOT_HINTS.locations}
              value={drafts.locations ?? ""} dirty={dirty.has("locations")} saved={saved === "locations"}
              onChange={(v) => edit("locations", v)} onSave={() => save("locations")}
              onRevise={(fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "locations", fb); setStatus("running"); poll(threadId); } })}
              onRollback={currentStage === "locations" ? () => guard(async () => { if (threadId && confirm("Откатить этап локаций: удалить карточки локаций и сгенерировать заново?")) { await rollback(threadId, "locations"); refresh(); } }) : undefined}
            />

            {/* Персонажи */}
            {showGen("characters") && <GenCard bot="03" title="Персонажи · карточки" rows={6} live={liveFor("characters")} />}
            <EditableCard
              title="Персонажи · карточки" bot="03" rows={12} valKey="characters" busy={busy} hint={BOT_HINTS.characters}
              value={drafts.characters ?? ""} dirty={dirty.has("characters")} saved={saved === "characters"}
              onChange={(v) => edit("characters", v)} onSave={() => save("characters")}
              onRevise={(fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "characters", fb); setStatus("running"); poll(threadId); } })}
              onRollback={currentStage === "characters" ? () => guard(async () => { if (threadId && confirm("Откатить этап персонажей: удалить карточки и сгенерировать заново?")) { await rollback(threadId, "characters"); refresh(); } }) : undefined}
            />

            {/* Синопсис */}
            {showGen("synopsis") && <GenCard bot="02" title="Синопсис" rows={5} live={liveFor("synopsis")} />}
            <EditableCard
              title="Синопсис" bot="02" rows={10} valKey="synopsis" busy={busy} hint={BOT_HINTS.synopsis}
              value={drafts.synopsis ?? ""} dirty={dirty.has("synopsis")} saved={saved === "synopsis"}
              onChange={(v) => edit("synopsis", v)} onSave={() => save("synopsis")}
              onRevise={(fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "synopsis", fb); setStatus("running"); poll(threadId); } })}
              onRollback={currentStage === "synopsis" ? () => guard(async () => { if (threadId && confirm("Откатить этап синопсиса: удалить синопсис и сгенерировать заново?")) { await rollback(threadId, "synopsis"); refresh(); } }) : undefined}
            />

            {/* Логлайн — выбор одного из вариантов */}
            {showGen("logline") && <GenCard bot="01" title="Логлайн · варианты" rows={5} live={liveFor("logline")} />}
            <LoglineCard
              loglines={st.loglines ?? []}
              selected={st.selected_logline ?? ""}
              busy={busy}
              onSelect={(l) => guard(async () => { if (threadId) { await selectLogline(threadId, l); refresh(); } })}
              onSave={(list, sel) => guard(async () => { if (threadId) { await patchState(threadId, { loglines: list, selected_logline: sel }); refresh(); } })}
              onRevise={(fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "logline", fb); setStatus("running"); poll(threadId); } })}
            />
          </>
        )}
        </>)}
      </main>
    </div>
  );
}


// ---------- Ассистент: чат по всему проекту + автоприменение правок ----------
function AssistantTab({ threadId, busy, onRefresh }: {
  threadId: string; busy?: boolean; onRefresh: () => void;
}) {
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [applying, setApplying] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  // чат закреплён за thread_id: грузим из localStorage при смене работы / релоаде
  const lsKey = `asst:${threadId}`;
  const loadedFor = useRef<string | null>(null);
  useEffect(() => {
    try {
      const raw = localStorage.getItem(lsKey);
      setMsgs(raw ? JSON.parse(raw) : []);
    } catch { setMsgs([]); }
    loadedFor.current = threadId;
  }, [threadId, lsKey]);
  useEffect(() => {
    // сохраняем только после того как загрузили историю текущего thread_id
    if (loadedFor.current !== threadId) return;
    try { localStorage.setItem(lsKey, JSON.stringify(msgs)); } catch { /* quota */ }
  }, [msgs, threadId, lsKey]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, sending]);

  async function send() {
    const text = input.trim();
    if (!text || sending) return;
    const next = [...msgs, { role: "user" as const, content: text }];
    setMsgs(next); setInput(""); setSending(true); setErr(null);
    try {
      const r = await projectChat(threadId, next);
      setMsgs([...next, { role: "assistant", content: r.reply }]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Ассистент недоступен");
      setMsgs(next);
    } finally { setSending(false); }
  }

  async function apply() {
    if (!msgs.length || applying) return;
    setApplying(true); setErr(null); setNote(null);
    try {
      const r = await projectApplyAuto(threadId, msgs);
      if (r.started === false && r.ok !== true) {
        setNote("Правка не запустилась — попробуй ещё раз чуть позже.");
      } else {
        setNote("ИИ применяет правку к нужному этапу — смотри вкладку «Конвейер».");
      }
      onRefresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Не удалось применить");
    } finally { setApplying(false); }
  }

  return (
    <div className="asst">
      <div className="asst-head">
        <MessageSquare size={16} /> Ассистент проекта
        <span className="asst-sub">обсуждай весь проект; «Применить» — ИИ сам решит, какой этап изменить</span>
      </div>
      <div className="asst-thread">
        {!msgs.length && (
          <div className="asst-empty">
            Спроси что угодно по проекту: логлайн, синопсис, персонажи, локации, планы и тексты глав.
            Обсуди правку и нажми «Применить» — ИИ сам поймёт, какой результат изменить.
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`ch-msg ${m.role}`}>{m.content}</div>
        ))}
        {sending && <div className="ch-msg assistant"><Loader2 size={14} className="spin" /> думает…</div>}
        <div ref={endRef} />
      </div>
      {err && <div className="asst-err">{err}</div>}
      {note && <div className="asst-note"><Check size={13} /> {note}</div>}
      <div className="asst-apply">
        <button className="small" disabled={busy || applying || !msgs.length}
          title="ИИ сам определит, какой результат бота изменить по обсуждению (пайплайн и порядок не трогает)"
          onClick={apply}>
          {applying ? <><Loader2 size={14} className="spin" /> Применяю…</> : <><Check size={14} /> Применить изменения</>}
        </button>
      </div>
      <div className="asst-input">
        <textarea rows={2} value={input} disabled={sending} placeholder="Сообщение ассистенту…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="small" disabled={sending || !input.trim()} onClick={send}>
          <SendHorizontal size={15} />
        </button>
      </div>
    </div>
  );
}

// блок-плейсхолдер «ИИ пишет…»: скелет + спиннер, на месте будущего артефакта.
// Появляется, пока этап генерится; превращается в обычный блок по готовности.
function GenCard({ bot, title, rows = 4, live }: { bot: string; title: string; rows?: number; live?: string }) {
  const txtRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => { if (txtRef.current) txtRef.current.scrollTop = txtRef.current.scrollHeight; }, [live]);
  return (
    <motion.div className="card gencard" layout
      initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
      <div className="head">
        <span className="t"><span className="botnum">{bot}</span>{title}</span>
        <span className="genbadge"><Loader2 size={14} className="spin" /> ИИ пишет…</span>
      </div>
      {live && live.trim() ? (
        <div className="livetext" ref={txtRef}>{live}<span className="gencaret" /></div>
      ) : (
        <div className="skel">
          {Array.from({ length: rows }).map((_, i) => (
            <span key={i} className="sl" style={{ width: `${[94, 78, 88, 64, 90, 72][i % 6]}%` }} />
          ))}
        </div>
      )}
      <div className="genhint">Бот генерирует содержимое в реальном времени. Блок станет редактируемым по готовности.</div>
    </motion.div>
  );
}

// #4 (dev): свёрнутая панель редактирования системных промптов по шагам
function PromptDevPanel({ threadId, overrides, onChanged, onLocalChange }: {
  threadId?: string; overrides: Record<string, string>; onChanged?: () => void;
  onLocalChange?: (stage: string, text: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [defs, setDefs] = useState<Record<string, string> | null>(null);
  const [stage, setStage] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  async function load() {
    if (defs) return;
    try { const d = await getPrompts(); setDefs(d.defaults); } catch {}
  }
  function pick(s: string) {
    setStage(s);
    setText(s ? (overrides[s] ?? defs?.[s] ?? "") : "");
  }
  async function save() {
    if (!stage) return;
    setBusy(true);
    try {
      if (threadId) { await setPromptOverride(threadId, stage, text); onChanged?.(); }
      else onLocalChange?.(stage, text);
    } finally { setBusy(false); }
  }
  async function reset() {
    if (!stage) return;
    setBusy(true);
    try {
      if (threadId) { await setPromptOverride(threadId, stage, ""); onChanged?.(); }
      else onLocalChange?.(stage, null);
      setText(defs?.[stage] ?? "");
    } finally { setBusy(false); }
  }
  const stages = defs ? Object.keys(defs) : [];
  return (
    <div className="subpanel">
      <div className="sp-head" style={{ cursor: "pointer" }}
        onClick={() => { const v = !open; setOpen(v); if (v) load(); }}>
        <span>Dev · промпты по шагам</span>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </div>
      {open && (
        <div style={{ marginTop: 8 }}>
          <div className="sp-hint">
            Системный промпт каждого бота. {threadId
              ? "Оверрайд хранится на этот прогон; пусто/сброс → дефолт."
              : "Задай ДО старта — применится с первого бота (вкл. логлайн)."}
          </div>
          <select className="model-select" value={stage}
            onChange={(e) => pick(e.target.value)}>
            <option value="">— выбрать шаг —</option>
            {stages.map((s) => (
              <option key={s} value={s}>{s}{overrides[s] ? " ✎" : ""}</option>
            ))}
          </select>
          {stage && (
            <>
              <textarea rows={10} value={text} style={{ marginTop: 8 }}
                onChange={(e) => setText(e.target.value)} />
              <div className="row" style={{ gap: 8, marginTop: 6 }}>
                <button className="small" disabled={busy} onClick={save}>Сохранить</button>
                <button className="small ghost" disabled={busy} onClick={reset}>Сбросить к дефолту</button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Stat({ v, k, cls }: { v: number; k: string; cls?: string }) {
  return (
    <div className={`stat ${cls ?? ""}`}><div className="v">{v}</div><div className="k">{k}</div></div>
  );
}

// строка ввода «попроси ИИ переделать». disabled — пока ИИ работает (no 409)
function ReviseBox({ label, onRevise, disabled }: {
  label?: string; onRevise: (fb: string) => Promise<void>; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [fb, setFb] = useState("");
  const [busy, setBusy] = useState(false);
  if (!open) return <button className="small ghost" disabled={disabled} onClick={() => setOpen(true)}><Wand2 size={14} /> {label ?? "Попросить ИИ переделать"}</button>;
  return (
    <div className="revise">
      <textarea rows={2} placeholder="Что изменить? напр. «больше внутреннего конфликта у героя»"
        value={fb} onChange={(e) => setFb(e.target.value)} />
      <div className="row">
        <button className="small" disabled={!fb.trim() || busy || disabled}
          onClick={async () => { setBusy(true); try { await onRevise(fb); setFb(""); setOpen(false); } finally { setBusy(false); } }}>
          {busy ? "…" : "Переделать"}
        </button>
        <button className="small ghost" onClick={() => setOpen(false)}>Отмена</button>
      </div>
    </div>
  );
}

function LoglineCard({
  loglines, selected, onSelect, onRevise, onSave, busy,
}: {
  loglines: string[]; selected: string; busy?: boolean;
  onSelect: (l: string) => void; onRevise: (fb: string) => Promise<void>;
  onSave: (list: string[], sel: string) => void;
}) {
  // локальные правки текста логлайнов; индекс выбранного держим по позиции
  const [edits, setEdits] = useState<Record<number, string>>({});
  const selIdx = loglines.findIndex((l) => l === selected);
  const dirty = Object.keys(edits).length > 0;
  if (!loglines.length) return null;
  const text = (i: number) => (i in edits ? edits[i] : loglines[i]);
  function save() {
    const list = loglines.map((_, i) => text(i));
    const sel = selIdx >= 0 ? list[selIdx] : selected;
    onSave(list, sel); setEdits({});
  }
  return (
    <div className="card">
      <div className="head">
        <span className="t"><span className="botnum">01</span>Логлайн · выбери и правь</span>
        {dirty && <button className="small" disabled={busy} onClick={save}><Check size={14} /> Сохранить</button>}
      </div>
      <BotHint stage="logline" />
      <div className="loglines">
        {loglines.map((l, i) => (
          <div key={i} className={`logline ${selected === l ? "sel" : ""}`}>
            <input type="radio" name="logline" disabled={busy} checked={selected === l} onChange={() => onSelect(l)} />
            <textarea className="logline-edit" rows={2} value={text(i)} disabled={busy}
              onChange={(e) => setEdits((s) => ({ ...s, [i]: e.target.value }))} />
          </div>
        ))}
      </div>
      <TranslateBox text={selIdx >= 0 ? text(selIdx) : loglines.join("\n\n")} />
      <ReviseBox label="Сгенерировать другие логлайны" onRevise={onRevise} disabled={busy} />
    </div>
  );
}

function EditableCard(props: {
  title: string; bot: string; rows: number; valKey: string; busy?: boolean;
  value: string; dirty: boolean; saved: boolean; hint?: string;
  onChange: (v: string) => void; onSave: () => void;
  onRevise: (fb: string) => Promise<void>; onRollback?: () => Promise<void>;
  onExpand?: () => Promise<void>;
}) {
  if (!props.value && !props.dirty) return null;
  return (
    <div className="card">
      <div className="head">
        <span className="t"><span className="botnum">{props.bot}</span>{props.title}</span>
        <span className="row">
          {props.saved && <span className="saved"><Check size={13} /> сохранено</span>}
          <button className="small" disabled={!props.dirty || props.busy} onClick={props.onSave}>Сохранить</button>
          {props.onExpand && (
            <button className="small ghost" disabled={props.busy} onClick={props.onExpand}
              title="Растянуть: сделать текст подробнее, сохранив суть"><ArrowRight size={14} /> Растянуть</button>
          )}
          {props.onRollback && (
            <button className="small ghost" disabled={props.busy} onClick={props.onRollback}
              title="Удалить результат этого этапа и сгенерировать заново"><Undo2 size={14} /> Откат</button>
          )}
        </span>
      </div>
      {props.hint && (
        <div className="bot-hint" title="Как работать с этим ботом">
          <HelpCircle size={13} /> <span>{props.hint}</span>
        </div>
      )}
      <textarea rows={props.rows} value={props.value} onChange={(e) => props.onChange(e.target.value)} />
      <TranslateBox text={props.value} />
      <ReviseBox onRevise={props.onRevise} disabled={props.busy} />
    </div>
  );
}

function Chapters(props: {
  threadId: string;
  phase?: string; activeIdx?: number; busy?: boolean;
  writingIdx?: number | null; writingText?: string;
  chapters: Chapter[]; reports: EditorReport[];
  canDownload?: boolean;
  downloading?: boolean;
  onDownload?: (fmt: "txt" | "md" | "docx") => void;
  onSaveAll: (chs: Chapter[]) => void;
  onReviseChapter: (i: number, fb: string) => Promise<void>;
  onReviseDialogue: (i: number, fb: string) => Promise<void>;
  onRewriteDialogue: (i: number) => Promise<void>;
  onSyncPlan: (i: number) => Promise<void>;
  onRefresh: () => void;
  rollbackStage: "dialogue" | "structure" | null;
  onRollbackWriting: () => Promise<void>;
  onRollbackStructure: () => Promise<void>;
  onDecide: (fid: string, body: { status?: FindingStatus; comment?: string; judge?: boolean }) => Promise<void>;
  onAdaptAdult: (i: number) => Promise<void>;
  onSkipAdult: (i: number) => Promise<void>;
  onAddChapter: (after: number, isAdult?: boolean, generate?: boolean) => Promise<void>;
  onGenChapterPlan: (idx: number) => Promise<void>;
  onDeleteChapter: (idx: number) => Promise<void>;
  onMoveChapter: (idx: number, dir: "up" | "down") => Promise<void>;
  onReorderChapters: (order: number[]) => Promise<void>;
  onSetWords: (idx: number, words: number) => Promise<void>;
  onExpandChapter: (idx: number) => Promise<void>;
}) {
  // overrides-модель: рендерим из серверных props.chapters, локально храним
  // ТОЛЬКО изменённые поля (ключ `${index}.${field}`). Поэтому новые главы/
  // диалоги с сервера видны сразу, а ручные правки не теряются (нет залипания).
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [activeFinding, setActiveFinding] = useState<string | null>(null);
  const [adapting, setAdapting] = useState<number | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [toggled, setToggled] = useState<Record<number, boolean>>({});
  const [hlOn, setHlOn] = useState<Record<number, boolean>>({});  // подсветка замечаний
  const [tall, setTall] = useState<Record<string, boolean>>({});  // высокие поля
  const [dragIdx, setDragIdx] = useState<number | null>(null);    // drag-drop
  const [overIdx, setOverIdx] = useState<number | null>(null);
  const dirty = Object.keys(edits).length > 0;
  // структурные операции переиндексируют главы → несохранённые правки, ключованные
  // индексом, попали бы в ЧУЖУЮ главу. Поэтому: сначала автосохраняем правки.
  async function commitThen(fn: () => void | Promise<void>) {
    if (dirty) await commit();
    await fn();
  }
  // drag-drop: переставить главу src на место dst (в порядке повествования).
  // ВАЖНО: позицию dst берём ПОСЛЕ удаления src — иначе при перетаскивании вниз
  // элемент вставал на слот дальше цели (аргументы splice считались до мутации).
  function reorderTo(src: number, dst: number) {
    if (src === dst) return;
    const asc = [...props.chapters].sort((a, b) => a.index - b.index).map((c) => c.index);
    const from = asc.indexOf(src);
    let to = asc.indexOf(dst);
    const [moved] = asc.splice(from, 1);
    if (from < to) to -= 1;   // удаление сдвинуло цель влево
    asc.splice(to, 0, moved); // встаём ровно на слот цели
    commitThen(() => props.onReorderChapters(asc));
  }
  const tkey = (i: number, f: string) => `${i}-${f}`;
  const tallCls = (i: number, f: string) => (tall[tkey(i, f)] ? "tall" : "");
  function TallBtn({ i, f }: { i: number; f: string }) {
    const on = !!tall[tkey(i, f)];
    return (
      <button className="xs ghost field-tall" type="button"
        onClick={() => setTall((s) => ({ ...s, [tkey(i, f)]: !on }))}
        title="Сделать поле выше/ниже для удобного чтения">
        {on ? <><Minimize2 size={12} /> свернуть</> : <><Maximize2 size={12} /> развернуть</>}
      </button>
    );
  }
  if (!props.chapters.length) return null;

  // сколько открытых критичных у главы (последний отчёт редактора)
  function critOpen(idx: number): number {
    const rs = props.reports.filter((r) => r.chapter_index === idx);
    const last = rs[rs.length - 1];
    return last ? last.findings.filter((f) => f.severity === "critical" && f.status !== "rejected").length : 0;
  }
  // все НЕзакрытые замечания (любой важности) — глава доведена, когда их 0
  function openAll(idx: number): number {
    const rs = props.reports.filter((r) => r.chapter_index === idx);
    const last = rs[rs.length - 1];
    return last ? last.findings.filter((f) => f.status !== "rejected").length : 0;
  }
  // аккордеон: авто-раскрыта глава, требующая внимания (есть замечания / активная),
  // остальные свёрнуты; клик по шапке переопределяет.
  function isOpen(idx: number): boolean {
    if (idx in toggled) return toggled[idx];
    return openAll(idx) > 0 || idx === props.activeIdx;
  }
  const toggle = (idx: number) =>
    setToggled((t) => ({ ...t, [idx]: !isOpen(idx) }));

  const val = (idx: number, field: string, server: string | null) => {
    const k = `${idx}.${field}`;
    return k in edits ? edits[k] : (server ?? "");
  };
  const upd = (idx: number, field: string, value: string) =>
    setEdits((e) => ({ ...e, [`${idx}.${field}`]: value }));
  function buildMerged(): Chapter[] {
    return props.chapters.map((c) => ({
      ...c,
      title: val(c.index, "title", c.title),
      plan: val(c.index, "plan", c.plan),
      dialogue: c.dialogue == null ? null : val(c.index, "dialogue", c.dialogue),
      adult_scene: c.adult_scene == null ? null : val(c.index, "adult_scene", c.adult_scene),
      translation: c.translation == null ? null : val(c.index, "translation", c.translation),
    }));
  }
  async function commit() {
    await props.onSaveAll(buildMerged());
    setEdits({});
  }
  // мгновенно переключить адалт/неадалт у главы (с сохранением текущих правок)
  async function withWork(key: string, fn: () => Promise<void>) {
    setWorking(key);
    try { await commit(); await fn(); } finally { setWorking(null); }
  }
  function rounds(idx: number) {
    return props.reports.filter((r) => r.chapter_index === idx);
  }
  function pick(id: string, chIdx?: number) {
    setActiveFinding(id);
    // клик по замечанию → включаем подсветку этой главы, затем скроллим к месту
    if (chIdx != null) setHlOn((s) => ({ ...s, [chIdx]: true }));
    setTimeout(() => document.getElementById(`mark-${id}`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" }), 60);
  }

  return (
    <>
      <h2 className="section">
        Главы · структура → диалоги+адалт → редактор
        <span className="row" style={{ marginLeft: "auto", gap: 8 }}>
          {props.canDownload && props.onDownload && (
            <button className="small ghost" disabled={props.busy || props.downloading}
              onClick={() => props.onDownload!("docx")}
              title="Скачать новеллу в .docx">
              {props.downloading
                ? <><Loader2 size={14} className="spin" /> Собираю…</>
                : <><Download size={14} /> Скачать .docx</>}
            </button>
          )}
          {props.rollbackStage === "dialogue" && (
            <button className="small ghost" disabled={props.busy} onClick={props.onRollbackWriting}
              title="Удалить все тексты глав и написать заново (план сохранится)"><Undo2 size={14} /> Откат — написание</button>
          )}
          {props.rollbackStage === "structure" && (
            <button className="small ghost" disabled={props.busy} onClick={props.onRollbackStructure}
              title="Удалить план глав и сгенерировать структуру заново"><Undo2 size={14} /> Откат — структура</button>
          )}
          {dirty && <button className="small" disabled={props.busy} onClick={commit}><Check size={14} /> Сохранить главы</button>}
          <button className="small ghost" disabled={props.busy}
            onClick={() => props.onAddChapter(props.chapters.length - 1)}
            title="Добавить пустой черновик главы в конец (план напишешь сам или сгенерируешь ИИ)">
            <Plus size={14} /> Добавить главу
          </button>
        </span>
      </h2>
      {[...props.chapters].sort((a, b) => b.index - a.index).map((c) => {
        const i = c.index;
        const rs = rounds(c.index);
        const lastReport = rs[rs.length - 1];
        const allFindings = lastReport?.findings ?? [];
        const crit = critOpen(c.index);
        const openN = openAll(c.index);
        const open = isOpen(c.index);
        const verdict = !lastReport ? null : openN > 0 ? "needs" : "ok";
        const isTop = i >= props.chapters.length - 1;   // верхняя на экране
        const isBottom = i === 0;                        // нижняя = первая глава
        return (
          <div className={`card chapter ${c.is_adult_point ? "adultcard" : ""} ${openN > 0 ? "needs" : ""} ${overIdx === i && dragIdx !== null && dragIdx !== i ? "drag-over" : ""} ${dragIdx === i ? "dragging" : ""}`}
            key={c.index}
            onDragOver={(e) => { if (dragIdx !== null) { e.preventDefault(); setOverIdx(i); } }}
            onDrop={(e) => { e.preventDefault(); if (dragIdx !== null) reorderTo(dragIdx, i); setDragIdx(null); setOverIdx(null); }}>
            <div className="chap-head-row">
              <span className="drag-handle" draggable
                onDragStart={() => setDragIdx(i)}
                onDragEnd={() => { setDragIdx(null); setOverIdx(null); }}
                title="Перетащи, чтобы изменить порядок глав">
                <GripVertical size={15} />
              </span>
              <button className="chap-head" onClick={() => toggle(c.index)}>
                <span className="caret">{open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</span>
                <span className="t">Глава {c.index + 1} · {val(i, "title", c.title)}</span>
                {c.is_adult_point && <span className="tag18">18+</span>}
                {verdict === "needs" && (
                  <span className={`cbadge ${crit > 0 ? "crit" : "warn"}`}>
                    <span className={`sevdot ${crit > 0 ? "crit" : "imp"}`} /> {openN} — решить
                  </span>
                )}
                {verdict === "ok" && <span className="cbadge ok"><Check size={12} /> готова</span>}
                {c.dialogue == null && <span className="cbadge wait">черновик плана</span>}
              </button>
              <span className="chap-head-words" title="Цель по словам (пусто = 3600) и текущий объём">
                <input type="number" min={0} step={250} placeholder="3600"
                  defaultValue={c.target_words ?? ""}
                  disabled={props.busy}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") props.onSetWords(c.index, +(e.target as HTMLInputElement).value || 0);
                  }}
                  onBlur={(e) => { const v = +e.target.value || 0; if ((c.target_words ?? 0) !== v) props.onSetWords(c.index, v); }} />
                <span className="cw-unit">сл.</span>
                {c.dialogue != null && <span className="cw-count">≈{wordCount(c.dialogue)}</span>}
              </span>
            </div>
            {open && (<>

            {/* управление главой — порядок (на экране), вставка пустой, удаление.
                Низ = первая глава по сюжету; «Выше»/«Ниже» = движение на экране. */}
            <div className="chap-manage">
              <button className="xs ghost" disabled={props.busy || isTop}
                onClick={() => commitThen(() => props.onMoveChapter(i, "down"))}
                title="Поднять главу выше на экране (ближе к финалу истории)">
                <ArrowUp size={12} /> Выше
              </button>
              <button className="xs ghost" disabled={props.busy || isBottom}
                onClick={() => commitThen(() => props.onMoveChapter(i, "up"))}
                title="Опустить главу ниже на экране (ближе к началу истории)">
                <ArrowDown size={12} /> Ниже
              </button>
              <button className="xs ghost danger" disabled={props.busy || props.chapters.length <= 1}
                onClick={() => commitThen(() => props.onDeleteChapter(i))}
                title="Удалить эту главу (остальные переиндексируются)">
                <Trash2 size={12} /> Удалить
              </button>
            </div>

            {/* редактируемое название главы */}
            <div className="fieldhead"><label>Название главы</label></div>
            <input className="chap-title-input" type="text" disabled={props.busy}
              value={val(i, "title", c.title)}
              onChange={(e) => upd(i, "title", e.target.value)} />

            {/* растянуть главу (объём/счётчик — в шапке) */}
            {c.dialogue != null && (
              <div className="chap-ops">
                <span className="cw-sep" />
                <button className="small ghost" disabled={props.busy}
                  onClick={() => props.onExpandChapter(c.index)}
                  title="Растянуть: сделать главу подробнее (~+800 слов)">
                  <ArrowRight size={14} /> Растянуть
                </button>
              </div>
            )}

            {/* пре-чек адалта: главе не из чего генерить сцену */}
            {c.adult_block_reason && (
              <div className="adult-warn">
                <div className="wt"><AlertTriangle size={14} /> Адалт не сгенерирован — нет почвы в главе</div>
                <div className="wr">{c.adult_block_reason}</div>
                {c.adult_bridge_hint && (
                  <div className="wh">Подсказка: {c.adult_bridge_hint}</div>
                )}
                <div className="row">
                  <button className="small" disabled={adapting === c.index || props.busy}
                    onClick={async () => {
                      setAdapting(c.index);
                      try { await props.onAdaptAdult(c.index); } finally { setAdapting(null); }
                    }}>
                    {adapting === c.index
                      ? <><Loader2 size={14} className="spin" /> Адаптирую…</>
                      : <><Wrench size={14} /> Адаптировать главу под адалт</>}
                  </button>
                  <button className="small ghost" disabled={adapting === c.index || props.busy}
                    onClick={() => props.onSkipAdult(c.index)}>
                    Оставить без адалта
                  </button>
                </div>
              </div>
            )}

            {/* панель ревизии: чат по правкам (применение — кнопкой слева).
                Сверху: свежая ревизия. Только если есть открытые замечания. */}
            {lastReport && lastReport.findings.length > 0 && (
              <RevisionPanel
                threadId={props.threadId}
                idx={c.index}
                round={rs.length}
                openCrit={crit}
                busy={props.busy}
              />
            )}

            {/* журнал редактора: ревизии ПО УБЫВАНИЮ — свежая сверху */}
            {rs.length > 0 && (
              <div className="journal">
                {rs.map((r, k) => ({ r, k })).reverse().map(({ r, k }) => (
                  <div className={`round ${critCount(r) > 0 ? "crit" : "clean"}`} key={k}>
                    <div className="rh">
                      <span className="lbl">ревизия {k + 1}</span>
                      <span className="counts">
                        <span className="countpair c-crit"><span className="sevdot crit" />{critCount(r)}</span>
                        <span className="countpair c-imp"><span className="sevdot imp" />{impCount(r)}</span>
                        <span className="countpair c-min"><span className="sevdot min" />{minCount(r)}</span>
                      </span>
                    </div>
                    {r.findings.length === 0 && (
                      <div className="round-clean"><Check size={13} /> замечаний нет</div>
                    )}
                    {r.findings.map((f) => (
                      <FindingRow key={f.id} f={f} active={activeFinding === f.id}
                        disabled={props.busy} readOnly={k !== rs.length - 1}
                        onPick={() => pick(f.id, i)} onDecide={props.onDecide} />
                    ))}
                  </div>
                ))}
              </div>
            )}

            <div className="fieldhead"><label>План · Бот 04</label><TallBtn i={i} f="plan" /></div>
            <BotHint stage="structure" />
            <textarea className={tallCls(i, "plan")} rows={3} value={val(i, "plan", c.plan)}
              placeholder="План пуст — напиши сам по разделам (LOCATIONS / CHARACTERS / STORY / CHOICE / ADULT SCENES) или сгенерируй кнопкой ниже"
              onChange={(e) => upd(i, "plan", e.target.value)} />
            <TranslateBox text={val(i, "plan", c.plan)} />
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              {c.dialogue != null && (
                <button className="small secondary" disabled={working === `rw${i}` || props.busy}
                  title="Сохранить план и переписать диалоги главы под него"
                  onClick={() => withWork(`rw${i}`, () => props.onRewriteDialogue(i))}>
                  {working === `rw${i}`
                    ? <><Loader2 size={14} className="spin" /> Переписываю…</>
                    : <><RefreshCw size={14} /> План → переписать диалоги</>}
                </button>
              )}
              <ReviseBox label="Попросить ИИ изменить план главы" disabled={props.busy} onRevise={(fb) => props.onReviseChapter(i, fb)} />
            </div>
            <ChatBox threadId={props.threadId} chapterIdx={i}
              discussOnly title="Обсудить главу с ИИ"
              hint="Обсуди главу целиком — план и текст (если он есть): задай вопрос, попроси совет или варианты. Ничего не меняется автоматически; правки вноси кнопкой «Попросить ИИ изменить»."
              disabled={props.busy} />

            {c.dialogue == null && props.writingIdx === i && (
              <div className="geninline">
                <span className="genbadge"><Loader2 size={14} className="spin" /> ИИ пишет главу{c.is_adult_point ? " (диалоги + адалт)" : ""}…</span>
                {props.writingText && props.writingText.trim() ? (
                  <div className="livetext">{props.writingText}<span className="gencaret" /></div>
                ) : (
                  <div className="skel">
                    {[92, 76, 88, 64, 90].map((w, k) => <span key={k} className="sl" style={{ width: `${w}%` }} />)}
                  </div>
                )}
              </div>
            )}

            {c.dialogue != null && (() => {
              const dtext = val(i, "dialogue", c.dialogue);
              const hasQ = allFindings.some((f) => f.quote && dtext.includes(f.quote));
              return (<>
              <div className="fieldhead">
                <label>Диалоги + адалт · Бот 05</label>
                <span className="row" style={{ gap: 6 }}>
                  {hasQ && (
                    <button className="xs ghost" disabled={props.busy}
                      onClick={() => setHlOn((s) => ({ ...s, [i]: !s[i] }))}
                      title="Подсветить жёлтым места из замечаний редактора / вернуться к правке">
                      {hlOn[i]
                        ? <><Wand2 size={13} /> Редактировать текст</>
                        : <><ScanSearch size={13} /> Подсветить замечания</>}
                    </button>
                  )}
                  <TallBtn i={i} f="dialogue" />
                </span>
              </div>
              <BotHint stage="dialogue" />
              {hasQ && hlOn[i] ? (
                <div className={`highlight-wrap ${tallCls(i, "dialogue")}`}>
                  <Highlighted text={dtext} findings={allFindings} active={activeFinding} onPick={(id) => pick(id, i)} />
                </div>
              ) : (
                <textarea className={tallCls(i, "dialogue")} rows={14} value={dtext} onChange={(e) => upd(i, "dialogue", e.target.value)} />
              )}
              <button className="small ghost" disabled={working === `sp${i}` || props.busy}
                title="Сохранить текст и подогнать план главы под написанное"
                onClick={() => withWork(`sp${i}`, () => props.onSyncPlan(i))}>
                {working === `sp${i}`
                  ? <><Loader2 size={14} className="spin" /> Синхронизирую…</>
                  : <><RefreshCw size={14} /> Диалоги → обновить план</>}
              </button>
              <TranslateBox text={dtext} />
              <ReviseBox label="Попросить ИИ переписать диалоги/сцену по правкам (вкл. адалт)"
                disabled={props.busy}
                onRevise={(fb) => props.onReviseDialogue(i, fb)} />
            </>); })()}
            {c.adult_scene != null && (<>
              <div className="fieldhead"><label className="adult">Адалт-сцена · Бот 06</label><TallBtn i={i} f="adult_scene" /></div>
              <textarea className={tallCls(i, "adult_scene")} rows={10} value={val(i, "adult_scene", c.adult_scene)} onChange={(e) => upd(i, "adult_scene", e.target.value)} />
            </>)}
            {c.translation != null && (<>
              <div className="fieldhead"><label>Перевод · Бот 08</label><TallBtn i={i} f="translation" /></div>
              <textarea className={tallCls(i, "translation")} rows={7} value={val(i, "translation", c.translation)} onChange={(e) => upd(i, "translation", e.target.value)} />
            </>)}
            </>)}
          </div>
        );
      })}
    </>
  );
}

function FindingRow({
  f, active, onPick, onDecide, disabled, readOnly,
}: {
  f: Finding; active: boolean; onPick: () => void; disabled?: boolean;
  readOnly?: boolean;
  onDecide: (fid: string, body: { status?: FindingStatus }) => Promise<void>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  async function act(key: string, status: FindingStatus) {
    setBusy(key);
    try { await onDecide(f.id, { status }); } finally { setBusy(null); }
  }
  const rejected = f.status === "rejected";
  return (
    <div className={`finding ${f.severity} st-${f.status} ${active ? "active" : ""}`}>
      <div className="meta">
        [{f.block}] · {f.locator}
        <span className={`fstatus ${f.status}`}>
          {rejected ? "не менять" : "будет исправлено"}
        </span>
      </div>
      <div className="fprob">{f.problem}</div>
      {f.quote && (
        <div className="fquote" onClick={onPick} title="Показать в тексте">“{f.quote}”</div>
      )}
      {!readOnly && (
        <div className="factions">
          {rejected ? (
            <button className="xs ghost" disabled={!!busy || disabled}
              onClick={() => act("open", "open")}>
              {busy === "open" ? "…" : <><Undo2 size={13} /> Вернуть в правки</>}
            </button>
          ) : (
            <button className="xs no" disabled={!!busy || disabled}
              title="ИИ не будет это менять при перепроверке"
              onClick={() => act("rej", "rejected")}>
              {busy === "rej" ? "…" : <><X size={13} /> Не менять это</>}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------- панель ревизии: ТОЛЬКО чат по правкам ----------
// У каждой ревизии свой чат (ключ с номером раунда). Применение — единой
// кнопкой слева «Исправить всё и перепроверить» (она читает этот чат).
function RevisionPanel({
  threadId, idx, round, openCrit, busy,
}: {
  threadId: string; idx: number; round: number;
  openCrit: number; busy?: boolean;
}) {
  const key = `chat:${threadId}:c${idx}:r${round}`;
  return (
    <div className="revpanel">
      <div className="rp-head">
        {openCrit > 0 && <span className="sevdot crit" />}
        {openCrit > 0
          ? `${openCrit} критич. — исправь (слева «Исправить всё»), иначе дальше не пойдём`
          : "Замечания не критичны. Обсуди при необходимости и жми «Исправить всё» слева"}
      </div>
      <ChatBox threadId={threadId} chapterIdx={idx} storageKey={key}
        disabled={busy} title={`Обсудить правки ревизии ${round}`} />
    </div>
  );
}

// ---------- чат с ИИ-редактором: память (localStorage) + применить к главе ----------
function ChatBox({
  threadId, chapterIdx, findingId, title, onApplied, storageKey, disabled,
  discussOnly, hint,
}: {
  threadId: string; chapterIdx?: number; findingId?: string; title: string;
  onApplied?: () => void; storageKey?: string; disabled?: boolean;
  discussOnly?: boolean; hint?: string;
}) {
  // storageKey задан → чат привязан к конкретной ревизии (своя память),
  // применение делает RevisionPanel, поэтому кнопку «применить к главе» прячем.
  const key = storageKey
    ?? `chat:${threadId}:${chapterIdx != null ? `c${chapterIdx}` : `f${findingId}`}`;
  const isRevisionChat = !!storageKey;
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [applyMsg, setApplyMsg] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement | null>(null);

  // память: грузим/сохраняем историю чата в localStorage (переживает рефреш)
  useEffect(() => {
    try { const v = localStorage.getItem(key); if (v) setMsgs(JSON.parse(v)); } catch {}
  }, [key]);
  useEffect(() => {
    try { localStorage.setItem(key, JSON.stringify(msgs)); } catch {}
  }, [key, msgs]);
  useEffect(() => {
    if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [msgs.length, busy]);

  async function send() {
    const t = text.trim();
    if (!t || busy) return;
    const next: ChatMsg[] = [...msgs, { role: "user", content: t }];
    setMsgs(next); setText(""); setBusy(true);
    try {
      const { reply } = await chatWithEditor(threadId, next, {
        chapter_idx: chapterIdx, finding_id: findingId,
      });
      setMsgs([...next, { role: "assistant", content: reply }]);
    } catch {
      setMsgs([...next, { role: "assistant", content: "Ошибка запроса — попробуй ещё раз." }]);
    } finally { setBusy(false); }
  }

  async function applyToChapter() {
    if (chapterIdx == null || busy || msgs.length === 0) return;
    setBusy(true); setApplyMsg(null);
    try {
      const r = await applyChatToChapter(threadId, chapterIdx, msgs);
      setApplyMsg(r.changed ? `Глава изменена: ${r.note}` : `Без изменений: ${r.note}`);
      if (r.changed) onApplied?.();
    } catch {
      setApplyMsg("Не удалось применить");
    } finally { setBusy(false); }
  }

  if (!open) {
    return (
      <button className="small ghost" disabled={disabled} onClick={() => setOpen(true)}>
        <MessageSquare size={14} /> {title}{msgs.length > 0 ? ` (${msgs.length})` : ""}
      </button>
    );
  }
  return (
    <div className="chatbox">
      <div className="ch-head">
        <span className="ch-title"><MessageSquare size={14} /> {title}</span>
        <span className="row" style={{ gap: 6 }}>
          {msgs.length > 0 && <button className="xs ghost" onClick={() => { setMsgs([]); setApplyMsg(null); }}>очистить</button>}
          <button className="xs ghost" onClick={() => setOpen(false)}>свернуть</button>
        </span>
      </div>
      <div className="ch-msgs" ref={boxRef}>
        {msgs.length === 0 && (
          <div className="ch-hint">{hint ?? "Обсуди правку: «как лучше переписать сцену?», «согласен с замечанием?», «предложи 2 варианта реплики». Потом можно применить обсуждённое к главе."}</div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`ch-msg ${m.role}`}>{m.content}</div>
        ))}
        {busy && <div className="ch-msg assistant typing">ИИ печатает…</div>}
      </div>
      {applyMsg && <div className="ch-apply-note">{applyMsg}</div>}
      <div className="ch-input">
        <textarea rows={2} value={text} placeholder="Сообщение…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="small icon" disabled={busy || !text.trim() || disabled} onClick={send}>
          {busy ? <Loader2 size={15} className="spin" /> : <SendHorizontal size={15} />}
        </button>
      </div>
      {!isRevisionChat && !discussOnly && chapterIdx != null && msgs.length > 0 && (
        <button className="small secondary" style={{ margin: "0 12px 12px" }}
          disabled={busy || disabled} onClick={applyToChapter}
          title="ИИ внесёт согласованное в главу (или оставит как есть)">
          {busy ? <><Loader2 size={14} className="spin" /> Применяю…</>
                : <><Wand2 size={14} /> Применить обсуждённое к главе</>}
        </button>
      )}
    </div>
  );
}

// ---------- Claude по подписке: статус + тумблер «все боты» + авторизация ----------
function ClaudeSubPanel() {
  const [s, setS] = useState<ClaudeStatus | null>(null);
  const [model, setModel] = useState("sonnet");
  const [showAuth, setShowAuth] = useState(false);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [rl, setRl] = useState<ClaudeRateLimit | null>(null);

  async function load() {
    try { const r = await claudeStatus(); setS(r); setModel(r.model || "sonnet"); } catch {}
    try { const u = await claudeUsage(); setRl(u.rate_limit); } catch {}
  }
  useEffect(() => {
    load();
    const t = setInterval(load, 30000);  // проактивно обновляем остаток лимита
    return () => clearInterval(t);
  }, []);

  async function toggle(enabled: boolean) {
    setBusy(true);
    try { setS(await setClaudeSubscription(enabled, model)); } finally { setBusy(false); }
  }
  async function saveToken() {
    if (!token.trim()) return;
    setBusy(true);
    try { setS(await setClaudeToken(token.trim())); setToken(""); setShowAuth(false); }
    finally { setBusy(false); }
  }
  const [authErr, setAuthErr] = useState<string | null>(null);
  const [authLink, setAuthLink] = useState<string | null>(null);
  // получить OAuth-ссылку и показать её в меню (window.open после await блочит
  // попап-блокер — поэтому даём кликабельную ссылку, юзер откроет сам)
  async function connectOauth() {
    setBusy(true); setAuthErr(null);
    try {
      const r = await claudeAuthUrl();
      if (r.authorized) { setAuthLink(null); setShowAuth(false); await load(); return; }
      setAuthLink(r.url ?? null);
      setShowAuth(true);
      if (r.url) { try { window.open(r.url, "_blank", "noopener,noreferrer"); } catch {} }
    } catch (e) {
      setAuthErr(e instanceof Error ? e.message : "не удалось получить ссылку");
      setShowAuth(true);
    } finally { setBusy(false); }
  }
  async function exchangeCode() {
    if (!token.trim()) return;
    setBusy(true); setAuthErr(null);
    try { setS(await claudeExchange(token.trim())); setToken(""); setShowAuth(false); }
    catch (e) { setAuthErr(e instanceof Error ? e.message : "обмен не удался"); }
    finally { setBusy(false); }
  }

  return (
    <div className="subpanel">
      <div className="sp-head">
        <span className="sp-title"><Sparkles size={14} /> Claude по подписке</span>
        <span className={`sp-dot ${s?.authorized ? "ok" : "no"}`} title={s?.authorized ? "подключено" : "нет авторизации"} />
      </div>
      <div className="sp-status">
        {s?.authorized
          ? <>подключено{s.expires ? ` · до ${s.expires}` : ""}{s.sub ? ` · ${s.sub}` : ""}</>
          : "не авторизован"}
      </div>
      {s?.warn && <div className="sp-warn"><AlertTriangle size={13} /> {s.warn}</div>}
      {rl && (rl.utilization != null || rl.resets_at != null) && (
        <div className={`sp-usage ${rl.status === "rejected" ? "rej" : rl.status === "allowed_warning" ? "warn" : ""}`}>
          {rl.utilization != null && (
            <>Лимит израсходован: <b>{Math.round(rl.utilization * 100)}%</b></>
          )}
          {rl.resets_at != null && (
            <> · сброс {new Date(rl.resets_at * 1000).toLocaleString()}</>
          )}
          {rl.status === "rejected" && <> · исчерпан</>}
        </div>
      )}

      <div className="check" style={{ margin: "8px 0" }}>
        {/* СНЯТЬ галочку можно всегда (даже без авторизации) — чтобы уйти на
            OpenRouter. Блокируем только ВКЛючение без авторизации. */}
        <input id="suball" type="checkbox"
          disabled={busy || (!s?.authorized && !s?.enabled)}
          checked={!!s?.enabled} onChange={(e) => toggle(e.target.checked)} />
        <label htmlFor="suball" className="inline">Все боты через подписку (кроме адалта)</label>
      </div>
      <select className="model-select" value={model}
        onChange={(e) => { setModel(e.target.value); if (s?.enabled) setClaudeSubscription(true, e.target.value).then(setS); }}>
        <option value="haiku">Haiku — быстро/дёшево по лимиту</option>
        <option value="sonnet">Sonnet — баланс</option>
        <option value="opus">Opus — максимум</option>
      </select>

      {!s?.authorized ? (
        <button className="small wide" style={{ marginTop: 8 }} disabled={busy} onClick={connectOauth}>
          {busy ? <Loader2 size={14} className="spin" /> : <Link2 size={14} />} Авторизоваться
        </button>
      ) : (
        <button className="small ghost wide" style={{ marginTop: 8 }} disabled={busy} onClick={connectOauth}>
          Сменить аккаунт
        </button>
      )}
      {showAuth && (
        <div className="sp-auth">
          {authLink && (
            <a className="sp-authlink" href={authLink} target="_blank" rel="noopener noreferrer">
              <Link2 size={14} /> Открыть страницу авторизации Claude
            </a>
          )}
          <div className="sp-step">1. Открой ссылку выше, войди в Claude и подтверди доступ.</div>
          <div className="sp-step">2. Скопируй выданный код и вставь сюда:</div>
          <textarea rows={2} value={token} placeholder="код авторизации (вид code#state)"
            onChange={(e) => setToken(e.target.value)} />
          <button className="small" disabled={busy || !token.trim()} onClick={exchangeCode}>
            {busy ? "…" : "Подключить"}
          </button>
          {authErr && <div className="sp-warn"><AlertTriangle size={13} /> {authErr}</div>}
          <div className="sp-note">
            Альтернатива: вставь долгоживущий токен из <code>claude setup-token</code> и нажми
            {" "}<button className="xs ghost" disabled={busy || !token.trim()} onClick={saveToken}>как токен</button>.
            Если уже залогинен в Claude Code — подписка работает без этого.
          </div>
        </div>
      )}
    </div>
  );
}
