import { useEffect, useReducer, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { createStudioController, stamp, TRANSPORT_VIEWS, VIEWS } from './controller.js';
import { studioQuery } from './queries.js';
import { StudioOverview } from './StudioOverview.jsx';
import { StudioCast } from './StudioCast.jsx';
import { StudioExport } from './StudioExport.jsx';
import { StudioExperiments } from './StudioExperiments.jsx';
import { DialogueView } from './DialogueView.jsx';
import { NotesPanel } from './NotesPanel.jsx';
import './studio-page.css';
import { formatTime } from '../../lib/media.js';

const STYLES = [['automatic', 'Automatic'], ['guided', 'Guided'], ['manual', 'Manual']];
const NEXT = { audition: 'audition the cast', render: 'render a draft', wait: 'wait for the render',
  review: 'review the script', rerender: 're-render stale lines',
  confirm_export: 'confirm the export', done: 'nothing required' };

// One controller per open studio session: built on entry, started and
// closed (player stopped, timers cleared) with the page.
function useController(sid, view) {
  const navigate = useNavigate();
  const [, bump] = useReducer(n => n + 1, 0);
  const { data: overview } = useQuery(studioQuery(sid));
  const [ctrl] = useState(() => createStudioController({ sid, overview, view, navigate, onChange: bump }));
  useEffect(() => {
    ctrl.start();
    return () => ctrl.close();
  }, [ctrl]);
  return ctrl;
}

function StudioHead({ ctrl, view }) {
  const navigate = useNavigate();
  const { session, overview } = ctrl.state;
  const next = overview?.next || {};
  const job = overview?.active_job;
  async function setStyle(style) {
    await ctrl.savePatch({ style });
    await ctrl.reload();
  }
  return (
    <div className="studio-head" id="studioHead">
      <div className="studio-title">
        <button type="button" className="btn btn-ghost" id="stBack" onClick={() => navigate('/dubs')}>← Dubs</button>
        <div className="studio-title-text">
          <h2>{session.title}</h2>
          <p className="hint">{session.media_name}{job ? ` · run ${job.id} (${job.status})` : ' · no draft yet'}</p>
        </div>
        <div className="studio-style" role="radiogroup" aria-label="Working style">
          {STYLES.map(([v, label]) => (
            <button key={v} type="button" className="opt" role="radio" aria-checked={session.style === v}
              aria-pressed={session.style === v} data-style={v} onClick={() => setStyle(v)}>{label}</button>
          ))}
        </div>
      </div>
      <p className="studio-next">
        {next.reason ? `Next: ${NEXT[next.action] || next.action} — ${next.reason}` : ''}
        {next.view && next.view !== view && (
          <button type="button" className="btn btn-ghost" data-go={next.view} onClick={() => ctrl.show(next.view)}>Go there</button>
        )}
      </p>
    </div>
  );
}

function Wave({ ctrl }) {
  const { windowSpan: span, lines, notes } = ctrl.state;
  const player = ctrl.player;
  if (!span) return <div className="studio-wave" id="stWave" role="slider" tabIndex={0} aria-label="Position in this window" aria-valuemin="0" aria-valuemax="100" />;
  const width = span.end - span.start;
  const t = player.time;
  const pct = v => `${v * 100}%`;
  const peaks = player.source?.peaks || [];
  function onClick(e) {
    const box = e.currentTarget.getBoundingClientRect();
    ctrl.seekFraction((e.clientX - box.left) / box.width);
  }
  function onKeyDown(e) {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      player.seek(player.time + (e.key === 'ArrowRight' ? 2 : -2));
    }
  }
  return (
    <div className="studio-wave" id="stWave" role="slider" tabIndex={0} aria-label="Position in this window"
      aria-valuemin="0" aria-valuemax="100" aria-valuenow={Math.round(((t - span.start) / width) * 100)}
      onClick={onClick} onKeyDown={onKeyDown}>
      <div className="studio-bars">{peaks.map((p, i) => <i key={i} style={{ height: `${Math.max(2, p * 100)}%` }} />)}</div>
      {lines.map(l => {
        const from = Math.max(l.start, span.start);
        return (
          <button key={`l${l.index}`} type="button" className="studio-lane" data-line={l.index}
            data-active={String(l.start <= t && t < l.end)}
            style={{ left: pct((from - span.start) / width), width: pct((Math.min(l.end, span.end) - from) / width) }}
            title={`${l.speaker}: ${l.text_translated || l.text_src}`} aria-label={`Line ${l.index + 1}`}
            onClick={e => { e.stopPropagation(); player.seek(l.start); }} />
        );
      })}
      {notes.filter(n => n.at >= span.start && n.at < span.end).map(n => (
        <button key={`n${n.id}`} type="button" className="studio-pin" data-sev={n.severity} data-at={n.at}
          style={{ left: pct((n.at - span.start) / width) }} title={`${stamp(n.at)} ${n.category} ${n.note}`}
          aria-label={`Note at ${stamp(n.at)}`} onClick={e => { e.stopPropagation(); player.seek(n.at); }}>!</button>
      ))}
      <div className="studio-head-line" id="stHead"
        style={{ left: `${Math.max(0, Math.min(100, ((t - span.start) / width) * 100))}%` }} />
    </div>
  );
}

function Sources({ ctrl }) {
  const { sources, blind, ready: current } = ctrl.state;
  return (
    <div className="studio-row studio-sources" id="stSources" role="radiogroup" aria-label="What to play">
      {sources.map(s => {
        const mapState = s.mapping?.state && s.mapping.state !== 'exact' ? s.mapping.state : '';
        const hidden = blind && ['dub', 'version'].includes(s.kind);
        return (
          <button key={s.id} type="button" className="studio-src" role="radio" data-kind={s.kind} data-id={s.id}
            aria-checked={current === s.id} disabled={s.available === false} title={s.reason || s.mapping?.note || ''}
            onClick={() => ctrl.player.select(s.id)}>
            <span className="studio-src-top"><strong>{ctrl.blindLabel(s)}</strong><span className="kbd">{s.key}</span></span>
            <span className="studio-src-sub">{hidden ? 'blind' : s.sub}
              {mapState && <> · <em>{mapState}</em></>}{s.available === false ? ' · unavailable' : ''}</span>
          </button>
        );
      })}
    </div>
  );
}

function Transport({ ctrl, shown }) {
  const { player, video } = ctrl;
  const span = ctrl.state.windowSpan;
  return (
    <div className="studio-transport" id="studioTransport" hidden={!shown}>
      <div className="studio-row">
        <button type="button" className="studio-play" id="stPlay" aria-label="Play or pause (space)" onClick={() => player.toggle()}>
          <span id="stGlyph" aria-hidden="true">{player.paused ? '▶' : '❚❚'}</span></button>
        <div className="studio-clock">
          <div className="m" id="stNow">{span ? stamp(player.time) : '0:00.0'}</div>
          <div className="m hint" id="stTotal">{span ? `${formatTime(span.start)}–${formatTime(span.end)}` : ''}</div>
        </div>
        <Wave ctrl={ctrl} />
        <div className="studio-picture" id="stPicture" ref={el => { if (el && !el.contains(video)) el.append(video); }} />
      </div>
      <Sources ctrl={ctrl} />
      <div className="studio-row studio-tools">
        <button type="button" className="opt" id="stLoop" aria-pressed={String(!!player.loop)} onClick={ctrl.toggleLoop}>Loop line <span className="kbd">L</span></button>
        <button type="button" className="opt" id="stSeq" aria-pressed={String(!!player.sequence)} onClick={ctrl.sequenceLine}>Play line in each version <span className="kbd">P</span></button>
        <button type="button" className="opt" id="stMatch" aria-pressed={String(player.matched)}
          title="Plays each source at the same speech level. The render is not changed."
          onClick={() => player.setMatched(!player.matched)}>Level-matched preview <span className="kbd">M</span></button>
        <button type="button" className="btn btn-primary" id="stMark" onClick={() => ctrl.mark('')}>Mark this moment <span className="kbd">N</span></button>
        <span className="hint" id="stMapping" role="status">{ctrl.state.mapping}</span>
      </div>
    </div>
  );
}

function VerdictPanel({ ctrl }) {
  const { windowSpan, sources, blind } = ctrl.state;
  if (!windowSpan) return <div className="panel studio-verdict" id="cmpVerdict" />;
  const verdict = ctrl.verdictNote();
  const choices = sources.filter(s => ['dub', 'version'].includes(s.kind) && s.available !== false).map(s => ctrl.blindLabel(s));
  const options = [...choices, 'No difference', 'All bad', 'Not sure'];
  return (
    <div className="panel studio-verdict" id="cmpVerdict">
      <h3>Which sounded best in this window?</h3>
      <div className="studio-style">
        {options.map(o => (
          <button key={o} type="button" className="opt" aria-pressed={verdict?.verdict === o} data-verdict={o}
            onClick={() => ctrl.setVerdict(o)}>{o}</button>
        ))}
      </div>
      <p className="hint">Saved with the window, the run and the labels shown{blind ? ' (blind)' : ''}. A verdict
        is yours; technical checks never fill it in.</p>
      <button type="button" className="btn btn-secondary" id="cmpResults" onClick={ctrl.downloadResults}>Download filled results</button>
    </div>
  );
}

function LegacyImport({ ctrl, summary, playing, onPlay }) {
  const { data, error } = useQuery({ queryKey: ['studio', 'import', summary.id],
    queryFn: () => api(`studio/imports/${summary.id}`), gcTime: 5_000 });
  if (error) return <p className="hint">{error.message}</p>;
  if (!data) return null;
  const found = data.import;
  const play = (scene, kind, name, label) => () => {
    onPlay(`${summary.id}:${scene}:${kind}:${name}`);
    ctrl.playLegacy({ importId: summary.id, scene, kind, name, label });
  };
  const pressed = id => playing === `${summary.id}:${id}`;
  return (
    <details className="studio-legacy-item">
      <summary>{found.legacy_id} · {found.scenes.length} scene(s)
        {found.judgments ? ` · ${found.judgments} imported judgment file(s)` : ''}
        {found.missing.length ? <> · <span className="review-marker">{found.missing.length} missing file(s)</span></> : null}</summary>
      {found.scenes.map(s => (
        <div key={s.index} className="studio-legacy-scene"><strong>{s.title}</strong>
          <div className="studio-style">
            {s.source.exists && <button type="button" className="opt" aria-pressed={pressed(`${s.index}:source:`)}
              onClick={play(s.index, 'source', '', 'Original')}>Original</button>}
            {s.references.filter(r => r.exists).map(r => (
              <button key={r.language} type="button" className="opt" aria-pressed={pressed(`${s.index}:reference:${r.language}`)}
                onClick={play(s.index, 'reference', r.language, `${r.role} (${r.language})`)}>{r.role} ({r.language})</button>
            ))}
            {s.variants.map(v => (v.mixed.exists
              ? <button key={v.name} type="button" className="opt" aria-pressed={pressed(`${s.index}:mixed:${v.name}`)}
                onClick={play(s.index, 'mixed', v.name, v.name)}>{v.name}</button>
              : <span key={v.name} className="hint">{v.name}: missing</span>))}
          </div>
        </div>
      ))}
      {(found.judgments_imported || []).map(j => (
        <p key={j.file} className="hint">Imported from {j.file}: {j.answered} verdict(s), {j.marks} marked moment(s). {j.provenance}.</p>
      ))}
      <p className="hint">{found.settings_note || ''}</p>
    </details>
  );
}

function LegacyImports({ ctrl }) {
  const [playing, setPlaying] = useState('');
  const imports = (ctrl.state.overview.imports || []).filter(i => i.kind === 'comparison');
  if (!imports.length) return null;
  return (
    <div className="panel studio-legacy">
      <h3>Imported comparisons</h3>
      <p className="hint">Earlier listening tests, played from their own files. Nothing was re-rendered.</p>
      <div id="legacyList">
        {imports.map(i => <LegacyImport key={i.id} ctrl={ctrl} summary={i} playing={playing} onPlay={setPlaying} />)}
      </div>
    </div>
  );
}

function CompareView({ ctrl }) {
  const { session, windowSpan, blind } = ctrl.state;
  const saved = session.compare || {};
  const [start, setStart] = useState(() => String(saved.start ?? (windowSpan?.start ?? session.position ?? 0)));
  const [length, setLength] = useState(() => String(saved.length ?? 30));
  const hasJob = !!ctrl.jobId();
  useEffect(() => {
    let live = true;
    ctrl.loadNotes().then(() => { if (live) ctrl.loadCompareWindow(start, length); });
    return () => { live = false; };
    // Load the saved window once on entry; later loads come from the button.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ctrl]);
  return (
    <div className="studio-compare">
      <div className="panel studio-window">
        <div className="studio-window-row">
          <label className="review-field">Window start (s)
            <input className="input m" type="number" min="0" step="0.5" id="cmpStart" value={start}
              onChange={e => setStart(e.target.value)} /></label>
          <label className="review-field">Length (s)
            <input className="input m" type="number" min="2" max="240" step="1" id="cmpLen" value={length}
              onChange={e => setLength(e.target.value)} /></label>
          <button type="button" className="btn btn-secondary" id="cmpLoad" onClick={() => ctrl.loadCompareWindow(start, length)}>Load window</button>
          <label className="studio-check"><input type="checkbox" id="cmpBlind" checked={blind} disabled={!!saved.revealed}
            onChange={e => ctrl.setBlind(e.target.checked)} /> Blind labels for dub versions</label>
          {saved.revealed
            ? <span className="tag tag-neutral">Labels revealed {(saved.revealed_at || '').slice(0, 16)}</span>
            : <button type="button" className="btn btn-ghost" id="cmpReveal" onClick={ctrl.reveal}>Reveal labels</button>}
        </div>
        <p className="hint">{hasJob ? 'Scene windows follow the selected line in Dialogue. ' : ''}
          References play at the same moment through their alignment; an unaligned reference
          plays at the same timestamps and says so.</p>
      </div>
      <VerdictPanel ctrl={ctrl} />
      <NotesPanel ctrl={ctrl} />
      <LegacyImports ctrl={ctrl} />
      <StudioExperiments ctrl={ctrl} />
    </div>
  );
}

function ViewBody({ ctrl, view }) {
  const key = `${view}:${ctrl.state.generation}`;
  if (view === 'overview') return <StudioOverview key={key} ctrl={ctrl} />;
  if (view === 'cast') return <StudioCast key={key} ctrl={ctrl} />;
  if (view === 'export') return <StudioExport key={key} ctrl={ctrl} />;
  if (view === 'dialogue') return <DialogueView key={key} ctrl={ctrl} />;
  return <CompareView key={key} ctrl={ctrl} />;
}

// A different session is a different page: a fresh controller and player.
export function Studio() {
  const { sid } = useParams();
  return <StudioSession key={sid} sid={sid} />;
}

function StudioSession({ sid }) {
  const { view } = useParams();
  const ctrl = useController(sid, view);
  useEffect(() => { ctrl.enterView(view); }, [ctrl, view]);
  useEffect(() => {
    document.addEventListener('keydown', ctrl.keydown);
    return () => document.removeEventListener('keydown', ctrl.keydown);
  }, [ctrl]);
  return (
    <div id="studioRoot" className="studio">
      <StudioHead ctrl={ctrl} view={view} />
      <Transport ctrl={ctrl} shown={TRANSPORT_VIEWS.includes(view)} />
      <nav className="studio-tabs" role="tablist" aria-label="Studio views">
        {VIEWS.map(([v, label]) => (
          <button key={v} type="button" role="tab" className="tab" data-view={v} aria-selected={v === view}
            aria-controls="studioBody" onClick={() => ctrl.show(v)}>{label}</button>
        ))}
      </nav>
      <p className="hint" id="studioStatus" role="status" aria-live="polite">{ctrl.state.status}</p>
      <section id="studioBody" className="studio-body" role="tabpanel"><ViewBody ctrl={ctrl} view={view} /></section>
    </div>
  );
}

