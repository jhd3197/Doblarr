import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { CharacterPicker } from '../../components/CharacterPicker.jsx';
import { thumbUrl } from './analysis.js';

// What the episode analysis knows beyond its lines: which stages ran and which
// are stale, failed or not supported here; older names waiting to be moved
// onto this episode's identity; title knowledge extraction; and the optional
// visual evidence (faces on screen, who appears to speak). None of it
// translates, clones or generates speech.

const STATE = { done: 'done', stale: 'needs a rerun', failed: 'failed', unsupported: 'not supported here',
  skipped: 'off', missing: 'not run', running: 'running' };
const STAGE = { probe: 'Read the file', separate: 'Separate dialogue', transcribe: 'Lines', diarize: 'Group voices',
  measure: 'Levels', baselines: 'Speaker baselines', analyze: 'Pitch and words', features: 'Energy curves',
  speaker_memory: 'Teach the show', emotion: 'How lines are said', dub_text: 'Official dub wording', shots: 'Shots', faces: 'Faces', tracks: 'Face tracks',
  active_speaker: 'Mouth movement', association: 'Who speaks (visual)', scenes: 'Scenes', knowledge: 'Title knowledge' };
const SCREEN = { 'onscreen-speaking': 'speaking on screen', 'onscreen-silent': 'face on screen, not speaking',
  offscreen: 'nobody on screen', unknown: 'not analysed' };
const GROUPS = [['audio', 'Audio'], ['visual', 'Picture'], ['emotion', 'Emotion'], ['knowledge', 'Knowledge']];
const KIND = { names: 'Episode names', prints: 'Show voice memory', traits: 'Voice identity', cast: 'Title cast',
  series_link: 'Same show?' };

// What the picture says about each line, for the line rows: cue -> text.
export function screenNotes(visual) {
  if (!visual?.analysed) return {};
  const notes = {};
  for (const row of visual.associations || []) {
    let text = SCREEN[row.screen] || row.screen;
    if (row.decision?.state === 'proposal') text += ` · picture suggests ${row.decision.name || 'another character'}`;
    if (row.conflicts?.length) text += ' · voice and face disagree';
    notes[row.cue] = text;
  }
  return notes;
}

function Migration({ reload, say }) {
  const { data: plan, error } = useQuery({ queryKey: ['analysis-migration'], queryFn: () => api('analysis/migration'), staleTime: 0 });
  const [picked, setPicked] = useState(null);            // null: the ready ones
  if (error) return <p className="hint">{error.message}</p>;
  if (!plan) return <p className="hint">Checking older names…</p>;
  const checked = picked ?? Object.fromEntries(plan.actions.filter(a => a.state === 'ready').map(a => [a.key, true]));
  const setChecked = setPicked;
  const actionable = plan.actions.filter(a => ['ready', 'confirm'].includes(a.state));
  async function apply() {
    const keys = Object.keys(checked).filter(k => checked[k]);
    try {
      const result = await api('analysis/migration', { method: 'POST', json: { fingerprint: plan.fingerprint, keys } });
      say(`Moved ${result.applied.length} item${result.applied.length === 1 ? '' : 's'}.`);
      reload();
    } catch (e) { say(e.message); }
  }
  return (
    <>
      <p className="hint">Nothing changes until you apply. Ambiguous items stay as they are.</p>
      <div className="table-wrap"><table className="table"><thead><tr><th /><th>What</th><th>State</th><th>Why</th></tr></thead><tbody>
        {plan.actions.map(a => (
          <tr key={a.key}>
            <td>{['ready', 'confirm'].includes(a.state) && <input type="checkbox" data-key={a.key} checked={Boolean(checked[a.key])}
              onChange={e => setChecked({ ...checked, [a.key]: e.target.checked })} />}</td>
            <td>{KIND[a.kind] || a.kind} <span className="hint">{a.key.split(':').slice(1).join(':').slice(0, 60)}</span></td>
            <td>{a.state}</td><td className="hint">{a.reason || ''}</td>
          </tr>
        ))}
      </tbody></table></div>
      <button type="button" className="btn btn-secondary" disabled={!actionable.length} onClick={apply}>Apply selected</button>
    </>
  );
}

function Coverage({ data, path, target, reload, say }) {
  const [migrating, setMigrating] = useState(false);
  const coverage = data.coverage || [];
  const rerunnable = data.rerunnable || [];
  async function queue(body, message) {
    try {
      await api('analysis/rerun', { method: 'POST', json: { path, target_lang: target, ...body } });
      say(message);
      setTimeout(reload, 1500);
    } catch (error) { say(error.message); }
  }
  async function extract() {
    try {
      const result = await api('narrative/extract', { method: 'POST', json: { path, target_lang: target } });
      say(`Extracting with ${result.model}. Review the proposals under Knowledge → Title knowledge.`);
    } catch (error) { say(error.message); }
  }
  return (
    <details className="analysis-coverage" open={rerunnable.length > 0 || data.names_from === 'legacy' || undefined}>
      <summary>What has been analysed <span className="hint">{coverage.filter(c => c.state === 'done').length} of {coverage.length} stages done{rerunnable.length > 0 && ` · ${rerunnable.length} to rerun`}</span></summary>
      {!data.identity && <p className="hint">This file cannot be read from here, so it cannot be identified by its content. Names are read the older way, by file name, until it is reachable.</p>}
      {['name', 'ambiguous'].includes(data.script_match) && <p className="hint">This analysis was found by file name only{data.script_match === 'ambiguous' && ', and other files share that name'}. Analyse this file again to tie it to its content.</p>}
      {GROUPS.map(([group, label]) => (
        <div key={group} className="analysis-stage-group"><strong>{label}</strong>
          {coverage.filter(c => c.group === group).map(c => (
            <span key={c.stage} className={`analysis-stage analysis-stage-${c.state}`} title={c.reason || ''}>{STAGE[c.stage] || c.stage}: {STATE[c.state] || c.state}</span>
          ))}
        </div>
      ))}
      <div className="studio-actions">
        {rerunnable.length > 0 && <button type="button" className="btn btn-secondary"
          onClick={() => queue({ stages: rerunnable }, `Rerunning ${rerunnable.join(', ')}. Earlier stages are reused.`)}>Rerun {rerunnable.length} stage{rerunnable.length === 1 ? '' : 's'}</button>}
        <button type="button" className="btn btn-ghost" onClick={() => queue({ stages: ['shots', 'faces', 'tracks', 'active_speaker', 'association', 'scenes'], visual: true },
          'Analysing the picture. Faces are evidence for you to check, never names on their own.')}>Analyse the picture</button>
        <button type="button" className="btn btn-ghost" onClick={() => queue({ stages: ['emotion'] },
          'Reading how each line is said: a still of every line with its words, and the voice. Takes about half a minute per minute of dialogue.')}>Read the emotions</button>
        <button type="button" className="btn btn-ghost" onClick={extract}>Extract title knowledge</button>
        {data.names_from === 'legacy' && <button type="button" className="btn btn-ghost" onClick={() => setMigrating(true)}>Move older names onto this episode</button>}
      </div>
      <p className="hint">Picture analysis and knowledge extraction are optional. Knowledge extraction asks a language model to read the lines; nothing it proposes is used until you review it under Knowledge.</p>
      <div data-migration>{migrating && <Migration reload={reload} say={say} />}</div>
    </details>
  );
}

function Faces({ data, path, visual, reload, say }) {
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState(false);
  if (!visual) return null;
  if (!visual.analysed) {
    const why = visual.capability?.reasons?.length ? ` Not available here: ${visual.capability.reasons.join('; ')}.` : '';
    return <p className="hint">The picture has not been analysed.{why}</p>;
  }
  // Named and proposed faces first, then the longest appearances: a person
  // names the faces that matter, not every passing glimpse.
  const rank = t => (t.assigned ? 0 : t.matches?.some(m => m.proposed) ? 1 : 2);
  const ordered = (visual.tracks || []).filter(t => t.frames >= 2)
    .sort((a, b) => rank(a) - rank(b) || b.frames - a.frames);
  const tracks = (all ? ordered : ordered.slice(0, 24)).sort((a, b) => rank(a) - rank(b) || a.start - b.start);
  async function assign(track, name) {
    try {
      await api('analysis/visual/tracks', { method: 'POST', json: { path, assign: { [track]: name } } });
      say(name ? `That face is ${name} now, and a reference for the show.` : 'Marked as not a character.');
      reload();
    } catch (error) { say(error.message); }
  }
  return (
    <details className="analysis-visual" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
      <summary>Faces on screen <span className="hint">{visual.tracks.length} face tracks · {visual.shots} shots · {visual.scenes.length} scenes · {(visual.backend?.detectors || []).join(', ')}</span></summary>
      <p className="hint">A face track is the same face across a few frames. Naming one keeps it as a reference for this show; the picture never names a line by itself, and an off-screen voice is normal.</p>
      <div className="analysis-faces">{tracks.map(t => {
        const proposed = !t.assigned && t.matches?.find(m => m.proposed);
        return (
          <figure key={t.id} className="analysis-face" data-track={t.id}>
            {t.thumbnail && <img src={thumbUrl(path, t.thumbnail)} alt={`Face at ${t.start.toFixed(1)} s`} width="72" height="72" />}
            <figcaption><span className="m">{t.start.toFixed(1)}–{t.end.toFixed(1)} s</span>
              <span>{t.character_name || (t.assigned ? 'unknown (by hand)' : '')}</span>
              {proposed && <span className="hint">looks like {proposed.name} ({proposed.similarity.toFixed(2)})</span>}
              <CharacterPicker label="Who is this" placeholder="Name this face" cast={data.cast || []}
                onPick={name => name && assign(t.id, name)} />
              <button type="button" className="btn btn-ghost" onClick={() => assign(t.id, '')}>Not a character</button></figcaption>
          </figure>
        );
      })}</div>
      {ordered.length > tracks.length && <button type="button" className="btn btn-ghost" onClick={() => { setAll(true); setOpen(true); }}>Show all {ordered.length} faces</button>}
    </details>
  );
}

export function Evidence(props) {
  return (
    <>
      <div data-evidence-top><Coverage {...props} /></div>
      <div data-visual><Faces {...props} /></div>
    </>
  );
}

// Lines ticked in the list, given to a character in one go.
export function SelectionBar({ data, path, selected, clear, reload, say }) {
  const cues = [...selected];
  return (
    <div className="analysis-selection" data-selection hidden={!cues.length}>
      <span className="hint" data-count>{cues.length} line{cues.length === 1 ? '' : 's'} selected</span>
      <CharacterPicker label="Give the selected lines to" placeholder="Pick or type a name" cast={data.cast || []}
        onPick={async name => {
          if (!cues.length || !name) return;
          try {
            await api('analysis/lines', { method: 'PUT', json: { path, cues, character: name } });
            say(`${cues.length} line${cues.length === 1 ? '' : 's'} given to ${name}; speaker baselines were recomputed.`);
            clear();
            reload();
          } catch (error) { say(error.message); }
        }} />
    </div>
  );
}
