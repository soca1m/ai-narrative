"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Play, Plus, FolderOpen, Download, RotateCw, Undo2, Check, X,
  ChevronDown, ChevronRight, MessageSquare, Sparkles, Wrench, Link2,
  ScanSearch, SendHorizontal, AlertTriangle, Loader2, BookOpenText,
  CircleCheckBig, RefreshCw, Wand2, ArrowRight, Circle, Minus, Pause,
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
  chatWithEditor,
  applyChatToChapter,
  applyRevision,
  exportProject,
  restructure,
  addChapter,
  deleteChapter,
  setStageProvider,
  resolveLimit,
  RunSummary,
  listRuns,
  claudeStatus,
  claudeAuthUrl,
  claudeExchange,
  setClaudeSubscription,
  setClaudeToken,
  adaptAdult,
  decideFinding,
  getState,
  patchState,
  resumeRun,
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
  logline: "Бот придумает несколько вариантов логлайна — выберешь один.",
  synopsis: "Развёрнутый синопсис на основе выбранного логлайна.",
  characters: "Карточки персонажей: внешность, характер, мотивации (канон).",
  locations: "Карточки локаций: места действия, атмосфера, теги для художника.",
  chapter_count: "ИИ оценит и предложит оптимальное число глав.",
  structure: "Поглавный план: что в каждой главе + где адалт-точки.",
  structure_editor: "Редактор проверит и сам починит план до написания.",
  dialogue: "Бот напишет диалоги глав (адалт — внутри сцены).",
  editor: "Редактор проверит готовые главы и предложит правки.",
  translation: "Перевод глав на выбранный язык.",
};

const BOT_NAME: Record<number, string> = {
  1: "Логлайн", 2: "Синопсис", 3: "Персонажи", 4: "Структура",
  5: "Диалоги", 6: "Адалт", 7: "Редактор", 8: "Перевод",
};

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
  const [lang, setLang] = useState("Russian");
  const [translation, setTranslation] = useState(false);
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

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState<string | null>(null);
  const [countInput, setCountInput] = useState<number | "">("");
  const [reCount, setReCount] = useState<number | "">("");  // #7 пересборка

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

  function syncDrafts(s: NarrativeState) {
    setDrafts((prev) => {
      const d = { ...prev };
      for (const k of ["synopsis", "characters", "locations"] as const) {
        if (dirty.has(k)) continue;
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
    });
    setThreadId(thread_id);
    poll(thread_id);
  }

  function poll(id: string) {
    if (pollRef.current) clearTimeout(pollRef.current);
    const tick = async () => {
      try {
        const r = await getState(id);
        setStatus(r.status);
        setNext(r.next);
        setErr(r.error || null);
        setLimit(r.limit || null);
        setGen(r.gen || null);
        setSt(r.state);
        syncDrafts(r.state);
        if (r.status === "running" || r.status === "idle")
          // во время генерации поллим часто → видно как ИИ пишет текст
          pollRef.current = setTimeout(tick, r.gen ? 400 : 1200);
      } catch {
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

  function refresh() { if (threadId) poll(threadId); }

  // #7: пересобрать структуру под новое число глав / добавить / удалить главу
  async function onRestructure() {
    if (!threadId || !reCount) return;
    if (!confirm(`Пересобрать структуру под ${reCount} глав? Сюжет растянется/`
      + `сожмётся, написанные тексты глав сбросятся.`)) return;
    await restructure(threadId, +reCount);
    setReCount(""); setStatus("running"); poll(threadId);
  }
  async function onAddChapter(after: number, isAdult = true) {
    if (!threadId) return;
    await addChapter(threadId, after, isAdult);
    setStatus("running"); poll(threadId);
  }
  async function onDeleteChapter(idx: number) {
    if (!threadId) return;
    if (!confirm(`Удалить главу ${idx + 1}?`)) return;
    await deleteChapter(threadId, idx); refresh();
  }

  // #5: решение по исчерпанному лимиту провайдера
  async function onLimit(action: "switch" | "wait" | "subscription") {
    if (!threadId) return;
    try { await resolveLimit(threadId, action); } catch {}
    setLimit(null);
    if (action !== "wait") setStatus("running");
    poll(threadId);
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

  async function onDownload(fmt: "txt" | "md") {
    if (!threadId) return;
    try {
      const r = await exportProject(threadId, fmt);
      downloadText(r.filename, r.text);
    } catch {
      setErr("Не удалось собрать проект для скачивания");
    }
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
        <label>Язык</label>
        <input type="text" value={lang} onChange={(e) => setLang(e.target.value)} />
        <div className="check">
          <input id="tr" type="checkbox" checked={translation}
            onChange={(e) => setTranslation(e.target.checked)} />
          <label htmlFor="tr" className="inline">Перевод (Бот 8) — сейчас на паузе</label>
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
              </div>
            )}
            {status === "paused" && !pausedAtCount && !gating && (
              <button className="wide" onClick={onResume}>
                {curEdited
                  ? <><Check size={16} /> Глава {curIdx + 1} готова — следующая</>
                  : <><ArrowRight size={16} /> Продолжить · {nextLabel(next)}</>}
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
            <div className="thread">thread · {threadId}</div>
          </>
        )}
      </aside>

      {/* STAGE */}
      <main className="stage-area">
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
                  <button onClick={() => onDownload("txt")}><Download size={16} /> Скачать .txt</button>
                  <button className="secondary" onClick={() => onDownload("md")}>
                    <Download size={16} /> Скачать .md
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
                <button className="small secondary" disabled={busy}
                  onClick={() => onAddChapter((st.chapters?.length ?? 1) - 1, true)}
                  title="ИИ допишет адалт-главу в конец">
                  <Plus size={14} /> Глава в конец 🔞
                </button>
                <button className="small ghost" disabled={busy}
                  onClick={() => onAddChapter((st.chapters?.length ?? 1) - 1, false)}
                  title="ИИ допишет обычную (неадалт) главу в конец">
                  <Plus size={14} /> Без адалта
                </button>
              </div>
            )}

            {/* Порядок убывания: свежие результаты сверху (главы → персонажи → синопсис → логлайн) */}
            {showGen("structure") && <GenCard bot="06" title="Структура · поглавный план" rows={5} live={liveFor("structure")} />}

            <Chapters
              onAddChapter={onAddChapter}
              onDeleteChapter={onDeleteChapter}
              threadId={threadId}
              phase={st.phase}
              activeIdx={curIdx}
              writingIdx={writingIdx}
              writingText={writingText}
              onRefresh={refresh}
              busy={busy}
              canDownload={allWritten}
              onDownload={onDownload}
              chapters={st.chapters ?? []}
              reports={reports}
              onSaveAll={(chs) => guard(async () => { if (threadId) { await patchState(threadId, { chapters: chs }); refresh(); } })}
              onReviseChapter={(i, fb) => guard(async () => { if (threadId) { await reviseChapter(threadId, i, fb); refresh(); } })}
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
            {showGen("locations") && <GenCard bot="04" title="Локации · места действия (канон)" rows={4} live={liveFor("locations")} />}
            <EditableCard
              title="Локации · места действия (канон)" bot="04" rows={10} valKey="locations" busy={busy}
              value={drafts.locations ?? ""} dirty={dirty.has("locations")} saved={saved === "locations"}
              onChange={(v) => edit("locations", v)} onSave={() => save("locations")}
              onRevise={(fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "locations", fb); refresh(); } })}
              onRollback={currentStage === "locations" ? () => guard(async () => { if (threadId && confirm("Откатить этап локаций: удалить карточки локаций и сгенерировать заново?")) { await rollback(threadId, "locations"); refresh(); } }) : undefined}
            />

            {/* Персонажи */}
            {showGen("characters") && <GenCard bot="03" title="Персонажи · карточки = канон" rows={6} live={liveFor("characters")} />}
            <EditableCard
              title="Персонажи · карточки = канон" bot="03" rows={12} valKey="characters" busy={busy}
              value={drafts.characters ?? ""} dirty={dirty.has("characters")} saved={saved === "characters"}
              onChange={(v) => edit("characters", v)} onSave={() => save("characters")}
              onRevise={(fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "characters", fb); refresh(); } })}
              onRollback={currentStage === "characters" ? () => guard(async () => { if (threadId && confirm("Откатить этап персонажей: удалить карточки и сгенерировать заново?")) { await rollback(threadId, "characters"); refresh(); } }) : undefined}
            />

            {/* Синопсис */}
            {showGen("synopsis") && <GenCard bot="02" title="Синопсис" rows={5} live={liveFor("synopsis")} />}
            <EditableCard
              title="Синопсис" bot="02" rows={10} valKey="synopsis" busy={busy}
              value={drafts.synopsis ?? ""} dirty={dirty.has("synopsis")} saved={saved === "synopsis"}
              onChange={(v) => edit("synopsis", v)} onSave={() => save("synopsis")}
              onRevise={(fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "synopsis", fb); refresh(); } })}
              onRollback={currentStage === "synopsis" ? () => guard(async () => { if (threadId && confirm("Откатить этап синопсиса: удалить синопсис и сгенерировать заново?")) { await rollback(threadId, "synopsis"); refresh(); } }) : undefined}
            />

            {/* Логлайн — выбор одного из вариантов */}
            {showGen("logline") && <GenCard bot="01" title="Логлайн · варианты" rows={5} live={liveFor("logline")} />}
            <LoglineCard
              loglines={st.loglines ?? []}
              selected={st.selected_logline ?? ""}
              busy={busy}
              onSelect={(l) => guard(async () => { if (threadId) { await selectLogline(threadId, l); refresh(); } })}
              onRevise={(fb) => guard(async () => { if (threadId) { await reviseStage(threadId, "logline", fb); refresh(); } })}
            />
          </>
        )}
      </main>
    </div>
  );
}

// #5: выбор провайдера (подписка Claude / OpenRouter) на каждый этап
function StageProviderPanel({
  threadId, providers, forceOR, onChanged, disabled,
}: {
  threadId: string; providers: Record<string, string>; forceOR: boolean;
  onChanged: () => void; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ROWS: [string, string][] = [
    ["logline", "Логлайн"], ["synopsis", "Синопсис"], ["characters", "Персонажи"],
    ["locations", "Локации"], ["chapter_count", "Объём"], ["structure", "Структура"],
    ["structure_editor", "Ред. структуры"], ["dialogue", "Диалоги (текст главы)"],
    ["editor", "Редактор"], ["translation", "Перевод"], ["chat", "Чат"],
  ];
  async function set(stage: string, v: string) {
    await setStageProvider(threadId, stage, v); onChanged();
  }
  return (
    <div className="subpanel">
      <div className="sp-head" style={{ cursor: "pointer" }} onClick={() => setOpen((o) => !o)}>
        <span>Провайдер по этапам</span>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </div>
      {forceOR && <div className="sp-hint">Сейчас весь ран на OpenRouter (фолбэк лимита).</div>}
      {open && (
        <div className="provrows">
          <div className="sp-hint">
            Подписка Claude (дёшево) или OpenRouter (платно) — на каждый этап.
            «по умолч.» = глобальный тумблер выше.
          </div>
          <div className="sp-hint" style={{ color: "var(--brick-soft)" }}>
            🔞 Адалт-главу ЦЕЛИКОМ пишет OpenRouter grok (без цензуры) — подписка
            Claude её не напишет. Настройка «Диалоги» влияет только на НЕ-адалт главы.
          </div>
          <div className="provrow allrow">
            <span>Все этапы</span>
            <select disabled={disabled} value=""
              onChange={(e) => e.target.value && set("all", e.target.value)}>
              <option value="">задать все…</option>
              <option value="subscription">подписка</option>
              <option value="openrouter">OpenRouter</option>
              <option value="default">по умолч.</option>
            </select>
          </div>
          {ROWS.map(([k, label]) => (
            <div className="provrow" key={k}>
              <span>{label}</span>
              <select disabled={disabled} value={providers[k] || "default"}
                onChange={(e) => set(k, e.target.value)}>
                <option value="default">по умолч.</option>
                <option value="subscription">подписка</option>
                <option value="openrouter">OpenRouter</option>
              </select>
            </div>
          ))}
        </div>
      )}
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
  loglines, selected, onSelect, onRevise, busy,
}: {
  loglines: string[]; selected: string; busy?: boolean;
  onSelect: (l: string) => void; onRevise: (fb: string) => Promise<void>;
}) {
  if (!loglines.length) return null;
  return (
    <div className="card">
      <div className="head"><span className="t"><span className="botnum">01</span>Логлайн · выбери один</span></div>
      <div className="loglines">
        {loglines.map((l, i) => (
          <label key={i} className={`logline ${selected === l ? "sel" : ""}`}>
            <input type="radio" name="logline" disabled={busy} checked={selected === l} onChange={() => onSelect(l)} />
            <span>{l}</span>
          </label>
        ))}
      </div>
      <ReviseBox label="Сгенерировать другие логлайны" onRevise={onRevise} disabled={busy} />
    </div>
  );
}

function EditableCard(props: {
  title: string; bot: string; rows: number; valKey: string; busy?: boolean;
  value: string; dirty: boolean; saved: boolean;
  onChange: (v: string) => void; onSave: () => void;
  onRevise: (fb: string) => Promise<void>; onRollback?: () => Promise<void>;
}) {
  if (!props.value && !props.dirty) return null;
  return (
    <div className="card">
      <div className="head">
        <span className="t"><span className="botnum">{props.bot}</span>{props.title}</span>
        <span className="row">
          {props.saved && <span className="saved"><Check size={13} /> сохранено</span>}
          <button className="small" disabled={!props.dirty || props.busy} onClick={props.onSave}>Сохранить</button>
          {props.onRollback && (
            <button className="small ghost" disabled={props.busy} onClick={props.onRollback}
              title="Удалить результат этого этапа и сгенерировать заново"><Undo2 size={14} /> Откат</button>
          )}
        </span>
      </div>
      <textarea rows={props.rows} value={props.value} onChange={(e) => props.onChange(e.target.value)} />
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
  onDownload?: (fmt: "txt" | "md") => void;
  onSaveAll: (chs: Chapter[]) => void;
  onReviseChapter: (i: number, fb: string) => Promise<void>;
  onRewriteDialogue: (i: number) => Promise<void>;
  onSyncPlan: (i: number) => Promise<void>;
  onRefresh: () => void;
  rollbackStage: "dialogue" | "structure" | null;
  onRollbackWriting: () => Promise<void>;
  onRollbackStructure: () => Promise<void>;
  onDecide: (fid: string, body: { status?: FindingStatus; comment?: string; judge?: boolean }) => Promise<void>;
  onAdaptAdult: (i: number) => Promise<void>;
  onSkipAdult: (i: number) => Promise<void>;
  onAddChapter: (after: number, isAdult?: boolean) => Promise<void>;
  onDeleteChapter: (idx: number) => Promise<void>;
}) {
  // overrides-модель: рендерим из серверных props.chapters, локально храним
  // ТОЛЬКО изменённые поля (ключ `${index}.${field}`). Поэтому новые главы/
  // диалоги с сервера видны сразу, а ручные правки не теряются (нет залипания).
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [activeFinding, setActiveFinding] = useState<string | null>(null);
  const [adapting, setAdapting] = useState<number | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [toggled, setToggled] = useState<Record<number, boolean>>({});
  const dirty = Object.keys(edits).length > 0;
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
  async function toggleAdult(ch: Chapter) {
    const merged = buildMerged().map((x) =>
      x.index === ch.index ? { ...x, is_adult_point: !ch.is_adult_point } : x);
    await props.onSaveAll(merged);
    setEdits({});
  }
  async function withWork(key: string, fn: () => Promise<void>) {
    setWorking(key);
    try { await commit(); await fn(); } finally { setWorking(null); }
  }
  function rounds(idx: number) {
    return props.reports.filter((r) => r.chapter_index === idx);
  }
  function pick(id: string) {
    setActiveFinding(id);
    document.getElementById(`mark-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  return (
    <>
      <h2 className="section">
        Главы · структура → диалоги+адалт → редактор
        <span className="row" style={{ marginLeft: "auto", gap: 8 }}>
          {props.canDownload && props.onDownload && (
            <button className="small ghost" disabled={props.busy} onClick={() => props.onDownload!("txt")}
              title="Скачать собранный текст всех глав"><Download size={14} /> Скачать</button>
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
        return (
          <div className={`card chapter ${c.is_adult_point ? "adultcard" : ""} ${openN > 0 ? "needs" : ""}`} key={c.index}>
            <button className="chap-head" onClick={() => toggle(c.index)}>
              <span className="caret">{open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</span>
              <span className="t">Глава {c.index + 1} · {c.title}</span>
              {c.is_adult_point && <span className="tag18">18+</span>}
              {verdict === "needs" && (
                <span className={`cbadge ${crit > 0 ? "crit" : "warn"}`}>
                  <span className={`sevdot ${crit > 0 ? "crit" : "imp"}`} /> {openN} — решить
                </span>
              )}
              {verdict === "ok" && <span className="cbadge ok"><Check size={12} /> готова</span>}
              {c.dialogue == null && <span className="cbadge wait">черновик плана</span>}
            </button>
            {open && (<>

            {/* #7: адалт-тоггл · вставить после (адалт/без) · удалить */}
            <div className="chap-ops">
              <button className={`xs ${c.is_adult_point ? "" : "ghost"}`} disabled={props.busy}
                onClick={() => toggleAdult(c)}
                title="Сделать главу адалт / неадалт">
                🔞 адалт: {c.is_adult_point ? "вкл" : "выкл"}
              </button>
              <button className="xs ghost" disabled={props.busy}
                onClick={() => props.onAddChapter(c.index, true)}
                title="ИИ вставит адалт-главу после этой">
                <Plus size={12} /> после 🔞
              </button>
              <button className="xs ghost" disabled={props.busy}
                onClick={() => props.onAddChapter(c.index, false)}
                title="ИИ вставит обычную (неадалт) главу после этой">
                <Plus size={12} /> после (без адалта)
              </button>
              <button className="xs ghost" disabled={props.busy || props.chapters.length <= 1}
                onClick={() => props.onDeleteChapter(c.index)}
                title="Удалить эту главу">
                <X size={12} /> удалить
              </button>
            </div>

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
                        disabled={props.busy}
                        onPick={() => pick(f.id)} onDecide={props.onDecide} />
                    ))}
                  </div>
                ))}
              </div>
            )}

            <label>План · Бот 04</label>
            <textarea rows={3} value={val(i, "plan", c.plan)} onChange={(e) => upd(i, "plan", e.target.value)} />
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
            <ChatBox threadId={props.threadId} chapterIdx={i} title="Обсудить главу с ИИ"
              disabled={props.busy} onApplied={props.onRefresh} />

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

            {c.dialogue != null && (<>
              <label>Диалоги + адалт · Бот 05</label>
              <Highlighted text={val(i, "dialogue", c.dialogue)} findings={allFindings} active={activeFinding} onPick={pick} />
              <textarea rows={12} value={val(i, "dialogue", c.dialogue)} onChange={(e) => upd(i, "dialogue", e.target.value)} />
              <button className="small ghost" disabled={working === `sp${i}` || props.busy}
                title="Сохранить текст и подогнать план главы под написанное"
                onClick={() => withWork(`sp${i}`, () => props.onSyncPlan(i))}>
                {working === `sp${i}`
                  ? <><Loader2 size={14} className="spin" /> Синхронизирую…</>
                  : <><RefreshCw size={14} /> Диалоги → обновить план</>}
              </button>
            </>)}
            {c.adult_scene != null && (<>
              <label className="adult">Адалт-сцена · Бот 06</label>
              <Highlighted text={val(i, "adult_scene", c.adult_scene)} findings={allFindings} active={activeFinding} onPick={pick} />
              <textarea rows={10} value={val(i, "adult_scene", c.adult_scene)} onChange={(e) => upd(i, "adult_scene", e.target.value)} />
            </>)}
            {c.translation != null && (<>
              <label>Перевод · Бот 08</label>
              <textarea rows={7} value={val(i, "translation", c.translation)} onChange={(e) => upd(i, "translation", e.target.value)} />
            </>)}
            </>)}
          </div>
        );
      })}
    </>
  );
}

function FindingRow({
  f, active, onPick, onDecide, disabled,
}: {
  f: Finding; active: boolean; onPick: () => void; disabled?: boolean;
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
}: {
  threadId: string; chapterIdx?: number; findingId?: string; title: string;
  onApplied?: () => void; storageKey?: string; disabled?: boolean;
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
          <div className="ch-hint">Обсуди правку: «как лучше переписать сцену?», «согласен с замечанием?», «предложи 2 варианта реплики». Потом можно применить обсуждённое к главе.</div>
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
      {!isRevisionChat && chapterIdx != null && msgs.length > 0 && (
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

  async function load() {
    try { const r = await claudeStatus(); setS(r); setModel(r.model || "sonnet"); } catch {}
  }
  useEffect(() => { load(); }, []);

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

      <div className="check" style={{ margin: "8px 0" }}>
        <input id="suball" type="checkbox" disabled={!s?.authorized || busy}
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
