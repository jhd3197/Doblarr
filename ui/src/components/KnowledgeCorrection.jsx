import { useCallback, useEffect, useRef, useState } from 'react';
import { api, apiUrl, withKey } from '../lib/api.js';
import { baseLanguage as baseOf } from '../lib/languages.js';
import { scopeOptions } from '../lib/knowledge.js';

const READY = ['completed', 'done', 'ready', 'success'];
const FAILED = ['failed', 'error', 'cancelled', 'canceled'];
const REGISTERS = [['', 'Unset'], ['formal', 'Formal'], ['neutral', 'Neutral'],
  ['colloquial', 'Colloquial'], ['vulgar', 'Vulgar']];

// Audition a text through the voice-catalog preview mechanism, polling until
// ready. Polling stops when the component using it unmounts.
//
//   const sample = useVoiceSample();
//   sample.listen({ key, text, language })  -> starts a sample
//   sample.status   text for a role="status" line ('' before the first listen)
//   sample.src      audio URL once ready ('' until then)
//   sample.busy     true while a sample is being generated
export function useVoiceSample() {
  const [state, setState] = useState({ status: '', src: '', busy: false });
  const run = useRef(0);
  useEffect(() => () => { run.current++; }, []);

  const listen = useCallback(async ({ key, text, language }) => {
    const id = ++run.current;
    const alive = () => run.current === id;
    const set = patch => { if (alive()) setState(s => ({ ...s, ...patch })); };
    set({ status: 'Generating a short sample…', busy: true });
    try {
      const response = await api('voice-catalog/preview', { method: 'POST', json: { key, text, language } });
      const deadline = Date.now() + 180000;
      const poll = async () => {
        if (!alive()) return;
        try {
          const result = await api(`voice-catalog/preview/${response.id}`);
          if (!alive()) return;
          if (READY.includes(result.status)) {
            set({ src: withKey(apiUrl(`voice-catalog/preview/${response.id}/audio`)),
              status: 'Sample ready.', busy: false });
          } else if (FAILED.includes(result.status)) {
            set({ status: result.error || 'Voice sample failed.', busy: false });
          } else if (Date.now() > deadline) {
            set({ status: 'Still processing in Voicebox.', busy: false });
          } else setTimeout(poll, 1500);
        } catch (error) { set({ status: error.message, busy: false }); }
      };
      poll();
    } catch (error) { set({ status: error.message, busy: false }); }
  }, []);

  return { ...state, listen };
}

// Shared "add a correction" dialog: draft -> preview against a sentence ->
// optional audition (voice-catalog preview of the resolved text) -> save.
// Saving and re-rendering affected dialogue are separate, explicit actions.
// Render it while open; it opens as a modal <dialog> on mount.
//
// Props (all optional except onClose):
//   kind        'pronunciation' (default) or 'term'
//   phrase      initial written word or phrase
//   sourceForm  initial source wording (term only)
//   locale      rule locale, default 'es'
//   text        sample sentence
//   engine      speech engine for the draft realization (default chatterbox)
//   voice       voice profile id; enables Listen
//   titleRef, showRef, lineRef   scope refs offered as "apply this rule" choices
//   onSaved(entry)  after the entry (and realization) are saved
//   onClose()       when the dialog closes (Close button or Escape)
export function KnowledgeCorrection({
  kind = 'pronunciation', phrase = '', sourceForm = '', locale = 'es',
  text = '', engine = '', voice = '', titleRef = '', showRef = '', lineRef = '',
  onSaved = null, onClose,
}) {
  const dialogRef = useRef(null);
  const audioRef = useRef(null);
  const isTerm = kind === 'term';
  const [scopes] = useState(() => scopeOptions({ lineRef, titleRef, showRef }));
  const [form, setForm] = useState({ phrase, source: sourceForm, replacement: '', scope: 0, text, register: '' });
  const [preview, setPreview] = useState('');
  const [resolved, setResolved] = useState('');
  const [status, setStatus] = useState('');
  const [saving, setSaving] = useState(false);
  const sample = useVoiceSample();

  useEffect(() => {
    const dialog = dialogRef.current;
    const opener = document.activeElement;
    dialog.showModal();
    const audio = audioRef.current;
    return () => { audio?.pause(); if (dialog.open) dialog.close(); opener?.focus?.(); };
  }, []);

  const set = key => e => setForm(f => ({ ...f, [key]: key === 'scope' ? e.target.selectedIndex : e.target.value }));

  function draft() {
    const scope = scopes[form.scope];
    const draftEngine = engine || 'chatterbox';
    return {
      entry: { phrase: form.phrase.trim(), kind, locale, source_form: isTerm ? form.source.trim() : '',
        register: isTerm && form.register ? form.register : null,
        scope: scope.value, scope_ref: scope.ref },
      realization: isTerm ? null : { entry_id: 'draft', engine: draftEngine, replacement: form.replacement.trim() || ' ' },
      text: form.text, engine: draftEngine, voice, title_ref: titleRef, show_ref: showRef, line_ref: lineRef,
    };
  }

  async function previewMatches() {
    setPreview('Resolving…');
    try {
      const data = await api('knowledge/preview', { method: 'POST', json: draft() });
      setResolved(data.after);
      const conflicts = data.conflicts.length ? ` Conflicts: ${data.conflicts.map(c => c.reason).join('; ')}.` : '';
      setPreview(data.before === data.after ? `No change to the sample sentence.${conflicts}`
        : `“${data.before}” → “${data.after}”${conflicts}`);
    } catch (error) { setPreview(error.message); }
  }

  async function save() {
    setSaving(true);
    setStatus('Saving…');
    try {
      const { entry, realization } = draft();
      if (!entry.phrase) throw new Error('Enter a word or phrase first.');
      const saved = await api('knowledge/entries', { method: 'POST', json: entry });
      if (!isTerm) {
        const replacement = form.replacement.trim();
        if (!replacement) throw new Error('Enter a replacement spelling first.');
        await api('knowledge/realizations', { method: 'POST', json: { ...realization, entry_id: saved.entry.id, replacement } });
      }
      setStatus('Saved as proposed. Existing jobs keep their frozen rules until you re-render.');
      onSaved?.(saved.entry);
    } catch (error) { setStatus(error.message); setSaving(false); }
  }

  return (
    <dialog ref={dialogRef} className="voice-picker" onClose={onClose} onCancel={e => { e.preventDefault(); onClose(); }}>
      <header>
        <div>
          <h2>{isTerm ? 'Improve regional wording' : 'Fix pronunciation'}</h2>
          <p className="hint">Saved as a proposed personal rule for {locale}; nothing is re-rendered until you ask.</p>
        </div>
        <button type="button" className="btn btn-ghost correction-close" onClick={onClose}>Close</button>
      </header>
      <div className="narrator-fields">
        <label>{isTerm ? 'Preferred wording' : 'Written word or phrase'}
          <input className="input correction-phrase" maxLength={300} value={form.phrase} onChange={set('phrase')} /></label>
        {isTerm
          ? <label>Source wording (for translation guidance)
            <input className="input correction-source" maxLength={300} value={form.source} onChange={set('source')} /></label>
          : <label>Replacement spelling
            <input className="input correction-replacement" maxLength={300} placeholder="How it should be spelled for the speech engine"
              value={form.replacement} onChange={set('replacement')} /></label>}
        {isTerm && <label>Register
          <select className="input correction-register" aria-label="Register" value={form.register} onChange={set('register')}>
            {REGISTERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>}
        <label>Apply this rule
          <select className="input correction-scope" aria-label="Rule scope" value={scopes[form.scope].value} onChange={set('scope')}>
            {scopes.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select></label>
        <label>Sample sentence
          <textarea className="input correction-text" rows={2} maxLength={2000} value={form.text} onChange={set('text')} /></label>
      </div>
      <div className="episode-actions">
        <button type="button" className="btn btn-secondary correction-preview" onClick={previewMatches}>Preview matches</button>
        <button type="button" className="btn btn-ghost correction-listen" disabled={!voice || sample.busy}
          onClick={() => sample.listen({ key: `profile:${voice}`, text: resolved || form.text, language: baseOf(locale) })}>Listen</button>
        <button type="button" className="btn btn-primary correction-save" disabled={saving} onClick={save}>Save correction</button>
      </div>
      <p className="correction-preview-out hint">{preview}</p>
      <audio ref={audioRef} controls className="correction-audio" hidden={!sample.src} src={sample.src || undefined} />
      <p className="correction-status hint" role="status">{status || sample.status}</p>
    </dialog>
  );
}
