const TABS = ['plan', 'voices', 'jobs', 'meta'];

export function parseTitlePath(path) {
  const parts = path.split('/').filter(Boolean);
  let titleKey;
  try { titleKey = decodeURIComponent(parts[1] || ''); }
  catch { return { invalid: true }; }
  const episode = parts[2] === 'episode';
  if (!titleKey || (episode && !/^[1-9]\d*$/.test(parts[3] || ''))
    || parts.length > (episode ? 5 : 3)) return { invalid: true };
  const episodeId = episode ? Number(parts[3]) : null;
  if (episode && !Number.isSafeInteger(episodeId)) return { invalid: true };
  return { titleKey, episodeId, titleTab: parts[episode ? 4 : 2] || null, invalid: false };
}

export function resolveTitleTab(item, requested) {
  const show = item.media_type === 'show' || (!item.media_type && /^sonarr/i.test(item.source || ''));
  const allowed = show ? ['episodes', ...TABS] : TABS;
  return allowed.includes(requested) ? requested : item.episode_id ? 'voices' : show ? 'episodes' : 'plan';
}

export function titlePath(key, episodeId, tab) {
  return `/title/${encodeURIComponent(key)}${episodeId ? `/episode/${episodeId}` : ''}/${tab}`;
}
