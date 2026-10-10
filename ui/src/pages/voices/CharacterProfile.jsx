import { useLayoutEffect, useRef, useState } from 'react';
import { Link, useLoaderData } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { ensure, libraryQuery, queryClient } from '../../lib/queries.js';
import { api } from '../../lib/api.js';
import { formatTime, frameUrl, pickFrames, trackLabel, videoUrl } from '../../lib/media.js';
import { voiceFor } from '../../lib/voices.js';
import { MediaPlayer } from '../../components/MediaPlayer.jsx';
import { catalogQuery, invalidateVoices, profileQuery, voiceQuery, voiceTemplatesQuery } from './queries.js';
import { ColourField, HearPanel, UsedPanel, VoiceOrb, VoiceTags } from './VoiceParts.jsx';
import { languageColumns, voicesIn } from '../../lib/research.js';

// One character of a show: the single page for who they are and how they
// sound. It carries the voice they speak with (hear it, change it, shape it),
// where they talk in the analysed episodes (stills and their lines), and
// the description a dub reads (pitch, pace, register…), shown as what is
// known and edited a group at a time. Only the fields you change are saved,
// against the revision you loaded, so an older tab can never erase what it
// does not know; locked fields are never changed by suggestions. Nothing
// here generates speech except "Hear it".

const LEVEL = ['unknown', 'low', 'medium', 'high'];
const GROUPS = [
  ['identity', 'Role', [
    ['identity.role', 'Role', ['unknown', 'lead', 'supporting', 'minor', 'narrator', 'crowd']],
    ['identity.notes', 'Notes', 'text']]],
  ['vocal', 'What the voice is like', [
    ['vocal.perceived_age', 'Sounds', ['unknown', 'child', 'teen', 'young', 'adult', 'older']],
    ['vocal.pitch_range.low', 'Pitch low (Hz)', 'number'], ['vocal.pitch_range.high', 'Pitch high (Hz)', 'number'],
    ['vocal.resonance', 'Resonance', LEVEL], ['vocal.brightness', 'Brightness', ['unknown', 'dark', 'neutral', 'bright']],
    ['vocal.breathiness', 'Breathiness', LEVEL], ['vocal.raspiness', 'Rasp', LEVEL], ['vocal.nasality', 'Nasality', LEVEL],
    ['vocal.articulation', 'Articulation', ['unknown', 'slurred', 'relaxed', 'clear', 'crisp']],
    ['vocal.character_gender', 'Character gender', 'text'],
    ['vocal.vocal_presentation', 'Voice reads as', ['unknown', 'masculine', 'feminine', 'androgynous', 'varies']],
    ['vocal.performer.name', 'Original performer', 'text'], ['vocal.performer.verified', 'Performer verified', 'check'],
    ['vocal.notes', 'Notes', 'text']]],
  ['locale', 'Language and accent', [
    ['locale.desired_locale', 'Dub locale', 'text'], ['locale.accent', 'Accent', 'text'],
    ['locale.languages', 'Languages (comma separated)', 'list']]],
  ['delivery', 'Default delivery', [
    ['delivery.pace', 'Pace', ['unknown', 'slow', 'measured', 'average', 'quick', 'rapid']],
    ['delivery.energy', 'Energy', LEVEL], ['delivery.expressiveness', 'Expressiveness', LEVEL],
    ['delivery.pauses', 'Pauses', ['unknown', 'few', 'some', 'many']],
    ['delivery.speech_register', 'Register', ['unknown', 'casual', 'neutral', 'formal', 'archaic', 'rough']],
    ['delivery.phrasing', 'Phrasing', 'text'], ['delivery.direction', 'Standing direction', 'text']]],
  ['pronunciation', 'Pronunciation and habits', [
    ['pronunciation.catchphrases', 'Catchphrases (comma separated)', 'list'],
    ['pronunciation.speech_register', 'Register notes', 'text'], ['pronunciation.honorifics', 'Honorifics', 'text']]],
  ['dynamics', 'Dynamics', [
    ['dynamics.desired.low', 'Quietest (dB vs. their ordinary)', 'number'],
    ['dynamics.desired.high', 'Loudest (dB vs. their ordinary)', 'number'],
    ['dynamics.max_boost_db', 'Never boost more than (dB)', 'number'], ['dynamics.max_cut_db', 'Never cut more than (dB)', 'number'],
    ['dynamics.envelope_strength', 'Envelope strength (0–1.5)', 'number'],
    ['dynamics.follow_source', 'Follow the original actor', ['unknown', 'follow', 'partly', 'ignore']]]],
];
const VARIATIONS = ['neutral', 'whisper', 'call', 'shout', 'restrained', 'excited', 'sad', 'exhausted', 'nonverbal'];
const VERDICTS = ['untested', 'good', 'acceptable', 'poor', 'failed'];

const get = (obj, path) => path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
const known = value => !(value == null || value === '' || value === 'unknown' || value === false
  || (Array.isArray(value) && !value.length));

export async function characterLoader({ params }) {
  const id = params.key.slice('character:'.length);
  const [data, catalog] = await Promise.all([ensure(profileQuery(id)), ensure(catalogQuery).catch(() => []),
    ensure(voiceTemplatesQuery)]);
  const voice = voiceFor({ ...data.character, voices: data.assignments.map(a => a.voice).filter(Boolean) }, catalog);
  if (voice) await ensure(voiceQuery(voice.key)).catch(() => null);
  return { id };
}

// One field of the description: uncontrolled, read back on save like a form.
function Control({ spec: [path, label, kind], profile, locked }) {
  const value = get(profile, path);
  let input;
  if (Array.isArray(kind)) {
    input = <select className="input" data-field={path} defaultValue={kind.includes(value) ? value : kind[0]}>
      {kind.map(o => <option key={o}>{o}</option>)}</select>;
  } else if (kind === 'check') {
    input = <input type="checkbox" data-field={path} data-kind="check" defaultChecked={Boolean(value)} />;
  } else if (kind === 'number') {
    input = <input className="input m" type="number" step="any" data-field={path} data-kind="number" defaultValue={value ?? ''} />;
  } else if (kind === 'list') {
    input = <input className="input" data-field={path} data-kind="list" defaultValue={(value || []).join(', ')} />;
  } else {
    input = <input className="input" data-field={path} defaultValue={value || ''} />;
  }
  // Two labels side by side, never one inside the other: the field's name must
  // not be read as "… lock", and a click on "lock" must not reach the field.
  return (
    <div className="review-field">
      <label>{label} {input}</label>
      <label className="hint profile-lock" title="Locked fields are never changed by suggestions">
        <input type="checkbox" data-lock={path} defaultChecked={locked(path)} /> lock</label>
    </div>
  );
}

function read(field) {
  const kind = field.dataset.kind;
  if (kind === 'check') return field.checked;
  if (kind === 'number') return field.value === '' ? null : Number(field.value);
  if (kind === 'list') return field.value.split(',').map(s => s.trim()).filter(Boolean);
  return field.value;
}
const fieldState = f => JSON.stringify(f.dataset.kind === 'tri' ? f.value : read(f));

// What is known about the voice, as short facts ("Pace: quick"), group by group.
function Described({ profile, locked }) {
  const groups = GROUPS.map(([id, title, list]) => {
    const facts = list.map(([path, label]) => [path, label, get(profile, path)]).filter(([, , v]) => known(v));
    return facts.length ? (
      <div key={id} className="profile-facts"><span className="hint">{title}</span>
        {facts.map(([path, label, v]) => (
          <span key={path} className={`profile-fact${locked(path) ? ' profile-fact-locked' : ''}`}
            title={locked(path) ? 'Locked: suggestions never change it' : ''}>
            {label}: <strong>{Array.isArray(v) ? v.join(', ') : v === true ? 'yes' : String(v)}</strong></span>
        ))}</div>
    ) : null;
  }).filter(Boolean);
  return groups.length ? groups : <p className="hint">Nothing described yet. Approve suggestions from analysed lines below, or describe the voice yourself.</p>;
}

// The description editor; remounted per profile revision so fields show what was saved.
function DescriptionEditor({ characterId, data, templates, locked, open, onToggle, say, refresh }) {
  const { profile } = data;
  const formRef = useRef(null);
  const original = useRef(new Map());
  useLayoutEffect(() => {
    original.current = new Map([...formRef.current.querySelectorAll('[data-field]')].map(f => [f.dataset.field, fieldState(f)]));
  }, []);

  async function save() {
    const root = formRef.current;
    const set = {};
    root.querySelectorAll('[data-field]').forEach(f => {
      if (fieldState(f) === original.current.get(f.dataset.field)) return;
      const value = f.dataset.kind === 'tri' ? f.value : read(f);
      set[f.dataset.field] = f.dataset.kind === 'tri' ? (value === '' ? null : value === 'yes') : value;
    });
    ['favored', 'discouraged'].forEach(kind => {
      const box = root.querySelector(`[data-templates="${kind}"]`);
      const picked = [...box.selectedOptions].map(o => o.value);
      const before = (profile.templates[kind] || []).map(p => p.template);
      if (JSON.stringify(picked) !== JSON.stringify(before))
        set[`templates.${kind}`] = picked.map(id => (profile.templates[kind] || []).find(p => p.template === id) || { template: id });
    });
    const lock = [], unlock = [];
    root.querySelectorAll('[data-lock]').forEach(b => {
      if (b.checked && !locked(b.dataset.lock)) lock.push(b.dataset.lock);
      if (!b.checked && locked(b.dataset.lock)) unlock.push(b.dataset.lock);
    });
    if (!Object.keys(set).length && !lock.length && !unlock.length) { say('Nothing changed.'); return; }
    try {
      await api(`characters/${encodeURIComponent(characterId)}/profile`, { method: 'PATCH',
        json: { base_revision: profile.revision, set, lock, unlock } });
      await refresh();
      say('Saved.');
    } catch (error) {
      say(error.status === 409 ? 'Someone changed this profile in the meantime. Reload it and apply your change again.' : error.message);
    }
  }

  const triValue = d => (d === true ? 'yes' : d === false ? 'no' : '');
  return (
    <details className="profile-edit" data-profile-edit open={open} onToggle={e => onToggle(e.currentTarget.open)} ref={formRef}>
      <summary>Edit the description</summary>
      {GROUPS.map(([id, title, list]) => (
        <div key={id}><h4 className="voice-subhead">{title}</h4>
          <div className="voice-form">{list.map(spec => <Control key={spec[0]} spec={spec} profile={profile} locked={locked} />)}</div></div>
      ))}
      <h4 className="voice-subhead">Performance range</h4>
      <p className="hint">What you want, what the cast engine can be asked for, and what an audition proved, kept apart.</p>
      <div className="table-wrap"><table className="table"><thead><tr><th>State</th><th>Wanted</th><th>Engine can be asked</th><th>Auditioned</th></tr></thead><tbody>
        {VARIATIONS.map(v => {
          const row = profile.variations[v] || {};
          const support = (data.assignments[0]?.variations || {})[v] || 'unknown';
          return (
            <tr key={v}><td>{v}</td>
              <td><select className="input" data-field={`variations.${v}.desired`} data-kind="tri" defaultValue={triValue(row.desired)}>
                {['', 'yes', 'no'].map(o => <option key={o} value={o}>{o || 'unknown'}</option>)}</select></td>
              <td className="m">{support}</td>
              <td><select className="input" data-field={`variations.${v}.auditioned`}
                defaultValue={VERDICTS.includes(row.auditioned) ? row.auditioned : VERDICTS[0]}>
                {VERDICTS.map(o => <option key={o}>{o}</option>)}</select></td></tr>
          );
        })}</tbody></table></div>
      <h4 className="voice-subhead">Templates</h4>
      <p className="hint">Envelope templates this character tends to suit, or should avoid. Recommendations weigh them; they never force one.</p>
      <div className="voice-form">
        {[['favored', 'Favoured'], ['discouraged', 'Avoid']].map(([kind, label]) => (
          <label key={kind} className="review-field">{label}
            <select className="input" multiple size={5} data-templates={kind}
              defaultValue={(profile.templates[kind] || []).map(p => p.template).filter(id => templates.some(t => t.id === id))}>
              {templates.map(t => <option key={t.id} value={t.id}>{t.title}</option>)}</select></label>
        ))}
      </div>
      <div className="studio-actions"><button type="button" className="btn btn-primary" data-save onClick={save}>Save changes</button>
        <span className="hint">Only what you changed is saved. Saving never generates speech.</span></div>
    </details>
  );
}

// Each analysed episode the character speaks in: how much, stills from
// their lines (click to watch from there) and the lines themselves.
function Appearances({ character, voice }) {
  const { data, error, isPending } = useQuery({ queryKey: ['character', character.id, 'appearances'],
    queryFn: () => api(`characters/${encodeURIComponent(character.id)}/appearances`) });
  const [player, setPlayer] = useState(null);
  if (isPending) return <p className="hint">Reading the analysed episodes…</p>;
  if (error) return <p className="hint">{error.message}</p>;
  if (!data.episodes.length) return <p className="hint">No analysed episode names this character yet. Name their voice on an episode’s Analysis tab.</p>;
  const colour = voice?.color || 'var(--color-accent)';

  async function watch(e, fromCue, button) {
    let found;
    try { found = await api(`analysis/tracks?path=${encodeURIComponent(e.path)}`); } catch (error) { button.title = error.message; return; }
    const clips = e.lines.map(l => {
      const start = Math.max(0, l.start - 0.35);
      const end = Math.min(start + 29.5, l.end + 0.45);
      return { start, end, label: formatTime(l.start), text: l.text, detail: l.original_text || '', cue: l.cue,
        url: stream => videoUrl(e.path, start, end, stream) };
    });
    setPlayer({ title: character.name, colour, subtitle: `${e.episode} · ${e.lines.length} lines, back to back`,
      clips, tracks: found.tracks.map(t => ({ key: t.stream, label: trackLabel(t) })), track: found.default,
      startAt: Math.max(0, e.lines.findIndex(l => l.cue === fromCue)) });
  }

  return (
    <>
      <p className="hint">{data.lines} line{data.lines === 1 ? '' : 's'} in {data.episodes.length} analysed episode{data.episodes.length === 1 ? '' : 's'}.</p>
      {data.episodes.map((e, n) => (
        <div key={n} className="appear-episode">
          <div className="appear-head"><strong>{e.episode || e.revision_id}</strong>
            <span className="hint"><span className="m">{e.lines.length}</span> lines · <span className="m">{formatTime(e.seconds)}</span></span>
            {e.reachable ? <button type="button" className="btn btn-ghost" data-watch-episode={n}
              onClick={ev => watch(e, undefined, ev.currentTarget)}>Watch their lines</button>
              : <span className="hint">video not reachable from here</span>}</div>
          {e.reachable && (
            <div className="cast-frames">{pickFrames(e.lines, {}, 6).map(l => (
              <button key={l.cue} type="button" className="cast-frame" style={{ '--tint': colour }} data-watch-episode={n}
                data-from={l.cue} title={`${formatTime(l.start)} · ${l.text}`} onClick={ev => watch(e, l.cue, ev.currentTarget)}>
                <img src={frameUrl(e.path, (l.start + l.end) / 2)} alt={`The picture at ${formatTime(l.start)}`} width="160" height="90" decoding="async" />
                <span className="cast-frame-time m">{formatTime(l.start)}</span></button>
            ))}</div>
          )}
          <details className="appear-lines"><summary>Their lines</summary><ol>{e.lines.map((l, i) => (
            <li key={i}><span className="m hint">{formatTime(l.start)}</span> {l.text}{l.original_text && <> <span className="hint">{l.original_text}</span></>}</li>
          ))}</ol></details>
        </div>
      ))}
      {player && <MediaPlayer {...player} onClose={() => setPlayer(null)} />}
    </>
  );
}

const LABELS = Object.fromEntries(GROUPS.flatMap(([, , list]) => list.map(([path, label]) => [path, label])));
const fieldLabel = path => LABELS[path] || ({ 'vocal.pitch_range': 'Pitch range', 'dynamics.measured': 'How loud they get (measured)' })[path]
  || (/^variations\.(\w+)\.desired$/.test(path) ? `Wanted: ${path.split('.')[1]}` : path);
function fieldValue(value) {
  if (value === true) return 'yes';
  if (value === false) return 'no';
  if (value && typeof value === 'object' && 'low' in value) {
    const unit = value.units === 'Hz' ? ' Hz' : value.units ? ` ${value.units.replace(' relative', '')}` : '';
    return `${value.low} to ${value.high}${unit}`;
  }
  return Array.isArray(value) ? value.join(', ') : String(value);
}

function Proposals({ characterId, revision, refresh }) {
  const { data, error, isPending } = useQuery({ queryKey: ['character', characterId, 'proposals'],
    queryFn: () => api(`characters/${encodeURIComponent(characterId)}/proposals`) });
  const [picked, setPicked] = useState(new Set());
  const [status, setStatus] = useState('');
  if (isPending) return <p className="hint">Reading analysed lines…</p>;
  if (error) return <p className="hint">{error.message}</p>;
  if (!data.proposals.length) return <p className="hint">{data.lines ? `${data.lines} analysed lines, nothing confident enough to suggest.` : 'No analysed lines are identified as this character yet.'}</p>;
  const toggle = id => { const next = new Set(picked); if (next.has(id)) next.delete(id); else next.add(id); setPicked(next); };
  async function approve() {
    if (!picked.size) return;
    try {
      await api(`characters/${encodeURIComponent(characterId)}/proposals/approve`, { method: 'POST',
        json: { base_revision: revision, ids: [...picked] } });
      setPicked(new Set());
      await refresh();
    } catch (err) { setStatus(err.message); }
  }
  return (
    <>
      <p className="hint">From {data.lines} analysed lines. Tick what you agree with and approve them together.</p>
      {data.proposals.map(p => (
        <label key={p.id} className="profile-proposal"><input type="checkbox" data-proposal={p.id} disabled={p.locked}
          checked={picked.has(p.id)} onChange={() => toggle(p.id)} />
          <span><strong>{p.kind === 'reference' ? `Reference line: ${p.value.kind}, ${p.value.variation}` : fieldLabel(p.field)}</strong>
            {p.kind === 'reference' ? `“${p.value.transcript}”` : fieldValue(p.value)}
            <span className="hint">{p.evidence}{p.locked ? ' · locked, will not change' : ''}</span></span></label>
      ))}
      <button type="button" className="btn btn-secondary" data-approve-all onClick={approve}>Approve selected</button>
      <p className="hint" role="status" data-proposal-status>{status}</p>
    </>
  );
}

function VoiceTraits({ voice, orbRef }) {
  const [traits, setTraits] = useState({ gender: voice.gender || 'unknown', age: voice.age || 'unknown', notes: voice.notes || '' });
  const [colour, setColour] = useState(voice.color || '');
  const [saved, setSaved] = useState('');
  const set = key => e => setTraits({ ...traits, [key]: e.target.value });
  async function save() {
    const body = { key: voice.key, color: colour };
    for (const [k, v] of Object.entries(traits)) body[k] = v.trim();
    try {
      await api('voice-catalog/traits', { method: 'PUT', json: body });
      invalidateVoices();
      setSaved('Saved.');
    } catch (error) { setSaved(error.message); }
  }
  return (
    <>
      <div className="voice-form">
        <label className="review-field">Gender <select className="input" data-t="gender" value={traits.gender} onChange={set('gender')}>
          {['unknown', 'male', 'female', 'neutral'].map(g => <option key={g}>{g}</option>)}</select></label>
        <label className="review-field">Age <select className="input" data-t="age" value={traits.age} onChange={set('age')}>
          {['unknown', 'child', 'young', 'adult', 'older'].map(a => <option key={a}>{a}</option>)}</select></label>
        <ColourField voice={voice} value={colour} onChange={setColour} orbRef={orbRef} />
        <label className="review-field voice-notes">Notes <textarea className="input" data-t="notes" rows={2} maxLength={500}
          value={traits.notes} onChange={set('notes')} /></label>
      </div>
      <div className="studio-actions"><button type="button" className="btn btn-secondary" data-save-voice onClick={save}>Save</button>
        <span className="hint" role="status" data-saved-voice>{saved}</span></div>
    </>
  );
}

function Assign({ character, catalog, onAssign, say }) {
  const [voiceId, setVoiceId] = useState('');
  const [locale, setLocale] = useState('');
  const [variant, setVariant] = useState('');
  function cast() {
    if (!voiceId) { say('Choose a voice first.'); return; }
    const engine = catalog.find(v => v.profile_id === voiceId)?.engine || '';
    onAssign({ voice: voiceId, engine, locale: locale.trim(), variant });
  }
  return (
    <div className="voice-assign">
      <label className="review-field">Voice<select className="input" data-assign-voice value={voiceId} onChange={e => setVoiceId(e.target.value)}>
        <option value="">Choose a voice</option>
        {catalog.map(v => <option key={v.key} value={v.profile_id || ''} disabled={!v.profile_id}>{v.display_name || v.name} · {v.engine || ''}</option>)}
      </select></label>
      <label className="review-field">Locale<input className="input m" data-assign-locale placeholder="any, or es-MX"
        value={locale} onChange={e => setLocale(e.target.value)} /></label>
      <label className="review-field">Variant<select className="input" data-assign-variant value={variant} onChange={e => setVariant(e.target.value)}>
        <option value="">any</option>
        {(character.variants || []).map(v => <option key={v.id} value={v.id}>{v.label}</option>)}</select></label>
      <button type="button" className="btn btn-secondary" data-assign onClick={cast}>Cast</button>
    </div>
  );
}

function Rename({ character, characterId, say, refresh }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(character.name);
  async function submit(e) {
    e.preventDefault();
    const next = name.trim();
    if (!next || next === character.name) { setOpen(false); return; }
    try {
      await api(`characters/${encodeURIComponent(characterId)}`, { method: 'PATCH', json: { base_revision: character.revision, name: next } });
      invalidateVoices();
      await refresh();
      setOpen(false);
    } catch (error) { say(error.message); }
  }
  return (
    <>
      <form className="character-rename" data-rename-form hidden={!open} onSubmit={submit}>
        <input className="input" data-rename value={name} maxLength={80} aria-label="Name" onChange={e => setName(e.target.value)}
          ref={el => { if (el && open) el.focus(); }} />
        <button type="submit" className="btn btn-secondary">Rename</button>
        <button type="button" className="btn btn-ghost" data-rename-cancel onClick={() => setOpen(false)}>Cancel</button></form>
      <RenameOpen onOpen={() => { setName(character.name); setOpen(true); }} />
    </>
  );
}
const RenameOpen = ({ onOpen }) => (
  <button type="button" className="btn btn-ghost character-rename-open" data-rename-open onClick={onOpen}>Rename</button>);

// Who voices the character in published versions, from the linked catalogues.
// Reference for casting only: nothing here picks a voice.
function PublishedVoices({ published }) {
  const voices = published?.voice_actors || [];
  if (!voices.length) return null;
  const languages = languageColumns([{ voice_actors: voices }]);
  return (
    <div data-published-voices>
      <h4 className="voice-subhead">Who voices them in published versions</h4>
      {languages.map(lang => (
        <p key={lang} className="character-published">
          <span className="hint">{lang || 'Language not stated'}:</span>{' '}
          {voicesIn({ voice_actors: voices }, lang).map((v, n) => (
            <span key={v.name + n}>{n ? ', ' : ''}<strong>{v.name}</strong>
              <span className="hint"> per {(v.sources || [published.source]).join(', ')}</span></span>
          ))}
        </p>
      ))}
      <p className="hint">For reference when casting. Doblarr never picks a voice from this.</p>
    </div>
  );
}

export function CharacterProfile() {
  const { id: characterId } = useLoaderData();
  const { data } = useQuery(profileQuery(characterId));
  const { data: catalog = [] } = useQuery(catalogQuery);
  const { data: templates = [] } = useQuery(voiceTemplatesQuery);
  const { data: library } = useQuery(libraryQuery);
  const [status, setStatus] = useState('');
  const [editing, setEditing] = useState(false);
  const orbRef = useRef(null);
  const { character, profile } = data;
  const cast = data.assignments.map(a => a.voice).filter(Boolean);
  const voice = voiceFor({ ...character, voices: cast }, catalog);
  const { data: voiceData } = useQuery({ ...voiceQuery(voice?.key || ''), enabled: Boolean(voice) });
  const firm = Boolean(voice && cast.includes(voice.profile_id));
  const fields = profile.meta?.fields || {};
  const locked = path => Boolean(fields[path]?.locked);
  const show = (library?.items || []).find(i => `show:tvdb:${i.tvdb_id}` === character.series_id
    || `movie:tmdb:${i.tmdb_id}` === character.series_id);
  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ['character', characterId] }),
    queryClient.invalidateQueries({ queryKey: ['voice-catalog'] })]);

  async function assign(body) {
    try {
      await api(`characters/${encodeURIComponent(characterId)}/assignments`, { method: 'PUT', json: body });
      invalidateVoices();
      await refresh();
      setStatus('Cast. Lines already rendered keep their voice until you re-render them.');
    } catch (error) { setStatus(error.message); }
  }
  async function retouchReference(ref, change) {
    try {
      await api(`characters/${encodeURIComponent(characterId)}/references/${encodeURIComponent(ref)}`, {
        method: 'PATCH', json: { base_revision: profile.revision, ...change } });
      await refresh();
    } catch (error) { setStatus(error.message); }
  }

  return (
    <div className="page" id="voicesRoot">
      <p><Link to="/voices">← All voices</Link></p>
      <div className="voice-hero">
        <div className="voice-orb-wrap">{voice ? <VoiceOrb voice={voice} orbRef={orbRef} /> : <span className="voice-orb-empty" aria-hidden="true" />}</div>
        <div className="voice-hero-body">
          <span className="card-kicker">Character{show ? ` · ${show.title}` : ''}</span>
          <h2 data-name>{character.name}</h2>
          {character.aliases?.length > 0 && <p className="hint">Also called {character.aliases.join(', ')}.</p>}
          <p className="character-voice-line">{voice ? (
            <>Speaks with <strong>{voice.display_name || voice.name}</strong> <span className="hint">{voice.kind === 'preset' ? 'preset' : 'clone'} · {voice.engine || ''}{voice.language ? ` · ${voice.language}` : ''}</span>
              {!firm && <button type="button" className="btn btn-ghost" data-make-firm title="This voice carries the character’s name; casting it makes dubs use it"
                onClick={() => assign({ voice: voice.profile_id, engine: voice.engine || '' })}>Cast it</button>}</>
          ) : <span className="hint">No voice yet. Choose one under Voice below.</span>}</p>
          <VoiceTags voice={voice} />
          {voice?.notes && <p>{voice.notes}</p>}
          <Rename key={character.revision} character={character} characterId={characterId} say={setStatus} refresh={refresh} />
        </div>
      </div>
      <p className="hint" role="status" data-status>{status}</p>
      {voice && <HearPanel voice={voice} orbRef={orbRef} />}

      <section className="panel voice-panel" aria-labelledby="charTalks"><h3 id="charTalks">Where they talk</h3>
        <div data-appearances><Appearances character={character} voice={voice} /></div></section>

      <section className="panel voice-panel" aria-labelledby="charVoice"><h3 id="charVoice">Voice</h3>
        {voice && <VoiceTraits key={voice.key} voice={voice} orbRef={orbRef} />}
        <h4 className="voice-subhead">Which voice, per language</h4>
        {data.assignments.length ? (
          <div className="table-wrap"><table className="table"><thead><tr><th>Locale</th><th>Variant</th><th>Voice</th><th>Engine can do</th><th></th></tr></thead><tbody>
            {data.assignments.map((a, n) => (
              <tr key={n}><td className="m">{a.locale || 'any'}</td><td>{a.variant || 'any'}</td>
                <td>{catalog.find(v => v.profile_id === a.voice || v.key === a.voice)?.display_name || catalog.find(v => v.profile_id === a.voice)?.name || a.voice}</td>
                <td className="hint">{(a.capabilities?.supported_controls || []).join(', ') || 'unknown'}{a.capabilities?.unsupported?.length ? `; cannot: ${a.capabilities.unsupported.join(', ')}` : ''}</td>
                <td><button type="button" className="btn btn-ghost" data-unassign={a.locale || ''} data-variant={a.variant || ''}
                  onClick={() => assign({ clear: true, locale: a.locale || '', variant: a.variant || '' })}>Remove</button></td></tr>
            ))}</tbody></table></div>
        ) : <p className="hint">{voice ? 'Not cast yet: the voice above is matched by its name only.' : 'No voice cast yet.'}</p>}
        <PublishedVoices published={character.published} />
        <Assign character={character} catalog={catalog} onAssign={assign} say={setStatus} />
      </section>
      {voice && <UsedPanel used={voiceData?.used_in || []} />}

      <section className="panel voice-panel" aria-labelledby="charDescribe"><h3 id="charDescribe">How they sound</h3>
        <Described profile={profile} locked={locked} />
        <DescriptionEditor key={profile.revision} characterId={characterId} data={data} templates={templates} locked={locked}
          open={editing} onToggle={setEditing} say={setStatus} refresh={refresh} />
        <h4 className="voice-subhead">Suggestions from analysed lines</h4>
        <div data-proposals><Proposals characterId={characterId} revision={profile.revision} refresh={refresh} /></div>
      </section>

      <section className="panel voice-panel" aria-labelledby="charRefs"><h3 id="charRefs">References</h3>
        <p className="hint">Lines of the original actor a clone learns from. {data.references.approved} approved{data.references.neutral ? '' : ', no neutral one yet'}{data.references.expressive ? '' : ', no expressive one yet'}.</p>
        {profile.references.length > 0 && (
          <div className="table-wrap"><table className="table"><thead><tr><th>Kind</th><th>Line</th><th>Quality</th><th>State</th><th></th></tr></thead><tbody>
            {profile.references.map(r => (
              <tr key={r.id}><td>{r.kind} · {r.variation}</td>
                <td>{r.transcript || ''}<br /><span className="hint m">{r.source.start.toFixed(1)}–{r.source.end.toFixed(1)} s · {r.provenance || ''}</span></td>
                <td className="hint">{r.quality.duration ? `${r.quality.duration} s` : ''}{r.quality.relative_db != null ? ` · ${r.quality.relative_db > 0 ? '+' : ''}${r.quality.relative_db} dB` : ''}</td>
                <td>{r.retired ? 'retired' : r.approved ? 'approved' : 'not approved'}</td>
                <td>{!r.retired && <>
                  <button type="button" className="btn btn-ghost" data-ref={r.id} onClick={() => retouchReference(r.id, { approved: !r.approved })}>{r.approved ? 'Unapprove' : 'Approve'}</button>
                  <button type="button" className="btn btn-ghost" data-ref={r.id} data-retire="true" onClick={() => retouchReference(r.id, { retired: true })}>Retire</button></>}</td></tr>
            ))}</tbody></table></div>
        )}
        {profile.auditions.length > 0 && <><h4 className="voice-subhead">Auditions</h4>
          {profile.auditions.map((a, n) => <p key={n} className="hint">{a.at} · {a.language} · {a.verdict} · {a.context || ''}</p>)}</>}
      </section>
    </div>
  );
}
