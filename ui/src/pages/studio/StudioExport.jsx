import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { exportQuery } from './queries.js';

// Export: exactly what the chosen version contains, what is stale, and what
// is still open — then hand it over without generating speech by surprise.

function ExportPlan({ ctrl, job }) {
  const { data: plan, error } = useQuery(exportQuery(ctrl.sid, job));
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  if (error) return <div className="panel studio-empty"><p>{error.message}</p></div>;
  if (!plan) return <p className="hint">Reading the run…</p>;
  const blocked = ctrl.state.session.unresolved === 'block_export' && plan.unresolved.length > 0;

  async function exportIt() {
    setBusy(true);
    try {
      const result = await api(`studio/sessions/${ctrl.sid}/export?job_id=${job}`, { method: 'POST',
        json: { base_revision: plan.revision } });
      setNote(result.exported === 'existing'
        ? `Exported saved version ${result.version_id.slice(0, 12)}. ${result.note}`
        : `Queued ${result.job}. ${result.note}`);
    } catch (err) {
      setNote(err.data?.lines ? `${err.message} (lines ${err.data.lines.join(', ')})` : err.message);
    }
    setBusy(false);
  }
  async function openLine(line) {
    await ctrl.savePatch({ line });
    ctrl.show('dialogue');
  }

  return (
    <>
      <section className="panel studio-card">
        <h3>What this export contains</h3>
        <p>{plan.lines.length} line(s) · {plan.stale} stale · {plan.unresolved.length} unresolved finding(s)
          {' '}· {plan.timing_edits} timing edit(s) · {plan.manual_gains} manual gain(s)</p>
        <p className="hint">{plan.note} {plan.version_saved ? `Saved version ${plan.version_id.slice(0, 12)} holds this run.`
          : 'This run has no saved version.'} The original media and earlier versions are never changed;
          publishing to a library is a separate step.</p>
        <div className="studio-actions">
          <button type="button" className="btn btn-primary" id="exGo" disabled={blocked || busy} onClick={exportIt}>Export the selected version</button>
          {blocked && <span className="review-marker">Unresolved findings block export under this studio&apos;s policy.</span>}
        </div>
        <p className="hint" id="exNote" role="status">{note}</p>
      </section>
      {plan.unresolved.length > 0 && (
        <section className="panel studio-card"><h3>Open findings</h3>
          <ul className="studio-list">{plan.unresolved.map((u, i) => (
            <li key={`${u.line}:${u.code}:${i}`}><button type="button" className="btn btn-ghost" data-line={u.line}
              onClick={() => openLine(u.line)}>Line {u.line + 1}</button>
              {' '}{u.code.replaceAll('_', ' ')} <span className={`tag ${u.severity === 'error' ? 'tag-outline' : 'tag-neutral'}`}>{u.severity}</span></li>
          ))}</ul>
          <p className="hint">Technical findings rank what to listen to. They are not acting judgments, and
            dismissing one is a decision you record in Dialogue.</p></section>
      )}
      <section className="panel studio-card"><h3>Line by line</h3>
        <div className="table-wrap"><table className="table"><thead><tr><th>Line</th><th>Speaker</th><th>Dialogue</th><th>Take</th><th>State</th></tr></thead><tbody>
          {plan.lines.map(l => (
            <tr key={l.index}><td className="m">{l.index + 1}</td><td>{l.speaker}</td><td>{l.text || ''}
              {l.spoken && l.spoken !== l.text && <div className="hint">spoken as: {l.spoken}</div>}</td>
              <td className="m">{(l.take || '—').slice(0, 14)}</td>
              <td>{l.stale ? <span className="review-marker">stale: the audio says older words</span>
                : <span className="hint">{l.selection || 'auto'}</span>}</td></tr>
          ))}
        </tbody></table></div></section>
    </>
  );
}

export function StudioExport({ ctrl }) {
  const job = ctrl.jobId();
  if (!job) return <div className="panel studio-empty"><p>Nothing to export yet: no run has finished.</p></div>;
  return <ExportPlan ctrl={ctrl} job={job} />;
}
