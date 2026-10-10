// Media helpers: clip, frame and video URLs, reel timing, track labels and
// why a line went to its voice group.
import { apiUrl, withKey } from './api.js';


export function videoUrl(path, start, end, stream) {
  return withKey(apiUrl(`analysis/video?path=${encodeURIComponent(path)}&start=${start.toFixed(2)}&end=${end.toFixed(2)}`
    + (stream === null || stream === undefined ? '' : `&audio=${stream}`)));
}

export function frameUrl(path, t) {
  return withKey(apiUrl(`analysis/frame?path=${encodeURIComponent(path)}&t=${t.toFixed(2)}`));
}

// The episode's cast: voice groups given the same name are one character
// (with each group kept, so a wrong join is visible); unnamed groups stand
// alone. Most talk first.
export function castOf(data) {
  const byKey = new Map();
  for (const l of data.lines) {
    const key = data.names[l.speaker] || l.speaker;
    if (!byKey.has(key)) byKey.set(key, { key, named: Boolean(data.names[l.speaker]), groups: new Map() });
    const groups = byKey.get(key).groups;
    if (!groups.has(l.speaker)) groups.set(l.speaker, { label: l.speaker, lines: 0, seconds: 0, list: [] });
    const g = groups.get(l.speaker);
    g.lines += 1; g.seconds += l.end - l.start; g.list.push(l);
  }
  return [...byKey.values()].map(c => {
    const groups = [...c.groups.values()].sort((a, b) => b.seconds - a.seconds);
    const all = groups.flatMap(g => g.list);
    // A line worth reading: the longest of the middle-length ones, not a grunt.
    const sample = [...all].filter(l => l.text.length >= 12).sort((a, b) => (b.end - b.start) - (a.end - a.start))[0];
    return { key: c.key, named: c.named, groups, labels: groups.map(g => g.label),
      lines: all.length, seconds: groups.reduce((t, g) => t + g.seconds, 0), sample };
  }).sort((a, b) => b.seconds - a.seconds);
}

// Which lines to show as pictures: a face talking on screen first, then a
// face on screen, then the longest; never two from the same moment.
export function pickFrames(lines, screen = {}, count = 5) {
  const score = l => ({ 'onscreen-speaking': 3, 'onscreen-silent': 1 }[screen[l.cue]] || 0)
    + Math.min(2, l.end - l.start) / 2;
  const picked = [];
  for (const l of [...lines].sort((a, b) => score(b) - score(a))) {
    if (picked.length >= count) break;
    if (picked.every(p => Math.abs(p.start - l.start) > 12)) picked.push(l);
  }
  return picked.sort((a, b) => a.start - b.start);
}

export function trackLabel(track) {
  let language = track.lang;
  try { language = new Intl.DisplayNames(['en'], { type: 'language' }).of(track.lang) || track.lang; } catch { /* keep the code */ }
  // "Spanish · Latino" and "Spanish · Castellano" are two tracks, not one.
  const title = (track.title || '').replace(/\[[^\]]*\]/g, '').replace(/\b\d\.\d\b|FLAC|AAC|AC3|DTS|-/gi, '').replace(/\s+/g, ' ').trim();
  return title && fold(title) !== fold(language) && !/^(eng|jpn|spa|english|japanese|spanish)$/i.test(title)
    ? `${language} · ${title}` : language;
}
const fold = s => String(s || '').toLowerCase();

export function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

// Reel time <-> (clip, offset). Pure, so it can be tested on its own.
export function reelLayout(clips) {
  let at = 0;
  const offsets = clips.map(c => { const o = at; at += Math.max(0, c.end - c.start); return o; });
  return { offsets, total: at };
}

export function locate(layout, time) {
  const { offsets, total } = layout;
  const t = Math.min(Math.max(0, time), total);
  let i = offsets.length - 1;
  while (i > 0 && offsets[i] > t) i -= 1;
  return { index: Math.max(0, i), offset: t - (offsets[i] || 0) };
}

const METHOD = { clustered: 'heard and grouped', merged_small: 'a small group folded into a larger voice',
  nearest: 'too short to group; joined the closest voice', inherited: 'no usable voice; took the previous speaker',
  manual: 'assigned by hand' };

export function whyText(why) {
  if (!why) return '';
  const parts = [METHOD[why.method] || why.method];
  if (why.margin != null) parts.push(`margin ${why.margin.toFixed(2)} over the next voice`);
  if (why.candidates?.length > 1) parts.push(`also close: ${why.candidates.slice(1).map(c => `${c.label} ${c.similarity.toFixed(2)}`).join(', ')}`);
  return parts.join(' · ');
}

export const weak = why => why && ['merged_small', 'nearest', 'inherited'].includes(why.method)
  || (why?.method === 'clustered' && why.margin != null && why.margin < 0.05);
