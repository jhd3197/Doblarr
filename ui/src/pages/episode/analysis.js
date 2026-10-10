import { apiUrl, withKey } from '../../lib/api.js';

// Labels and small helpers shared by the episode analysis views.

export const BANDS = { quiet: 'quiet', calm: 'calm', intense: 'intense', unmeasured: '—' };
// The episode's parts and a line's role, from the subtitle track's styles
// (doblarr/subtitle_roles.py).
export const PARTS = { 'cold-open': 'Cold open', opening: 'Opening', episode: 'Episode', ending: 'Ending',
  preview: 'Preview', extra: 'Extra', song: 'Song' };
export const ROLES = { inner: 'voice-over', preview: 'preview', narration: 'narrator', flashback: 'flashback', extra: 'extra' };
export const ROLE_HELP = { inner: 'In italics in the subtitles: a thought, a flashback or a voice from off screen',
  preview: 'The narrated preview of the next episode', narration: 'Narration over the picture',
  flashback: 'A line from a flashback', extra: 'A bonus segment after the episode (omake)' };
export const PALETTE = ['#F08A24', '#5B8DEF', '#E0567A', '#3BAA7C', '#9B6CD6', '#E8B33A', '#4FB6C9', '#B5674D', '#7A8F2E', '#D46FC4', '#2F6F8F', '#C7432B', '#6E7BD9', '#8C6B3F'];
export const PAD_BEFORE = 0.35, PAD_AFTER = 0.45;      // a breath of context around a watched line
export const SCREEN_NOTE = { 'onscreen-speaking': 'a face on screen is talking', 'onscreen-silent': 'a face on screen, not talking',
  offscreen: 'nobody on screen' };

export const clock = s => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;
export const fold = s => String(s || '').toLowerCase();
export const totalTalk = data => data.lines.reduce((t, l) => t + l.end - l.start, 0);
export const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;


export function clipUrl(path, start, end, track) {
  return withKey(apiUrl(`analysis/clip?path=${encodeURIComponent(path)}&start=${start.toFixed(2)}&end=${end.toFixed(2)}&track=${track}`));
}

export function thumbUrl(path, name) {
  return withKey(apiUrl(`analysis/visual/thumb?path=${encodeURIComponent(path)}&name=${encodeURIComponent(name)}`));
}

// One line of separated dialogue at a time, across the whole page.
let audio = null;
export function playClip(path, start, end, track) {
  audio?.pause();
  audio = new Audio(clipUrl(path, start, end, track));
  audio.play().catch(() => {});
}
export function stopClip() { audio?.pause(); }
