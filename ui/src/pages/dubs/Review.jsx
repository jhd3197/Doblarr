import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { api } from '../../lib/api.js';
import { createScenePlayer } from '../../lib/scene-player.js';
import { defaultOrder, FILTERS, matchesFilter, ORDERS, orderRows } from '../../lib/review.js';
import { LineEditor } from './LineEditor.jsx';
import { ScenePanel } from './ScenePanel.jsx';
import { collectScene, createSceneState, resetSceneState } from './scene-collect.js';
import './dubs.css';
import { formatTime } from '../../lib/media.js';

// Dialogue review for one job: the line list beside a focused editor, the
// scene panel (playback, takes, direction, timing, space, coverage, verdicts)
// and one "Render changes" for everything edited.
//
// Two ways to show it:
//   <ReviewDialog jobId onClose />  the modal opened from Dubs (#reviewDialog).
//   <Review ref jobId embedded onSelect onQueued />  inline, e.g. in the
//     studio's Dialogue panel; there is no Close button.
//
// Props:
//   jobId            the job to review; changing it opens that job's review
//   embedded         inline mode (no Close button, no "Close to follow progress")
//   onClose()        Close was pressed (dialog mode)
//   onSelect(row, data)        each time a line is selected
//   onQueued(result, cueIds)   after changes were queued; cueIds are the cues
//                              sent as edits ([] for a correction re-render)
// Handle (ref):
//   focusLine(index) -> boolean   select that line and scroll it into view
//   data             the loaded review (segments, revision, …) or null
//   jobId            the job on screen


function describeRerun(plan) {
  if (!plan) return 'Unchanged clips will be reused.';
  const parts = [];
  if (plan.generating) parts.push(`${plan.generating} line${plan.generating === 1 ? '' : 's'} will be generated`);
  if (plan.processing) parts.push(`${plan.processing} will be re-rendered from existing audio`);
  if (plan.candidates) parts.push(`${plan.candidates} alternative takes requested`);
  if (plan.coverage) {
    parts.push(`${plan.coverage} coverage decision${plan.coverage === 1 ? '' : 's'} will be re-mixed without generating speech`);
  }
  if (!parts.length) parts.push('nothing needs re-rendering');
  return `${parts.join(', ')}. ${plan.note || ''}`.trim();
}

// What the *exported file* was measured to be, kept in its own sentence and
// deliberately not merged with the review counts: a passing export says the
// container is structurally right, nothing about whether the dub sounds right.
function deliveryNote(report) {
  if (!report || !report.state) return '';
  const state = {
    passed: 'export checks passed',
    warned: 'export checks passed with warnings',
    failed: 'export checks FAILED',
    unavailable: 'no exported file to check',
    skipped: 'export checks were off',
  }[report.state] || report.state;
  const loudness = report.loudness?.lufs != null
    ? ` · ${report.loudness.lufs.toFixed(1)} LUFS`
      + (report.loudness.true_peak_db != null ? `, true peak ${report.loudness.true_peak_db.toFixed(1)} dBFS` : '')
      + (report.profile?.target_lufs == null ? ' (measured, no target set)' : '')
    : '';
  return ` · ${state}${loudness}. Technical checks are not a listening pass.`;
}

export const Review = forwardRef(function Review({ jobId, embedded = false, onClose, onSelect, onQueued }, ref) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState(null);
  const [editorKey, setEditorKey] = useState(0);
  const [voices, setVoices] = useState([]);
  const [filter, setFilter] = useState('all');
  const [order, setOrder] = useState('timeline');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const [, setTick] = useState(0);
  const [scene, setScene] = useState({ data: null, status: '', key: 0 });
  const [{ audio, player }] = useState(() => {
    const element = new Audio();
    element.preload = 'none';
    element.id = 'scenePlayer';
    element.controls = true;
    element.setAttribute('aria-label', 'Scene preview');
    return { audio: element, player: createScenePlayer(element) };
  });
  const [kind, setKind] = useState(player.kind);
  const pending = useRef(new Map());
  const sceneState = useRef(createSceneState()).current;
  const dataRef = useRef(null);
  const selectedRef = useRef(null);
  const epoch = useRef(0);
  const sceneEpoch = useRef(0);
  const editorRef = useRef(null);
  const listRef = useRef(null);
  const sceneBox = useRef(null);
  const sceneDataRef = useRef(null);
  sceneDataRef.current = scene.data;
  const callbacks = useRef({ onSelect, onQueued });
  callbacks.current = { onSelect, onQueued };
  const rerender = () => setTick(t => t + 1);

  useEffect(() => {
    const scenes = sceneEpoch;
    player.on((event, detail) => {
      if (event === 'loading') setScene(s => ({ ...s, status: 'Loading preview…' }));
      if (event === 'ready') setScene(s => ({ ...s, status: '' }));
      if (event === 'error') setScene(s => ({ ...s, status: detail.message }));
    });
    return () => { scenes.current++; player.reset(); player.stop(); };
  }, [player]);

  const loadScene = useCallback(async row => {
    const version = ++sceneEpoch.current;
    setScene(s => ({ data: null, status: 'Loading the surrounding exchange…', key: s.key + 1 }));
    let loaded;
    try {
      loaded = await api(`jobs/${jobId}/scene/${row.index}?context=${sceneState.context}`);
      loaded.context = sceneState.context;
      if (version !== sceneEpoch.current) return;
    } catch (err) {
      if (version === sceneEpoch.current) setScene(s => ({ data: null, status: err.message, key: s.key + 1 }));
      return;
    }
    // Previously saved renders of this dub, so the changed scene can be
    // compared with the one it replaced. A failure only means no comparison.
    if (!sceneState.versions.length) {
      try {
        const saved = await api(`jobs/${jobId}/versions`);
        if (version !== sceneEpoch.current) return;
        sceneState.versions = (saved.versions || []).filter(v => v.available && !v.current);
      } catch { sceneState.versions = []; }
    }
    if (sceneState.versions.length) player.setVersion(sceneState.versions[0].version_id);
    player.attach(jobId, row.index, loaded, row.cue?.level?.applied_db || 0);
    setScene(s => ({ data: loaded, status: '', key: s.key + 1 }));
    // Point the player at the dubbed scene straight away so the control is
    // usable on arrival; `play` only resumes what was already running.
    await player.play(player.kind, { keepPosition: false });
  }, [jobId, player, sceneState]);

  const select = useCallback(index => {
    const current = dataRef.current;
    player.stop();
    selectedRef.current = index;
    setSelected(index);
    setEditorKey(k => k + 1);
    const row = current?.segments.find(s => s.index === index);
    if (!row) return;
    loadScene(row);
    callbacks.current.onSelect?.(row, current);
  }, [loadScene, player]);

  // Open the job's review.
  useEffect(() => {
    const version = ++epoch.current;
    sceneEpoch.current++;
    dataRef.current = null;
    pending.current = new Map();
    resetSceneState(sceneState);
    player.stop();
    setData(null); setError(''); setStatus(''); setSelected(null); setVoices([]);
    (async () => {
      let result;
      try { result = await api(`jobs/${jobId}/review`); } catch (err) {
        if (version === epoch.current) setError(err.message);
        return;
      }
      if (version !== epoch.current) return;
      dataRef.current = result;
      setData(result);
      setFilter(result.flagged > 0 ? 'flagged' : 'all');
      setOrder(defaultOrder(result));
      select((result.segments.find(s => s.issues.length) || result.segments[0])?.index);
      // Voice discovery must not block listening or discard edits made while it loads.
      api('voices').then(found => { if (version === epoch.current) setVoices(found.voices || []); }).catch(() => {});
    })();
  }, [jobId, player, sceneState, select]);

  const currentRow = () => dataRef.current?.segments.find(s => s.index === selectedRef.current);

  // Collect the line on screen into `pending` whenever one of its fields changes.
  const collect = useCallback(() => {
    const row = currentRow();
    const editor = editorRef.current;
    if (!row || !editor) return;
    const field = id => editor.querySelector(id);
    if (!field('#reviewText')) return;
    const extra = collectScene({ container: sceneBox.current, scene: sceneDataRef.current, row, state: sceneState });
    const patch = { index: row.index, cue: row.cue?.cue_id || undefined, text: field('#reviewText').value,
      start: Number(field('#reviewStart').value), end: Number(field('#reviewEnd').value),
      voice: field('#reviewVoice').value, delivery: field('#reviewDelivery').value,
      regenerate: field('#reviewRegenerate').checked, exclude: field('#reviewExclude').checked, ...extra };
    const changed = patch.text !== (row.text_translated || row.text_src) || patch.start !== row.start
      || patch.end !== row.end || patch.voice !== (row.voice || '') || patch.delivery !== (row.delivery || '')
      || patch.regenerate || patch.exclude || Object.keys(extra).length > 0;
    if (changed) pending.current.set(row.index, patch); else pending.current.delete(row.index);
    setStatus('');
    rerender();
  }, [sceneState]);
  // Escape in the editor returns to the line list rather than closing the
  // review, so a keyboard user keeps their place. (A native listener, so the
  // correction dialog's Escape doesn't arrive here through React's portal.)
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) return undefined;
    const onKey = event => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      event.preventDefault();
      listRef.current?.querySelector(`[data-line="${selectedRef.current}"]`)?.focus();
    };
    editor.addEventListener('keydown', onKey);
    return () => editor.removeEventListener('keydown', onKey);
  }, []);

  useImperativeHandle(ref, () => ({
    focusLine(index) {
      if (!dataRef.current || !dataRef.current.segments.some(s => s.index === index)) return false;
      select(index);
      requestAnimationFrame(() => listRef.current?.querySelector(`[data-line="${index}"]`)?.scrollIntoView({ block: 'nearest' }));
      return true;
    },
    get data() { return dataRef.current; },
    get jobId() { return jobId; },
  }), [jobId, select]);

  const lock = () => {
    dataRef.current = { ...dataRef.current, editable: false };
    setData(dataRef.current);
  };

  async function submit() {
    const events = [...sceneState.pendingEvents.values()];
    if (!pending.current.size && !events.length) return;
    const edits = [...pending.current.values()];
    if (edits.some(e => !Number.isFinite(e.start) || !Number.isFinite(e.end) || e.start < 0
      || e.end <= e.start || !e.text.trim())) {
      setStatus('Each line needs dialogue and an end time after its start.'); return;
    }
    setBusy(true);
    setStatus('Queuing changes…');
    try {
      const useUpdated = document.getElementById('reviewUpdatedKnowledge')?.checked || false;
      const result = await api(`jobs/${jobId}/review`, { method: 'POST', json: {
        edits, events, use_updated_knowledge: useUpdated, base_revision: dataRef.current.revision } });
      const sent = edits.map(e => e.cue).filter(Boolean);
      pending.current.clear();
      lock();
      select(selectedRef.current);
      // Say plainly what was queued: which lines cost new speech, which only
      // re-render, and that everything else is reused.
      setStatus(`Queued. ${describeRerun(result.rerun)}` + (embedded ? '' : ' Close to follow progress.'));
      callbacks.current.onQueued?.(result, sent);
    } catch (err) { setStatus(err.message); }
    setBusy(false);
  }

  const onTab = useCallback(async next => { await player.play(next); setKind(next); }, [player]);
  const onPlay = useCallback((next, options) => player.play(next, options), [player]);
  const onContext = useCallback(next => {
    sceneState.context = next;
    const row = currentRow();
    if (row) loadScene(row);
  }, [loadScene, sceneState]);

  const rows = data ? orderRows(data.segments.filter(s => matchesFilter(s, filter)), order) : [];
  const events = sceneState.pendingEvents.size;
  const parts = [];
  if (pending.current.size) parts.push(`${pending.current.size} changed line${pending.current.size === 1 ? '' : 's'}`);
  if (events) parts.push(`${events} reaction${events === 1 ? '' : 's'}`);
  const row = data?.segments.find(s => s.index === selected);

  return (
    <>
      <div className="review-header">
        <div>
          <h2 id="reviewTitle">{data ? data.title : 'Review dialogue'}</h2>
          <p id="reviewSummary" className="hint">{data
            ? `${data.segments.length} lines · ${data.flagged} flagged for review${deliveryNote(data.delivery)}` : ''}</p>
        </div>
        {!embedded && <button type="button" id="reviewClose" className="btn btn-secondary" onClick={onClose}>Close</button>}
      </div>
      <div className="review-toolbar">
        <label><input type="checkbox" id="reviewFlagged" checked={filter !== 'all'}
          onChange={e => setFilter(e.target.checked ? 'flagged' : 'all')} /> Flagged lines only</label>
        <label className="review-filter">Show{' '}
          <select id="reviewFilter" className="input" value={filter} onChange={e => setFilter(e.target.value)}>
            {FILTERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
        <label className="review-filter">Order{' '}
          <select id="reviewOrder" className="input" value={order} onChange={e => setOrder(e.target.value)}>
            {ORDERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
        <span className="hint">Hear the exchange, compare the original with the dub, then render the changed lines.</span>
      </div>
      <div className="review-layout">
        <nav id="reviewLines" ref={listRef} className="review-lines" aria-label="Dialogue lines">
          {data && (rows.length ? rows.map(s => (
            <button key={s.index} type="button" className="review-line" data-line={s.index}
              aria-pressed={s.index === selected ? 'true' : 'false'}
              title={s.review_priority ? s.review_priority.reasons.join(', ') : undefined}
              onClick={() => select(s.index)}>
              <span className="m">{formatTime(s.source_start ?? s.start)}</span>
              <span><strong>{s.speaker}</strong>{' '}
                <span className="review-excerpt">{s.text_translated || s.text_src}</span></span>
              <span className="review-marker">{pending.current.has(s.index) ? 'Edited' : s.issues.length ? 'Review' : ''}</span>
            </button>
          )) : <p className="hint">No lines match this filter.</p>)}
        </nav>
        <section id="reviewEditor" ref={editorRef} className="review-editor" aria-label="Line editor" onInput={collect}>
          {error ? error
            : !data ? <p>Loading dialogue…</p>
            : !row ? <p>Select a line to review.</p>
            : <LineEditor key={`${row.index}:${editorKey}`} row={row} data={data} jobId={jobId}
                edit={pending.current.get(row.index) || {}} voices={voices} editorRef={editorRef}
                onQueued={() => callbacks.current.onQueued?.(undefined, [])} onLocked={lock}
                scene={<ScenePanel key={`${row.index}:${scene.key}`} row={row} data={data} jobId={jobId}
                  scene={scene.data} status={scene.status} kind={kind} audio={audio} player={player}
                  state={sceneState} onPlay={onPlay} onTab={onTab} onContext={onContext}
                  onDecision={rerender} containerRef={sceneBox} />} />}
        </section>
      </div>
      <div className="review-footer">
        <p id="reviewStatus" role="status">{status}</p>
        <label className="hint review-knowledge"><input type="checkbox" id="reviewUpdatedKnowledge" /> Use updated language knowledge</label>
        <button type="button" id="reviewSubmit" className="btn btn-primary" onClick={submit}
          disabled={busy || (!pending.current.size && !events) || !data?.editable}>
          {parts.length ? `Render ${parts.join(' and ')}` : 'Render changes'}</button>
      </div>
    </>
  );
});

// The review as a modal, opened from Dubs. Focus returns to the button that
// opened it (or that job's Review button) when it closes.
export function ReviewDialog({ jobId, onClose, onQueued }) {
  const dialog = useRef(null);
  const opener = useRef(null);
  useEffect(() => {
    opener.current = document.activeElement;
    dialog.current.showModal();
  }, []);
  function closed() {
    const target = opener.current?.isConnected ? opener.current
      : [...document.querySelectorAll('.job-review')].find(button => button.dataset.id === jobId);
    target?.focus();
    onClose();
  }
  return (
    <dialog id="reviewDialog" ref={dialog} className="review-dialog" aria-labelledby="reviewTitle" onClose={closed}>
      <Review jobId={jobId} onClose={() => dialog.current.close()} onQueued={onQueued} />
    </dialog>
  );
}
