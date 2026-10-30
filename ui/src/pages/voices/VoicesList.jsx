import { useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { ensure, libraryQuery } from '../../lib/queries.js';
import { characterFor, seriesOfShow, showOfSeries, voiceFor } from '../../lib/voices.js';
import { catalogQuery, charactersQuery } from './queries.js';
import { PageHeader } from '../../components/PageHeader.jsx';
import { dotStyle, voiceTitle, VoiceTags } from './VoiceParts.jsx';

// The voice catalogue as a cast. A show's characters are the cast: each one
// is a single page (/voices/character:<id>) with the voice it speaks with.
// A voice nobody cast yet has a page of its own (/voices/<catalog key>) to
// hear it and cast it as a character; once cast, that address opens the
// character. Presets and the working clones runs made are filters.

export async function voicesLoader() {
  // Show names come from the library when it is already scanned; a cold scan never holds the page.
  await Promise.all([ensure(catalogQuery), ensure(charactersQuery)]);
  return null;
}

const FILTERS = [['cast', 'Cast'], ['preset', 'Presets'], ['working', 'Working clones'], ['all', 'All']];

// A clone made by a run (<file stem>-<16 hex>) or a studio audition.
const isWorking = v => v.kind !== 'preset' && !v.display_name
  && (/-[0-9a-f]{16}$/.test(v.name) || v.name.startsWith('Doblarr studio') || v.name.startsWith('Doblarr audition'));
const isNamed = v => Boolean(v.display_name || v.character || v.show);

export function showTitle(items, seriesId, fallback = '') {
  const tvdb = showOfSeries(seriesId).slice(5);
  const item = (items || []).find(i => (tvdb && String(i.tvdb_id) === tvdb)
    || (seriesId.startsWith('movie:tmdb:') && `movie:tmdb:${i.tmdb_id}` === seriesId));
  return item?.title || fallback || 'No show';
}

const voiceHref = key => `/voices/${encodeURIComponent(key)}`;

function Tile({ name, voice, character, onOpen }) {
  const attrs = character ? { 'data-character': character.id } : { 'data-voice': voice.key };
  return (
    <button type="button" className={`voice-tile${voice ? '' : ' voice-tile-silent'}`} {...attrs} onClick={onOpen}>
      <span className="voice-dot voice-dot-lg" style={voice ? dotStyle(voice) : undefined} />
      <span className="voice-tile-body"><strong>{name}</strong>
        <span className="hint">{voice ? `${voice.kind === 'preset' ? 'preset' : 'clone'} · ${voice.engine || ''}${voice.language ? ` · ${voice.language}` : ''}` : 'No voice yet'}</span>
        <VoiceTags voice={voice} /></span>
    </button>
  );
}

export function VoicesList() {
  const navigate = useNavigate();
  const { data: catalog = [] } = useQuery(catalogQuery);
  const { data: characters = [] } = useQuery(charactersQuery);
  const { data: library } = useQuery(libraryQuery);
  const [filter, setFilter] = useState('cast');
  const [query, setQuery] = useState('');
  const q = query.toLowerCase();
  const matches = (...words) => !q || words.join(' ').toLowerCase().includes(q);
  const items = library?.items;

  const linked = new Set();
  const groups = new Map();
  const add = (series, label, tile) => {
    if (!groups.has(series)) groups.set(series, { series, label, tiles: [] });
    groups.get(series).tiles.push(tile);
  };
  if (filter === 'cast' || filter === 'all') {
    for (const c of characters) {
      const voice = voiceFor(c, catalog);
      if (voice) linked.add(voice.key);
      if (!matches(c.name, ...(c.aliases || []), voice ? voiceTitle(voice) : '', voice?.engine || '')) continue;
      add(c.series_id, showTitle(items, c.series_id, voice?.show_name), { name: c.name, voice, character: c });
    }
    for (const v of catalog.filter(x => isNamed(x) && !linked.has(x.key) && !characterFor(x, characters))) {
      if (!matches(voiceTitle(v), v.character || '', v.show_name || '', v.engine)) continue;
      const series = seriesOfShow(v.show) || `voice-show:${v.show_name || ''}`;
      add(series, v.show_name || showTitle(items, series), { name: voiceTitle(v), voice: v });
    }
  }
  for (const g of groups.values()) g.tiles.sort((x, y) => x.name.localeCompare(y.name));
  const loose = catalog.filter(v => !isNamed(v) && matches(voiceTitle(v), v.engine)
    && (filter === 'all' || (filter === 'preset' && v.kind === 'preset') || (filter === 'working' && isWorking(v))));
  const counts = { cast: characters.length + catalog.filter(v => isNamed(v) && !characterFor(v, characters)).length,
    preset: catalog.filter(v => v.kind === 'preset' && !isNamed(v)).length,
    working: catalog.filter(isWorking).length, all: catalog.length };
  const open = tile => navigate(voiceHref(tile.character ? `character:${tile.character.id}` : tile.voice.key));

  return (
    <div className="page" id="voicesRoot">
      <PageHeader title="Voices"
        hint="Every show’s characters and the voice each one speaks with. Open one to hear it, see where it talks and describe it.">
        <input className="input voices-search" type="search" placeholder="Search characters and voices" aria-label="Search voices"
          value={query} onChange={e => setQuery(e.target.value)} />
      </PageHeader>
      <div className="opts voices-filter" role="group" aria-label="Which voices">
        {FILTERS.map(([id, label]) => (
          <button key={id} type="button" className="opt" data-filter={id} aria-pressed={filter === id ? 'true' : 'false'}
            onClick={() => setFilter(id)}>{label} <span className="m">{counts[id]}</span></button>
        ))}
      </div>
      {filter === 'cast' && !groups.size && (
        <div className="panel voices-empty"><p>No characters yet.</p>
          <p className="hint">Name the voices on an episode’s Analysis tab, or open a working clone and cast it as a character.</p></div>
      )}
      {[...groups.values()].sort((x, y) => x.label.localeCompare(y.label)).map(g => (
        <section key={g.series} className="voices-group">
          <h3>{g.label} <span className="hint">{g.tiles.length} character{g.tiles.length === 1 ? '' : 's'}</span></h3>
          <div className="voice-grid">{g.tiles.map(tile => (
            <Tile key={tile.character ? `c:${tile.character.id}` : `v:${tile.voice.key}`} {...tile} onOpen={() => open(tile)} />
          ))}</div>
        </section>
      ))}
      {loose.length > 0 && (
        <div className="panel voices-table"><div className="table-wrap"><table className="table">
          <thead><tr><th>Voice</th><th>Engine</th><th>Kind</th><th>Language</th></tr></thead>
          <tbody>{loose.map(v => (
            <tr key={v.key} data-voice={v.key} onClick={() => navigate(voiceHref(v.key))}>
              <td><span className="voice-dot" style={dotStyle(v)} />
                <Link to={voiceHref(v.key)} onClick={e => e.stopPropagation()}>{voiceTitle(v)}</Link></td>
              <td className="m">{v.engine || ''}</td><td>{v.kind || ''}</td><td className="m">{v.language || '—'}</td></tr>
          ))}</tbody>
        </table></div></div>
      )}
    </div>
  );
}
