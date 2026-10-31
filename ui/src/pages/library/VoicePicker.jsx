import { useEffect, useRef, useState } from 'react';
import { api } from '../../lib/api.js';
import { baseLanguage, languageMatches } from '../../lib/languages.js';
import { previewSample } from '../../lib/voices.js';

// Voices are tagged with base languages (es); a regional target (es-MX) matches them.
const AGES = ['unknown', 'child', 'young', 'adult', 'older'];
const GENDERS = ['unknown', 'male', 'female', 'neutral'];
const QWEN = ['qwen', 'qwen_custom_voice'];

function VoiceCard({ voice, language, onTraits, onListen, onUse }) {
  const [busy, setBusy] = useState(false);
  const wrongPreset = voice.engine === 'kokoro' && !languageMatches(voice.language, language);
  return (
    <article className="voice-card" data-key={voice.key}>
      <div>
        <h3>{voice.name}</h3>
        <p className="hint">{voice.engine} · {voice.language.toUpperCase()} · {voice.gender} · {voice.age === 'unknown' ? 'Age not tagged' : voice.age}</p>
        <p>{voice.description}</p>
      </div>
      <label>Age after listening
        <select className="input voice-age" aria-label={`Age tag for ${voice.name}`} value={voice.age}
          onChange={e => onTraits(voice, { age: e.target.value, gender: voice.gender })}>
          {AGES.map(a => <option key={a} value={a}>{a}</option>)}
        </select></label>
      <label>Voice gender tag
        <select className="input voice-gender" aria-label={`Gender tag for ${voice.name}`} value={voice.gender}
          onChange={e => onTraits(voice, { age: voice.age, gender: e.target.value })}>
          {GENDERS.map(g => <option key={g} value={g}>{g}</option>)}
        </select></label>
      {wrongPreset && <p className="hint">This Kokoro preset is for {voice.language.toUpperCase()}; choose a {language.toUpperCase()} preset for this dub.</p>}
      <div className="episode-actions">
        <button type="button" className="btn btn-secondary voice-listen" disabled={wrongPreset} onClick={() => onListen(voice)}>Generate sample</button>
        <button type="button" className="btn btn-primary voice-use" disabled={wrongPreset || busy}
          onClick={async () => { setBusy(true); if (!(await onUse(voice))) setBusy(false); }}>Use this voice</button>
      </div>
    </article>
  );
}

// Choose a character voice from the Voicebox catalog, ranked by the
// character's language, age and gender, with a sample before assigning.
export function VoicePicker({ language, category = 'speaker', onSelect, onClose }) {
  const base = baseLanguage(language);
  const dialog = useRef(null);
  const audio = useRef(null);
  const preview = useRef({ version: 0 });
  const initialAge = category.startsWith('elderly') ? 'older' : category.startsWith('child') ? 'child' : category.startsWith('young') ? 'young' : 'unknown';
  const initialGender = category.endsWith('_m') ? 'male' : category.endsWith('_f') ? 'female' : 'unknown';
  const [voices, setVoices] = useState([]);
  const [status, setStatus] = useState('Loading catalog…');
  const [query, setQuery] = useState('');
  const [source, setSource] = useState('all');
  const [age, setAge] = useState(initialAge);
  const [gender, setGender] = useState(initialGender);
  const [onlyLanguage, setOnlyLanguage] = useState(false);
  const [limit, setLimit] = useState(24);
  const [text, setText] = useState(base === 'es' ? 'Al caer la noche, el anciano comenzó a contar su historia.' : 'As night fell, the old man began to tell his story.');
  const [direction, setDirection] = useState(initialAge === 'older'
    ? `Speak as an older ${initialGender === 'male' ? 'man' : initialGender === 'female' ? 'woman' : 'adult'}, with a naturally weathered tone and measured pacing.` : '');
  const [sample, setSample] = useState('');

  useEffect(() => {
    const node = dialog.current;
    const opener = document.activeElement;
    const state = preview.current;
    if (!node.open) node.showModal();
    let alive = true;
    api('voice-catalog').then(result => {
      if (!alive) return;
      setVoices(result.voices);
      setStatus(result.warnings.join('; '));
    }).catch(error => { if (alive) setStatus(error.message); });
    return () => {
      alive = false;
      state.version++;
      opener?.focus?.();
    };
  }, []);

  const rank = v => (languageMatches(v.language, language) ? 8 : 0) + (gender !== 'unknown' && v.gender === gender ? 4 : 0)
    + (age !== 'unknown' && v.age === age ? 8 : 0);
  const q = query.toLowerCase();
  const rows = voices.filter(v => (!q || `${v.name} ${v.description} ${v.engine}`.toLowerCase().includes(q))
    && (source === 'all' || (source === 'profile' ? v.key.startsWith('profile:') : v.engine === source))
    && (!onlyLanguage || languageMatches(v.language, language) || v.kind === 'cloned')
    && (gender === 'unknown' || v.gender === gender || v.gender === 'unknown')
    && (age === 'unknown' || v.age === age || v.age === 'unknown'))
    .sort((a, b) => rank(b) - rank(a) || a.name.localeCompare(b.name));

  async function saveTraits(voice, traits) {
    try {
      await api('voice-catalog/traits', { method: 'PUT', json: { key: voice.key, ...traits } });
      setVoices(list => list.map(v => (v.key === voice.key ? { ...v, ...traits } : v)));
      setStatus('Voice traits saved.');
    } catch (error) { setStatus(error.message); }
  }

  async function listen(voice) {
    const state = preview.current;
    const version = ++state.version;
    audio.current?.pause();
    setSample('');
    setStatus('Generating a short sample…');
    try {
      const url = await previewSample({ key: voice.key, language: base, text,
        direction: QWEN.includes(voice.engine) ? direction : '' }, { current: () => version === state.version });
      if (!url) return;
      setSample(url);
      setStatus(`${voice.name}: sample ready. Listen before assigning.`);
    } catch (error) { if (version === state.version) setStatus(error.message); }
  }

  async function use(voice) {
    try {
      const selected = await api('voice-catalog/select', { method: 'POST', json: { key: voice.key } });
      onSelect({ ...selected, direction: QWEN.includes(selected.engine) ? direction : '' });
      dialog.current?.close();
      return true;
    } catch (error) { setStatus(error.message); return false; }
  }

  return (
    <dialog ref={dialog} className="voice-picker" onClose={onClose}>
      <header>
        <div><h2>Choose a character voice</h2>
          <p className="hint">Saved voices and presets exposed by your Voicebox engines. Age is a listening tag, not an automatic identity estimate.</p></div>
        <button type="button" className="btn btn-ghost picker-close" onClick={() => dialog.current?.close()}>Close</button>
      </header>
      <div className="voice-filters">
        <label>Search<input className="input picker-search" type="search" value={query} onChange={e => { setQuery(e.target.value); setLimit(24); }} /></label>
        <label>Character age
          <select className="input picker-age" aria-label="Character age" value={age} onChange={e => setAge(e.target.value)}>
            {AGES.map(a => <option key={a} value={a}>{a === 'unknown' ? 'Any age' : a}</option>)}
          </select></label>
        <label>Voice gender
          <select className="input picker-gender" aria-label="Voice gender" value={gender} onChange={e => setGender(e.target.value)}>
            {GENDERS.map(g => <option key={g} value={g}>{g === 'unknown' ? 'Any voice' : g}</option>)}
          </select></label>
        <label>Catalog
          <select className="input picker-source" aria-label="Catalog" value={source} onChange={e => setSource(e.target.value)}>
            <option value="all">All voices</option><option value="profile">Saved voices</option>
            <option value="kokoro">Kokoro</option><option value="qwen_custom_voice">Qwen CustomVoice</option>
          </select></label>
        <label><input className="picker-language" type="checkbox" checked={onlyLanguage} onChange={e => setOnlyLanguage(e.target.checked)} /> {language.toUpperCase()} native voices and clones only</label>
      </div>
      <div className="voice-preview">
        <label>Audition text<input className="input picker-text" maxLength={300} value={text} onChange={e => setText(e.target.value)} /></label>
        <label>Delivery direction (Qwen)<input className="input picker-direction" maxLength={500} value={direction}
          placeholder="For example: a low, weathered older voice; calm and thoughtful" onChange={e => setDirection(e.target.value)} /></label>
        <audio ref={audio} controls className="picker-audio" hidden={!sample} src={sample || undefined} />
        <p className="picker-status" role="status">{status}</p>
      </div>
      <div className="voice-results">
        {voices.length > 0 && <>
          <p className="hint">{rows.length} matching voices · {voices.length} in the catalog. Untagged voices remain candidates; audition to confirm the character fit.</p>
          {rows.slice(0, limit).map(v => <VoiceCard key={v.key} voice={v} language={language}
            onTraits={saveTraits} onListen={listen} onUse={use} />)}
        </>}
      </div>
      <button type="button" className="btn btn-secondary picker-more" hidden={rows.length <= limit} onClick={() => setLimit(limit + 24)}>Show more voices</button>
    </dialog>
  );
}
