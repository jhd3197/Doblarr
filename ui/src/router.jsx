import { createBrowserRouter, redirect } from 'react-router';
import { ensure, languagesQuery } from './lib/queries.js';
import { Shell } from './shell/Shell.jsx';
import { BootScreen, NotFound, RootError, RouteError } from './shell/BootScreen.jsx';
import { Overview, overviewLoader } from './pages/Overview.jsx';
import { Settings, settingsLoader } from './pages/Settings.jsx';
import { routes as libraryRoutes } from './pages/library/routes.jsx';
import { routes as dubsRoutes } from './pages/dubs/routes.jsx';
import { routes as voicesRoutes } from './pages/voices/routes.jsx';
import { routes as studioRoutes } from './pages/studio/routes.jsx';

// Every route loads its data in `loader` before it renders: the boot screen
// covers the first load, a progress bar later ones, and a page never paints
// half-filled. `handle.nav` names the sidebar section, `handle.title` the tab.
export const router = createBrowserRouter([{
  path: '/',
  element: <Shell />,
  HydrateFallback: BootScreen,
  errorElement: <RootError />,
  // Language names appear on most pages; the catalog is small and cached.
  loader: () => ensure(languagesQuery).then(() => null),
  children: [{
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Overview />, loader: overviewLoader, handle: { nav: 'Overview', title: 'Overview' } },
      { path: 'settings', loader: () => redirect('/settings/connections') },
      { path: 'settings/:tab', element: <Settings />, loader: settingsLoader, handle: { nav: 'Settings', title: 'Settings' } },
      ...libraryRoutes,
      ...dubsRoutes,
      ...voicesRoutes,
      ...studioRoutes,
      { path: '*', element: <NotFound />, handle: { title: 'Not found' } },
    ],
  }],
}]);
