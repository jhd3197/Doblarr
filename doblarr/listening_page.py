"""The listening-test page: an instrument for judging, not a list of files.

Everything a listener produces — a per-scene verdict, the issues behind it, a
note, and a marked moment with its time, its version and how bad it was — is
captured here and exported. The previous page played files and left a Markdown
table to fill in by hand afterwards, which is the wrong way round: the thing
you noticed at 0:21 is gone by the time you have found the right row.

Three rules the layout enforces rather than suggests:

- **The transport never scrolls away.** Sticky header, one audio element, and a
  timeline you can click. A/B judgement is made of going back and forth, and a
  control you have to scroll to is a control you use less.
- **A source says what it is.** The track the dub was made from is dashed and
  labelled `original`; another localisation is labelled `reference`. They are
  never both just "source", because that is how a listener ends up comparing
  one dub against another.
- **A defect in the shared take is shown before a verdict is asked for.** Those
  lines sound identical in every version, so judging them is judging noise, and
  the page says so on the line itself and on the timeline.

The page is standalone: one HTML file plus the audio beside it. Judgements are
kept in `localStorage` so a half-finished session survives a reload, and every
read and write of it is guarded — a file opened from a private window still has
to work, it just will not remember.
"""

from __future__ import annotations

import json
from pathlib import Path

# Tokens lifted from the Doblarr design system so this page and the app look
# like one product. Radii are the listening-test overrides, not the system's.
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700;800&display=swap');
:root {
  --color-bg: #f3f2f2; --color-surface: #eae9e9; --color-text: #201e1d;
  --color-accent: #ec3013; --color-accent-700: #ae1800;
  --color-neutral-100: #f8f4f4; --color-neutral-200: #eae7e7;
  --font-body: "Archivo", system-ui, -apple-system, "Segoe UI", sans-serif;
  --shadow-sm: 0 1px 2px color-mix(in srgb, #2d2b2b 14%, transparent);
  --line: color-mix(in srgb, var(--color-text) 12%, transparent);
  --line-strong: color-mix(in srgb, var(--color-text) 20%, transparent);
  --muted: color-mix(in srgb, var(--color-text) 62%, transparent);
  --panel: var(--color-neutral-100);
}
[data-theme="dark"] {
  --color-bg: #191817; --color-surface: #232120; --color-text: #f2f0ef;
  --color-accent: #ff6a50; --color-accent-700: #ff8f7c;
  --color-neutral-100: #232120; --color-neutral-200: #2c2a28;
  --line: rgba(255,255,255,0.10); --line-strong: rgba(255,255,255,0.18);
  --muted: rgba(242,240,239,0.62); --panel: #201f1e; color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--color-bg); color: var(--color-text);
       font-family: var(--font-body); min-height: 100vh; }
h1, h3 { font-weight: 800; letter-spacing: -0.015em; }
.m { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
     font-feature-settings: "tnum" 1; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; }
.tag { border-radius: 999px; padding: 3px 10px; font-size: 12px;
       background: color-mix(in srgb, var(--color-text) 8%, transparent); }
.tag-ok { background: color-mix(in srgb, #1a6b39 14%, transparent); color: #1a6b39; }
[data-theme="dark"] .tag-ok { color: #6ee7a0; }
.tag-bad { background: color-mix(in srgb, var(--color-accent) 16%, transparent);
           color: var(--color-accent-700); }
.kbd { font-family: ui-monospace, Menlo, monospace; font-size: 11px; line-height: 1;
       padding: 4px 6px; border-radius: 5px; border: 1px solid var(--line-strong);
       color: var(--muted); min-width: 10px; text-align: center; display: inline-block; }
.btn { appearance: none; cursor: pointer; border: 1px solid var(--line-strong);
       background: transparent; color: var(--color-text); border-radius: 10px;
       padding: 7px 14px; font-family: var(--font-body); font-size: 13px;
       white-space: nowrap; }
.btn:hover { border-color: var(--color-text); }
.btn-primary { background: var(--color-accent); border-color: var(--color-accent);
               color: #fff; font-weight: 600; }
.themebtn { display: inline-flex; gap: 2px; padding: 3px; border-radius: 999px;
            background: color-mix(in srgb, var(--color-text) 8%, transparent); }
.themebtn button { appearance: none; border: 0; cursor: pointer; border-radius: 999px;
  padding: 6px 13px; font-family: var(--font-body); font-size: 12.5px;
  color: var(--muted); background: transparent; }
.themebtn button[aria-pressed="true"] { background: var(--color-bg);
  color: var(--color-text); font-weight: 600; box-shadow: var(--shadow-sm); }
.opt { appearance: none; cursor: pointer; border: 1px solid var(--line-strong);
  background: transparent; color: var(--muted); border-radius: 999px; padding: 6px 13px;
  font-family: var(--font-body); font-size: 12.5px; display: inline-flex;
  align-items: center; gap: 8px; }
.opt:hover { color: var(--color-text);
             background: color-mix(in srgb, var(--color-text) 6%, transparent); }
.opt[aria-pressed="true"] { background: var(--color-accent); border-color: var(--color-accent);
  color: #fff; font-weight: 600; }
.opt[aria-pressed="true"] .kbd { color: #fff; border-color: rgba(255,255,255,0.5); }
.src { appearance: none; cursor: pointer; text-align: left; font-family: var(--font-body);
  color: var(--color-text); background: var(--panel); border: 1px solid var(--line-strong);
  border-radius: 12px; padding: 11px 14px; display: flex; flex-direction: column;
  gap: 4px; min-width: 0; }
.src:hover { border-color: var(--color-text); }
.src[aria-pressed="true"] { background: var(--color-accent); border-color: var(--color-accent);
  color: #fff; }
.src[aria-pressed="true"] .kbd, .src[aria-pressed="true"] .srcsub {
  color: #fff; border-color: rgba(255,255,255,0.5); }
.src[data-kind="original"] { border-style: dashed; }
.srcsub { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden;
          text-overflow: ellipsis; }
.scene { appearance: none; cursor: pointer; text-align: left; width: 100%;
  font-family: var(--font-body); color: var(--color-text); background: transparent;
  border: 1px solid transparent; border-radius: 12px; padding: 11px 12px; display: grid;
  grid-template-columns: 22px minmax(0, 1fr); gap: 2px 10px; }
.scene:hover { background: color-mix(in srgb, var(--color-text) 5%, transparent); }
.scene[aria-current="true"] { background: var(--panel); border-color: var(--line-strong); }
.line { display: grid; grid-template-columns: 62px 92px minmax(0, 1fr) minmax(0, 1fr) auto;
  gap: 14px; align-items: start; padding: 11px 12px; border-radius: 10px; cursor: pointer; }
.line:hover { background: color-mix(in srgb, var(--color-text) 5%, transparent); }
.line[aria-current="true"] { background: color-mix(in srgb, var(--color-accent) 12%, transparent); }
.line + .line { border-top: 1px solid var(--line); }
.ibtn { appearance: none; cursor: pointer; border: 1px solid var(--line-strong);
  background: transparent; color: var(--muted); border-radius: 8px; padding: 5px 9px;
  font-family: var(--font-body); font-size: 12px; white-space: nowrap; }
.ibtn:hover { color: var(--color-text); border-color: var(--color-text); }
.ibtn[aria-pressed="true"] { background: var(--color-text); color: var(--color-bg);
  border-color: var(--color-text); }
.sopt { appearance: none; cursor: pointer; border: 1px solid var(--line-strong);
  background: transparent; color: var(--muted); padding: 4px 10px;
  font-family: var(--font-body); font-size: 12px; margin-left: -1px; }
.sopt:first-child { border-radius: 8px 0 0 8px; margin-left: 0; }
.sopt:last-child { border-radius: 0 8px 8px 0; }
.sopt[aria-pressed="true"] { background: var(--color-text); color: var(--color-bg);
  border-color: var(--color-text); position: relative; }
.wave { position: relative; flex: 1; min-width: 0; height: 92px; cursor: pointer;
  border-radius: 8px; background: color-mix(in srgb, var(--color-text) 4%, transparent); }
.bars { position: absolute; left: 6px; right: 6px; top: 6px; bottom: 14px;
  display: flex; align-items: center; gap: 1px; pointer-events: none; }
.wb { flex: 1; min-width: 0; min-height: 2px; border-radius: 1px;
  background: color-mix(in srgb, var(--color-text) 34%, transparent); }
.wb[data-played="true"] { background: var(--color-text); }
.seg { position: absolute; bottom: 4px; height: 5px; border-radius: 3px;
  background: var(--line-strong); }
.seg[data-active="true"] { background: var(--color-accent); }
.seg[data-defect="true"]::after { content: ""; position: absolute; left: 50%; top: -9px;
  width: 6px; height: 6px; margin-left: -3px; border-radius: 50%;
  background: var(--color-accent); }
.pin { position: absolute; bottom: -9px; transform: translateX(-50%); appearance: none;
  cursor: pointer; z-index: 2; width: 18px; height: 18px; padding: 0;
  border-radius: 50% 50% 50% 0; rotate: -45deg; border: 2px solid var(--color-bg);
  background: var(--color-text); color: var(--color-bg); font: 700 9px/1 var(--font-body); }
.pin[data-sev="major"] { background: var(--color-accent); color: #fff; }
.playhead { position: absolute; top: 2px; bottom: 2px; width: 2px; border-radius: 2px;
  background: var(--color-text); }
.mark { display: grid; grid-template-columns: 74px minmax(0, 1fr) auto; gap: 12px 16px;
  padding: 14px; border-radius: 12px; border: 1px solid var(--line); }
.mark[data-fresh="true"] { border-color: var(--color-accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-accent) 18%, transparent); }
.minput, textarea.note { width: 100%; box-sizing: border-box; font-family: var(--font-body);
  font-size: 13.5px; color: var(--color-text); background: var(--color-surface);
  border: 1px solid var(--line-strong); border-radius: 8px; padding: 8px 10px; }
textarea.note { min-height: 64px; resize: vertical; border-radius: 10px; padding: 10px 12px; }
.qmark { appearance: none; cursor: pointer; border: 1px dashed var(--line-strong);
  background: transparent; color: var(--color-text); border-radius: 999px;
  padding: 6px 12px; font-family: var(--font-body); font-size: 12.5px; }
.qmark:hover { border-style: solid; border-color: var(--color-accent);
  color: var(--color-accent-700); }
.layout { max-width: 1320px; margin: 0 auto; padding: 22px 24px 60px; display: grid;
  grid-template-columns: 280px minmax(0, 1fr); gap: 26px; align-items: start; }
.rail { position: sticky; top: 252px; display: flex; flex-direction: column; gap: 16px; }
.eyebrow { margin: 0 0 6px 12px; font-size: 11.5px; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--muted); }
.callout { display: flex; gap: 12px; align-items: baseline; padding: 12px 16px;
  border-radius: 12px; background: color-mix(in srgb, var(--color-accent) 10%, transparent); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--color-accent);
  flex: none; transform: translateY(-1px); }
@media (max-width: 1080px) {
  .layout { grid-template-columns: 1fr; }
  .rail { position: static; }
  .line { grid-template-columns: 56px minmax(0,1fr) auto; }
  .line .subs { display: none; }
}
"""

JS = r"""
const $ = s => document.querySelector(s);
const el = (tag, attrs = {}, kids = []) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  (Array.isArray(kids) ? kids : [kids]).filter(Boolean).forEach(c => n.append(c));
  return n;
};
const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const stamp = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}.`
  + String(Math.floor((s % 1) * 10));
// A runaway generation can transcribe to hundreds of characters. The whole
// string is in the manifest and the export; a script row only needs enough of
// it to recognise which defect this is.
const clip = (text, n) => (text || '').length > n ? (text.slice(0, n) + '…') : (text || '');

// Judgements live here. Every access is guarded: a page opened from a private
// window must still work, it simply will not remember anything.
const KEY = 'doblarr.listening.' + DATA.id;
function load() {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch { return {}; }
}
function save() {
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* not fatal */ }
}
const state = Object.assign({ scenes: {}, marks: [], theme: 'light', revealed: false },
                            load());
const sceneState = n => (state.scenes[n] ||= { verdict: '', issues: [], note: '' });

const player = new Audio();
player.preload = 'none';
// Reachable for a headless check: the element is created rather than markup,
// so a verifier would otherwise have nothing to query.
window.__player = player;
document.addEventListener('DOMContentLoaded', () => document.body.append(player));
let scene = 0, source = 0, matched = false, looping = null, sequence = null, fresh = null;
const scn = () => DATA.scenes[scene];
const src = () => scn().sources[source];

const VERDICTS = ['A', 'B', 'C', 'No difference', 'All bad'];
const ISSUES = [
  { label: 'Words', sub: 'wrong or missing',
    chips: ['Wrong word', 'Missing line', 'Name mangled', 'Hard to make out'] },
  { label: 'Timing', sub: 'rhythm and overlap',
    chips: ['Rushed', 'Dragging', 'Pause too short', 'Talks over', 'Late'] },
  { label: 'Voice', sub: 'acting and sound',
    chips: ['Flat', 'Too loud', 'Too quiet', 'Odd noise', 'Wrong space', 'Robotic'] },
];
const QUICK = ['Odd noise', 'Wrong word', 'Rushed', 'Too loud', 'Too quiet',
               'Flat delivery', 'Not in the original'];
const SEVERITIES = ['Minor', 'Noticeable', 'Major'];
const IN_ORIGINAL = ['Yes', 'No', 'Not sure'];

/* ---------------------------------------------------------------- transport */

function setSource(index, { keep = true } = {}) {
  const list = scn().sources;
  if (index < 0 || index >= list.length) return;
  const was = !player.paused && !player.ended;
  const at = keep ? player.currentTime : 0;
  source = index;
  const chosen = src();
  player.src = (matched && chosen.matched) || chosen.src;
  player.load();
  player.addEventListener('loadedmetadata', () => {
    if (at && isFinite(player.duration)) {
      player.currentTime = Math.min(at, Math.max(0, player.duration - 0.05));
    }
    if (was) player.play().catch(() => {});
  }, { once: true });
  paint();
}

function setScene(n, { play = true } = {}) {
  scene = Math.max(0, Math.min(DATA.scenes.length - 1, n));
  looping = null; sequence = null;
  source = Math.min(source, scn().sources.length - 1);
  render();
  if (play) setSource(source, { keep: false });
}

const seek = t => { if (isFinite(player.duration)) player.currentTime =
  Math.max(0, Math.min(t, player.duration - 0.03)); };

function versionIndexes() {
  return scn().sources.map((s, i) => [s, i]).filter(([s]) => s.kind === 'version')
    .map(([, i]) => i);
}

/* ------------------------------------------------------------------ marking */

function mark(category) {
  const at = player.currentTime || 0;
  const entry = { id: Date.now() + '-' + Math.random().toString(36).slice(2, 7),
    scene, at, source: src().id, sourceLabel: src().label,
    cats: category ? [category] : [], severity: 'Noticeable', inOriginal: '',
    also: [], note: '' };
  state.marks.push(entry); fresh = entry.id; save(); render();
}

/* ------------------------------------------------------------------ export */

function exportResults() {
  const lines = [`# Listening results — ${DATA.id}`, '',
    'Captured in the page. `same` and `worse` are real answers.', '',
    '| Label | Version |', '| --- | --- |'];
  for (const [k, v] of Object.entries(DATA.labels)) lines.push(`| ${k} | \`${v}\` |`);
  DATA.scenes.forEach((s, n) => {
    const js = state.scenes[n] || {};
    lines.push('', `## Scene ${n + 1} — ${s.title} (episode ${s.episode})`, '',
      `- Best: **${js.verdict || '(not answered)'}**`,
      `- Issues: ${(js.issues || []).join(', ') || '(none marked)'}`,
      `- Note: ${js.note || '(none)'}`);
    const mine = state.marks.filter(m => m.scene === n);
    if (mine.length) {
      lines.push('', '| At | Version | What | How bad | In the original? | Note |',
                 '| --- | --- | --- | --- | --- | --- |');
      mine.sort((a, b) => a.at - b.at).forEach(m => lines.push(
        `| ${stamp(m.at)} | ${m.sourceLabel} | ${m.cats.join(', ') || '—'} `
        + `| ${m.severity} | ${m.inOriginal || '—'} | ${m.note || ''} |`));
    }
  });
  lines.push('', '## What this cannot settle', '',
    '- A bounded excerpt says nothing about an unreviewed episode.',
    '- Recognition proves a wrong word, never a right one.', '');
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
  const a = el('a', { href: URL.createObjectURL(blob), download: `results-${DATA.id}.md` });
  document.body.append(a); a.click(); a.remove();
}

/* ------------------------------------------------------------------ drawing */

function paint() {
  const total = player.duration || scn().duration || 1;
  const at = player.currentTime || 0;
  $('#timeNow').textContent = stamp(at);
  $('#timeTotal').textContent = 'of ' + clock(total);
  $('#playGlyph').textContent = player.paused ? '▶' : '❚❚';
  $('#nowLabel').textContent = src().label;
  $('#nowSub').textContent = `Scene ${scene + 1} · ${scn().title}`
    + (matched && src().matched ? ' · level-matched' : '');
  $('#playhead').style.left = `calc(6px + ${(at / total) * 100}% * 0.985)`;
  document.querySelectorAll('.wb').forEach((b, i, all) => {
    b.dataset.played = String(i / all.length <= at / total);
  });
  document.querySelectorAll('.src').forEach((b, i) =>
    b.setAttribute('aria-pressed', String(i === source)));
  document.querySelectorAll('.seg').forEach(s =>
    s.dataset.active = String(Number(s.dataset.start) <= at && at < Number(s.dataset.end)));
  document.querySelectorAll('.line').forEach(l =>
    l.setAttribute('aria-current',
      String(Number(l.dataset.start) <= at && at < Number(l.dataset.end))));
}

function drawWave() {
  const host = $('#wave');
  host.innerHTML = '';
  const peaks = src().peaks || [];
  host.append(el('div', { class: 'bars' },
    peaks.map(p => el('div', { class: 'wb', style: `height:${Math.max(2, p * 100)}%` }))));
  const total = scn().duration || 1;
  scn().lines.forEach((l, i) => {
    const seg = el('div', { class: 'seg', title: `${l.speaker}: ${l.dub}`,
      'data-start': l.start, 'data-end': l.end, 'data-defect': String(!!l.heard),
      style: `left:${(l.start / total) * 100}%;width:${((l.end - l.start) / total) * 100}%`,
      onclick: e => { e.stopPropagation(); seek(l.start); } });
    seg.dataset.line = i;
    host.append(seg);
  });
  state.marks.filter(m => m.scene === scene).forEach(m => host.append(
    el('button', { class: 'pin', 'data-sev': m.severity === 'Major' ? 'major' : '',
      title: `${stamp(m.at)} ${m.cats.join(', ')}`, text: '!',
      style: `left:${(m.at / total) * 100}%`,
      onclick: e => { e.stopPropagation(); seek(m.at); } })));
  host.append(el('div', { class: 'playhead', id: 'playhead' }));
  paint();
}

function render() {
  const s = scn(), js = sceneState(scene);
  document.documentElement.dataset.theme = state.theme;
  document.querySelectorAll('.themebtn button').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.theme === state.theme)));

  // sources
  const sources = $('#sources');
  sources.innerHTML = '';
  sources.style.gridTemplateColumns = `repeat(${s.sources.length}, minmax(0, 1fr))`;
  s.sources.forEach((row, i) => sources.append(el('button', {
    class: 'src', 'data-kind': row.kind, 'aria-pressed': String(i === source),
    title: row.note, onclick: () => setSource(i) }, [
      el('span', { style: 'display:flex;align-items:center;gap:8px;width:100%' }, [
        el('span', { style: 'font-size:14px;font-weight:700', text: row.label }),
        el('span', { class: 'kbd', style: 'margin-left:auto', text: row.key }),
      ]),
      el('span', { class: 'srcsub', text: row.sub }),
    ])));

  // rail
  const nav = $('#sceneNav'); nav.innerHTML = '';
  DATA.scenes.forEach((row, n) => {
    const pick = (state.scenes[n] || {}).verdict;
    nav.append(el('button', { class: 'scene', 'aria-current': String(n === scene),
      onclick: () => setScene(n) }, [
        el('span', { class: 'm', style: 'font-size:13px;color:var(--muted)', text: n + 1 }),
        el('span', { style: 'font-size:13.5px;font-weight:600;line-height:1.35',
                     text: row.title }),
        el('span'),
        el('span', { style: 'display:flex;gap:8px;align-items:center;margin-top:4px' }, [
          el('span', { class: 'm', style: 'font-size:11.5px;color:var(--muted)',
                       text: `${row.duration.toFixed(0)}s · ${row.lines.length} lines` }),
          pick ? el('span', { class: 'tag tag-ok', style: 'font-size:11.5px',
                              text: pick }) : null,
        ]),
      ]));
  });

  const done = Object.values(state.scenes).filter(v => v.verdict).length;
  $('#judged').textContent = `${done} of ${DATA.scenes.length} judged`;
  const tally = $('#tally'); tally.innerHTML = '';
  const counts = {};
  Object.values(state.scenes).forEach(v => {
    if (v.verdict) counts[v.verdict] = (counts[v.verdict] || 0) + 1;
  });
  VERDICTS.forEach(v => {
    const n = counts[v] || 0;
    tally.append(el('div', {}, [
      el('div', { style: 'display:flex;gap:8px;align-items:baseline' }, [
        el('span', { style: 'font-size:13px;font-weight:600', text: v }),
        el('span', { class: 'm', style: 'margin-left:auto;font-size:13px;font-weight:700',
                     text: n }),
      ]),
      el('div', { style: 'height:5px;border-radius:999px;margin-top:6px;overflow:hidden;'
        + 'background:color-mix(in srgb, var(--color-text) 10%, transparent)' },
        el('div', { style: `height:5px;border-radius:999px;background:var(--color-accent);`
          + `width:${DATA.scenes.length ? (n / DATA.scenes.length) * 100 : 0}%` })),
    ]));
  });

  // scene header
  $('#sceneEyebrow').textContent =
    `Scene ${scene + 1} of ${DATA.scenes.length} · episode ${s.episode}`;
  $('#sceneTitle').textContent = s.title;
  $('#sceneMeta').textContent =
    `${s.duration.toFixed(0)}s · ${s.lines.length} lines · ${s.speakers.join(', ')}`;

  // One callout, about the scene on screen. A list of every defect in the run
  // would be a list of lines you are not currently listening to.
  const defects = s.lines.map((l, i) => [l, i]).filter(([l]) => l.heard);
  const box = $('#defects');
  box.innerHTML = '';
  box.hidden = !defects.length;
  if (defects.length) {
    box.style.flexDirection = 'column';
    box.append(el('p', { style: 'margin:0;font-size:13.5px;line-height:1.5;display:flex;'
      + 'gap:10px;align-items:baseline' }, [
      el('span', { class: 'dot' }),
      el('span', { text: `${defects.length} line${defects.length === 1 ? '' : 's'} here `
        + `have a flaw in the shared take — dotted on the timeline. Every version plays `
        + `them the same way, so they are not something to judge a version on.` }),
    ]));
    box.append(el('ul', { style: 'margin:0;padding-left:26px;font-size:12.5px;'
      + 'line-height:1.6' }, defects.map(([l, i]) => el('li', {}, [
        el('button', { class: 'ibtn', text: stamp(l.start),
          style: 'margin-right:8px', onclick: () => seek(l.start) }),
        el('span', { title: l.heard,
          text: `${l.speaker}: “${l.dub}” — recognizer heard “${clip(l.heard, 70)}”` }),
      ]))));
  }

  // verdict
  const verdicts = $('#verdicts'); verdicts.innerHTML = '';
  VERDICTS.forEach(v => verdicts.append(el('button', {
    class: 'opt', 'aria-pressed': String(js.verdict === v),
    style: 'padding:8px 16px;font-size:13.5px',
    text: v === 'A' || v === 'B' || v === 'C' ? `Version ${v}` : v,
    onclick: () => { js.verdict = js.verdict === v ? '' : v; save(); render(); } })));

  const cols = $('#issues'); cols.innerHTML = '';
  ISSUES.forEach(group => cols.append(el('div', {
    style: 'border:1px solid var(--line);border-radius:12px;padding:12px 14px' }, [
      el('div', { style: 'display:flex;gap:8px;align-items:baseline;margin-bottom:10px' }, [
        el('span', { style: 'font-size:13.5px;font-weight:700', text: group.label }),
        el('span', { style: 'font-size:12px;color:var(--muted)', text: group.sub }),
      ]),
      el('div', { style: 'display:flex;flex-wrap:wrap;gap:6px' },
        group.chips.map(chip => el('button', {
          class: 'ibtn', 'aria-pressed': String(js.issues.includes(chip)), text: chip,
          onclick: () => {
            js.issues = js.issues.includes(chip)
              ? js.issues.filter(c => c !== chip) : [...js.issues, chip];
            save(); render();
          } }))),
    ])));
  const note = $('#sceneNote');
  if (note.value !== js.note) note.value = js.note;

  // marks
  $('#markNow').textContent = `Mark ${stamp(player.currentTime || 0)} on ${src().label} · N`;
  const quick = $('#quick'); quick.innerHTML = '';
  QUICK.forEach(q => quick.append(el('button', { class: 'qmark', text: '+ ' + q,
    onclick: () => mark(q) })));
  const marks = $('#marks'); marks.innerHTML = '';
  const mine = state.marks.filter(m => m.scene === scene).sort((a, b) => a.at - b.at);
  $('#noMarks').hidden = mine.length > 0;
  mine.forEach(m => marks.append(markRow(m, s)));

  // script
  const script = $('#script'); script.innerHTML = '';
  s.lines.forEach(l => {
    script.append(el('div', { class: 'line', 'data-start': l.start, 'data-end': l.end,
      onclick: () => seek(l.start) }, [
      el('span', { class: 'm', style: 'font-size:12.5px;color:var(--muted);padding-top:2px',
                   text: stamp(l.start) }),
      el('span', { style: 'font-size:11.5px;font-weight:700;letter-spacing:0.05em;'
                          + 'padding-top:3px', text: l.speaker }),
      el('div', { style: 'min-width:0' }, [
        el('div', { style: 'font-size:14.5px;line-height:1.45', text: l.dub }),
        l.heard ? el('div', { style: 'margin-top:5px;font-size:12.5px;display:flex;gap:6px;'
          + 'align-items:baseline;color:var(--color-accent-700)' }, [
            el('span', { class: 'dot' }),
            el('span', { style: 'min-width:0;overflow-wrap:anywhere',
                         title: l.heard,
                         text: `Recognizer heard “${clip(l.heard, 90)}”` }),
          ]) : null,
        l.script ? el('div', { style: 'margin-top:4px;font-size:12px;color:var(--muted)',
          text: `cached script said: ${l.script}` }) : null,
      ]),
      el('div', { class: 'subs', style: 'min-width:0;font-size:13px;line-height:1.5;'
                                        + 'color:var(--muted)', text: l.source }),
      el('div', { style: 'display:flex;gap:6px' }, [
        el('button', { class: 'ibtn', text: 'Loop',
          'aria-pressed': String(looping === l.start),
          onclick: e => { e.stopPropagation(); looping = looping === l.start ? null : l.start;
                          if (looping !== null) seek(l.start); render(); } }),
        el('button', { class: 'ibtn', text: 'A→B→C',
          'aria-pressed': String(sequence && sequence.start === l.start),
          onclick: e => { e.stopPropagation(); startSequence(l); } }),
      ]),
    ]));
  });

  drawWave();
}

function markRow(m, s) {
  const CATS = QUICK;
  const row = el('div', { class: 'mark', 'data-fresh': String(fresh === m.id) });
  row.append(
    el('div', { style: 'display:flex;flex-direction:column;gap:6px' }, [
      el('button', { class: 'ibtn m', text: stamp(m.at),
        style: 'font-size:13px;font-weight:700;color:var(--color-text)',
        onclick: () => { const i = s.sources.findIndex(x => x.id === m.source);
                         if (i >= 0) setSource(i); seek(m.at); } }),
      el('span', { class: 'tag', style: 'font-size:11.5px;text-align:center',
                   text: m.sourceLabel }),
    ]),
    el('div', { style: 'min-width:0;display:flex;flex-direction:column;gap:10px' }, [
      el('div', { style: 'display:flex;flex-wrap:wrap;gap:6px' }, CATS.map(c =>
        el('button', { class: 'ibtn', text: c, 'aria-pressed': String(m.cats.includes(c)),
          onclick: () => { m.cats = m.cats.includes(c) ? m.cats.filter(x => x !== c)
                                                       : [...m.cats, c]; save(); render(); } }))),
      el('div', { style: 'display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center' }, [
        el('div', { style: 'display:flex;align-items:center;gap:8px' }, [
          el('span', { style: 'font-size:12px;color:var(--muted)', text: 'How bad' }),
          el('div', { style: 'display:flex' }, SEVERITIES.map(v => el('button', {
            class: 'sopt', text: v, 'aria-pressed': String(m.severity === v),
            onclick: () => { m.severity = v; save(); render(); } }))),
        ]),
        el('div', { style: 'display:flex;align-items:center;gap:8px' }, [
          el('span', { style: 'font-size:12px;color:var(--muted)',
                       text: 'In the original too?' }),
          el('div', { style: 'display:flex' }, IN_ORIGINAL.map(v => el('button', {
            class: 'sopt', text: v, 'aria-pressed': String(m.inOriginal === v),
            onclick: () => { m.inOriginal = v; save(); render(); } }))),
        ]),
      ]),
      el('input', { class: 'minput', type: 'text', value: m.note,
        placeholder: 'Describe it — e.g. short giggle under Renzu’s line, not in the original',
        onchange: e => { m.note = e.target.value; save(); } }),
    ]),
    el('div', { style: 'display:flex;flex-direction:column;gap:6px' }, [
      el('button', { class: 'ibtn', text: 'Hear it', onclick: () => {
        const i = s.sources.findIndex(x => x.id === m.source);
        if (i >= 0) setSource(i);
        seek(Math.max(0, m.at - 1)); player.play().catch(() => {}); } }),
      el('button', { class: 'ibtn', text: 'Hear original', onclick: () => {
        const i = s.sources.findIndex(x => x.kind === 'original');
        if (i >= 0) setSource(i);
        seek(Math.max(0, m.at - 1)); player.play().catch(() => {}); } }),
      el('button', { class: 'ibtn', text: 'Delete', onclick: () => {
        state.marks = state.marks.filter(x => x.id !== m.id); save(); render(); } }),
    ]));
  return row;
}

function startSequence(line) {
  const order = versionIndexes();
  if (!order.length) return;
  if (sequence && sequence.start === line.start) { sequence = null; render(); return; }
  sequence = { start: line.start, end: line.end, order, at: 0 };
  setSource(order[0], { keep: false });
  seek(line.start);
  player.play().catch(() => {});
  render();
}

/* ---------------------------------------------------------------- behaviour */

player.addEventListener('timeupdate', () => {
  if (looping !== null) {
    const line = scn().lines.find(l => l.start === looping);
    if (line && player.currentTime >= line.end) seek(line.start);
  }
  if (sequence && player.currentTime >= sequence.end) {
    sequence.at += 1;
    if (sequence.at >= sequence.order.length) { sequence = null; player.pause(); render(); }
    else { setSource(sequence.order[sequence.at], { keep: false }); seek(sequence.start);
           player.play().catch(() => {}); }
  }
  paint();
});
player.addEventListener('play', paint);
player.addEventListener('pause', paint);

document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t.matches('input:not([type=checkbox]), textarea, select')) return;
  const k = e.key.toLowerCase();
  const versions = versionIndexes();
  if (k === ' ') { e.preventDefault();
    player.paused ? player.play().catch(() => {}) : player.pause(); return; }
  if (k >= '1' && k <= '9') { const i = versions[Number(k) - 1];
    if (i != null) { e.preventDefault(); setSource(i); } return; }
  if (k === '0' || k === 'o') { const i = scn().sources.findIndex(s => s.kind === 'original');
    if (i >= 0) { e.preventDefault(); setSource(i); } return; }
  if (k === 'r') { const i = scn().sources.findIndex(s => s.kind === 'reference');
    if (i >= 0) { e.preventDefault(); setSource(i); } return; }
  if (k === 'm') { e.preventDefault(); matched = !matched; setSource(source); return; }
  if (k === 'n') { e.preventDefault(); mark(null); return; }
  if (k === 'g') { e.preventDefault();
    const at = player.currentTime;
    const i = scn().sources.findIndex(s => s.kind === 'original');
    if (i >= 0) { setSource(i); seek(at); } return; }
  if (k === 'p') { e.preventDefault();
    const line = scn().lines.find(l => l.start <= player.currentTime && player.currentTime < l.end);
    if (line) startSequence(line); return; }
  if (k === 'j' || k === 'k') { e.preventDefault();
    const lines = scn().lines;
    const here = lines.findIndex(l => l.start <= player.currentTime && player.currentTime < l.end);
    const next = Math.max(0, Math.min(lines.length - 1,
      (here < 0 ? 0 : here) + (k === 'k' ? 1 : -1)));
    seek(lines[next].start); return; }
  if (k === '[' || k === ']') { e.preventDefault();
    setScene(scene + (k === ']' ? 1 : -1)); }
});

/* -------------------------------------------------------------------- setup */

function boot() {
  $('#runId').textContent = DATA.id;
  $('#status').textContent = DATA.objective.ready
    ? 'Checks passed · versions comparable' : 'Checks found problems';
  $('#status').className = 'tag ' + (DATA.objective.ready ? 'tag-ok' : 'tag-bad');
  $('#note').textContent = DATA.note || '';
  $('#mapping').textContent = Object.entries(DATA.labels)
    .map(([k, v]) => `${k} = ${v}`).join(' · ');
  $('#mapping').hidden = !state.revealed;
  $('#reveal').textContent = state.revealed ? 'Mapping shown' : 'Reveal which is which';
  $('#reveal').addEventListener('click', () => {
    state.revealed = true; save();
    $('#mapping').hidden = false; $('#reveal').textContent = 'Mapping shown';
  });
  $('#export').addEventListener('click', exportResults);
  $('#playBtn').addEventListener('click', () =>
    player.paused ? player.play().catch(() => {}) : player.pause());
  $('#wave').addEventListener('click', e => {
    const box = e.currentTarget.getBoundingClientRect();
    seek(((e.clientX - box.left) / box.width) * (player.duration || scn().duration));
  });
  $('#matched').addEventListener('click', () => { matched = !matched;
    $('#matched').setAttribute('aria-pressed', String(matched)); setSource(source); });
  $('#markNow').addEventListener('click', () => mark(null));
  $('#sceneNote').addEventListener('input', e => {
    sceneState(scene).note = e.target.value; save();
  });
  $('#prevScene').addEventListener('click', () => setScene(scene - 1));
  $('#nextScene').addEventListener('click', () => setScene(scene + 1));
  document.querySelectorAll('.themebtn button').forEach(b =>
    b.addEventListener('click', () => { state.theme = b.dataset.theme; save(); render(); }));
  if (DATA.takeFindings.length) {
    $('#findingsCount').textContent =
      `${DATA.takeFindings.length} flagged take${DATA.takeFindings.length === 1 ? '' : 's'}`;
    $('#findingsCount').hidden = false;
  }
  render();
  setSource(0, { keep: false });
}
boot();
"""


def _scene_sources(row, labels, rel) -> list[dict]:
    """Every playable source for one scene, in the order a listener wants them."""
    sources = []
    for ref in sorted(row.get("references") or [], key=lambda r: r["role"] != "original"):
        original = ref["role"] == "original"
        sources.append({
            "id": f"ref-{ref['audio_index']}",
            "kind": "original" if original else "reference",
            "label": ("Original" if original else "Reference dub"),
            "sub": (f"{ref['language']} · the track this dub was made from" if original
                    else f"{ref['language']} · another localisation"),
            "note": ref["note"],
            "key": "0" if original else "R",
            "src": rel(ref["path"]),
            "matched": "",
            "peaks": ref.get("peaks") or [],
        })
    for position, (letter, name) in enumerate(labels.items(), start=1):
        variant = next((v for v in row["variants"] if v["name"] == name), None)
        if variant is None or not variant.get("mixed"):
            continue
        sources.append({
            "id": f"v-{name}",
            "kind": "version",
            "label": f"Version {letter}",
            "sub": "this dub" + (" · level-matched available" if variant.get("matched") else ""),
            "note": "",
            "key": str(position),
            "src": rel(variant["mixed"]),
            "matched": rel(variant["matched"]) if variant.get("matched") else "",
            "peaks": variant.get("peaks") or [],
        })
    return sources


def render(manifest: dict, root: Path) -> str:
    """The page, as one standalone file beside the audio it plays."""
    def rel(path) -> str:
        try:
            return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, TypeError):
            return ""

    def clock(seconds: float) -> str:
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

    labels = manifest["labels"]
    scenes = []
    for row in manifest["scenes"]:
        s = row["scene"]
        heard = {t["line"]: t for t in (row.get("takes") or [])
                 if t.get("state") == "mismatch"}
        scenes.append({
            "title": s["title"],
            "duration": s["duration"],
            "speakers": s["speakers"],
            "episode": f"{clock(s['window']['start'])}–{clock(s['window']['end'])}",
            "sources": _scene_sources(row, labels, rel),
            "lines": [{
                "start": line["start"],
                "end": min(line["start"] + 4.0, s["duration"]) if index + 1 >= len(s["text"])
                       else s["text"][index + 1]["start"],
                "speaker": line["speaker"],
                "source": line["source"],
                "dub": line["dub"] or "",
                "script": line.get("script", "") if line.get("stale_script") else "",
                "heard": (heard.get(index) or {}).get("heard", ""),
            } for index, line in enumerate(s["text"])],
        })

    data = {
        "id": manifest["comparison_id"],
        "note": manifest.get("note") or "",
        "labels": labels,
        "objective": manifest["objective"],
        "takeFindings": manifest.get("take_findings") or [],
        "scenes": scenes,
    }
    shared = manifest.get("shared_takes", True)
    takes_note = (
        "Every version reused the same imported takes — no speech was generated for "
        "this comparison." if shared else
        "Each version plays its own takes — this compares performances, not only "
        "processing. The takes were generated before the page was built.")
    findings_title = (
        "Defects in the generated speech every version shares. Each one is listed on "
        "the scene it belongs to." if shared else
        "Defects a recognizer could prove in each version's own takes.")
    return _HTML.replace("__TAKES_NOTE__", takes_note) \
                .replace("__FINDINGS_TITLE__", findings_title) \
                .replace("__DATA__", json.dumps(data, ensure_ascii=False)) \
                .replace("__CSS__", CSS).replace("__JS__", JS)


_HTML = """<!doctype html>
<html lang="en" data-theme="light"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Doblarr listening test</title>
<style>__CSS__</style>

<div style="position:sticky;top:0;z-index:5;background:var(--color-bg);
            border-bottom:1px solid var(--line)">
  <div style="max-width:1320px;margin:0 auto;padding:12px 24px 14px;display:flex;
              flex-direction:column;gap:12px">

    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <span style="font-weight:800;font-size:16px;letter-spacing:-0.01em">Doblarr</span>
      <span style="width:1px;height:18px;background:var(--line-strong)"></span>
      <span style="font-weight:600;font-size:14px">Listening test</span>
      <span class="m" id="runId" style="font-size:12px;color:var(--muted)"></span>
      <span class="tag" id="status"></span>
      <span class="tag tag-bad" id="findingsCount" hidden
            title="__FINDINGS_TITLE__"></span>
      <div style="margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="m" id="mapping" hidden
              style="font-size:12px;color:var(--muted)"></span>
        <button type="button" class="btn" id="reveal"></button>
        <button type="button" class="btn" id="export">Export results</button>
        <div class="themebtn">
          <button type="button" data-theme="light" aria-pressed="true">Light</button>
          <button type="button" data-theme="dark" aria-pressed="false">Dark</button>
        </div>
      </div>
    </div>

    <div style="display:flex;align-items:center;gap:16px">
      <button type="button" id="playBtn" aria-label="Play or pause"
        style="appearance:none;border:0;cursor:pointer;width:46px;height:46px;flex:none;
               border-radius:50%;background:var(--color-accent);color:#fff;font-size:15px;
               font-weight:700;font-family:var(--font-body)"><span id="playGlyph">▶</span></button>
      <div style="flex:none;width:92px">
        <div class="m" id="timeNow" style="font-size:15px;font-weight:700">0:00.0</div>
        <div class="m" id="timeTotal" style="font-size:11.5px;color:var(--muted)"></div>
      </div>
      <div class="wave" id="wave"></div>
      <div style="flex:none;width:190px;min-width:0">
        <div id="nowLabel" style="font-size:15px;font-weight:700;white-space:nowrap;
             overflow:hidden;text-overflow:ellipsis"></div>
        <div id="nowSub" style="font-size:12px;color:var(--muted);white-space:nowrap;
             overflow:hidden;text-overflow:ellipsis"></div>
      </div>
    </div>

    <div style="display:flex;gap:10px;align-items:stretch;flex-wrap:wrap">
      <div id="sources" style="display:grid;gap:8px;flex:1;min-width:560px"></div>
      <div style="display:grid;gap:6px;align-content:center">
        <button type="button" class="opt" id="matched" aria-pressed="false">Level-matched
          <span class="kbd">M</span></button>
      </div>
    </div>
  </div>
</div>

<div class="layout">
  <aside class="rail">
    <div style="display:flex;flex-direction:column;gap:2px">
      <p class="eyebrow">Scenes · <span id="judged"></span></p>
      <div id="sceneNav" style="display:flex;flex-direction:column;gap:2px"></div>
    </div>
    <div class="panel" style="padding:16px 18px">
      <p class="eyebrow" style="margin-left:0">Your picks so far</p>
      <div id="tally" style="display:flex;flex-direction:column;gap:10px"></div>
    </div>
    <div style="padding:0 12px;font-size:12px;line-height:1.9;color:var(--muted)">
      <div style="display:flex;flex-wrap:wrap;gap:4px 12px">
        <span><span class="kbd">1</span>–<span class="kbd">3</span> version</span>
        <span><span class="kbd">0</span> original</span>
        <span><span class="kbd">R</span> reference</span>
        <span><span class="kbd">space</span> play</span>
        <span><span class="kbd">J</span><span class="kbd">K</span> prev / next line</span>
        <span><span class="kbd">[</span><span class="kbd">]</span> scene</span>
        <span><span class="kbd">P</span> A→B→C this line</span>
        <span><span class="kbd">N</span> mark a moment</span>
        <span><span class="kbd">G</span> same moment in original</span>
      </div>
      <p style="margin:6px 0 0">Switching keeps your place in the scene.</p>
    </div>
  </aside>

  <main style="min-width:0;display:flex;flex-direction:column;gap:16px">
    <div style="display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap">
      <div style="min-width:0">
        <p class="m" id="sceneEyebrow"
           style="margin:0 0 4px;font-size:12px;color:var(--muted)"></p>
        <h1 id="sceneTitle" style="margin:0;font-size:28px;line-height:1.15"></h1>
        <p id="sceneMeta" style="margin:6px 0 0;font-size:13px;color:var(--muted)"></p>
      </div>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button type="button" class="btn" id="prevScene">← Prev</button>
        <button type="button" class="btn" id="nextScene">Next scene →</button>
      </div>
    </div>

    <div class="callout" id="defects" hidden></div>

    <div class="panel" style="padding:18px 20px 20px;display:flex;flex-direction:column;
                              gap:16px">
      <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <h3 style="margin:0;font-size:16px">Which sounded best in this scene?</h3>
        <div id="verdicts" style="display:flex;gap:6px;flex-wrap:wrap"></div>
      </div>
      <div id="issues" style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
                              gap:12px"></div>
      <textarea class="note" id="sceneNote" placeholder="What did you hear? e.g. B rushes
Ginko's second line, A breathes more naturally"></textarea>
    </div>

    <div class="panel" style="padding:18px 20px 20px;display:flex;flex-direction:column;
                              gap:14px">
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <div style="min-width:0">
          <h3 style="margin:0;font-size:16px">Moments</h3>
          <p style="margin:3px 0 0;font-size:12.5px;color:var(--muted)">Heard something odd?
            Mark it the instant it happens — it is pinned to the time and the version
            playing.</p>
        </div>
        <button type="button" class="btn btn-primary" id="markNow"
                style="margin-left:auto"></button>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
        <span style="font-size:12px;color:var(--muted);margin-right:4px">One-tap:</span>
        <div id="quick" style="display:flex;flex-wrap:wrap;gap:6px"></div>
      </div>
      <p id="noMarks" style="margin:0;padding:14px 0 2px;font-size:13px;color:var(--muted);
         border-top:1px solid var(--line)">No moments marked in this scene yet.</p>
      <div id="marks" style="display:flex;flex-direction:column;gap:10px"></div>
    </div>

    <div class="panel" style="padding:8px 8px 10px">
      <div style="display:flex;align-items:baseline;gap:12px;padding:10px 12px 8px;
                  flex-wrap:wrap">
        <h3 style="margin:0;font-size:16px">Script</h3>
        <p style="margin:0;font-size:12.5px;color:var(--muted)">Click a line to jump there.
          Loop it, or play it in every version back to back.</p>
      </div>
      <div id="script"></div>
    </div>

    <div style="padding:4px;display:flex;flex-direction:column;gap:6px;font-size:12.5px;
                line-height:1.55;color:var(--muted);max-width:90ch">
      <p id="note" style="margin:0"></p>
      <p style="margin:0">__TAKES_NOTE__ The dashed button is the original performance; the
        reference is another localisation, worth hearing but not evidence about the
        original.</p>
      <p style="margin:0">This is a bounded excerpt. A passing check means the files are
        comparable, not that any version is better.</p>
    </div>
  </main>
</div>

<script>const DATA = __DATA__;</script>
<script>__JS__</script>
</html>
"""
