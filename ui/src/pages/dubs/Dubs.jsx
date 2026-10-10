import { useState } from 'react';
import { useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { configQuery, ensure, jobsQuery, queryClient } from '../../lib/queries.js';
import { jobStatusClass, useJobs } from '../../lib/jobs.js';
import { useOpenStudio } from '../../lib/studio.js';
import { useSearch } from '../../shell/Shell.jsx';
import { ReviewDialog } from './Review.jsx';
import './dubs.css';

export async function dubsLoader() {
  // The dry-run tag reads the config; a config failure only hides the tag.
  await Promise.all([ensure(jobsQuery).catch(() => null), ensure(configQuery).catch(() => null)]);
  return null;
}

const refreshJobs = () => queryClient.invalidateQueries({ queryKey: ['jobs'] });

function stageText(j) {
  if (j.status === 'done') return j.message || 'done';
  if (j.status === 'failed') return j.message || 'failed';
  if (j.status === 'cancelled') return j.message || 'cancelled';
  return j.message || j.stage || 'queued';
}

function JobRow({ job: j, onReview, onStudio, onWatch }) {
  const [busy, setBusy] = useState(false);
  const active = j.status === 'queued' || j.status === 'running';
  const dim = j.status === 'failed' || j.status === 'cancelled';
  const status = j.status === 'done' && (j.message || '').startsWith('planned') ? 'planned' : j.status;

  async function remove() {
    setBusy(true);
    try { await api('api/jobs/' + j.id, { method: 'DELETE' }); } catch (err) { window.alert(err.message); } finally { setBusy(false); }
    refreshJobs();
  }

  return (
    <tr>
      <td className="dubs-title">{j.title}{j.kind === 'tease' && <> <span className="tag tag-outline dubs-tease">tease</span></>}</td>
      <td className="m dubs-track">{j.source_lang} → {j.target_lang}
        {j.version_id && (
          <div className="m dubs-version" title={j.version_id}>
            {j.version_name || 'Dub'} · {j.version_id.slice(0, 12)}<br />Script {(j.translation_id || '').slice(0, 12)}
          </div>
        )}
      </td>
      <td>{stageText(j)}</td>
      <td>
        <div className="bar"><span className={dim ? 'bar-dim' : undefined} style={{ width: `${j.progress || 0}%` }} /></div>
        {active && <div className="m dubs-pct">{j.progress || 0}%</div>}
      </td>
      <td><span className={`tag ${jobStatusClass(status)}`}>{status}</span></td>
      <td>
        <button type="button" className="btn btn-ghost job-del" data-id={j.id} disabled={busy} onClick={remove}>{active ? 'Cancel' : 'Remove'}</button>
        {j.has_file && <button type="button" className="btn btn-ghost job-watch" data-id={j.id} onClick={() => onWatch(j)}>Watch</button>}
        {j.has_review && (
          <button type="button" className="btn btn-secondary job-review" data-id={j.id} onClick={() => onReview(j)}>
            Review{j.review_count ? ` (${j.review_count})` : ''}</button>
        )}
        {j.input_file && !String(j.kind || '').startsWith('studio_') && (
          <button type="button" className="btn btn-ghost job-studio" data-id={j.id} onClick={() => onStudio(j)}>Studio</button>
        )}
      </td>
    </tr>
  );
}

export function Dubs() {
  const { data, isError } = useJobs();
  const { data: config } = useQuery({ ...configQuery, retry: false });
  const search = useSearch();
  const navigate = useNavigate();
  const openStudio = useOpenStudio();
  const [reviewing, setReviewing] = useState(null);
  const jobs = (data?.jobs || []).filter(j => !search || String(j.title).toLowerCase().includes(search));
  const counts = data?.counts || {};
  const paused = !!data?.paused;

  async function toggleQueue() {
    try { await api(paused ? 'api/queue/resume' : 'api/queue/pause', { method: 'POST' }); } catch (e) { window.alert(e.message); }
    refreshJobs();
  }
  async function clearFinished() {
    try { await api('api/jobs/clear-finished', { method: 'POST' }); } catch (e) { window.alert(e.message); }
    refreshJobs();
  }

  let body;
  if (isError && !data) body = <tr><td colSpan="6" className="dubs-empty">Jobs unavailable — is Doblarr running (doblarr serve)?</td></tr>;
  else if (!jobs.length) body = <tr><td colSpan="6" className="dubs-empty">No jobs yet — queue one from the Library.</td></tr>;
  else body = jobs.map(j => (
    <JobRow key={j.id} job={j} onReview={job => setReviewing(job.id)}
      onWatch={job => navigate('/watch/' + encodeURIComponent(job.id))}
      onStudio={job => openStudio({ path: job.input_file, title: job.title, jobId: job.id })} />
  ));

  return (
    <div className="page">
      <div className="dubs-toolbar">
        <button type="button" className="btn btn-secondary" id="pauseQueue" onClick={toggleQueue}>{paused ? 'Resume queue' : 'Pause queue'}</button>
        <button type="button" className="btn btn-secondary" id="clearFinished" onClick={clearFinished}>Clear finished</button>
        <span className="tag tag-outline" id="pausedTag" hidden={!paused}>Queue paused</span>
        <span className="tag tag-outline" id="dryRunTag" hidden={!config?.dub?.dry_run}>Dry run — jobs are planned only, nothing is written to Plex</span>
        <span className="tag tag-accent dubs-worker" id="workerTag">{counts.running ? `processing ${counts.running}` : 'worker idle'}</span>
      </div>
      <div className="panel dubs-panel">
        <div className="table-wrap"><table className="table">
          <thead>
            <tr><th>Item</th><th className="dubs-col-track">Track</th><th>Stage</th><th className="dubs-col-progress">Progress</th>
              <th className="dubs-col-status">Status</th><th className="dubs-col-actions" /></tr>
          </thead>
          <tbody id="dubsBody">{body}</tbody>
        </table></div>
      </div>
      {reviewing && <ReviewDialog jobId={reviewing} onClose={() => setReviewing(null)} onQueued={refreshJobs} />}
    </div>
  );
}
