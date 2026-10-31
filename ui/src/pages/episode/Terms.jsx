import { useState } from 'react';
import { api } from '../../lib/api.js';
import { formatTime } from '../../lib/media.js';

// The names and terms an episode keeps saying (doblarr/key_terms.py): how this
// dub says each one, how the official dub in the same language says it when
// the file has one, and the wording kept for the show. A wording kept here is
// the show's: every later translation uses it, and a line that says the term
// another way is rewritten.


function drifts(row) {
  const ours = row.ours;
  return Boolean(ours && (ours.share < 1 || ours.variants.length));
}

function Side({ side, count }) {
  if (!side || !side.rendering) return <span className="hint">—</span>;
  const held = Math.round(side.share * count);
  return (
    <>
      {side.rendering}{held < count && <> <span className="hint m">{held}/{count}</span></>}
      {side.variants.map(v => (
        <span key={v.rendering} className="terms-variant" title={`${v.cues.length} line${v.cues.length === 1 ? '' : 's'}`}>{v.rendering}</span>
      ))}
    </>
  );
}

export function Terms({ data, path, reload, say }) {
  const terms = data.terms;
  const [open, setOpen] = useState({});
  const [edits, setEdits] = useState({});
  // A reload brings the saved wordings; typed ones give way to them.
  const [seen, setSeen] = useState(terms);
  if (seen !== terms) { setSeen(terms); setEdits({}); }
  if (!terms || (!terms.rows.length && !terms.other_saved.length)) return null;

  const rows = [...terms.rows].sort((a, b) =>
    (drifts(b) - drifts(a)) || ((a.kind === 'name') - (b.kind === 'name')) || (b.count - a.count));
  const drifting = rows.filter(drifts).length;
  const kept = rows.filter(r => r.saved).length + terms.other_saved.length;
  const official = terms.official?.title;
  const valueOf = (row, n) => edits[n] ?? (row.saved?.rendering || row.ours?.rendering || row.dub?.rendering || '');

  async function keep(source, rendering) {
    try {
      await api('analysis/terms', { method: 'PUT', json: { path, source, rendering, locale: terms.locale } });
      await reload();
      say(rendering ? `“${source}” is “${rendering}” for the whole show now.` : `“${source}” has no set wording now.`);
    } catch (error) { say(error.message); }
  }

  return (
    <details className="analysis-terms" open={drifting > 0 || undefined}>
      <summary>Names and terms <span className="hint">{rows.length} said more than once{drifting > 0 && <> · <strong>{drifting}</strong> said more than one way</>}{kept > 0 && ` · ${kept} kept for the show`}</span></summary>
      <p className="hint">Words the episode keeps saying. Keep one wording for the show and every episode uses it; lines that say it another way are rewritten when the dub is made.{official && ` The official ${official} dub is transcribed beside ours: its translators saw the picture, so take its wording as a suggestion.`}{terms.show ? '' : ' This episode is not linked to a show yet, so nothing can be kept.'}</p>
      <div className="terms-table" role="table" aria-label="Names and terms">
        <div className="terms-row terms-headrow" role="row"><span role="columnheader">Says</span><span role="columnheader">Our dub</span>{official && <span role="columnheader">{official} dub</span>}<span role="columnheader">For the show</span></div>
        {rows.map((row, n) => (
          <div key={row.term} className={`terms-row${drifts(row) ? ' terms-drift' : ''}${official ? '' : ' terms-two'}`} role="row" data-term-row={n}>
            <span role="cell"><button type="button" className="terms-open" data-term-lines={n} aria-expanded={open[n] ? 'true' : 'false'}
              onClick={() => setOpen({ ...open, [n]: !open[n] })}>{row.term}</button> <span className="hint m">{row.count}×</span></span>
            <span role="cell"><Side side={row.ours} count={row.count} /></span>
            {official && <span role="cell">{row.dub?.rendering
              ? <button type="button" className="analysis-suggest analysis-suggest-alt" data-term-use={n} title="Use the official dub's wording"
                  onClick={e => { setEdits({ ...edits, [n]: row.dub.rendering }); e.currentTarget.closest('.terms-row')?.querySelector('[data-term-input]')?.focus(); }}>{row.dub.rendering}</button>
              : <span className="hint">not heard</span>}</span>}
            <span role="cell" className="terms-keep">{terms.show && <>
              <input className="input" data-term-input={n} value={valueOf(row, n)} aria-label={`Wording of ${row.term} for the show`}
                onChange={e => setEdits({ ...edits, [n]: e.target.value })} />
              {row.saved ? <>
                {row.saved.reviewed && <span className="terms-kept">kept</span>}
                <button type="button" className="btn btn-ghost" data-term-save={n} onClick={() => { const v = valueOf(row, n).trim(); if (v) keep(row.term, v); }}>{row.saved.reviewed ? 'Change' : 'Confirm'}</button>
                <button type="button" className="btn btn-ghost" data-term-forget={n} onClick={() => keep(row.term, '')}>Forget</button>
              </> : <button type="button" className="btn btn-ghost" data-term-save={n} onClick={() => { const v = valueOf(row, n).trim(); if (v) keep(row.term, v); }}>Keep</button>}
            </>}</span>
            <div className="terms-lines" data-term-detail={n} hidden={!open[n]}>{row.lines.map(l => (
              <p key={l.cue}><span className="m">{formatTime(l.start || 0)}</span> {l.text}<br /><span className="terms-ours">{l.ours || '—'}</span>
                {official && <><br /><span className="hint">{official}: {l.dub || '—'}</span></>}</p>
            ))}</div>
          </div>
        ))}
      </div>
      {terms.other_saved.length > 0 && (
        <p className="hint terms-other">Also kept for the show: {terms.other_saved.map((s, i) => (
          <span key={s.term}>{i > 0 && ' · '}<span className="terms-saved">{s.term} → {s.rendering}{!s.reviewed && <> <button type="button" className="btn btn-ghost" data-other-confirm={i}
            title="Saved but not confirmed, so translations do not use it yet" onClick={() => keep(s.term, s.rendering)}>Confirm</button></>}</span></span>
        ))}</p>
      )}
    </details>
  );
}
