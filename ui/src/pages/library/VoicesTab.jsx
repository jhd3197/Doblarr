import { useState } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { lighten } from '../../lib/voices.js';
import { configQuery, queryClient } from '../../lib/queries.js';
import { isShow } from '../../lib/library.js';
import { castQuery, identityBody, voicesQuery } from './queries.js';
import { VoicePicker } from './VoicePicker.jsx';
import { Loading, EmptyState } from '../../components/States.jsx';

const ROLE_LABELS = { speaker: 'Unknown speaker', narrator: 'Narrator', child_f: 'Girl', child_m: 'Boy',
  young_f: 'Young woman', young_m: 'Young man', adult_f: 'Adult woman', adult_m: 'Adult man',
  elderly_f: 'Older woman', elderly_m: 'Older man' };
const ROLES = Object.keys(ROLE_LABELS);
const ENGINES = ['chatterbox', 'qwen', 'qwen_custom_voice', 'kokoro'];
const QWEN = ['qwen', 'qwen_custom_voice'];
const BANDS = [['quiet', 'quiet'], ['calm', 'calm'], ['intense', 'intense'], ['unmeasured', 'not measured']];

const configValue = (config, dotted) =>
  dotted.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), config);

function clock(seconds) {
  const s = Math.round(seconds);
  return s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}

// A show's characters across every episode Doblarr has a script for: how much
// each one talks, how they talk (quiet / calm / intense), their most intense
// lines (what an energetic voice is trained on), and the voice they have.
function ShowVoices({ item }) {
  const { data, error, isPending } = useQuery({ queryKey: ['series-voices', item.tvdb_id], queryFn: () => api(`series/${item.tvdb_id}/voices`) });
  if (isPending) return <p className="hint">Reading the scripts of this show’s episodes…</p>;
  if (error) return <p className="hint">{error.message}</p>;
  if (!data.speakers.length) {
    return <p className="hint">No episode of {data.title} has been run yet, so nobody has been heard.
      Dub or audition an episode and its characters appear here with how much each one talks.</p>;
  }
  const lead = data.speakers[0].share || 1;
  return (
    <>
      <p className="hint">From {data.analysed.length} of {data.episode_count} episode{data.episode_count === 1 ? '' : 's'}
        {' '}({data.analysed.map(e => e.label).join(', ')}) · {clock(data.total_seconds)} of dialogue. {data.note}</p>
      <div className="cast-share" role="table" aria-label="Characters by share of dialogue">
        {data.speakers.map(r => (
          <div key={r.speaker} className="cast-share-row" role="row">
            <div className="cast-share-who" role="cell">
              <strong>{r.voice?.color && <span className="voice-dot"
                style={{ background: `radial-gradient(circle at 35% 30%, #fff 0%, ${lighten(r.voice.color)} 40%, ${r.voice.color} 100%)` }} />}
                {r.voice?.name || r.label || r.speaker}</strong>
              <span className="hint m">{r.speaker} · {r.lines} line{r.lines === 1 ? '' : 's'} · {r.episodes.length} ep</span>
            </div>
            <div className="cast-share-bar" role="cell" aria-label={`${(r.share * 100).toFixed(1)}% of dialogue`}>
              <span className="cast-share-fill" style={{ width: `${Math.max(2, (r.share / lead) * 100).toFixed(1)}%`,
                ...(r.voice?.color ? { background: r.voice.color } : {}) }} />
              <span className="cast-share-pct m">{(r.share * 100).toFixed(1)}%</span>
              <span className="hint m">{clock(r.seconds)}</span>
            </div>
            <div className="cast-bands" role="cell">
              {BANDS.filter(([k]) => r.bands[k] > 0).map(([k, label]) => (
                <span key={k} className={`cast-band cast-band-${k}`} style={{ flex: r.bands[k] }}
                  title={`${label}: ${Math.round(r.bands[k] * 100)}%`}>{r.bands[k] >= 0.12 ? label : ''}</span>
              ))}
            </div>
            <div className="cast-share-voice" role="cell">
              {r.voice ? <Link to={`/voices/${encodeURIComponent(r.voice.key)}`} data-voice={r.voice.key}>{r.voice.name || 'Open voice'}</Link>
                : <span className="hint">no voice yet</span>}
            </div>
            {r.highlights.length > 0 && (
              <details className="cast-highlights">
                <summary className="hint">{r.highlights.length} intense line{r.highlights.length === 1 ? '' : 's'} to train on</summary>
                <ul>{r.highlights.map((h, i) => <li key={i}><span className="m">{h.episode_label} {clock(h.start)} · +{h.relative_db} dB</span> {h.text}</li>)}</ul>
              </details>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

function Narrator({ item, targets, planState }) {
  const { plan, save } = planState;
  const { data: config } = useQuery(configQuery);
  const voices = useQuery(voicesQuery);
  const value = key => plan?.[key] ?? configValue(config, key) ?? '';
  const [voice, setVoice] = useState(() => value('dub.narrator_voice'));
  const [engine, setEngine] = useState(() => value('voicebox.default_engine'));
  const [delivery, setDelivery] = useState(() => value('dub.narrator_delivery'));
  const [extra, setExtra] = useState([]);
  const [said, setStatus] = useState(null);
  const [picking, setPicking] = useState(false);
  // What the voice list says until an action says something newer.
  const status = said ?? (voices.error ? voices.error.message : voices.data
    ? voices.data.warning || (!voices.data.voices?.length ? 'No saved voices found. Create a voice in Voicebox first.' : '') : '');

  const listed = voices.data?.voices || [];
  const options = [...listed.map(v => [v.id, v.name]), ...extra.filter(([id]) => !listed.some(v => v.id === id))];
  if (voice && !options.some(([id]) => id === voice)) options.push([voice, voices.data ? `${voice} (not currently listed)` : voice]);
  const engines = ENGINES.includes(engine) || !engine ? ENGINES : [...ENGINES, engine];

  async function saveNarrator() {
    const text = delivery.trim();
    if (text && !QWEN.includes(engine)) {
      setStatus('Choose a Qwen engine to use delivery direction, or leave it empty.'); return;
    }
    setStatus('Saving…');
    setStatus(await save({ ...plan, 'dub.narrator_voice': voice, 'dub.narrator_delivery': text, 'voicebox.default_engine': engine }));
  }

  return (
    <div className="narrator-panel">
      <h3>Narrator voice</h3>
      <p className="hint">Used for a single narrator or a speaker marked Narrator. An explicit character voice takes priority. Multiple detected speakers keep their own voices.</p>
      <div className="narrator-fields">
        <label>Voice
          <select className="input" id="narratorVoice" aria-label="Voice" value={voice} onChange={e => setVoice(e.target.value)}>
            <option value="">Clone from the original audio</option>
            {options.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
        </label>
        <label>Voice engine
          <select className="input" id="narratorEngine" aria-label="Voice engine" value={engine} onChange={e => setEngine(e.target.value)}>
            {engines.map(e => <option key={e} value={e}>{e}</option>)}
          </select>
        </label>
        <label className="narrator-direction">Delivery direction
          <input className="input" id="narratorDelivery" maxLength={500} placeholder="Warm, calm storytelling with gentle pauses"
            value={delivery} onChange={e => setDelivery(e.target.value)} />
        </label>
      </div>
      <p className="hint">Delivery direction requires Qwen and a compatible voice. Engine selection applies to this title&apos;s new jobs. Create or clone additional voices in Voicebox, then refresh this list.</p>
      <div className="episode-actions">
        <button type="button" className="btn btn-primary" id="narratorSave" onClick={saveNarrator}>Save narrator</button>
        <button type="button" className="btn btn-secondary" id="narratorBrowse" onClick={() => setPicking(true)}>Browse all voices &amp; samples</button>
        <button type="button" className="btn btn-ghost" id="narratorRefresh" onClick={() => { setStatus(null); voices.refetch(); }}>Refresh voices</button>
        <span id="narratorStatus" role="status">{status}</span>
      </div>
      {picking && <VoicePicker language={plan?.target_lang || targets[0] || 'en'} onClose={() => setPicking(false)}
        onSelect={picked => {
          setExtra(list => [...list, [picked.profile_id, picked.name]]);
          setVoice(picked.profile_id);
          setEngine(picked.engine);
          setDelivery(picked.direction || '');
          setStatus('Voice selected. Save narrator to apply it to new jobs.');
        }} />}
    </div>
  );
}

function CastEditor({ item, targets, plan }) {
  const castQ = useQuery(castQuery(item));
  const voices = useQuery(voicesQuery);
  // Edits start from the saved cast; until the first edit the query is the truth.
  const [edits, setEdits] = useState(null);
  const [extra, setExtra] = useState([]);
  const [said, setStatus] = useState('');
  const [picking, setPicking] = useState(null);
  const cast = edits ?? (castQ.data ? castQ.data.cast || [] : castQ.error ? [] : null);
  const status = said || (castQ.error ? 'Cast unavailable — is the API reachable?' : '');

  const known = [...(voices.data?.voices || []), ...extra];
  const update = (i, patch) => setEdits(cast.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  async function saveCast() {
    setStatus('Saving…');
    try {
      await api('api/cast', { method: 'PUT', json: { title: item.title, cast, ...identityBody(item) } });
      queryClient.setQueryData(castQuery(item).queryKey, { ...(castQ.data || {}), cast });
      setStatus('Saved ✓');
    } catch (err) {
      setStatus('Save failed: ' + err.message);
    }
  }

  let rows;
  if (cast === null) rows = null;
  else if (!cast.length) {
    rows = <EmptyState>No speakers discovered for this file yet. Assign a narrator above, or run an episode audition to discover its speakers.</EmptyState>;
  } else {
    rows = cast.map((e, i) => (
      <div key={e.speaker_id || i} className="cast-row">
        <label className="cast-name-field">Character name
          <input className="input cast-name" value={e.label || ''} onChange={ev => update(i, { label: ev.target.value })} /></label>
        <label>Role
          <select className="input cast-category" value={e.category} onChange={ev => update(i, { category: ev.target.value })}>
            {ROLES.map(c => <option key={c} value={c}>{ROLE_LABELS[c] || c}</option>)}
          </select></label>
        <span className="m cast-speaker">{e.speaker_id}</span>
        <label>Voice
          <select className="input cast-voice" value={e.voice || ''} onChange={ev => {
            const engine = known.find(v => v.id === ev.target.value)?.engine || '';
            update(i, { voice: ev.target.value, engine, ...(engine && !QWEN.includes(engine) ? { delivery: '' } : {}) });
          }}>
            <option value="">Use narrator default / clone original</option>
            {e.voice && !known.some(v => v.id === e.voice) && <option value={e.voice}>{e.voice} (not currently listed)</option>}
            {known.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}
          </select></label>
        <button type="button" className="btn btn-secondary cast-browse" onClick={() => setPicking(i)}>Find matching voice</button>
        <label>Delivery (Qwen)
          <input className="input cast-delivery" maxLength={500} value={e.delivery || ''} onChange={ev => update(i, { delivery: ev.target.value })} /></label>
      </div>
    ));
  }

  return (
    <>
      <div id="titleCastRows" className="cast-rows">{rows}</div>
      <div className="cast-foot">
        <span id="titleCastStatus" className="cast-status">{status}</span>
        <button type="button" className="btn btn-primary" id="titleCastSave" disabled={!cast?.length} onClick={saveCast}>Save cast</button>
      </div>
      {picking !== null && cast?.[picking] && (
        <VoicePicker language={plan?.target_lang || targets[0] || 'en'} category={cast[picking].category}
          onClose={() => setPicking(null)}
          onSelect={picked => {
            update(picking, { voice: picked.profile_id, engine: picked.engine, delivery: picked.direction || '' });
            if (!known.some(v => v.id === picked.profile_id)) setExtra(list => [...list, { id: picked.profile_id, name: picked.name }]);
            setStatus('Voice selected. Save cast to apply.');
          }} />
      )}
    </>
  );
}

export function VoicesTab({ item, targets, planState }) {
  const show = isShow(item);
  const scope = show
    ? 'Choose Voices on an episode to edit its discovered cast. Narrator defaults apply to new jobs throughout this show.'
    : item.parent ? `Character cast: S${String(item.season).padStart(2, '0')}E${String(item.episode_number).padStart(2, '0')} · saved for this episode`
      : 'Character cast for this movie';
  return (
    <>
      {show && item.tvdb_id && (
        <div className="panel title-panel title-panel-gap">
          <h3>Characters</h3>
          <div id="showVoices" className="show-voices"><ShowVoices item={item} /></div>
        </div>
      )}
      <div className="panel title-panel">
        <div className="title-panel-head">
          <h3>Speakers and voices</h3>
          <p className="title-panel-sub">Choose a narrator below, or edit voices discovered by an audition. Speaker labels identify this episode only; they do not prove character identity.</p>
        </div>
        <div id="narratorControls">
          {/* Controls shown before the plan arrives would let a choice be made
              and then silently replaced by the saved plan. */}
          {planState.plan ? <Narrator item={item} targets={targets} planState={planState} />
            : <Loading>Loading this title’s plan…</Loading>}
        </div>
        <p id="castScope" className="hint">{scope}</p>
        <CastEditor item={item} targets={targets} plan={planState.plan} />
      </div>
    </>
  );
}
