import { useState } from 'react';
import { NavLink, useMatches } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { hardwareQuery, statusQuery } from '../lib/queries.js';
import { hardwareSummary } from '../lib/hardware.js';
import { nudgeDue, REPO_URL, SNOOZE_KEY, SNOOZE_MS, STARRED_KEY } from '../lib/star.js';
import { safeSet } from '../lib/storage.js';

const NAV = [['Overview', '/'], ['Library', '/library'], ['Dubs', '/dubs'], ['Voices', '/voices'],
  ['Knowledge', '/knowledge'], ['Settings', '/settings']];

// The section a page belongs to: a title lives under Library, studio and watch under Dubs.
function useSection() {
  const matches = useMatches();
  return matches.findLast(m => m.handle?.nav)?.handle.nav || 'Overview';
}

function Star({ onStarred }) {
  return (
    <a id="brandStar" className="brand-star" href={REPO_URL} target="_blank" rel="noopener noreferrer"
      aria-label="Star Doblarr on GitHub" title="Star on GitHub" onClick={onStarred}>
      <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M12 2.5l2.94 5.96 6.56.95-4.75 4.63 1.12 6.54L12 17.5l-5.87 3.08 1.12-6.54L2.5 9.41l6.56-.95z" fill="currentColor" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" /></svg>
      <span className="star-sparks" aria-hidden="true"><i /><i /><i /><i /><i /><i /></span>
    </a>
  );
}

export function Sidebar() {
  const section = useSection();
  const [nudge, setNudge] = useState(() => nudgeDue());
  const { data: hw, isFetched } = useQuery(hardwareQuery);
  const summary = hardwareSummary(hw, hw?.memory);
  const { data: status, isError: offline } = useQuery({ ...statusQuery, refetchInterval: 30_000 });
  const working = !offline && status?.worker_alive !== false;
  const worker = !working ? (offline ? 'server unreachable' : 'worker stopped')
    : isFetched ? `worker online · ${summary.device}` : 'worker online';
  const starred = () => { safeSet(STARRED_KEY, '1'); setNudge(false); };
  const later = () => { safeSet(SNOOZE_KEY, String(Date.now() + SNOOZE_MS)); setNudge(false); };

  return (
    <aside className="side">
      <div className="side-brand">
        <img src="/logo.png" alt="" width="26" height="26" />
        <span className="side-name">doblarr</span>
        <Star onStarred={starred} />
      </div>
      <nav className="sidenav" id="nav">
        {NAV.map(([page, to]) => (
          <NavLink key={page} to={to} className="navitem" data-page={page}
            aria-current={section === page ? 'page' : 'false'}>
            <span className="dot9" />{page}
          </NavLink>
        ))}
      </nav>
      <div id="starNudge" className="star-nudge" role="note" hidden={!nudge}>
        <p className="star-nudge-title"><span aria-hidden="true">★</span> Enjoying Doblarr?</p>
        <p className="star-nudge-body">Please don&apos;t forget to star the repo. It&apos;s free and helps the project go a long way.</p>
        <div className="star-nudge-actions">
          <a className="btn btn-primary" href={REPO_URL} target="_blank" rel="noopener noreferrer" data-star-go onClick={starred}>Star on GitHub</a>
          <button type="button" className="btn btn-ghost" data-star-later onClick={later}>Not now</button>
        </div>
      </div>
      <div className="sidefoot panel">
        <div className="sidefoot-row">
          <span className="sidefoot-dot" data-state={working ? 'ok' : 'down'} />
          <span id="workerStatus" className="m">{worker}</span>
        </div>
        {status?.version && (
          <div className="sidefoot-row sidefoot-version">
            <span>Doblarr</span>
            <span className="m">{status.version}</span>
          </div>
        )}
      </div>
    </aside>
  );
}
