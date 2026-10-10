// What the voices pages share: a voice's colours, and which catalogue voice
// speaks for which character.
import { api, apiUrl, withKey } from './api.js';

const PALETTES = [['#CADCFC', '#A0B9D1'], ['#F6C6A8', '#E8845C'], ['#C9F2D6', '#7FC8A4'],
  ['#E7D1FA', '#B18AE0'], ['#FCE7A8', '#E9B949'], ['#FAD0DA', '#E47A98']];

const SAMPLE_READY = ['completed', 'done', 'ready', 'success'];
const SAMPLE_FAILED = ['failed', 'error', 'cancelled', 'canceled'];

// Ask the speech service for a short sample and wait for it: the audio URL,
// or null when `current()` says a newer request replaced this one. Throws with
// a readable message when the sample fails or takes longer than `limit` ms.
export async function previewSample(body, { current = () => true, limit = 180_000, every = 1500 } = {}) {
  const made = await api('voice-catalog/preview', { method: 'POST', json: body });
  const until = Date.now() + limit;
  for (;;) {
    if (!current()) return null;
    const state = await api(`voice-catalog/preview/${made.id}`);
    if (!current()) return null;
    if (SAMPLE_READY.includes(state.status)) return withKey(apiUrl(`voice-catalog/preview/${made.id}/audio`));
    if (SAMPLE_FAILED.includes(state.status)) throw new Error(state.error || 'The voice sample failed.');
    if (Date.now() > until) throw new Error('Still generating in the speech service; try again shortly.');
    await new Promise(resolve => setTimeout(resolve, every));
  }
}

export const SAMPLE = '¡Esto es una prueba de voz! A ver cómo suena este personaje.';

// The orb ramps black → first → second → white, so the second is the chosen
// colour lifted toward white.
export function lighten(hex, amount = 0.5) {
  const v = parseInt(hex.slice(1), 16);
  const mix = c => Math.round(c + (255 - c) * amount);
  return '#' + [(v >> 16) & 255, (v >> 8) & 255, v & 255].map(c => mix(c).toString(16).padStart(2, '0')).join('');
}

export function palette(v) {
  if (/^#[0-9a-f]{6}$/i.test(v.color || '')) return [v.color, lighten(v.color)];
  let h = 0;
  for (const c of String(v.key)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTES[h % PALETTES.length];
}

export function seedOf(key) {
  let h = 2166136261;
  for (const c of String(key)) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0;
  return h;
}

export function dotStyle(v) {
  const [a, b] = palette(v);
  return `background: radial-gradient(circle at 35% 30%, #fff 0%, ${a} 38%, ${b} 100%);`;
}

const fold = s => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();

// "tvdb-81234" (a voice's show) ↔ "show:tvdb:81234" (a character's series).
export const seriesOfShow = show => (/^tvdb-\d+$/.test(show || '') ? `show:tvdb:${show.slice(5)}` : '');

export const showOfSeries = series => (/^show:tvdb:\d+$/.test(series || '') ? `tvdb-${series.split(':')[2]}` : '');

const namesOf = c => new Set([c.name, ...(c.aliases || [])].map(fold));

// The voice a character speaks with: its cast voice first, else a voice
// named for it in the same show.
export function voiceFor(character, catalog) {
  const cast = catalog.find(v => v.profile_id && (character.voices || []).includes(v.profile_id));
  if (cast) return cast;
  const names = namesOf(character);
  return catalog.find(v => seriesOfShow(v.show) === character.series_id
    && (names.has(fold(v.character)) || names.has(fold(v.display_name)))) || null;
}

// The character a voice speaks for, if any.
export function characterFor(voice, characters) {
  return characters.find(c => voice.profile_id && (c.voices || []).includes(voice.profile_id))
    || characters.find(c => seriesOfShow(voice.show) === c.series_id
      && (namesOf(c).has(fold(voice.character)) || namesOf(c).has(fold(voice.display_name))))
    || null;
}
