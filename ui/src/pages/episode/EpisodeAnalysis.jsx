import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { queryClient } from '../../lib/queries.js';
import { catalogQuery } from '../voices/queries.js';
import { Breakdown } from './Breakdown.jsx';

// One episode broken down line by line: the voices found in it (with how much
// each talks, and who they are once picked from the show's cast), then every
// line with its speaker, both languages, how it was performed and a way to
// watch it. The analysis is a queued run; this view starts it and follows it.
// Unnamed voices show the named characters they sound like (voice tags, kept
// for the show), and the lines can be grouped again with other voice models
// and the episode's dub tracks in seconds.
//
//   <EpisodeAnalysis item={episodeItem} target="es" />
//   item: the episode as the title page builds it (path, episode_id, tvdb_id, parent)

const q = path => encodeURIComponent(path);

export const analysisQuery = path => ({
  queryKey: ['analysis', path], queryFn: () => api(`analysis?path=${q(path)}`),
  // A running analysis is followed until it lands.
  refetchInterval: query => (query.state.data?.job ? 4000 : false),
});
const tracksQuery = path => ({ queryKey: ['analysis', path, 'tracks'],
  queryFn: () => api(`analysis/tracks?path=${q(path)}`).catch(() => null) });
const visualQuery = path => ({ queryKey: ['analysis', path, 'visual'],
  queryFn: () => api(`analysis/visual?path=${q(path)}`).catch(() => null) });
const modelsQuery = { queryKey: ['voice-models'], queryFn: () => api('voice-models').then(r => r.models).catch(() => []) };

function Pending({ job }) {
  return (
    <div className="analysis-empty">
      <div className="analysis-pulse" aria-hidden="true" />
      <h3>Analysing this episode</h3>
      <p className="hint">{job.status}{job.stage ? ` · ${job.stage}` : ''}{job.progress ? ` · ${job.progress}%` : ''}</p>
      <p className="hint">Separating the dialogue, cutting every line, grouping the voices and measuring how each line was said. Nothing is translated or dubbed.</p>
    </div>
  );
}

function NotAnalysed({ start, busy, status }) {
  return (
    <div className="analysis-empty">
      <h3>Break this episode down</h3>
      <p className="hint">Every line: who says it, when, what in both languages, and how (quiet, calm or intense, pitch and how much it moves). The voices it finds can be named once and become the show’s cast.</p>
      <button type="button" className="btn btn-primary analysis-go" data-analyze disabled={busy} onClick={start}>Analyze episode</button>
      <p className="hint" role="status" data-status>{status}</p>
    </div>
  );
}

export function EpisodeAnalysis({ item, target = 'es' }) {
  const path = item.path;
  const tvdb = item.parent?.tvdb_id || item.tvdb_id;
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const followTimer = useRef(null);
  // Another episode starts with a clean status line.
  const [shown, setShown] = useState(path);
  if (shown !== path) { setShown(path); setStatus(''); setBusy(false); }
  useEffect(() => () => clearTimeout(followTimer.current), []);

  const analysis = useQuery(analysisQuery(path));
  const analysed = Boolean(analysis.data?.analysed && !analysis.data?.job);
  // The rest of the breakdown arrives with the analysis, so the page draws once.
  const tracks = useQuery({ ...tracksQuery(path), enabled: analysed });
  const visual = useQuery({ ...visualQuery(path), enabled: analysed });
  const models = useQuery({ ...modelsQuery, enabled: analysed });
  const catalog = useQuery({ ...catalogQuery, enabled: analysed });

  // Everything the analysis drew from, read again after a change.
  const reload = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ['analysis', path] }),
    queryClient.invalidateQueries({ queryKey: ['voice-catalog'] }),
  ]);

  async function start() {
    setBusy(true);
    try {
      const result = await api(`series/${tvdb}/queue`, { method: 'POST', json: {
        episode_ids: [item.episode_id], target_lang: target, kind: 'analyze', missing_only: false } });
      if (!result.queued.length) throw new Error(result.skipped[0]?.reason || 'Not queued');
      setStatus('Queued. Separating the dialogue comes first; it is the long part.');
      clearTimeout(followTimer.current);
      followTimer.current = setTimeout(() => queryClient.invalidateQueries({ queryKey: ['analysis', path] }), 4000);
    } catch (error) {
      setStatus(error.message);
      setBusy(false);
    }
  }

  if (analysis.error) return <p className="hint">{analysis.error.message}</p>;
  if (!analysis.data) return <p className="hint">Reading this episode’s analysis…</p>;
  if (analysis.data.job) return <Pending job={analysis.data.job} />;
  if (!analysis.data.analysed) return <NotAnalysed start={start} busy={busy} status={status} />;
  if ([tracks, visual, models, catalog].some(r => r.isPending)) return <p className="hint">Reading this episode’s analysis…</p>;

  return (
    <Breakdown key={path} data={analysis.data} path={path} target={target} tvdb={tvdb}
      models={models.data || []} found={tracks.data} visual={visual.data} voices={catalog.data || []}
      status={status} say={setStatus} reload={reload} start={start} busy={busy} />
  );
}
