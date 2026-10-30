import { useNavigate } from 'react-router';
import { jobsFor } from '../../lib/identity.js';
import { jobStatusClass, useJobs } from '../../lib/jobs.js';

// The last jobs for this title's file (or any episode of a show's folder).
export function JobsTab({ item }) {
  const navigate = useNavigate();
  const { data, error, isPending } = useJobs();
  let body;
  if (isPending) body = <p className="note title-jobs-note">Loading…</p>;
  else if (error) body = <p className="note title-jobs-note">Jobs unavailable.</p>;
  else {
    const list = jobsFor(item, data.jobs || []);
    body = !list.length ? <p className="note title-jobs-note">No jobs for this title yet.</p>
      : list.slice(0, 10).map(j => (
        <div key={j.id} className="title-job">
          <span className="m title-job-time">{(j.created_at || '').slice(5, 16)}</span>
          <span className={`tag ${jobStatusClass(j.status)}`}>{j.status}</span>
          {j.kind === 'tease' && <span className="tag tag-outline title-job-tease">tease</span>}
          <span className="title-job-message">{j.message || j.stage || ''}</span>
          {j.has_file && <button type="button" className="btn btn-ghost detail-watch title-job-watch"
            onClick={() => navigate(`/watch/${encodeURIComponent(j.id)}`)}>Watch</button>}
        </div>
      ));
  }
  return <div className="panel title-jobs-panel"><div id="titleJobs">{body}</div></div>;
}
