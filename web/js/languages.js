import { api } from './api.js';
import { applyLanguageOptions } from './settings-model.js';

// Canonical catalog from GET /api/languages, cached for the session. An empty
// map means the fetch failed; every consumer falls back to raw uppercased codes.
let byId = null;

export async function loadLanguages() {
  if (byId) return byId;
  try {
    const data = await api('languages');
    byId = new Map((data.languages || []).map(l => [l.id, l]));
  } catch (e) {
    byId = new Map();
  }
  applyLanguageOptions([...byId.values()]);
  return byId;
}

export function languageName(code) {
  const entry = byId && byId.get(code);
  return entry ? entry.name : String(code).toUpperCase();
}

// Dub-target picker choices: the configured base languages plus the regional
// locales available for them, with the current selection always present.
export function targetChoices(targets, current) {
  const bases = new Set(targets);
  const regional = byId
    ? [...byId.values()].filter(l => l.region && l.supported && bases.has(l.base)).map(l => l.id)
    : [];
  return [...new Set([...targets, ...regional, ...(current ? [current] : [])])];
}
