import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { queryClient } from './lib/queries.js';
import { startEventStream } from './lib/events.js';
import { router } from './router.jsx';
import { DialogHost } from './components/Dialog.jsx';
import './styles/index.css';
import './shell/shell.css';
import './pages/overview.css';
import './pages/settings.css';

startEventStream();

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      {/* Outside the router: a 401 during the first route load asks before the shell exists. */}
      <DialogHost />
    </QueryClientProvider>
  </StrictMode>,
);
