import { isRouteErrorResponse, Link, useMatches, useRouteError } from 'react-router';
import { initialTheme } from '../lib/storage.js';

const SECTION_PATH = { Overview: '/', Library: '/library', Dubs: '/dubs', Voices: '/voices',
  Knowledge: '/knowledge', Settings: '/settings' };

// Covers the app until the first route's data is in, so a deep link renders
// its page once instead of passing through Overview and partial states.
export function BootScreen() {
  return <div id="boot" role="status">Loading Doblarr…</div>;
}

// A loader that failed: say what failed, keep the shell around it.
export function RouteError() {
  const error = useRouteError();
  const section = useMatches().findLast(m => m.handle?.nav)?.handle.nav;
  // A thrown redirect/response carries text; a failed api() call is an Error
  // whose message already reads well (its `data` is the raw JSON body).
  const message = isRouteErrorResponse(error)
    ? (typeof error.data === 'string' ? error.data : error.statusText || 'Not found')
    : (error?.message || String(error));
  return (
    <div className="page">
      <div className="panel route-error" role="alert">
        <p className="route-error-title">This page couldn&apos;t load.</p>
        <p className="hint">{message}</p>
        {section && section !== 'Overview' && (
          <Link className="btn btn-ghost route-error-back" to={SECTION_PATH[section]}>← {section}</Link>
        )}
      </div>
    </div>
  );
}

// An address no route owns: a typo or an old bookmark.
export function NotFound() {
  return (
    <div className="page">
      <div className="panel route-error">
        <p className="route-error-title">Page not found.</p>
        <p className="hint">Nothing lives at this address.</p>
        <Link className="btn btn-ghost route-error-back" to="/">← Overview</Link>
      </div>
    </div>
  );
}

// The last resort, when the shell itself failed: the same panel in the app's
// frame and theme, so it never renders unstyled.
export function RootError() {
  return (
    <div className="app" data-theme={initialTheme()}>
      <RouteError />
    </div>
  );
}
