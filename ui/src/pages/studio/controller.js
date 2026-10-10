import { api, withKey } from '../../lib/api.js';
import { safeGet } from '../../lib/storage.js';
import { queryClient } from '../../lib/queries.js';
import { createStudioPlayer } from './player.js';
import { studioQuery } from './queries.js';
import { formatTime } from '../../lib/media.js';

// The episode studio: one persistent workspace over one episode.
//
// Where you are (view, line, window, playback position) is saved to the
// server as you move, so a reload or another browser lands in the same place.
// Dialogue editing is the review editor embedded here, not a copy of it;
// notes are server records tied to a cue, a source and a time.
//
// This controller owns the mutable studio state and the player; the React
// components in Studio.jsx render `ctrl.state` and re-render on `onChange`.

export const VIEWS = [
  ['overview', 'Overview'], ['cast', 'Cast'], ['dialogue', 'Dialogue'],
  ['compare', 'Compare'], ['export', 'Export'],
];
export const isView = v => VIEWS.some(([id]) => id === v);
export const viewLabel = v => (VIEWS.find(([id]) => id === v) || VIEWS[0])[1];
export const stamp = s => `${formatTime(s)}.${Math.floor((s % 1) * 10)}`;
export const TRANSPORT_VIEWS = ['dialogue', 'compare'];

function hash(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

// Keyboard names for the source rail: 0 the original, R a reference, 1… the rest.
function assignKeys(list) {
  let n = 0;
  list.forEach(s => { s.key = s.kind === 'original' ? '0' : s.kind === 'reference' ? 'R' : String(++n); });
  return list;
}

export function createStudioController({ sid, overview, view, navigate, onChange }) {
  const audio = new Audio();
  audio.preload = 'none';
  const video = document.createElement('video');
  video.muted = true; video.playsInline = true; video.className = 'studio-video';
  video.setAttribute('aria-label', 'Scene picture (follows the audio)');

  const state = {
    overview, session: { ...overview.session }, view, notes: [],
    windowSpan: null, sources: [], lines: [], status: '', mapping: '', fresh: null,
    // The source that finished loading: the one the rail shows as playing.
    ready: '',
    blind: !!overview.session.compare?.blind && !overview.session.compare?.revealed,
    generation: 0,
  };
  let saveTimer = null;
  let closed = false;
  let entered = '';
  const changed = () => { if (!closed) onChange(); };
  // Playback time changes several times a second; only what shows it
  // subscribes, so the rest of the studio does not re-render with it.
  const timeListeners = new Set();
  const subscribeTime = fn => { timeListeners.add(fn); return () => timeListeners.delete(fn); };
  const player = createStudioPlayer({ audio, video, onChange: onPlayer });
  // Set by the Dialogue view while the review editor is mounted.
  let review = null;

  const jobId = () => state.overview?.active_job?.id || '';
  const status = text => { state.status = text; changed(); };

  // ---------------------------------------------------------------- session

  async function reload() {
    const fresh = await api(`studio/sessions/${sid}`);
    queryClient.setQueryData(studioQuery(sid).queryKey, fresh);
    state.overview = fresh;
    state.session = { ...fresh.session };
    changed();
    return fresh;
  }

  // Reload and draw the current view again from the fresh record.
  async function refresh() {
    await reload();
    state.generation += 1;
    changed();
  }

  // Save where the person is, a moment after they stop moving. A conflict
  // means another tab moved on: take its revision and keep ours on top.
  function remember(patch) {
    Object.assign(state.session, patch);
    changed();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try {
        const saved = await api(`studio/sessions/${sid}`, { method: 'PATCH',
          json: { base_revision: state.session.revision, ...patch } });
        state.session = { ...state.session, ...saved.session };
        status('');
      } catch (error) {
        if (error.status === 409 && error.data?.current) {
          state.session.revision = error.data.current.revision;
          remember(patch);
        } else status(`Could not save your place: ${error.message}`);
      }
    }, 400);
  }

  async function savePatch(patch) {
    try {
      const saved = await api(`studio/sessions/${sid}`, { method: 'PATCH',
        json: { base_revision: state.session.revision, ...patch } });
      state.session = saved.session;
      status('Saved.');
      return true;
    } catch (error) {
      if (error.status === 409 && error.data?.current) {
        state.session.revision = error.data.current.revision;
        status('Someone else changed this studio; your change was applied on top.');
        return savePatch(patch);
      }
      status(error.message);
      return false;
    }
  }

  function show(next) {
    navigate(`/studio/${encodeURIComponent(sid)}/${next}`);
  }

  // The URL named a view: follow it (called by the page on every view change).
  function enterView(next) {
    if (entered === next) return;
    entered = next;
    state.view = next;
    if (state.session.view !== next) remember({ view: next });
    if (!TRANSPORT_VIEWS.includes(next)) player.stop();
    changed();
  }

  // --------------------------------------------------------------- dialogue

  function attachReview(handle) { review = handle; }

  // The editor told us which line is open: follow it with the transport.
  async function onLine(row) {
    if (state.view !== 'dialogue' || row?.index == null) return;
    if (state.session.line !== row.index) remember({ line: row.index, cue: row.cue?.cue_id || null });
    try {
      const scene = await api(`jobs/${jobId()}/scene/${row.index}?context=2`);
      // Another line of the same exchange: keep playing, only move the playhead.
      const span = state.windowSpan;
      if (span && scene.target.start === span.start && scene.target.end === span.end) {
        player.seek(row.start);
        return;
      }
      await playWindow({ start: scene.target.start, end: scene.target.end },
        (review?.data?.segments || []).filter(s => scene.cues.includes(s.index)));
    } catch (error) { status(error.message); }
  }

  // ---------------------------------------------------------------- compare

  async function loadCompareWindow(startValue, lengthValue) {
    const start = Math.max(0, Number(startValue) || 0);
    const length = Math.max(2, Math.min(240, Number(lengthValue) || 30));
    remember({ compare: { ...(state.session.compare || {}), start, length }, position: start });
    const inWindow = (review?.data?.segments || []).filter(s => s.start < start + length && s.end > start);
    await playWindow({ start, end: start + length }, inWindow);
  }

  function setBlind(on) {
    state.blind = on;
    remember({ compare: { ...(state.session.compare || {}), blind: on } });
  }

  function reveal() {
    state.blind = false;
    remember({ compare: { ...(state.session.compare || {}), blind: false, revealed: true,
      revealed_at: new Date().toISOString() } });
  }

  // Build the source rail for a target window and load the first playable one.
  async function playWindow(span, rows) {
    const o = state.overview;
    state.windowSpan = span;
    state.lines = rows || [];
    const list = [];
    const meaning = (o.references || []).find(r => r.roles.includes('meaning') && r.track);
    list.push(meaning
      ? { id: 'original', kind: 'original', label: 'Original', sub: `${meaning.language} · ${meaning.label}` }
      : { id: 'original', kind: 'original', label: jobId() ? 'Run source' : 'First track',
        sub: 'not confirmed as the original' });
    if (jobId()) list.push({ id: 'dub', kind: 'dub', label: 'This dub', sub: 'as rendered' });
    (o.output_tracks || []).forEach(t => list.push({ id: `output:${t.audio_index}`, kind: 'dub',
      label: `${t.title && t.title !== 'AAC' ? t.title : `Track ${t.audio_index}`} (${t.language || '?'})`,
      sub: 'delivered file' }));
    (o.references || []).filter(r => r.track && !r.evaluation_only && !r.roles.includes('meaning'))
      .forEach(r => list.push({ id: `reference:${r.id}`, kind: 'reference', label: r.label,
        sub: `${r.language} · ${r.roles.join(', ') || 'no role'}` }));
    if (jobId()) {
      try {
        const saved = await api(`jobs/${jobId()}/versions`);
        (saved.versions || []).filter(v => v.available && !v.current).slice(0, 3).forEach(v =>
          list.push({ id: `version:${v.version_id}`, kind: 'version', label: `Saved ${v.name}`,
            sub: (v.created_at || '').slice(0, 16).replace('T', ' ') }));
      } catch { /* versions are optional */ }
    }
    state.sources = assignKeys(list);
    player.attach({ session: sid, job: jobId(), span, list,
      showVideo: safeGet('doblarr.studio.video', '1') === '1' });
    changed();
    await Promise.all(list.map(async s => {
      const q = new URLSearchParams({ source: s.id, start: span.start, end: span.end });
      if (jobId()) q.set('job_id', jobId());
      try {
        const w = await api(`studio/sessions/${sid}/media/window?${q}`);
        Object.assign(s, { peaks: w.peaks, level_db: w.level_db, mapping: w, available: true });
      } catch (error) { Object.assign(s, { available: false, reason: error.message }); }
    }));
    if (closed) return;
    changed();
    const first = list.find(s => s.available) || null;
    if (first) await player.load(first, { keep: false, play: false });
    else status('Nothing in this window can be played.');
  }

  function blindLabel(source) {
    if (!state.blind || !['dub', 'version'].includes(source.kind)) return source.label;
    const versions = state.sources.filter(s => ['dub', 'version'].includes(s.kind))
      .map(s => s.id).sort((a, b) => hash(`${sid}:${a}`) - hash(`${sid}:${b}`));
    return `Version ${versions.indexOf(source.id) + 1}`;
  }

  function onPlayer(what, detail) {
    if (what === 'time') {
      timeListeners.forEach(fn => fn());
      return;
    }
    if (what === 'ready') {
      state.ready = detail.source.id;
      const m = detail.source.mapping;
      state.mapping = m?.note
        ? `${blindLabel(detail.source)}: ${m.note}${m.mapped_start != null ? ` (its ${formatTime(m.mapped_start)})` : ''}`
        : '';
      state.status = '';
    }
    if (what === 'error') state.status = detail.message;
    if (what === 'pause' && state.windowSpan) remember({ position: Number(player.time.toFixed(2)) });
    changed();
  }

  function currentLine() {
    const t = player.time;
    return state.lines.find(l => l.start <= t && t < l.end) || null;
  }

  function toggleLoop() {
    if (player.loop) { player.setLoop(null); return; }
    const line = currentLine() || state.lines[0];
    if (line) player.setLoop({ start: line.start, end: line.end });
  }

  function sequenceLine() {
    const line = currentLine() || state.lines[0];
    const order = state.sources.filter(s => s.available && s.kind !== 'reference');
    if (line && order.length) player.playSequence({ start: line.start, end: line.end }, order);
  }

  function seekFraction(fraction) {
    const span = state.windowSpan;
    if (span) player.seek(span.start + fraction * (span.end - span.start));
  }

  // ------------------------------------------------------------------ notes

  async function loadNotes() {
    try {
      const data = await api(`studio/sessions/${sid}/notes${jobId() ? `?job_id=${jobId()}` : ''}`);
      state.notes = data.notes || [];
    } catch { state.notes = []; }
    changed();
  }

  async function mark(category) {
    if (!state.windowSpan) return;
    const line = currentLine();
    const source = player.source;
    try {
      const saved = await api(`studio/sessions/${sid}/notes`, { method: 'POST', json: {
        job_id: jobId(), cue: line?.cue?.cue_id || '', line: line?.index ?? null,
        source: source ? `${source.id}${state.blind ? ` (${blindLabel(source)})` : ''}` : '',
        at: Number(player.time.toFixed(2)), category, severity: 'noticeable',
        snapshot_revision: review?.data?.revision || '' } });
      state.notes = [...state.notes, saved.note];
      state.fresh = saved.note.id;
      status(`Marked ${stamp(saved.note.at)}${line ? ` on line ${line.index + 1}` : ''}.`);
    } catch (error) { status(error.message); }
  }

  async function patchNote(note, changes) {
    try {
      const saved = await api(`studio/notes/${note.id}`, { method: 'PATCH',
        json: { base_revision: note.revision, ...changes } });
      state.notes = state.notes.map(n => (n.id === note.id ? { ...n, ...saved.note } : n));
      changed();
    } catch (error) { status(error.message); }
  }

  function hearNote(note) {
    const id = (note.source || '').split(' ')[0];
    if (state.sources.some(s => s.id === id)) player.select(id);
    player.seek(Math.max(state.windowSpan?.start ?? 0, note.at - 1));
  }

  async function hearOriginal(note) {
    await player.select('original');
    player.seek(Math.max(0, note.at - 1));
  }

  function setNoteFilter(value) {
    remember({ filters: { ...(state.session.filters || {}), notes: value } });
  }

  // A scene verdict is a note of its own kind, pinned to the window start.
  function verdictNote() {
    const span = state.windowSpan;
    return span ? state.notes.find(n => n.category === 'verdict' && Math.abs(n.at - span.start) < 0.01) : null;
  }

  async function setVerdict(choice) {
    const span = state.windowSpan;
    if (!span) return;
    const verdict = verdictNote();
    try {
      if (verdict) {
        const saved = await api(`studio/notes/${verdict.id}`, { method: 'PATCH',
          json: { base_revision: verdict.revision, verdict: choice } });
        state.notes = state.notes.map(n => (n.id === verdict.id ? { ...n, ...saved.note } : n));
      } else {
        const saved = await api(`studio/sessions/${sid}/notes`, { method: 'POST', json: {
          job_id: jobId(), at: span.start, category: 'verdict',
          note: `${formatTime(span.start)}–${formatTime(span.end)}${state.blind ? ' (blind)' : ''}`,
          snapshot_revision: review?.data?.revision || '' } });
        const again = await api(`studio/notes/${saved.note.id}`, { method: 'PATCH',
          json: { base_revision: saved.note.revision, verdict: choice } });
        state.notes = [...state.notes, again.note];
      }
      changed();
    } catch (error) { status(error.message); }
  }

  function downloadResults() {
    const notes = state.notes;
    const out = [`# Studio listening results — ${state.session.title}`, '',
      `Run: ${jobId() || '(none)'} · exported ${new Date().toISOString().slice(0, 16)}`, '',
      '## Verdicts', ''];
    notes.filter(n => n.category === 'verdict').forEach(n =>
      out.push(`- ${n.note}: **${n.verdict || '(not answered)'}**`));
    out.push('', '## Marked moments', '', '| At | Source | What | How bad | Line | Note | State |',
      '| --- | --- | --- | --- | --- | --- | --- |');
    notes.filter(n => n.category !== 'verdict').sort((a, b) => a.at - b.at).forEach(n =>
      out.push(`| ${stamp(n.at)} | ${n.source} | ${n.category || '—'} | ${n.severity} | `
        + `${n.line != null ? n.line + 1 : '—'} | ${(n.note || '').replaceAll('|', '/')} | ${n.resolution || 'open'}`
        + `${n.stale ? ' (older audio)' : ''} |`));
    out.push('', '## What this cannot settle', '',
      '- A bounded window says nothing about an unreviewed episode.',
      '- Technical checks rank defects; they do not judge acting.', '');
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([out.join('\n')], { type: 'text/markdown' }));
    link.download = `studio-results-${sid}.md`;
    document.body.append(link); link.click(); link.remove();
  }

  // --------------------------------------------------------- legacy imports

  async function playLegacy({ importId, scene, kind, name, label }) {
    const q = new URLSearchParams({ scene, kind, name });
    const url = withKey(`/api/studio/imports/${importId}/media?${q}`);
    state.windowSpan = { start: 0, end: 240 };
    state.lines = [];
    const source = { id: `legacy:${kind}:${name}`, url, label, kind: 'legacy', available: true };
    state.sources = assignKeys([source]);
    player.attach({ session: sid, job: '', span: state.windowSpan, list: state.sources, showVideo: false });
    changed();
    await player.load(source, { keep: false, play: true });
  }

  // --------------------------------------------------------------- keyboard

  function keydown(e) {
    if (closed || !TRANSPORT_VIEWS.includes(state.view)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.matches?.('input, textarea, select, [contenteditable]')) return;
    if (document.querySelector('dialog[open]')) return;
    const k = e.key.toLowerCase();
    const pick = name => state.sources.find(s => s.key === name && s.available !== false);
    if (k === ' ') { e.preventDefault(); player.toggle(); return; }
    if (k === 'n') { e.preventDefault(); mark(''); return; }
    if (k === 'm') { e.preventDefault(); player.setMatched(!player.matched); return; }
    if (k === 'l') { e.preventDefault(); toggleLoop(); return; }
    if (k === 'p') { e.preventDefault(); sequenceLine(); return; }
    if (k === 'j' || k === 'k') {
      e.preventDefault();
      const ordered = [...state.lines].sort((a, b) => a.start - b.start);
      const here = ordered.findIndex(l => l.start <= player.time && player.time < l.end);
      const next = ordered[Math.max(0, Math.min(ordered.length - 1, (here < 0 ? 0 : here) + (k === 'k' ? 1 : -1)))];
      if (next) player.seek(next.start);
      return;
    }
    const target = k === 'o' ? pick('0') : k === 'r' ? pick('R') : pick(k);
    if (target) { e.preventDefault(); player.load(target); }
  }

  // A repair was queued from the editor: every open note on those lines now
  // points at the run that should fix it, so Compare can close the loop.
  async function repairQueued(result, cues) {
    if (closed || !result?.job?.id) return;
    for (const note of state.notes.filter(n => cues.includes(n.cue) && (n.resolution || 'open') === 'open')) {
      try {
        const saved = await api(`studio/notes/${note.id}`, { method: 'PATCH', json: {
          base_revision: note.revision, resolution: 'repair_queued',
          linked_edit: { job: result.job.id, cue: note.cue } } });
        state.notes = state.notes.map(n => (n.id === note.id ? { ...n, ...saved.note } : n));
      } catch { /* the note moved on elsewhere; it is still listed */ }
    }
    status(`Repair queued as run ${result.job.id}. Compare it with this one when it finishes, then resolve the note.`);
  }

  // Entering the studio. Reachable for a headless playback check, as on the
  // listening page.
  function start() {
    closed = false;
    window.__studioAudio = audio;
  }

  // Leaving the studio stops its player and hands the editor back.
  function close() {
    closed = true;
    player.stop();
    clearTimeout(saveTimer);
    review = null;
    if (window.__studioAudio === audio) delete window.__studioAudio;
  }

  return {
    sid, state, player, video, jobId, reload, refresh, remember, savePatch, show, enterView, status,
    attachReview, get review() { return review; }, onLine, loadCompareWindow, setBlind, reveal, playWindow,
    blindLabel, toggleLoop, sequenceLine, seekFraction, currentLine, loadNotes, mark, patchNote, subscribeTime,
    hearNote, hearOriginal, setNoteFilter, verdictNote, setVerdict, downloadResults, playLegacy,
    keydown, repairQueued, start, close,
  };
}
