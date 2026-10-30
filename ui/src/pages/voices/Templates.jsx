import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { queryClient } from '../../lib/queries.js';
import { anchorsText } from '../../lib/knowledge.js';
import { api, apiUrl } from '../../lib/api.js';
import { safeGet } from '../../lib/storage.js';
import { promptDialog } from '../../lib/dialogs.js';

// The template catalogue: voice envelopes, background policies and presets.
// A template is data; editing one saves a new version and saved dubs keep the
// version they used. Previews play a template over a test phrase (or a take
// inside the work folder) and never generate speech.

const KINDS = [['voice', 'Voice envelopes'], ['background', 'Background policies'], ['preset', 'Presets']];
const FAMILY = { preserve: 'leaves the take alone', curve: 'gain curve over the line', flatten: 'evens out the take',
  peak_linked: 'lifts the take’s own stresses', bed: 'background policy', bed_legacy: 'current sidechain ducking',
  preset: 'voice + background pairing' };

function blank(kind) {
  return {
    id: `${kind}/my-template`, kind,
    family: kind === 'voice' ? 'curve' : kind === 'preset' ? 'preset' : 'bed',
    title: 'My template', purpose: '',
    ...(kind === 'voice' ? { anchors: [{ at: 0, db: 0 }, { at: 1, db: 2 }],
      params: { strength: { default: 1, min: 0, max: 1.5, units: 'share' } } } : {}),
    ...(kind === 'background' ? { params: {
      duck_db: { default: -5, min: -12, max: 0, units: 'dB' },
      attack_ms: { default: 100, min: 20, max: 400, units: 'ms' },
      release_ms: { default: 450, min: 150, max: 1500, units: 'ms' } } } : {}),
    ...(kind === 'preset' ? { voice: 'voice/preserve', background: 'background/gentle-ducking' } : {}),
    version: 0,
  };
}

// `view` keeps the chosen kind and the retired toggle across tab switches.
export function Templates({ view, setView }) {
  const kind = view.templateKind || 'voice';
  const retired = Boolean(view.showRetired);
  const key = ['templates', kind, retired];
  const { data, error, isPending } = useQuery({ queryKey: key,
    queryFn: () => api(`templates?kind=${kind}&include_retired=${retired ? 'true' : 'false'}`) });
  const [picked, setPicked] = useState(new Set());
  const [editor, setEditor] = useState(null);   // { template } or { import: true }
  const [status, setStatus] = useState('');
  if (isPending) return <p className="hint">Reading the template catalogue…</p>;
  if (error) return <p className="hint">{error.message}</p>;
  const reload = () => queryClient.invalidateQueries({ queryKey: ['templates'] });
  const toggle = id => { const next = new Set(picked); if (next.has(id)) next.delete(id); else next.add(id); setPicked(next); };

  async function exportPicked() {
    const ids = [...picked];
    if (!ids.length) { setStatus('Select the templates to export first.'); return; }
    try {
      const bundle = await api('templates-export', { method: 'POST', json: { ids } });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 1)], { type: 'application/json' }));
      link.download = 'doblarr-templates.json';
      link.click();
      setStatus(`Exported ${ids.length} template${ids.length === 1 ? '' : 's'}.`);
    } catch (err) { setStatus(err.message); }
  }

  return (
    <div className="panel templates-panel">
      <div className="opts" role="group" aria-label="Kind">{KINDS.map(([k, label]) => (
        <button key={k} type="button" className="opt" data-kind={k} aria-pressed={kind === k ? 'true' : 'false'}
          onClick={() => { setView({ ...view, templateKind: k }); setEditor(null); }}>{label}</button>
      ))}</div>
      <label className="hint"><input type="checkbox" data-retired checked={retired}
        onChange={e => setView({ ...view, showRetired: e.target.checked })} /> Show retired</label>
      <p className="hint">Adding a curve is a data change: save it here and it is offered, previewed and rendered like the built-in ones. Gains are relative dB; 0.5 linear gain is about −6 dB.</p>
      <div className="table-wrap"><table className="table"><thead><tr><th></th><th>Template</th><th>Shape or settings</th><th>Version</th><th></th></tr></thead><tbody>
        {data.templates.map(t => (
          <tr key={t.id}><td><input type="checkbox" data-pick={t.id} aria-label={`Select ${t.title}`} checked={picked.has(t.id)} onChange={() => toggle(t.id)} /></td>
            <td><strong>{t.title}</strong>{t.retired && <> <span className="tag tag-outline">retired</span></>}<br /><span className="hint">{t.purpose || FAMILY[t.family] || ''}</span></td>
            <td className="hint">{anchorsText(t)}</td>
            <td className="m">v{t.version}<span className="hint"> · {t.provenance?.origin || ''}</span></td>
            <td><button type="button" className="btn btn-ghost" data-edit={t.id} onClick={() => setEditor({ template: t })}>Open</button></td></tr>
        ))}</tbody></table></div>
      <div className="studio-actions">
        <button type="button" className="btn btn-secondary" data-new onClick={() => setEditor({ template: blank(kind) })}>New template</button>
        <button type="button" className="btn btn-ghost" data-export onClick={exportPicked}>Export selected</button>
        <button type="button" className="btn btn-ghost" data-import onClick={() => setEditor({ import: true })}>Import a bundle</button>
      </div>
      <div data-editor>
        {editor?.template && <Editor key={`${editor.template.id}:${editor.template.version}`} template={editor.template}
          onSaved={async msg => { setEditor(null); await reload(); setStatus(msg); }}
          onChanged={async () => { setEditor(null); await reload(); }} />}
        {editor?.import && <Import onDone={async msg => { setEditor(null); await reload(); setStatus(msg); }} />}
      </div>
      <p className="hint" role="status" data-status>{status}</p>
    </div>
  );
}

function Import({ onDone }) {
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  async function run() {
    try {
      const result = await api('templates-import', { method: 'POST', json: { bundle: JSON.parse(text) } });
      await onDone(`Added ${result.added.length}, updated ${result.updated.length}, unchanged ${result.unchanged.length}.`);
    } catch (err) { setError(err.message); }
  }
  return (
    <>
      <label className="review-field">Bundle JSON<textarea className="input m" rows={8} data-bundle value={text} onChange={e => setText(e.target.value)} /></label>
      <button type="button" className="btn btn-secondary" data-do-import onClick={run}>Import</button>
      <p className="hint">Every template is checked first; if one is invalid, nothing is imported.</p>
      {error && <p className="hint" role="status">{error}</p>}
    </>
  );
}

function Editor({ template, onSaved, onChanged }) {
  const [json, setJson] = useState(() => {
    const fields = { ...template };
    ['version', 'updated_at', 'fingerprint', 'template_id', 'preview_curve'].forEach(k => delete fields[k]);
    return JSON.stringify(fields, null, 1);
  });
  const [strength, setStrength] = useState('1');
  const [status, setStatus] = useState('');
  const [audio, setAudio] = useState('');
  const parsed = () => { try { return JSON.parse(json); } catch { setStatus('That is not valid JSON.'); return null; } };

  async function save() {
    const doc = parsed();
    if (!doc) return;
    try {
      const saved = await api('templates', { method: 'PUT', json: { template: doc, base_version: template.version || 0 } });
      await onSaved(`Saved ${saved.id} as version ${saved.version}.`);
    } catch (error) { setStatus(error.message); }
  }
  async function duplicate() {
    const id = await promptDialog({ title: 'Duplicate template', label: 'Id for the copy', defaultValue: `${template.id}-copy` });
    if (!id) return;
    try { await api(`templates/${template.id}/duplicate`, { method: 'POST', json: { new_id: id } }); await onChanged(); }
    catch (error) { setStatus(error.message); }
  }
  async function retire() {
    try { await api(`templates/${template.id}/retire`, { method: 'POST', json: { base_version: template.version } }); await onChanged(); }
    catch (error) { setStatus(error.message); }
  }
  async function preview() {
    const doc = parsed();
    if (!doc) return;
    setStatus('Rendering the preview…');
    try {
      const key = safeGet('doblarr_api_key', '');
      const response = await fetch(apiUrl('templates-preview'), { method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(key ? { 'X-Api-Key': key } : {}) },
        body: JSON.stringify({ template: doc, params: { strength: Number(strength) } }) });
      if (!response.ok) throw new Error((await response.json()).error || 'Preview failed');
      setAudio(URL.createObjectURL(await response.blob()));
      setStatus(`Moves ${response.headers.get('X-Envelope-Range-Db')} dB inside the phrase · peak ${response.headers.get('X-Envelope-Peak')}. A test phrase, not a dubbed line.`);
    } catch (error) { setStatus(error.message); }
  }

  return (
    <div className="template-editor">
      <h4>{template.title} <span className="hint">{template.version ? `v${template.version}` : 'new'}</span></h4>
      <label className="review-field">Definition (JSON)<textarea className="input m" rows={14} data-json value={json} onChange={e => setJson(e.target.value)} /></label>
      <p className="hint">Fields outside the schema are refused. Anchors sit on the line&apos;s speech from 0 (first word) to 1 (last word).</p>
      <div className="studio-actions">
        <button type="button" className="btn btn-primary" data-save onClick={save}>{template.version ? 'Save as a new version' : 'Create'}</button>
        {template.version > 0 && <>
          <button type="button" className="btn btn-ghost" data-duplicate onClick={duplicate}>Duplicate</button>
          <button type="button" className="btn btn-ghost" data-retire disabled={template.retired} onClick={retire}>Retire</button></>}
        {template.kind === 'voice' && <>
          <button type="button" className="btn btn-ghost" data-preview onClick={preview}>Hear it on a test phrase</button>
          <label className="hint">Strength <input className="input m template-strength" type="number" min="0" max="1.5" step="0.1"
            value={strength} onChange={e => setStrength(e.target.value)} data-strength /></label></>}
      </div>
      {audio && <audio controls data-audio src={audio} autoPlay />}
      <p className="hint" role="status" data-edit-status>{status}</p>
    </div>
  );
}
