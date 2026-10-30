import { createContext, useContext, useEffect, useState } from 'react';
import { Outlet, useMatches, useNavigation } from 'react-router';
import { dismiss, useNotice } from '../lib/notice.js';
import { initialTheme, safeSet } from '../lib/storage.js';
import { Sidebar } from './Sidebar.jsx';
import { NewDubModal } from './NewDubModal.jsx';

const SearchContext = createContext('');
// The header search filters the current page's list by title (lowercased).
export const useSearch = () => useContext(SearchContext);

function useTheme() {
  const [theme, setTheme] = useState(initialTheme);
  useEffect(() => { safeSet('doblarr.theme', theme); }, [theme]);
  return [theme, setTheme];
}

// "Doblarr — <page>" from the deepest route that names itself.
function useDocumentTitle() {
  const matches = useMatches();
  const match = matches.findLast(m => m.handle?.title);
  const title = match?.handle.title;
  const text = typeof title === 'function' ? title(match) : title;
  useEffect(() => { document.title = text ? `Doblarr — ${text}` : 'Doblarr'; }, [text]);
}

export function Shell() {
  const [theme, setTheme] = useTheme();
  const [search, setSearch] = useState('');
  const [newDub, setNewDub] = useState(false);
  const navigation = useNavigation();
  const notice = useNotice();
  useDocumentTitle();

  return (
    <div className="app shell" data-theme={theme} id="app">
      <Sidebar />
      <main className="main">
        {navigation.state === 'loading' && <div className="route-progress" role="progressbar" aria-label="Loading page" />}
        <header className="topbar">
          <input id="globalSearch" className="input" type="search" placeholder="Search titles on this page…"
            aria-label="Search titles on this page"
            value={search} onChange={e => setSearch(e.target.value)} />
          <div className="themebtn" id="themebtn">
            {['light', 'dark'].map(t => (
              <button key={t} type="button" data-theme-set={t} aria-pressed={theme === t ? 'true' : 'false'}
                onClick={() => setTheme(t)}>{t === 'light' ? 'Light' : 'Dark'}</button>
            ))}
          </div>
          <button type="button" className="btn btn-primary" id="newDubBtn" onClick={() => setNewDub(true)}>New dub</button>
        </header>
        {notice && (
          <div className="app-notice" role="alert" id="appNotice">
            <span>{notice.message}</span>
            <button type="button" className="btn btn-ghost" aria-label="Dismiss" onClick={dismiss}>×</button>
          </div>
        )}
        <SearchContext.Provider value={search.trim().toLowerCase()}>
          <Outlet />
        </SearchContext.Provider>
      </main>
      {newDub && <NewDubModal onClose={() => setNewDub(false)} />}
    </div>
  );
}
