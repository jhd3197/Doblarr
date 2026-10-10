import { useState } from 'react';
import { api } from '../../lib/api.js';
import { queryClient } from '../../lib/queries.js';
import { statusClass } from '../../lib/library.js';
import { planQuery } from './queries.js';
import { notify } from '../../lib/notice.js';

export function StatusTag({ label }) {
  return <span className={`tag ${statusClass(label)}`}>{label}</span>;
}

// ISO 639-1 -> flag emoji. Windows browsers don't render regional-indicator
// emoji, so chips ALWAYS carry the code text — the emoji is a bonus glyph.
const LANG_FLAGS = {
  en: '🇬🇧', es: '🇪🇸', ja: '🇯🇵', fr: '🇫🇷', de: '🇩🇪', it: '🇮🇹', pt: '🇵🇹',
  ko: '🇰🇷', zh: '🇨🇳', hi: '🇮🇳', ru: '🇷🇺', ar: '🇸🇦',
};

function LangChip({ code, missing }) {
  const tip = missing ? `no ${code.toUpperCase()} audio (target language)` : `${code.toUpperCase()} audio present`;
  return (
    <span className={`lang-chip${missing ? ' missing' : ''}`} title={tip}>
      {LANG_FLAGS[code] ? LANG_FLAGS[code] + ' ' : ''}{code.toUpperCase()}
    </span>
  );
}

// Audio languages present (the original when unprobed), then the target
// languages still missing, dashed.
export function LangChips({ item, targets }) {
  const present = new Set(item.audio_langs?.length
    ? item.audio_langs : (item.original && item.original !== '??' ? [item.original] : []));
  return (
    <div className="lang-chips">
      {[...present].map(c => <LangChip key={c} code={c} />)}
      {(targets || []).filter(t => !present.has(t)).map(t => <LangChip key={t} code={t} missing />)}
    </div>
  );
}

// Queue one file. The title's dub plan rides along: target_lang picks the
// job's language, the rest are per-title config overrides the worker merges in.
export async function queueDub(item, kind, targets) {
  const plan = await queryClient.ensureQueryData(planQuery(item));
  const overrides = { ...plan };
  const target = overrides.target_lang;
  delete overrides.target_lang;
  if (item.episode_id) {
    const result = await api(`series/${item.tvdb_id}/queue`, { method: 'POST', json: {
      episode_ids: [item.episode_id], target_lang: target || targets?.[0] || 'en', kind, missing_only: false,
    } });
    if (!result.queued.length) throw new Error(result.skipped.map(s => s.reason).join('; '));
  } else {
    await api('jobs', { method: 'POST', json: {
      title: item.title, source: item.source, source_lang: item.original,
      target_lang: target || targets?.[0] || 'en', path: item.path, kind,
      overrides: Object.keys(overrides).length ? overrides : undefined,
    } });
  }
  queryClient.invalidateQueries({ queryKey: ['jobs'] });
}

// A button that queues a dub and says how it went: "Queuing…", "Queued ✓", "Retry".
export function QueueButton({ item, kind, targets, label, className = 'btn btn-ghost', id, disabled, title, onShow }) {
  const [state, setState] = useState({ text: label, busy: false, done: false, error: '' });
  async function run() {
    if (onShow) { onShow(); return; }
    setState({ text: kind === 'tease' ? 'Teasing…' : 'Queuing…', busy: true, done: false, error: '' });
    try {
      await queueDub(item, kind, targets);
      setState({ text: kind === 'tease' ? 'Teased ✓' : 'Queued ✓', busy: true, done: true, error: '' });
    } catch (err) {
      setState({ text: 'Retry', busy: false, done: false, error: err.message });
      notify('Could not queue dub: ' + err.message);
    }
  }
  return (
    <button type="button" className={className} id={id} disabled={disabled || state.busy}
      title={state.error || title} onClick={e => { e.stopPropagation(); run(); }}>{state.text}</button>
  );
}
