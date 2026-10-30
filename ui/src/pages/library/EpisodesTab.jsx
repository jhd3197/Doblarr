import { useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../lib/api.js';
import { languageName, targetChoices } from '../../lib/languages.js';
import { queryClient } from '../../lib/queries.js';
import { seriesEpisodesQuery } from './queries.js';
import { Loading, EmptyState } from '../../components/States.jsx';

const LABELS = { 'audio-present': 'Audio present', 'dub-ready': 'AI dub ready',
  'not-downloaded': 'Not downloaded', 'needs-dub': 'Needs dub', 'unknown-audio': 'Audio unknown',
  queued: 'Queued', running: 'Generating', failed: 'Failed — retry available' };

const code = e => `S${String(e.season).padStart(2, '0')}E${String(e.episode).padStart(2, '0')}`;

function EpisodeRow({ item, episode: e, selected, busy, onSelect, onQueue, onCast }) {
  const navigate = useNavigate();
  const locked = busy || !!e.job_id;
  return (
    <div className="episode-row" data-episode={e.id}>
      <input className="episode-check" type="checkbox" aria-label={`Select ${code(e)}`} checked={selected}
        disabled={busy || !e.downloaded || !!e.job_id} onChange={ev => onSelect(e.id, ev.target.checked)} />
      <div className="episode-name">
        <span className="m">{code(e)}</span>
        <Link to={`/title/tvdb-${item.tvdb_id}/episode/${e.id}/analysis`} className="episode-open"><strong>{e.title}</strong></Link>
        <span className="hint">{e.audio_langs.length ? e.audio_langs.map(l => l.toUpperCase()).join(' · ') : 'No audio metadata'}</span>
      </div>
      <span className={`tag ${e.dubbed ? 'tag-neutral' : 'tag-outline'}`}>{LABELS[e.status] || e.status}</span>
      <div className="episode-row-actions">
        {e.downloaded && <>
          <button type="button" className="btn btn-ghost episode-analyze" disabled={locked} onClick={() => onQueue([e.id], 'analyze', false)}>Analyze</button>
          <button type="button" className="btn btn-ghost episode-cast" disabled={busy} onClick={() => onCast(e, 'voices')}>Voices</button>
          <button type="button" className="btn btn-secondary episode-audition" disabled={locked} onClick={() => onQueue([e.id], 'audition', false)}>Audition</button>
          <button type="button" className="btn btn-primary episode-queue" disabled={locked} onClick={() => onQueue([e.id], 'full', false)}>{e.dubbed ? 'Create AI dub' : 'Queue dub'}</button>
        </>}
        {e.output_job_id && <button type="button" className="btn btn-ghost episode-watch" disabled={busy}
          onClick={() => navigate(`/watch/${encodeURIComponent(e.output_job_id)}`)}>Watch dub</button>}
      </div>
    </div>
  );
}

// A show's seasons and episodes in one dub language, with explicit per-file queueing.
export function EpisodesTab({ item, target, targets, onTarget, onCast }) {
  const query = seriesEpisodesQuery(item.tvdb_id, target);
  const { data, error, isPending } = useQuery(query);
  const [filter, setFilter] = useState('all');
  const [selected, setSelected] = useState(() => new Set());
  const [feedback, setFeedback] = useState('');
  const [busy, setBusy] = useState(false);

  if (isPending) return <Loading>Loading seasons and episodes from Sonarr…</Loading>;
  if (error) return <>Episodes unavailable: {error.message}</>;

  async function refresh() {
    try {
      queryClient.setQueryData(query.queryKey, await api(`series/${item.tvdb_id}/episodes?target_lang=${encodeURIComponent(target)}&refresh=true`));
    } catch (e) { setFeedback(e.message); }
  }

  async function queue(ids, kind = 'full', missingOnly = true) {
    setBusy(true);
    setFeedback('Queuing episode files…');
    try {
      const result = await api(`series/${item.tvdb_id}/queue`, { method: 'POST', json: {
        episode_ids: ids, target_lang: target, kind, missing_only: missingOnly,
      } });
      const message = `${result.queued.length} file${result.queued.length === 1 ? '' : 's'} queued.`
        + (result.skipped.length ? ` Skipped: ${[...new Set(result.skipped.map(s => s.reason))].join('; ')}.` : '');
      await queryClient.fetchQuery({ ...query, staleTime: 0 });
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
      setSelected(new Set());
      setFeedback(message);
    } catch (e) {
      setFeedback(e.message);
    } finally {
      setBusy(false);
    }
  }

  const select = (id, on) => setSelected(prev => {
    const next = new Set(prev);
    if (on) next.add(id); else next.delete(id);
    return next;
  });
  const rows = data.episodes.filter(e => filter === 'all' || (filter === 'downloaded' ? e.downloaded : e.downloaded && !e.dubbed));
  const seasons = [...new Set(rows.map(e => e.season))];
  const openSeason = rows.find(e => e.downloaded)?.season ?? seasons[0];
  const missing = data.episodes.filter(e => e.downloaded && !e.dubbed && !e.job_id);

  return (
    <>
      <div className="episode-toolbar">
        <div>
          <h3>Episodes</h3>
          <p className="hint">{data.downloaded} downloaded of {data.total} episodes · {data.dubbed} with {languageName(target)} audio or a completed AI dub</p>
        </div>
        <label>Dub language
          <select className="input episode-target" aria-label="Dub language" value={target} disabled={busy}
            onChange={e => onTarget(e.target.value)}>
            {targetChoices(targets, target).map(t => <option key={t} value={t}>{languageName(t)}</option>)}
          </select>
        </label>
        <label>Show
          <select className="input episode-filter" aria-label="Show episodes" value={filter} disabled={busy}
            onChange={e => setFilter(e.target.value)}>
            <option value="all">All episodes</option><option value="downloaded">Downloaded</option><option value="missing">Missing dub</option>
          </select>
        </label>
      </div>
      <div className="episode-actions">
        <button type="button" className="btn btn-primary episode-selected" disabled={busy || !selected.size}
          onClick={() => queue([...selected], 'full', false)}>Queue selected ({selected.size})</button>
        <button type="button" className="btn btn-secondary episode-missing" disabled={busy || !missing.length}
          onClick={() => queue(missing.map(e => e.id))}>Queue missing dubs</button>
        <button type="button" className="btn btn-secondary episode-analyze-selected" disabled={busy || !selected.size}
          onClick={() => queue([...selected], 'analyze', false)}>Analyze selected ({selected.size})</button>
        <button type="button" className="btn btn-ghost episode-refresh" disabled={busy} onClick={refresh}>Refresh episodes</button>
      </div>
      <p className="hint">Audio present means Sonarr reports that language in the source file. AI dub ready is a separate generated output. Teases and auditions do not count as full dubs.</p>
      <p className="episode-feedback" role="status">{feedback}</p>
      {seasons.length ? seasons.map(season => (
        <details key={season} className="episode-season" open={season === openSeason}>
          <summary>{season === 0 ? 'Specials' : `Season ${season}`} <span className="hint">{rows.filter(e => e.season === season).length} episodes</span></summary>
          {rows.filter(e => e.season === season).map(e => (
            <EpisodeRow key={e.id} item={item} episode={e} selected={selected.has(e.id)} busy={busy}
              onSelect={select} onQueue={queue} onCast={onCast} />
          ))}
        </details>
      )) : <EmptyState>No episodes match this filter.</EmptyState>}
    </>
  );
}
