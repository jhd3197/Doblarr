import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { queryClient } from '../../lib/queries.js';
import { probeQuery } from './queries.js';

// Overview: how this episode is worked on, what it may read, and what exists.

const ROLES = [
  ['meaning', 'Meaning source', 'the original dialogue: facts and intent'],
  ['adaptation', 'Adaptation reference', 'another dub\'s writing; phrasing under a policy'],
  ['performance', 'Performance reference', 'a track to hear for delivery'],
  ['voice', 'Voice reference', 'a sample for voice identity'],
  ['evaluation', 'Evaluation only', 'held out from every generation request'],
];
const POLICIES = [
  ['original_only', 'Original only', 'No reference is ever sent to the writer.'],
  ['reference_suggestions', 'Original plus reference suggestions',
    'The original decides facts and relationships; the reference may lend phrasing or '
    + 'wordplay that keeps them. Uncertain alignment is never used.'],
  ['follow_edition', 'Follow a localized edition',
    'Target a chosen adaptation\'s choices, keeping the original\'s facts; departures are recorded.'],
];
const TEXT_KINDS = [
  ['original_transcript', 'Original-language transcript'],
  ['dub_transcript', 'Dub transcript (what that cast said)'],
  ['subtitle_translation', 'Subtitles translating the original'],
  ['manual', 'Typed or pasted'],
];
const CHECKPOINTS = [['casting', 'Casting, before the first draft'], ['script', 'Script review, before export'],
  ['export', 'Final export']];

const labelOf = (refs, id) => (refs.find(r => r.id === id) || {}).label || id;
function stateSummary(stats) {
  const states = stats?.states || {};
  return Object.entries(states).map(([k, v]) => `${v} ${k}`).join(', ') || 'no groups';
}
const toggle = (list, value, on) => (on ? [...new Set([...list, value])] : list.filter(v => v !== value));

function RunCard({ ctrl }) {
  const { session: s, overview: o } = ctrl.state;
  const job = o.active_job;
  const budgets = s.budgets || {};
  const [checkpoints, setCheckpoints] = useState(s.checkpoints || []);
  const [requests, setRequests] = useState(String(budgets.requests ?? 0));
  const [candidates, setCandidates] = useState(String(budgets.candidates ?? 2));
  const [retries, setRetries] = useState(String(budgets.retries ?? 1));
  const [unresolved, setUnresolved] = useState(s.unresolved === 'block_export' ? 'block_export' : 'flag');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  async function save() {
    await ctrl.savePatch({ checkpoints, unresolved, budgets: {
      requests: Number(requests) || 0, candidates: Number(candidates) || 0, retries: Number(retries) || 0 } });
    await ctrl.reload();
  }
  async function render() {
    setBusy(true);
    try {
      const result = await api(`studio/sessions/${ctrl.sid}/render`, { method: 'POST', json: {} });
      setNote(`Queued run ${result.job.id}. It uses the direction and budgets above; `
        + 'follow it on the Dubs page or here after a reload.');
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
      await ctrl.reload();
    } catch (error) { setNote(error.message); }
    setBusy(false);
  }

  return (
    <section className="panel studio-card" aria-labelledby="ovRun">
      <h3 id="ovRun">Draft and working style</h3>
      <p className="hint">{job ? `Latest run ${job.id}: ${job.status} — ${job.message || job.stage || ''}`
        : 'No run yet. A draft uses the direction and budgets below.'}</p>
      {o.export && <p>{o.export.stale} stale line(s) · {o.export.unresolved} unresolved finding(s)</p>}
      <fieldset className="studio-fieldset"><legend>Stop and ask me at</legend>
        {CHECKPOINTS.map(([c, label]) => (
          <label key={c} className="studio-check"><input type="checkbox" data-check={c} checked={checkpoints.includes(c)}
            onChange={e => setCheckpoints(toggle(checkpoints, c, e.target.checked))} /> {label}</label>
        ))}
        <p className="hint">Used by the Guided style. Automatic still shows every unresolved finding;
          Manual never queues anything you did not ask for.</p>
      </fieldset>
      <div className="studio-inline">
        <label className="review-field">Request budget <input className="input m" type="number" min="0" max="5000"
          id="ovRequests" value={requests} onChange={e => setRequests(e.target.value)} /><span className="hint">0 = counted, not capped</span></label>
        <label className="review-field">Alternative takes per line <input className="input m" type="number" min="0" max="4"
          id="ovCandidates" value={candidates} onChange={e => setCandidates(e.target.value)} /></label>
        <label className="review-field">Automatic retries <input className="input m" type="number" min="0" max="3"
          id="ovRetries" value={retries} onChange={e => setRetries(e.target.value)} /></label>
      </div>
      <label className="review-field">Unresolved findings at export <select className="input" id="ovUnresolved"
        value={unresolved} onChange={e => setUnresolved(e.target.value)}>
        <option value="flag">List them, allow export</option>
        <option value="block_export">Block export until resolved</option>
      </select></label>
      <div className="studio-actions">
        <button type="button" className="btn btn-secondary" id="ovSave" onClick={save}>Save working style</button>
        <button type="button" className="btn btn-primary" id="ovRender" disabled={busy} onClick={render}>Render a draft</button>
      </div>
      <p className="hint" id="ovRenderNote" role="status">{note}</p>
    </section>
  );
}

function DirectionCard({ ctrl }) {
  const { session: s, overview: o } = ctrl.state;
  const refs = o.references || [];
  const d = s.direction || {};
  const [form, setForm] = useState({
    target_locale: d.target_locale || '', adaptation: d.adaptation || 'natural', slang: !!d.slang,
    reference_policy: d.reference_policy || 'original_only', reference: d.reference || '',
    alignment: d.alignment || '', evaluation: d.evaluation || [],
  });
  const set = (key, value) => setForm({ ...form, [key]: value });
  const save = () => ctrl.savePatch({ direction: { ...form, target_locale: form.target_locale.trim() } });
  const held = refs.filter(r => r.evaluation_only);

  return (
    <section className="panel studio-card" aria-labelledby="ovDirection">
      <h3 id="ovDirection">Writing direction</h3>
      <label className="review-field">Spanish region (output locale)
        <input className="input m" id="ovLocale" value={form.target_locale} placeholder="es-MX, es-419, es-ES…"
          onChange={e => set('target_locale', e.target.value)} /></label>
      <p className="hint">Latin American Spanish is not one accent: pick the region you want to hear.</p>
      <label className="review-field">Adaptation style <select className="input" id="ovAdaptation" value={form.adaptation}
        onChange={e => set('adaptation', e.target.value)}>
        {['natural', 'faithful', 'localized'].map(a => <option key={a}>{a}</option>)}
      </select></label>
      <label className="studio-check"><input type="checkbox" id="ovSlang" checked={form.slang}
        onChange={e => set('slang', e.target.checked)} /> Allow regional slang</label>
      <fieldset className="studio-fieldset"><legend>Reference policy</legend>
        {POLICIES.map(([v, label, note]) => (
          <label key={v} className="studio-radio"><input type="radio" name="ovPolicy" value={v}
            checked={form.reference_policy === v} onChange={() => set('reference_policy', v)} />
            <span><strong>{label}</strong><span className="hint">{note}</span></span></label>
        ))}
      </fieldset>
      <label className="review-field">Adaptation reference <select className="input" id="ovReference" value={form.reference}
        onChange={e => set('reference', e.target.value)}>
        <option value="">None</option>
        {refs.filter(r => r.roles.includes('adaptation')).map(r => <option key={r.id} value={r.id}>{r.label} ({r.language})</option>)}
      </select></label>
      <label className="review-field">Alignment <select className="input" id="ovAlignment" value={form.alignment}
        onChange={e => set('alignment', e.target.value)}>
        <option value="">None</option>
        {(o.alignments || []).map(a => <option key={a.id} value={a.id}>{labelOf(refs, a.reference)} · revision {a.revision}</option>)}
      </select></label>
      <fieldset className="studio-fieldset"><legend>Held out from generation</legend>
        {held.length ? held.map(r => (
          <label key={r.id} className="studio-check"><input type="checkbox" data-eval={r.id}
            checked={form.evaluation.includes(r.id)} onChange={e => set('evaluation', toggle(form.evaluation, r.id, e.target.checked))} /> {r.label}</label>
        )) : <p className="hint">No evaluation-only reference yet.</p>}
        <p className="hint">Checked tracks are fingerprinted and every translation request of a render is
          scanned for them before it is sent.</p>
      </fieldset>
      <button type="button" className="btn btn-secondary" id="ovDirSave" onClick={save}>Save direction</button>
    </section>
  );
}

async function transcribe(ctrl, refId) {
  const text = window.prompt('Windows to transcribe, in seconds (e.g. "60-120, 300-360"). '
    + 'Each is at most 240 s; nothing outside them is read.', '');
  if (!text) return;
  const windows = text.split(',').map(w => w.split('-').map(Number)).filter(w => w.length === 2
    && w.every(Number.isFinite));
  try {
    const result = await api(`studio/references/${refId}/transcribe`, { method: 'POST', json: { windows } });
    ctrl.status(`Queued transcription ${result.job.id}. It runs through the ordinary queue.`);
  } catch (error) { ctrl.status(error.message); }
}

function AddReference({ ctrl }) {
  const { data: probe, error } = useQuery(probeQuery(ctrl.sid));
  const [form, setForm] = useState({ label: '', language: '', track: '', kind: TEXT_KINDS[0][0], from: 'none', paste: '' });
  const [roles, setRoles] = useState([]);
  if (error) return <div id="ovAddRef"><p className="hint">{error.message}</p></div>;
  if (!probe) return <div id="ovAddRef"><p className="hint">Reading the file&apos;s tracks…</p></div>;
  const audio = probe.streams.filter(s => s.type === 'audio');
  const subs = probe.streams.filter(s => s.type === 'subtitle');
  const set = key => e => setForm({ ...form, [key]: e.target.value });

  async function add() {
    const json = { label: form.label.trim(), language: form.language.trim(), roles,
      track: form.track === '' ? null : { media_path: '', audio_index: Number(form.track) },
      text: { kind: form.kind } };
    if (form.from === 'job') json.from_job = true;
    if (form.from.startsWith('sub:')) json.subtitle_stream = Number(form.from.slice(4));
    if (form.from === 'paste') {
      json.utterances = form.paste.split('\n').map(l => l.split('|')).filter(p => p.length >= 3)
        .map((p, i) => ({ utt_id: `p${i}`, start: Number(p[0]), end: Number(p[1]), text: p.slice(2).join('|').trim() }));
    }
    try {
      await api(`studio/sessions/${ctrl.sid}/references`, { method: 'POST', json });
      await ctrl.refresh();
    } catch (err) { ctrl.status(err.message); }
  }

  return (
    <div id="ovAddRef">
      <p className="hint">{probe.note}</p>
      <div className="studio-inline">
        <label className="review-field">Label <input className="input" id="arLabel" placeholder="English dub" value={form.label} onChange={set('label')} /></label>
        <label className="review-field">Language <input className="input m" id="arLang" placeholder="en" value={form.language} onChange={set('language')} /></label>
        <label className="review-field">Audio track <select className="input" id="arTrack" value={form.track} onChange={set('track')}>
          <option value="">None (text only)</option>
          {audio.map(s => <option key={s.audio_index} value={s.audio_index}>{s.audio_index}: {s.language || '?'} {s.title || ''}</option>)}
        </select></label>
      </div>
      <fieldset className="studio-fieldset"><legend>Roles</legend>
        {ROLES.map(([v, name, note]) => (
          <label key={v} className="studio-check"><input type="checkbox" data-role={v} checked={roles.includes(v)}
            onChange={e => setRoles(toggle(roles, v, e.target.checked))} /> {name}
            <span className="hint">— {note}</span></label>
        ))}
      </fieldset>
      <div className="studio-inline">
        <label className="review-field">What the text is <select className="input" id="arKind" value={form.kind} onChange={set('kind')}>
          {TEXT_KINDS.map(([v, n]) => <option key={v} value={v}>{n}</option>)}<option value="none">No text</option></select></label>
        <label className="review-field">Text from <select className="input" id="arFrom" value={form.from} onChange={set('from')}>
          <option value="none">Nothing yet (transcribe windows later)</option>
          <option value="job">The run&apos;s own cues</option>
          {subs.map((s, i) => <option key={i} value={`sub:${i}`}>Subtitle track {i}: {s.language} {s.title || ''}</option>)}
          <option value="paste">Pasted lines (start|end|text)</option></select></label>
      </div>
      <textarea className="input" id="arPaste" rows={4} hidden={form.from !== 'paste'} placeholder="12.0|14.5|I'm back"
        value={form.paste} onChange={set('paste')} />
      <button type="button" className="btn btn-secondary" id="arAdd" onClick={add}>Add reference</button>
    </div>
  );
}

function ReferencesCard({ ctrl }) {
  const refs = ctrl.state.overview.references || [];
  return (
    <section className="panel studio-card" aria-labelledby="ovRefs">
      <h3 id="ovRefs">References</h3>
      <p className="hint">A track&apos;s language tag says what the release claims, not what it contains.
        Nothing here is sent to a model unless a role and a policy allow it.</p>
      <div className="table-wrap"><table className="table"><thead><tr><th>Reference</th><th>Language</th><th>Roles</th><th>Text</th><th /></tr></thead>
        <tbody>{refs.length ? refs.map(r => (
          <tr key={r.id}>
            <td><strong>{r.label}</strong><div className="hint">{r.track?.media_name || 'text only'}
              {r.track?.audio_index != null ? ` · audio ${r.track.audio_index}` : ''}</div></td>
            <td className="m">{r.language}</td>
            <td>{r.evaluation_only ? <span className="tag tag-outline">evaluation only</span>
              : r.roles.map(x => <span key={x} className="tag tag-neutral studio-role">{x}</span>)}</td>
            <td className="hint">{r.text?.kind || 'none'} · {r.text?.utterances || 0} line(s)
              {r.text?.provenance ? ` · ${r.text.provenance}` : ''}{r.text?.uncertain ? ' · unreviewed' : ''}</td>
            <td>{r.track && <button type="button" className="btn btn-ghost" data-transcribe={r.id}
              onClick={() => transcribe(ctrl, r.id)}>Transcribe windows</button>}</td>
          </tr>
        )) : <tr><td colSpan="5" className="hint">No references yet.</td></tr>}</tbody></table></div>
      <details className="studio-add"><summary>Add a reference</summary><AddReference ctrl={ctrl} /></details>
    </section>
  );
}

function AlignmentDetail({ ctrl, id }) {
  const query = { queryKey: ['studio', ctrl.sid, 'alignment', id], queryFn: () => api(`studio/alignments/${id}`), gcTime: 5_000 };
  const { data } = useQuery(query);
  if (!data) return null;
  const alignment = data.alignment;
  async function act(group, action) {
    try {
      await api(`studio/alignments/${id}/overrides`, { method: 'POST', json: {
        base_revision: alignment.revision, action, group_id: group } });
      queryClient.invalidateQueries({ queryKey: query.queryKey });
    } catch (error) { ctrl.status(error.message); }
  }
  return (
    <div className="studio-align">
      <p className="hint">Method {alignment.method} · {stateSummary(alignment.stats)}.
        Uncertain and excluded groups are never sent to a writer.</p>
      <div className="table-wrap"><table className="table"><thead><tr><th>Original</th><th>Reference</th><th>State</th><th /></tr></thead><tbody>
        {alignment.groups.slice(0, 400).map(g => (
          <tr key={g.group_id} data-group={g.group_id}>
            <td>{g.source_text || '—'}</td>
            <td>{g.reference_text || '—'}
              {(g.differences || []).length > 0 && <div className="review-marker">{g.differences.join('; ')}</div>}</td>
            <td><span className={`tag ${g.state === 'matched' ? 'tag-neutral' : 'tag-outline'}`}>{g.state}</span>
              {g.confidence != null && <span className="hint m"> {g.confidence.toFixed(2)}</span>}
              {g.manual && <span className="hint"> manual</span>}</td>
            <td>{g.state === 'excluded'
              ? <button type="button" className="btn btn-ghost" data-act="include" onClick={() => act(g.group_id, 'include')}>Include</button>
              : <button type="button" className="btn btn-ghost" data-act="exclude" onClick={() => act(g.group_id, 'exclude')}>Exclude</button>}
              {g.source.length > 0 && g.reference.length > 0 &&
                <button type="button" className="btn btn-ghost" data-act="unlink" onClick={() => act(g.group_id, 'unlink')}>Unlink</button>}</td>
          </tr>
        ))}
      </tbody></table></div>
    </div>
  );
}

function AlignmentsCard({ ctrl }) {
  const o = ctrl.state.overview;
  const refs = o.references || [];
  const meaning = refs.filter(r => r.roles.includes('meaning'));
  const others = refs.filter(r => !r.roles.includes('meaning'));
  const [src, setSrc] = useState(meaning[0]?.id || '');
  const [ref, setRef] = useState(others[0]?.id || '');
  const [offset, setOffset] = useState('0');
  const [estimate, setEstimate] = useState(false);
  const [detail, setDetail] = useState('');

  async function align() {
    try {
      const result = await api(`studio/sessions/${ctrl.sid}/alignments`, { method: 'POST', json: {
        source: src, reference: ref, estimate,
        time_map: estimate ? null
          : { segments: [{ start: 0, end: 100000, offset: Number(offset) || 0, rate: 1, method: 'manual' }] } } });
      await ctrl.reload();
      setDetail(result.alignment.id);
    } catch (error) { ctrl.status(error.message); }
  }

  return (
    <section className="panel studio-card" aria-labelledby="ovAlign">
      <h3 id="ovAlign">Alignments</h3>
      <div id="ovAlignList">{(o.alignments || []).length ? o.alignments.map(a => (
        <div key={a.id} className="studio-align-row">
          <strong>{labelOf(refs, a.source_ref)} ↔ {labelOf(refs, a.reference)}</strong>
          <span className="hint">revision {a.revision} · {a.segments} map segment(s) · {stateSummary(a.stats)}</span>
          <button type="button" className="btn btn-ghost" data-align={a.id} onClick={() => setDetail(a.id)}>Review lines</button>
        </div>
      )) : <p className="hint">No alignment yet.</p>}</div>
      <div className="studio-inline">
        <label className="review-field">Original <select className="input" id="ovAlSrc" value={src} onChange={e => setSrc(e.target.value)}>
          {meaning.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}</select></label>
        <label className="review-field">Reference <select className="input" id="ovAlRef" value={ref} onChange={e => setRef(e.target.value)}>
          {others.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}</select></label>
        <label className="review-field">Offset (s, reference → original)
          <input className="input m" type="number" step="0.01" id="ovAlOffset" value={offset} onChange={e => setOffset(e.target.value)} /></label>
        <label className="studio-check"><input type="checkbox" id="ovAlEstimate" checked={estimate}
          onChange={e => setEstimate(e.target.checked)} /> Estimate from the audio</label>
        <button type="button" className="btn btn-secondary" id="ovAlign" onClick={align}>Align</button>
      </div>
      <div id="ovAlignDetail">{detail && <AlignmentDetail key={detail} ctrl={ctrl} id={detail} />}</div>
    </section>
  );
}

function ImportPreview({ ctrl, path, preview }) {
  const refs = ctrl.state.overview.references || [];
  const [speakers, setSpeakers] = useState(() => Object.fromEntries(preview.speakers.map(sp => [sp, sp])));
  const [tracks, setTracks] = useState({});
  async function apply() {
    const mapping = { speakers: Object.fromEntries(Object.entries(speakers).map(([k, v]) => [k, v.trim()])),
      tracks: Object.fromEntries(Object.entries(tracks).filter(([, v]) => v)) };
    try {
      const result = await api(`studio/sessions/${ctrl.sid}/imports`, { method: 'POST', json: { path, mapping } });
      ctrl.status(result.repeated ? 'Already imported; nothing changed.' : 'Imported. Play it from Compare.');
      await ctrl.refresh();
    } catch (error) { ctrl.status(error.message); }
  }
  return (
    <div className="studio-import">
      <p><strong>{preview.legacy_id}</strong> · {preview.kind}{preview.media ? ` · media ${preview.media.state}` : ''}</p>
      <p className="hint">{preview.generates}. {preview.judgments}</p>
      {preview.missing.length ? <p className="review-marker">Missing: {preview.missing.join(', ')}</p>
        : <p className="hint">Every listed file is present.</p>}
      <fieldset className="studio-fieldset"><legend>Map each legacy speaker</legend>
        {preview.speakers.map(sp => (
          <label key={sp} className="studio-inline">{sp} →
            <input className="input m" data-speaker={sp} value={speakers[sp]}
              onChange={e => setSpeakers({ ...speakers, [sp]: e.target.value })} /></label>
        ))}
      </fieldset>
      {(preview.tracks || []).length > 0 && (
        <fieldset className="studio-fieldset"><legend>What each legacy source track is</legend>
          {preview.tracks.map(t => (
            <label key={t.key} className="studio-inline">{t.language} ({t.legacy_role}) →
              <select className="input" data-track={t.key} value={tracks[t.key] || ''}
                onChange={e => setTracks({ ...tracks, [t.key]: e.target.value })}>
                <option value="">Choose…</option>
                {ROLES.map(([v, n]) => <option key={v} value={v}>{n}</option>)}
                {refs.map(r => <option key={r.id} value={`ref:${r.id}`}>{r.label}</option>)}
              </select></label>
          ))}
        </fieldset>
      )}
      <button type="button" className="btn btn-primary" id="ovImportApply" onClick={apply}>Import with this mapping</button>
    </div>
  );
}

function ImportCard({ ctrl }) {
  const o = ctrl.state.overview;
  const [path, setPath] = useState('');
  const [result, setResult] = useState(null);
  async function preview() {
    const target = path.trim();
    try {
      const { preview: found } = await api(`studio/sessions/${ctrl.sid}/imports/preview`, { method: 'POST', json: { path: target } });
      setResult({ path: target, preview: found });
    } catch (error) { setResult({ error: error.message }); }
  }
  async function results(importId, file) {
    if (!file) return;
    try {
      const saved = await api(`studio/imports/${importId}/results`, { method: 'POST',
        json: { name: file.name, text: await file.text() } });
      ctrl.status(`Imported judgments: ${saved.import.judgments} results file(s) on this import.`);
      await ctrl.refresh();
    } catch (error) { ctrl.status(error.message); }
  }
  return (
    <section className="panel studio-card" aria-labelledby="ovImport">
      <h3 id="ovImport">Import an earlier experiment</h3>
      <p className="hint">Reads a comparison manifest or a voice-audition record from the work folder,
        shows what is there and what is missing, and asks you to map each identity. Nothing is generated.</p>
      <div className="studio-inline"><input className="input m" id="ovImportPath" placeholder="work/benchmarks/…/manifest.json"
        value={path} onChange={e => setPath(e.target.value)} />
        <button type="button" className="btn btn-secondary" id="ovImportPreview" onClick={preview}>Preview</button></div>
      <div id="ovImportResult">
        {result?.error && <p className="review-marker">{result.error}</p>}
        {result?.preview && <ImportPreview key={result.preview.legacy_id} ctrl={ctrl} path={result.path} preview={result.preview} />}
      </div>
      {(o.imports || []).map(i => (
        <div key={i.id} className="studio-align-row"><strong>{i.legacy_id}</strong>
          <span className="hint">{i.kind} · {i.judgments} judgment file(s) · {i.missing.length} missing</span>
          <label className="btn btn-ghost">Import exported results<input type="file" accept=".md,text/markdown"
            data-results={i.id} hidden onChange={e => results(i.id, e.target.files[0])} /></label>
        </div>
      ))}
    </section>
  );
}

function StudioJobs({ ctrl }) {
  const jobs = ctrl.state.overview.studio_jobs || [];
  if (!jobs.length) return null;
  async function cancel(id) {
    await api(`jobs/${id}`, { method: 'DELETE' }).catch(e => ctrl.status(e.message));
    await ctrl.reload();
  }
  return (
    <section className="panel studio-card"><h3>Studio work in the queue</h3>
      <div className="table-wrap"><table className="table"><tbody>{jobs.map(j => (
        <tr key={j.id}><td>{j.kind.replace('studio_', '')}</td><td>{j.status}</td>
          <td className="hint">{j.message || j.stage || ''}</td>
          <td>{['queued', 'running'].includes(j.status) &&
            <button type="button" className="btn btn-ghost" data-cancel={j.id} onClick={() => cancel(j.id)}>Cancel</button>}</td></tr>
      ))}</tbody></table></div>
    </section>
  );
}

export function StudioOverview({ ctrl }) {
  return (
    <>
      <div className="studio-grid">
        <RunCard ctrl={ctrl} />
        <DirectionCard ctrl={ctrl} />
      </div>
      <ReferencesCard ctrl={ctrl} />
      <AlignmentsCard ctrl={ctrl} />
      <ImportCard ctrl={ctrl} />
      <StudioJobs ctrl={ctrl} />
    </>
  );
}
