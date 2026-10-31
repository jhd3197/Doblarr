// What the scene panel's controls are asking for. The panel keeps its fields
// in one `form` object (initialForm); collectScene turns that form into the
// part of a line edit that differs from what the run already decided.
//
// `state` lives for one review: { pendingTiming, pendingEvents, pendingSpace }
// keyed by line and by event, so moving to another line does not quietly drop
// a decision a reviewer already made.

export function createSceneState() {
  return { pendingTiming: new Map(), pendingEvents: new Map(), pendingSpace: new Map(), versions: [], context: 2 };
}

export function resetSceneState(state) {
  state.pendingTiming.clear(); state.pendingEvents.clear(); state.pendingSpace.clear();
  state.versions = []; state.context = 2;
}

// Whether the Space controls are on screen for this scene.
export function spaceShown(scene, data) {
  const treatment = scene?.treatment;
  if (!treatment) return false;
  return (data?.treatments || {}).mode === 'on' || treatment.outcome === 'applied';
}

// The fields as the panel first shows them: a pending decision for this line
// if there is one, otherwise what the run decided.
export function initialForm({ scene, row, state, data }) {
  const cue = row.cue || {};
  const intent = cue.intent || {};
  const timing = state.pendingTiming.get(row.index) || scene?.timing_edit || {};
  const anchors = new Map((timing.anchors || []).map(a => [a.phrase || '', a]));
  const pauses = new Map(Object.entries(timing.pauses || {}));
  const space = state.pendingSpace.get(row.index) || scene?.treatment_edit || {};
  const treatment = scene?.treatment || {};
  const policy = data?.treatments || {};
  return {
    take: (scene?.takes || []).find(t => t.selected)?.take_id || '',
    mode: intent.mode || 'unknown',
    traits: (intent.traits || []).join(', '),
    direction: intent.direction || '',
    gain: cue.review_gain == null ? '' : String(cue.review_gain),
    candidates: '0',
    anchors: Object.fromEntries((scene?.phrasing?.phrases || [])
      .map(p => [p.phrase_id, String(anchors.get(p.phrase_id)?.at ?? '')])),
    protect: Object.fromEntries((scene?.phrasing?.pauses || [])
      .map(p => [p.pause_id, pauses.get(p.pause_id)?.protected ?? p.protected])),
    bypassTiming: !!timing.bypass,
    overlap: timing.overlap != null ? !!timing.overlap
      : (scene?.collisions || []).some(r => r.accepted && !r.accepted.stale),
    preset: space.bypass ? 'dry' : (space.preset || treatment.preset || 'dry'),
    intensity: Number(space.intensity ?? treatment.intensity ?? policy.intensity ?? 1).toFixed(1),
    dry: !!space.bypass,
    events: Object.fromEntries((scene?.events || [])
      .map(e => [e.event_id, { decision: e.decision, asset: e.asset || '' }])),
  };
}

// What the timing controls are asking for, or nothing when this line has no
// phrase plan on screen. Returning the anchors and pauses only when they
// differ from what the run already decided keeps an untouched line out of the
// re-render entirely.
function collectTiming(form, scene, row, state) {
  const plan = scene?.phrasing;
  if (!plan || plan.mode !== 'phrase') return {};
  const before = scene?.timing_edit || {};
  const patch = {};
  const anchors = (plan.phrases || [])
    .map(p => ({ phrase: p.phrase_id, at: form.anchors[p.phrase_id] }))
    .filter(a => a.at !== '' && a.at != null)
    .map(a => ({ phrase: a.phrase, edge: 'start', at: Number(a.at) }))
    .filter(a => Number.isFinite(a.at));
  const wasAnchors = (before.anchors || []).map(a => `${a.phrase}:${a.at}`).sort().join('|');
  if (anchors.map(a => `${a.phrase}:${a.at}`).sort().join('|') !== wasAnchors) patch.anchors = anchors;
  const inner = (plan.pauses || []).filter(p => p.origin !== 'boundary');
  const pauses = inner.map(p => ({
    pause: p.pause_id, protected: !!form.protect[p.pause_id], kind: form.protect[p.pause_id] ? 'pause' : 'padding',
  }));
  const planned = new Map((plan.pauses || []).map(p => [p.pause_id, p.protected]));
  const overrides = new Map(Object.entries(before.pauses || {}));
  const changed = pauses.filter(p => {
    const current = overrides.has(p.pause) ? overrides.get(p.pause).protected : planned.get(p.pause);
    return p.protected !== current;
  });
  if (changed.length) patch.pauses = pauses.filter(p => p.protected !== planned.get(p.pause));
  if (form.bypassTiming !== !!before.bypass) patch.bypass_timing = form.bypassTiming;
  if ((scene?.collisions || []).length) {
    const was = before.overlap != null ? !!before.overlap
      : (scene?.collisions || []).some(r => r.accepted && !r.accepted.stale);
    if (form.overlap !== was) patch.overlap = form.overlap;
  }
  if (Object.keys(patch).length) {
    state.pendingTiming.set(row.index, { ...(state.pendingTiming.get(row.index) || {}), ...patch });
  } else state.pendingTiming.delete(row.index);
  return patch;
}

// What the space controls are asking for, or nothing when this line's space
// is already what the run decided. A bypass is kept as a bypass rather than
// rewritten as "dry": it says *this line* is to be left alone, and it has to
// survive a change to the scene rule above it.
function collectSpace(form, scene, row, state, data) {
  if (!spaceShown(scene, data)) return {};
  const before = scene?.treatment_edit || {};
  if (form.dry) {
    if (before.bypass) { state.pendingSpace.delete(row.index); return {}; }
    state.pendingSpace.set(row.index, { bypass: true });
    return { treatment: { bypass: true } };
  }
  const intensity = Number(form.intensity);
  const wasPreset = before.bypass ? 'dry' : (before.preset || scene.treatment.preset);
  const wasIntensity = before.intensity ?? scene.treatment.intensity;
  const movedPreset = form.preset !== wasPreset;
  const movedIntensity = Number.isFinite(intensity) && Math.abs(intensity - Number(wasIntensity ?? 1)) > 1e-6;
  if (!movedPreset && !movedIntensity && !before.bypass) {
    state.pendingSpace.delete(row.index);
    return {};
  }
  const patch = { preset: form.preset };
  if (Number.isFinite(intensity)) patch.intensity = intensity;
  state.pendingSpace.set(row.index, patch);
  return { treatment: patch };
}

// Coverage decisions belong to the run, not to the line on screen: an event
// very often has no surviving cue at all. They are collected into their own
// map and submitted alongside the line edits.
function collectEvents(form, scene, state) {
  for (const before of scene?.events || []) {
    const id = before.event_id;
    const decision = form.events[id]?.decision ?? before.decision;
    const asset = (form.events[id]?.asset ?? '').trim();
    if (decision === before.decision && asset === (before.asset || '')) {
      state.pendingEvents.delete(id);
      continue;
    }
    state.pendingEvents.set(id, { event: id, decision, ...(asset ? { asset } : {}) });
  }
}

// The scene panel's part of a line edit: performance, take, timing and space.
export function collectScene({ form, scene, row, state, data }) {
  if (!form) return {};
  collectEvents(form, scene, state);
  const patch = {};
  const cue = row?.cue || {};
  const traits = form.traits.split(',').map(t => t.trim()).filter(Boolean);
  const candidates = Number(form.candidates) || 0;
  if (form.mode !== (cue.intent?.mode || 'unknown')) patch.mode = form.mode;
  if (traits.join(',') !== (cue.intent?.traits || []).join(',')) patch.traits = traits;
  if (form.direction !== (cue.intent?.direction || '')) patch.direction = form.direction;
  if (form.gain !== '') patch.gain_db = Number(form.gain);
  if (candidates) patch.candidates = candidates;
  if (form.take && form.take !== scene?.selection?.take_id) patch.take = form.take;
  const line = row || { index: -1 };
  return { ...patch, ...collectTiming(form, scene, line, state), ...collectSpace(form, scene, line, state, data) };
}
