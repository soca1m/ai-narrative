"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Chapter,
  EditorReport,
  Finding,
  FindingStatus,
  NarrativeState,
  adaptAdult,
  decideFinding,
  getState,
  patchState,
  resumeRun,
  reviseChapter,
  reviseStage,
  rollback,
  selectLogline,
  skipAdult,
  startRun,
  structureMore,
  structureProceed,
} from "../lib/api";

const STAGES: [string, string, string][] = [
  ["logline", "Логлайн", "01"],
  ["synopsis", "Синопсис", "02"],
  ["characters", "Персонажи", "03"],
  ["structure", "Структура", "04"],
  ["dialogue", "Диалоги", "05"],
  ["adult", "Адалт", "06"],
  ["editor", "Редактор", "07"],
  ["translation", "Перевод", "08"],
];

const BOT_NAME: Record<number, string> = {
  1: "Логлайн", 2: "Синопсис", 3: "Персонажи", 4: "Структура",
  5: "Диалоги", 6: "Адалт", 7: "Редактор", 8: "Перевод",
};

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

const EV_ICON: Record<string, string> = {
  approve: "✓", rollback: "↩", revise: "✦", escalate: "⚠", skip: "·", bot: "●",
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
  const [batch, setBatch] = useState(3);
  const [translation, setTranslation] = useState(false);
  const [stepMode, setStepMode] = useState(true);

  const [threadId, setThreadId] = useState<string | null>(null);
  const [status, setStatus] = useState("idle");
  const [next, setNext] = useState<string[]>([]);
  const [st, setSt] = useState<NarrativeState>({});
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState<string | null>(null);

  const events = useMemo(() => classifyLog(st.log ?? []), [st.log]);
  const rollbackTick = useMemo(
    () => events.filter((e) => e.kind === "rollback").length,
    [events],
  );

  function syncDrafts(s: NarrativeState) {
    setDrafts((prev) => {
      const d = { ...prev };
      for (const k of ["synopsis", "characters"] as const) {
        if (!dirty.has(k) && typeof s[k] === "string") d[k] = s[k] as string;
      }
      return d;
    });
  }

  async function onStart() {
    setSt({});
    setDirty(new Set());
    const { thread_id } = await startRun({
      theme, genre: genre || undefined, target_language: lang,
      chapters_per_batch: batch, translation_enabled: translation, step_mode: stepMode,
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
        setSt(r.state);
        syncDrafts(r.state);
        if (r.status === "running" || r.status === "idle")
          pollRef.current = setTimeout(tick, 1500);
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
    structure: !!st.chapters?.length,
    dialogue: !!st.chapters?.some((c) => c.dialogue),
    adult: !!st.chapters?.some((c) => c.adult_scene),
    editor: !!st.editor_reports?.length,
    translation: !!st.chapters?.some((c) => c.translation),
  };
  const activeStage = next[0] ?? null;
  const pausedAtStructure = status === "paused" && activeStage === "structure";

  const totalRevisions = Object.values(st.retry_count ?? {}).reduce((a, b) => a + b, 0);
  const reports = st.editor_reports ?? [];
  const openCrit = (st.chapters ?? []).reduce((sum, c) => {
    const rs = reports.filter((r) => r.chapter_index === c.index);
    return sum + (rs.length ? critCount(rs[rs.length - 1]) : 0);
  }, 0);
  const botsRun = new Set(events.filter((e) => e.bot).map((e) => e.bot)).size;
  const busy = status === "running";

  return (
    <div className="shell">
      {/* RAIL */}
      <aside className="rail">
        <div className="brand"><span className="dot" /> ai-narrative</div>
        <div className="tagline">КОМНАТА СЦЕНАРИСТОВ · 8 БОТОВ · 18+</div>

        <label>Тема и референсы</label>
        <textarea rows={4} value={theme} onChange={(e) => setTheme(e.target.value)} />
        <label>Особый жанр</label>
        <input type="text" value={genre} onChange={(e) => setGenre(e.target.value)}
          placeholder="напр. комедийная драма" />
        <label>Язык</label>
        <input type="text" value={lang} onChange={(e) => setLang(e.target.value)} />
        <label>Глав за порцию (структура пишется частями)</label>
        <input type="number" min={1} max={10} value={batch}
          onChange={(e) => setBatch(Math.max(1, +e.target.value || 1))} />
        <div className="check">
          <input id="tr" type="checkbox" checked={translation}
            onChange={(e) => setTranslation(e.target.checked)} />
          <label htmlFor="tr" className="inline">Перевод (Бот 8) — сейчас на паузе</label>
        </div>
        <div className="check">
          <input id="step" type="checkbox" checked={stepMode}
            onChange={(e) => setStepMode(e.target.checked)} />
          <label htmlFor="step" className="inline">Пошаговый режим — пауза после каждого бота</label>
        </div>
        <button className="wide" onClick={onStart} disabled={busy}>
          {threadId ? "↻ Запустить заново" : "▶ Запустить пайплайн"}
        </button>

        {threadId && (
          <>
            <div className="statusbar">
              <span className={`tally ${status}`}>{status}</span>
              {next.length > 0 && <span className="next-chip">→ {next.join(", ")}</span>}
              <button className="ghost small" onClick={refresh}>⟳</button>
            </div>
            {status === "paused" && !pausedAtStructure && (
              <button className="wide" onClick={onResume}>
                ⏭ Продолжить — {next.join(", ") || "далее"}
              </button>
            )}
            {pausedAtStructure && (
              <div className="batch-ctl">
                <div className="hint">
                  Структура: {st.chapters?.length ?? 0} глав
                  {st.structure_done ? " · история завершена" : " · порция готова"}
                </div>
                <button className="wide" onClick={async () => { await structureMore(threadId); setStatus("running"); poll(threadId); }}>
                  ＋ Ещё порцию ({batch} глав)
                </button>
                <button className="wide alt" onClick={async () => { await structureProceed(threadId); setStatus("running"); poll(threadId); }}>
                  ✍ Перейти к написанию
                </button>
              </div>
            )}

            <h2 className="section">Лента событий</h2>
            <div className="feed">
              <AnimatePresence initial={false}>
                {[...events].reverse().map((e) => (
                  <motion.div key={e.i} className={`ev ${e.kind}`} layout
                    initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
                    <span className="ic">{EV_ICON[e.kind] ?? "●"}</span>
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
            <div className="pipeline">
              {STAGES.map(([k, label, ix]) => {
                const off = k === "translation" && !st.translation_enabled;
                const cls = activeStage === k ? "active" : has[k] ? "done" : "";
                return (
                  <div key={k} className={`node ${cls} ${off ? "off" : ""}`}>
                    <span className="ix">{ix}</span>{label}{off ? " ⏸" : ""}
                  </div>
                );
              })}
            </div>

            <div className="stats">
              <Stat v={st.chapters?.length ?? 0} k="главы" />
              <Stat v={botsRun} k="ботов" />
              <Stat v={totalRevisions} k="правок" cls="rev" />
              <Stat v={openCrit} k="критич." cls="crit" />
            </div>

            {/* Порядок убывания: свежие результаты сверху (главы → персонажи → синопсис → логлайн) */}
            <Chapters
              threadId={threadId}
              chapters={st.chapters ?? []}
              reports={reports}
              onSaveAll={async (chs) => { if (threadId) { await patchState(threadId, { chapters: chs }); refresh(); } }}
              onReviseChapter={async (i, fb) => { if (threadId) { await reviseChapter(threadId, i, fb); refresh(); } }}
              onRollbackWriting={async () => { if (threadId && confirm("Сбросить все написанные тексты глав (диалоги/адалт/перевод) и начать написание заново? План глав сохранится.")) { await rollback(threadId, "dialogue"); refresh(); } }}
              onDecide={async (fid, body) => { if (threadId) { await decideFinding(threadId, fid, body); refresh(); } }}
              onAdaptAdult={async (i) => { if (threadId) { await adaptAdult(threadId, i); refresh(); } }}
              onSkipAdult={async (i) => { if (threadId) { await skipAdult(threadId, i); refresh(); } }}
            />

            {/* Персонажи */}
            <EditableCard
              title="Персонажи · карточки = канон" bot="03" rows={12} valKey="characters"
              value={drafts.characters ?? ""} dirty={dirty.has("characters")} saved={saved === "characters"}
              onChange={(v) => edit("characters", v)} onSave={() => save("characters")}
              onRevise={async (fb) => { if (threadId) { await reviseStage(threadId, "characters", fb); refresh(); } }}
              onRollback={async () => { if (threadId && confirm("Откатиться к персонажам? Все главы будут сброшены и перегенерированы.")) { await rollback(threadId, "characters"); refresh(); } }}
            />

            {/* Синопсис */}
            <EditableCard
              title="Синопсис" bot="02" rows={10} valKey="synopsis"
              value={drafts.synopsis ?? ""} dirty={dirty.has("synopsis")} saved={saved === "synopsis"}
              onChange={(v) => edit("synopsis", v)} onSave={() => save("synopsis")}
              onRevise={async (fb) => { if (threadId) { await reviseStage(threadId, "synopsis", fb); refresh(); } }}
              onRollback={async () => { if (threadId && confirm("Откатиться к синопсису? Персонажи и все главы будут сброшены и перегенерированы.")) { await rollback(threadId, "synopsis"); refresh(); } }}
            />

            {/* Логлайн — выбор одного из вариантов */}
            <LoglineCard
              loglines={st.loglines ?? []}
              selected={st.selected_logline ?? ""}
              onSelect={async (l) => { if (threadId) { await selectLogline(threadId, l); refresh(); } }}
              onRevise={async (fb) => { if (threadId) { await reviseStage(threadId, "logline", fb); refresh(); } }}
            />
          </>
        )}
      </main>
    </div>
  );
}

function Stat({ v, k, cls }: { v: number; k: string; cls?: string }) {
  return (
    <div className={`stat ${cls ?? ""}`}><div className="v">{v}</div><div className="k">{k}</div></div>
  );
}

// строка ввода «попроси ИИ переделать»
function ReviseBox({ label, onRevise }: { label?: string; onRevise: (fb: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [fb, setFb] = useState("");
  const [busy, setBusy] = useState(false);
  if (!open) return <button className="small ghost" onClick={() => setOpen(true)}>✦ {label ?? "Попросить ИИ переделать"}</button>;
  return (
    <div className="revise">
      <textarea rows={2} placeholder="Что изменить? напр. «больше внутреннего конфликта у героя»"
        value={fb} onChange={(e) => setFb(e.target.value)} />
      <div className="row">
        <button className="small" disabled={!fb.trim() || busy}
          onClick={async () => { setBusy(true); try { await onRevise(fb); setFb(""); setOpen(false); } finally { setBusy(false); } }}>
          {busy ? "…" : "Переделать"}
        </button>
        <button className="small ghost" onClick={() => setOpen(false)}>Отмена</button>
      </div>
    </div>
  );
}

function LoglineCard({
  loglines, selected, onSelect, onRevise,
}: {
  loglines: string[]; selected: string;
  onSelect: (l: string) => void; onRevise: (fb: string) => Promise<void>;
}) {
  if (!loglines.length) return null;
  return (
    <div className="card">
      <div className="head"><span className="t"><span className="botnum">01</span>Логлайн · выбери один</span></div>
      <div className="loglines">
        {loglines.map((l, i) => (
          <label key={i} className={`logline ${selected === l ? "sel" : ""}`}>
            <input type="radio" name="logline" checked={selected === l} onChange={() => onSelect(l)} />
            <span>{l}</span>
          </label>
        ))}
      </div>
      <ReviseBox label="Сгенерировать другие логлайны" onRevise={onRevise} />
    </div>
  );
}

function EditableCard(props: {
  title: string; bot: string; rows: number; valKey: string;
  value: string; dirty: boolean; saved: boolean;
  onChange: (v: string) => void; onSave: () => void;
  onRevise: (fb: string) => Promise<void>; onRollback: () => Promise<void>;
}) {
  if (!props.value && !props.dirty) return null;
  return (
    <div className="card">
      <div className="head">
        <span className="t"><span className="botnum">{props.bot}</span>{props.title}</span>
        <span className="row">
          {props.saved && <span className="saved">✓ сохранено</span>}
          <button className="small" disabled={!props.dirty} onClick={props.onSave}>Сохранить</button>
          <button className="small ghost" onClick={props.onRollback} title="Откатиться и перегенерировать с этого этапа">↩ Откат</button>
        </span>
      </div>
      <textarea rows={props.rows} value={props.value} onChange={(e) => props.onChange(e.target.value)} />
      <ReviseBox onRevise={props.onRevise} />
    </div>
  );
}

function Chapters(props: {
  threadId: string;
  chapters: Chapter[]; reports: EditorReport[];
  onSaveAll: (chs: Chapter[]) => void;
  onReviseChapter: (i: number, fb: string) => Promise<void>;
  onRollbackWriting: () => Promise<void>;
  onDecide: (fid: string, body: { status?: FindingStatus; comment?: string; judge?: boolean }) => Promise<void>;
  onAdaptAdult: (i: number) => Promise<void>;
  onSkipAdult: (i: number) => Promise<void>;
}) {
  const [draft, setDraft] = useState<Chapter[]>(props.chapters);
  const [dirty, setDirty] = useState(false);
  const [activeFinding, setActiveFinding] = useState<string | null>(null);
  const [adapting, setAdapting] = useState<number | null>(null);
  useEffect(() => { if (!dirty) setDraft(props.chapters); }, [props.chapters, dirty]);
  if (!draft.length) return null;

  function upd(chIndex: number, patch: Partial<Chapter>) {
    // ключ — c.index (главы рендерятся в обратном порядке, позиция != индекс)
    setDraft((d) => d.map((c) => (c.index === chIndex ? { ...c, ...patch } : c)));
    setDirty(true);
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
        Главы · Боты 04–08 · адалт в каждой
        <span className="row" style={{ marginLeft: "auto", gap: 8 }}>
          <button className="small ghost" onClick={props.onRollbackWriting} title="Сбросить все написанные тексты и начать главы заново">↩ Перезапустить написание</button>
          {dirty && <button className="small" onClick={() => { props.onSaveAll(draft); setDirty(false); }}>Сохранить главы</button>}
        </span>
      </h2>
      {[...draft].sort((a, b) => b.index - a.index).map((c) => {
        const i = c.index;
        const rs = rounds(c.index);
        const lastReport = rs[rs.length - 1];
        const allFindings = lastReport?.findings ?? [];
        return (
          <div className={`card ${c.is_adult_point ? "adultcard" : ""}`} key={c.index}>
            <div className="head">
              <span className="t">Глава {c.index + 1} · {c.title}
                {c.is_adult_point && <span className="adult">🔞 адалт</span>}</span>
            </div>

            {/* пре-чек адалта: главе не из чего генерить сцену */}
            {c.adult_block_reason && (
              <div className="adult-warn">
                <div className="wt">⚠ Адалт не сгенерирован — нет почвы в главе</div>
                <div className="wr">{c.adult_block_reason}</div>
                {c.adult_bridge_hint && (
                  <div className="wh">Подсказка: {c.adult_bridge_hint}</div>
                )}
                <div className="row">
                  <button className="small" disabled={adapting === c.index}
                    onClick={async () => {
                      setAdapting(c.index);
                      try { await props.onAdaptAdult(c.index); } finally { setAdapting(null); }
                    }}>
                    {adapting === c.index ? "Адаптирую…" : "🔧 Адаптировать главу под адалт"}
                  </button>
                  <button className="small ghost" disabled={adapting === c.index}
                    onClick={() => props.onSkipAdult(c.index)}>
                    Оставить без адалта
                  </button>
                </div>
              </div>
            )}

            {/* журнал редактора с принять/отклонить/коммент */}
            {rs.length > 0 && (
              <div className="journal">
                {rs.map((r, k) => (
                  <div className={`round ${critCount(r) > 0 ? "crit" : "clean"}`} key={k}>
                    <div className="rh">
                      <span className="lbl">раунд {k + 1}</span>
                      <span className="counts">
                        <span className="c-crit">🔴 {critCount(r)}</span>
                        <span className="c-imp">🟡 {impCount(r)}</span>
                        <span className="c-min">🟢 {minCount(r)}</span>
                      </span>
                    </div>
                    {r.findings.map((f) => (
                      <FindingRow key={f.id} f={f} active={activeFinding === f.id}
                        onPick={() => pick(f.id)} onDecide={props.onDecide} />
                    ))}
                  </div>
                ))}
              </div>
            )}

            <label>План · Бот 04</label>
            <textarea rows={3} value={c.plan} onChange={(e) => upd(i, { plan: e.target.value })} />
            <ReviseBox label="Попросить ИИ изменить эту главу" onRevise={(fb) => props.onReviseChapter(i, fb)} />

            {c.dialogue != null && (<>
              <label>Диалоги · Бот 05</label>
              <Highlighted text={c.dialogue} findings={allFindings} active={activeFinding} onPick={pick} />
              <textarea rows={9} value={c.dialogue} onChange={(e) => upd(i, { dialogue: e.target.value })} />
            </>)}
            {c.adult_scene != null && (<>
              <label className="adult">Адалт-сцена · Бот 06</label>
              <Highlighted text={c.adult_scene} findings={allFindings} active={activeFinding} onPick={pick} />
              <textarea rows={10} value={c.adult_scene} onChange={(e) => upd(i, { adult_scene: e.target.value })} />
            </>)}
            {c.translation != null && (<>
              <label>Перевод · Бот 08</label>
              <textarea rows={7} value={c.translation} onChange={(e) => upd(i, { translation: e.target.value })} />
            </>)}
          </div>
        );
      })}
    </>
  );
}

function FindingRow({
  f, active, onPick, onDecide,
}: {
  f: Finding; active: boolean; onPick: () => void;
  onDecide: (fid: string, body: { status?: FindingStatus; comment?: string; judge?: boolean }) => Promise<void>;
}) {
  const [comment, setComment] = useState(f.user_comment || "");
  const [open, setOpen] = useState(false);
  return (
    <div className={`finding ${f.severity} st-${f.status} ${active ? "active" : ""}`}>
      <div className="meta">
        [{f.block}] → {f.responsible_node} · {f.locator}
        <span className={`fstatus ${f.status}`}>
          {f.status === "accepted" ? "принято" : f.status === "rejected" ? "отклонено" : "открыто"}
        </span>
      </div>
      <div className="fprob">{f.problem}</div>
      {f.quote && (
        <div className="fquote" onClick={onPick} title="Показать в тексте">“{f.quote}”</div>
      )}
      <div className="factions">
        <button className="xs ok" onClick={() => onDecide(f.id, { status: "accepted" })}>✓ Принять</button>
        <button className="xs no" onClick={() => onDecide(f.id, { status: "rejected" })}>✕ Отклонить</button>
        <button className="xs" onClick={() => onDecide(f.id, { comment, judge: true })} title="ИИ сам решит по комментарию">🤖 ИИ решит</button>
        <button className="xs ghost" onClick={() => setOpen((o) => !o)}>💬 Коммент</button>
      </div>
      {(open || f.user_comment) && (
        <div className="fcomment">
          <textarea rows={2} value={comment} placeholder="Комментарий к правке…"
            onChange={(e) => setComment(e.target.value)} />
          <button className="xs" onClick={() => onDecide(f.id, { comment })}>Сохранить коммент</button>
        </div>
      )}
    </div>
  );
}
