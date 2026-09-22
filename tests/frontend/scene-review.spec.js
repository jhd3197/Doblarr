import { test, expect } from '@playwright/test';

// A short decodable WAV so the player's loadedmetadata actually fires: a
// preview test that never loads its media proves nothing about playback.
function wav(seconds = 1) {
  const rate = 8000, samples = rate * seconds, size = samples * 2;
  const buffer = Buffer.alloc(44 + size);
  buffer.write('RIFF', 0); buffer.writeUInt32LE(36 + size, 4); buffer.write('WAVE', 8);
  buffer.write('fmt ', 12); buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22); buffer.writeUInt32LE(rate, 24);
  buffer.writeUInt32LE(rate * 2, 28); buffer.writeUInt16LE(2, 32);
  buffer.writeUInt16LE(16, 34); buffer.write('data', 36); buffer.writeUInt32LE(size, 40);
  for (let i = 0; i < samples; i += 1) {
    buffer.writeInt16LE(Math.round(6000 * Math.sin((2 * Math.PI * 220 * i) / rate)), 44 + i * 2);
  }
  return buffer;
}

const REVIEW = {
  title: 'The Green Gathering', flagged: 1, editable: true, cue_schema: 2,
  revision: 'rev-7', language: 'es', locale: 'es-MX', candidate_limit: 4,
  verification_policy: 'all',
  previews: { source: true, dub: true, note: '' },
  levels: { mode: 'follow_source', target_db: -20, strength: 0.7 },
  treatments: {
    mode: 'on', default: 'room', intensity: 0.8,
    scenes: [{ start: 190, end: 200, preset: 'room', intensity: 0.8, id: '190-200:room' }],
    catalogue: [
      { preset: 'dry', capability: 'supported', missing: [], tail: 0,
        summary: 'No acoustic treatment.' },
      { preset: 'room', capability: 'supported', missing: [], tail: 0.072,
        summary: 'A few early reflections.' },
      { preset: 'distant', capability: 'supported', missing: [], tail: 0.136,
        summary: 'Duller, wetter and quieter.' },
      { preset: 'phone', capability: 'unsupported', missing: ['acompressor'], tail: 0,
        summary: 'A handset.' },
    ],
  },
  delivery: {
    state: 'warned', publishable: true, output_name: 'episode.mkv', available: true,
    profile: { id: 'local:abc', meter: 'ffmpeg-ebur128/bs1770-4', target_lufs: null },
    loudness: { lufs: -18.2, true_peak_db: -4.9, meter: 'ffmpeg-ebur128/bs1770-4' },
    findings: [{ code: 'start_offset', severity: 'warning',
      detail: 'the dub stream starts at +0.300s', evidence: {} }],
  },
  segments: [
    {
      index: 0, start: 192.1, end: 194.7, speaker: 'Ginko', text_src: 'Someone is coming.',
      text_translated: 'Alguien viene hacia nosotros.', has_audio: true,
      issues: ['text_mismatch'],
      cue: {
        cue_id: 'cue-aaa',
        source: { spans: [{ start: 192.1, end: 194.7, domain: 'source' }] },
        preparation: { decision: 'kept', reason: 'no removable boundary padding' },
        intent: { mode: 'whisper', traits: ['urgent'], direction: '',
          effective: 'speak in Mexican Spanish; whispering, breathy and unprojected; urgent',
          capability: 'unsupported', unsupported: ['mode: whispering'] },
        level: { mode: 'follow_source', outcome: 'clamped', applied_db: -4,
          reason: 'source suggested -9.2 dB; bounded to -4.0 dB', peak_limited: false },
        measurement: { state: 'measured', relative_db: -9.2 },
        verification: { state: 'mismatch', reason: 'critical term difference (negation)',
          expected: 'No viene nadie', heard: 'Viene nadie',
          differences: [{ op: 'omission', at: 0, expected: ['no'], heard: [] }] },
        audio: { takes: [{ take_id: 't1' }], renders: [{ role: 'leveled', available: true }] },
        findings: [{ finding_id: 'f-1', code: 'content_critical_term', kind: 'content',
          severity: 'error', disposition: 'open',
          evidence: { expected: 'No viene nadie', heard: 'Viene nadie',
            differences: [{ op: 'omission', at: 0, expected: ['no'], heard: [] }] } }],
      },
    },
    {
      index: 1, start: 197, end: 199.4, speaker: 'Shinra', text_src: 'I see.',
      text_translated: 'Entiendo.', has_audio: true, issues: [],
      cue: { cue_id: 'cue-bbb', source: { spans: [] }, preparation: { decision: 'kept' },
        intent: {}, level: {}, measurement: {}, verification: {},
        audio: { takes: [], renders: [] }, findings: [] },
    },
  ],
};

const SCENE = {
  index: 0, cues: [0, 1], cue_ids: ['cue-aaa', 'cue-bbb'],
  target: { start: 191.5, end: 200.0, domain: 'target' },
  source: { start: 191.5, end: 200.0, domain: 'source' },
  boundary: 'heuristic',
  boundary_note: 'cues grouped by a 2.5s silence gap; this is a listening window, '
    + 'not a detected scene cut',
  revision: 'rev-7',
  available: { source: true, dub: true, line: true, take: true, note: '',
    vocals: true, bed: true, event: true, dry: true, treated: true,
    bed_note: 'estimated background (separated)' },
  phrasing: {
    mode: 'phrase', state: 'applied', planner: 'phrase-timing/1',
    reason: '2 phrases; 0.18s of padding redistributed; no stretching needed',
    slot: 2.6, planned_duration: 2.4, actual_duration: 2.42, max_stretch: 1,
    min_stretch: 1, moved: 0.18, protected_kept: 0.5, bypassed: false,
    attempts: 0, conflicts: [],
    phrases: [
      { phrase_id: 'cue-aaa:p0', order: 0, text: 'Alguien viene', at: 0, out: 0.9 },
      { phrase_id: 'cue-aaa:p1', order: 1, text: 'hacia nosotros.', at: 1.4, out: 1.0 },
    ],
    pauses: [{ pause_id: 'cue-aaa:g0', after: 'cue-aaa:p0', protected: true,
      kind: 'pause', origin: 'auto', planned: 0.5,
      clip: { start: 0.9, end: 1.4, domain: 'clip' } }],
    anchors: [{ anchor_id: 'cue-aaa:ap1s', phrase_id: 'cue-aaa:p1', edge: 'start',
      kind: 'hard', origin: 'review', at: 1.4, tolerance: 0.12, observed: 1.72,
      error: 0.32 }],
  },
  collisions: [{ code: 'timing_collision', severity: 'warning', with_line: 1,
    seconds: 0.3, source_overlap: 0,
    note: 'these two lines did not overlap in the original' }],
  events: [{ event_id: 'event-1', type: 'laugh', category: 'vocal',
    text: '[laughter]', decision: 'unresolved', coverage: 'unresolved',
    reason: 'no coverage decision has been made for this event',
    target: { start: 195.2, end: 196.0, domain: 'target' }, asset: '',
    playable: false, findings: [{ finding_id: 'ev-f1',
      code: 'reaction_uncovered', disposition: 'open', evidence: {
      note: 'a subtitle tag is not proof the sound is missing, and not proof it is there',
    } }] }],
  timing_edit: {},
  treatment: {
    preset: 'room', intensity: 0.8, origin: 'scene', outcome: 'applied',
    capability: 'supported', missing: [], tail: 0.072, makeup: -0.9,
    scene: '190-200:room', dry_role: 'edged',
    reason: 'scene rule 190-200:room; -0.9 dB of makeup kept the level',
  },
  treatment_edit: {},
  treatment_findings: [],
  takes: [
    { take_id: 't1', origin: 'auto', state: 'generated', attempt: 0, direction: '',
      checks: { state: 'usable', duration: 2.4, rms_db: -18.2 }, selected: true,
      available: true },
    { take_id: 't2', origin: 'candidate', state: 'generated', attempt: 1, direction: 'hold back',
      checks: { state: 'defective', defects: ['clipping'] }, selected: false, available: true },
  ],
  selection: { take_id: 't1', reason: 'auto' },
};

async function openReview(page,
  { onDecision, onQueue, previews, versions, phrasing, scene, review } = {}) {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/library', route => route.fulfill({ json: { items: [], counts: {} } }));
  await page.route('**/api/jobs', route => route.fulfill({ json: {
    jobs: [{ id: 'review-1', title: 'The Green Gathering', status: 'done', source_lang: 'ja',
      target_lang: 'es', has_review: true, review_count: 1, progress: 100 }],
    counts: { done: 1 }, paused: false } }));
  await page.route('**/api/voices', route => route.fulfill({ json: { voices: [] } }));
  const served = [];
  await page.route('**/api/jobs/review-1/review', route => {
    if (route.request().method() === 'POST') {
      onQueue?.(route.request().postDataJSON());
      return route.fulfill({ json: { ok: true, job: { id: 'next' }, rerun: {
        lines: [{ index: 0, work: 'generates new speech' }],
        generating: 1, processing: 0, candidates: 2,
        note: 'Lines not listed here reuse their existing audio.',
      } } });
    }
    return route.fulfill({ json: review ? { ...REVIEW, ...review } : REVIEW });
  });
  const scenes = [];
  await page.route('**/api/jobs/review-1/scene/**', route => {
    scenes.push(new URL(route.request().url()).searchParams.get('context'));
    const body = { ...SCENE, ...(phrasing ? { phrasing } : {}), ...(scene || {}) };
    return route.fulfill({ json: body });
  });
  await page.route('**/api/jobs/review-1/versions', route => route.fulfill({ json: {
    current: 'v-new', versions: versions ?? [
      { version_id: 'v-old', name: 'Dub', created_at: '2026-09-01T10:00:00+00:00',
        current: false, available: true },
      { version_id: 'v-new', name: 'Dub', created_at: '2026-09-20T10:00:00+00:00',
        current: true, available: true },
    ] } }));
  await page.route('**/api/jobs/review-1/versions/*/file**', route => {
    served.push(new URL(route.request().url()).pathname);
    return route.fulfill({ status: 200, contentType: 'audio/wav', body: wav(4) });
  });
  await page.route('**/api/jobs/review-1/decisions', route => {
    onDecision?.(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true, decision: {} } });
  });
  await page.route('**/api/jobs/review-1/preview/**', route => {
    served.push(new URL(route.request().url()).pathname);
    if (previews === 'fail') {
      return route.fulfill({ status: 409, json: { error: 'this line has no rendered audio yet' } });
    }
    return route.fulfill({ status: 200, contentType: 'audio/wav', body: wav() });
  });
  await page.goto('/dubs');
  await page.getByRole('button', { name: 'Review (1)' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  // The scene panel renders twice: once while the exchange is still loading and
  // again once it has arrived. Typing into the editor between the two would be
  // overwritten by the second render, so every test starts after it.
  await expect(page.locator('.scene-timing')).toBeVisible();
  return { errors, served, scenes };
}

test('a reviewer hears the exchange, switches original/dub, and sees why a line is flagged',
  async ({ page }) => {
    const { errors, served } = await openReview(page);
    await expect(page.getByRole('heading', { name: 'Line 1' })).toBeVisible();

    // The window says what it is: a listening window, not a detected scene cut.
    await expect(page.getByText(/2 lines · 3:11–3:20 .* not a detected scene cut/))
      .toBeVisible();

    // Exactly one player, whichever source is chosen.
    await expect(page.locator('#reviewEditor audio')).toHaveCount(1);
    const player = page.locator('#scenePlayer');
    await expect(player).toHaveAttribute('src', /preview\/dub\/0/);

    await page.getByRole('tab', { name: 'Original scene' }).click();
    await expect(player).toHaveAttribute('src', /preview\/source\/0/);
    await expect(page.getByRole('tab', { name: 'Original scene' }))
      .toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('tab', { name: 'Dubbed scene' }))
      .toHaveAttribute('aria-selected', 'false');
    await expect(page.locator('#reviewEditor audio')).toHaveCount(1);

    // The dry take and the processed line are offered as separate things.
    await page.getByRole('tab', { name: 'Processed line' }).click();
    await expect(player).toHaveAttribute('src', /preview\/line\/0/);
    await page.getByRole('tab', { name: 'Dry take' }).click();
    await expect(player).toHaveAttribute('src', /preview\/take\/0/);
    // Each source was fetched once, from its own route — never two at a time.
    await expect.poll(() => new Set(served).size).toBe(4);

    // What recognition heard, and the ordered difference behind the finding.
    await expect(page.getByText(/Words: Recognition did not match/)).toBeVisible();
    await expect(page.getByText(/Differences: omission “no”/)).toBeVisible();
    await page.getByRole('group', { name: 'Scene playback' }).waitFor();
    await page.getByText('Evidence').click();
    await expect(page.locator('.scene-finding pre')).toContainText('omission at 0');

    // An instruction the engine could not honor is shown as not applied.
    await expect(page.getByText(/does not accept delivery instructions/)).toBeVisible();
    // A clamped level says it was clamped and by how much.
    await expect(page.getByText(/Levels: follow_source .* bounded to -4\.0 dB/)).toBeVisible();
    await expect(page.getByText(/original was -9\.2 dB vs ordinary dialogue/)).toBeVisible();

    await page.screenshot({ path: 'test-results/scene-review-desktop.png' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: 'test-results/scene-review-mobile.png' });
    expect((await page.getByRole('dialog').boundingBox()).width).toBeLessThanOrEqual(390);
    expect(errors).toEqual([]);
  });

test('takes are auditioned and chosen without claiming a technical winner',
  async ({ page }) => {
    let queued;
    const { errors } = await openReview(page, { onQueue: p => { queued = p; } });
    await expect(page.getByText('Technical checks explain defects')).toBeVisible();
    await expect(page.getByText('checks: clipping')).toBeVisible();

    await page.locator('.scene-take', { hasText: 'Alternative' })
      .getByRole('button', { name: 'Audition' }).click();
    await expect(page.locator('#scenePlayer')).toHaveAttribute('src', /take=t2/);
    await expect(page.locator('#reviewEditor audio')).toHaveCount(1);

    await page.locator('input[name="sceneTake"][value="t2"]').check();
    await page.getByLabel('Gain (dB)').fill('-2.5');
    await page.getByLabel('Alternative takes').fill('2');
    await page.getByRole('button', { name: /Render 1 changed line/ }).click();
    await expect(page.locator('#reviewStatus')).toContainText('Queued.');
    expect(queued.base_revision).toBe('rev-7');
    expect(queued.edits[0]).toMatchObject({
      index: 0, cue: 'cue-aaa', take: 't2', gain_db: -2.5, candidates: 2,
    });
    expect(errors).toEqual([]);
  });

test('a verdict is recorded against the exact snapshot and a render never implies one',
  async ({ page }) => {
    let decision;
    const { errors } = await openReview(page, { onDecision: p => { decision = p; } });
    await page.locator('.scene-finding select').selectOption('accepted');
    await page.locator('.scene-finding .scene-note').fill('the character trails off here');
    await page.getByRole('button', { name: 'Save verdicts' }).click();
    await expect(page.locator('#sceneVerdictStatus'))
      .toContainText('Saved against this version of the audio');
    expect(decision).toMatchObject({
      cue: 'cue-aaa', base_revision: 'rev-7',
      dispositions: [{ finding: 'f-1', disposition: 'accepted',
        note: 'the character trails off here' }],
    });
    await expect(page.getByText(/A rendered export never marks a scene reviewed/)).toBeVisible();
    expect(errors).toEqual([]);
  });

test('filters narrow the list and a failed preview is explicit, not silent',
  async ({ page }) => {
    const { errors } = await openReview(page, { previews: 'fail' });
    await expect(page.locator('#sceneStatus')).toContainText('could not be played');

    await page.getByLabel('Show').selectOption('timing');
    await expect(page.locator('#reviewLines')).toContainText('No lines match this filter');
    await page.getByLabel('Show').selectOption('content');
    await expect(page.locator('#reviewLines [data-line="0"]')).toBeVisible();
    await expect(page.locator('#reviewLines [data-line="1"]')).toHaveCount(0);
    await page.getByLabel('Show').selectOption('all');
    await expect(page.locator('#reviewLines [data-line="1"]')).toBeVisible();
    expect(errors).toEqual([]);
  });

test('keyboard users reach the scene controls and escape back to the line list',
  async ({ page }) => {
    const { errors } = await openReview(page);
    await page.locator('#reviewLines [data-line="0"]').focus();
    await page.getByRole('tab', { name: 'Original scene' }).focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('#scenePlayer')).toHaveAttribute('src', /preview\/source\/0/);
    await page.getByLabel('Loop').check();
    await expect(page.locator('#scenePlayer')).toHaveJSProperty('loop', true);
    await page.keyboard.press('Escape');
    await expect(page.locator('#reviewLines [data-line="0"]')).toBeFocused();
    await expect(page.getByRole('dialog')).toBeVisible();   // Escape did not close it

    // Closing the review releases playback and returns focus to the opener.
    await page.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Review (1)' })).toBeFocused();
    expect(errors).toEqual([]);
  });

test('the changed scene can be compared with the version it replaced',
  async ({ page }) => {
    const { errors, served } = await openReview(page);
    await page.getByLabel('Compare with').selectOption('v-old');
    await page.getByRole('tab', { name: 'Previous version' }).click();
    await expect(page.locator('#scenePlayer')).toHaveAttribute('src', /versions\/v-old\/file/);
    await expect(page.locator('#reviewEditor audio')).toHaveCount(1);
    await expect.poll(() => served.some(p => p.endsWith('/versions/v-old/file'))).toBe(true);

    // Back to the current dub at the same moment of the scene.
    await page.getByRole('tab', { name: 'Dubbed scene' }).click();
    await expect(page.locator('#scenePlayer')).toHaveAttribute('src', /preview\/dub\/0/);
    expect(errors).toEqual([]);
  });

test('with no earlier version there is nothing to compare and the tab says so',
  async ({ page }) => {
    const { errors } = await openReview(page, { versions: [] });
    await expect(page.getByLabel('Compare with')).toHaveCount(0);
    await expect(page.getByRole('tab', { name: 'Previous version' })).toBeDisabled();
    expect(errors).toEqual([]);
  });

test('queuing says what it will cost before anything runs', async ({ page }) => {
  const { errors } = await openReview(page);
  await page.getByLabel('Line direction').fill('hold back');
  await page.getByRole('button', { name: /Render 1 changed line/ }).click();
  await expect(page.locator('#reviewStatus')).toContainText('1 line will be generated');
  await expect(page.locator('#reviewStatus')).toContainText('2 alternative takes requested');
  await expect(page.locator('#reviewStatus'))
    .toContainText('Lines not listed here reuse their existing audio');
  expect(errors).toEqual([]);
});

test('the window can be widened and the repair history is visible',
  async ({ page }) => {
    const { errors, scenes } = await openReview(page);
    await expect.poll(() => scenes).toEqual(['2']);
    await page.getByLabel('Neighbouring lines to include').fill('5');
    await page.getByLabel('Neighbouring lines to include').blur();
    await expect.poll(() => scenes).toEqual(['2', '5']);
    // What was heard, and that the pipeline already tried to repair it.
    await expect(page.getByText(/Heard: “Viene nadie”/)).toBeVisible();
    expect(errors).toEqual([]);
  });


// -- Plan 04: phrase timing and coverage -----------------------------------

test('the timing panel shows the phrases, the kept pause and the anchor that missed',
  async ({ page }) => {
    const { errors } = await openReview(page);
    const timing = page.locator('.scene-timing');
    await expect(timing).toContainText('Fitted by phrase');
    await expect(timing).toContainText('padding redistributed');
    await expect(timing.locator('.scene-phrases li')).toHaveCount(2);
    await expect(timing).toContainText('Alguien viene');
    // A protected pause says it is being kept, not that it was removed.
    await expect(timing.locator('.scene-pause')).toContainText('kept as performance');
    await expect(timing.locator('.scene-pause input')).toBeChecked();
    // The anchor reports where it actually landed.
    await expect(timing).toContainText('landed 0.32s late');
    expect(errors).toEqual([]);
  });

test('a collision names the neighbouring line and can be accepted as deliberate',
  async ({ page }) => {
    let queued = null;
    const { errors } = await openReview(page, { onQueue: body => { queued = body; } });
    const collisions = page.locator('.scene-collisions');
    await expect(collisions).toContainText('Introduced collision');
    await expect(collisions).toContainText('line 1');
    await expect(collisions).toContainText('did not overlap in the original');
    await page.locator('#sceneOverlap').check();
    await page.getByRole('button', { name: /^Render/ }).click();
    await expect.poll(() => queued?.edits?.[0]?.overlap).toBe(true);
    expect(errors).toEqual([]);
  });

test('an anchor and a protected pause are submitted as a re-render, not a regeneration',
  async ({ page }) => {
    let queued = null;
    const { errors } = await openReview(page, { onQueue: body => { queued = body; } });
    await page.locator('.scene-phrases li').nth(1).locator('.scene-anchor').fill('1.2');
    await page.locator('.scene-pause input').uncheck();
    await page.getByRole('button', { name: /^Render/ }).click();
    await expect.poll(() => queued?.edits?.[0]?.anchors?.[0]?.at).toBe(1.2);
    expect(queued.edits[0].anchors[0].phrase).toBe('cue-aaa:p1');
    expect(queued.edits[0].pauses).toEqual([
      { pause: 'cue-aaa:g0', protected: false, kind: 'padding' }]);
    // None of the fields that would cost new speech is present.
    expect(queued.edits[0].text).toBe('Alguien viene hacia nosotros.');
    expect(queued.edits[0].regenerate).toBe(false);
    expect(errors).toEqual([]);
  });

test('a line can be left exactly as generated', async ({ page }) => {
  let queued = null;
  await openReview(page, { onQueue: body => { queued = body; } });
  await page.locator('#sceneBypass').check();
  await page.getByRole('button', { name: /^Render/ }).click();
  await expect.poll(() => queued?.edits?.[0]?.bypass_timing).toBe(true);
});

test('an undecided reaction is shown as undecided and says why that is not proof',
  async ({ page }) => {
    const { errors } = await openReview(page);
    const coverage = page.locator('.scene-coverage');
    await expect(coverage).toContainText('laugh');
    await expect(coverage).toContainText('Not decided');
    await expect(coverage).toContainText('not proof');
    await expect(coverage).toContainText('estimated background (separated)');
    // Nothing to hear yet, so the control is disabled rather than absent.
    await expect(coverage.getByRole('button', { name: 'Hear it' })).toBeDisabled();
    expect(errors).toEqual([]);
  });

test('a coverage decision is submitted on its own without editing any line',
  async ({ page }) => {
    let queued = null;
    const { errors } = await openReview(page, { onQueue: body => { queued = body; } });
    await page.locator('.scene-event .scene-decision').selectOption('retain');
    const submit = page.getByRole('button', { name: /^Render/ });
    await expect(submit).toContainText('1 reaction');
    await submit.click();
    await expect.poll(() => queued?.events?.[0]).toEqual(
      { event: 'event-1', decision: 'retain' });
    expect(queued.edits).toEqual([]);
    expect(errors).toEqual([]);
  });

test('a note about a reaction is recorded against this version of the audio',
  async ({ page }) => {
    let decision = null;
    const { errors } = await openReview(page, { onDecision: p => { decision = p; } });
    await page.locator('#sceneCoverageNote').fill('the laugh is a beat late');
    await page.locator('.scene-event-disposition').selectOption('accepted');
    await page.getByRole('button', { name: 'Save coverage notes' }).click();
    await expect(page.locator('#sceneCoverageStatus'))
      .toContainText('Saved against this version of the audio');
    expect(decision).toMatchObject({
      event: 'event-1', base_revision: 'rev-7',
      note: 'the laugh is a beat late',
      dispositions: [{ finding: 'ev-f1', disposition: 'accepted' }],
    });
    expect(errors).toEqual([]);
  });

test('the bed and the separated voices are offered as their own sources',
  async ({ page }) => {
    const { errors, served } = await openReview(page);
    await page.getByRole('tab', { name: 'Background bed' }).click();
    await page.getByRole('tab', { name: 'Original voices' }).click();
    // Loading two excerpts back to back is slower than the default poll window
    // on a busy machine; the point of the test is that both are requested.
    await expect.poll(() => served.some(p => p.includes('/preview/bed/')),
      { timeout: 15000 }).toBe(true);
    await expect.poll(() => served.some(p => p.includes('/preview/vocals/')),
      { timeout: 15000 }).toBe(true);
    expect(errors).toEqual([]);
  });

test('whole-clip fitting says so instead of showing an empty phrase list',
  async ({ page }) => {
    const { errors } = await openReview(page, { phrasing: {
      mode: 'whole', state: 'bypassed', reason: 'whole-clip fitting owns this run',
      phrases: [], pauses: [], anchors: [], conflicts: [] } });
    await expect(page.locator('.scene-timing')).toContainText('Whole-clip fitting owns');
    await expect(page.locator('.scene-phrases')).toHaveCount(0);
    expect(errors).toEqual([]);
  });


// --- scene space and device treatments (Plan 05) ---------------------------

test('the space panel says which preset played, where the choice came from, and '
  + 'what it cost in level', async ({ page }) => {
  await openReview(page);
  const panel = page.locator('.scene-space');
  await expect(panel).toBeVisible();
  await expect(panel).toContainText('Playing through room');
  await expect(panel).toContainText('chosen by a scene rule');
  await expect(panel).toContainText('ringing out for 0.07s past the words');
  await expect(panel).toContainText('corrected by -0.9 dB');
  await expect(panel.getByLabel('Preset')).toHaveValue('room');
});

test('a preset this build cannot render is offered as unavailable, not silently refused',
  async ({ page }) => {
    await openReview(page);
    const option = page.locator('.scene-space option[value="phone"]');
    await expect(option).toHaveAttribute('disabled', '');
    await expect(option).toContainText('unavailable (needs acompressor)');
    // and the ones this build can do are selectable
    await expect(page.locator('.scene-space option[value="distant"]'))
      .not.toHaveAttribute('disabled', '');
  });

test('an unsupported treatment is reported as asked-for rather than applied',
  async ({ page }) => {
    await openReview(page, { scene: { treatment: {
      preset: 'phone', intensity: 1, origin: 'scene', outcome: 'unsupported',
      capability: 'unsupported', missing: ['acompressor'], tail: 0, makeup: 0,
      reason: 'this FFmpeg build has no acompressor filter' },
      available: { ...SCENE.available, treated: false } } });
    const panel = page.locator('.scene-space');
    await expect(panel).toContainText('phone was asked for');
    await expect(panel).toContainText('cannot render it');
    await expect(panel).toContainText('the line is dry');
    // and there is no "with the space" audio pretending otherwise
    await expect(page.locator('.scene-tab[data-kind="treated"]')).toBeDisabled();
  });

test('the dry line and the treated line are two separate things to play',
  async ({ page }) => {
    const { served } = await openReview(page);
    await page.locator('.scene-tab[data-kind="dry"]').click();
    await expect(page.locator('.scene-tab[data-kind="dry"]'))
      .toHaveAttribute('aria-selected', 'true');
    await page.locator('.scene-tab[data-kind="treated"]').click();
    await expect(page.locator('.scene-tab[data-kind="treated"]'))
      .toHaveAttribute('aria-selected', 'true');
    expect(served.some(p => p.includes('/preview/dry/0'))).toBe(true);
    expect(served.some(p => p.includes('/preview/treated/0'))).toBe(true);
  });

test('changing the space is queued as a re-render, never as a new performance',
  async ({ page }) => {
    let sent = null;
    await openReview(page, { onQueue: body => { sent = body; } });
    await page.locator('#sceneTreatment').selectOption('distant');
    await page.locator('#sceneIntensity').fill('0.4');
    await page.getByRole('button', { name: /^Render/ }).click();
    await expect.poll(() => sent).not.toBeNull();
    expect(sent.edits[0].treatment).toEqual({ preset: 'distant', intensity: 0.4 });
    expect(sent.edits[0].regenerate).toBeFalsy();
  });

test('a line can be left dry whatever the scene rule says', async ({ page }) => {
  let sent = null;
  await openReview(page, { onQueue: body => { sent = body; } });
  await page.locator('#sceneNoSpace').check();
  await page.getByRole('button', { name: /^Render/ }).click();
  await expect.poll(() => sent).not.toBeNull();
  expect(sent.edits[0].treatment).toEqual({ bypass: true });
});

test('an unchanged space is not submitted as an edit', async ({ page }) => {
  let sent = null;
  await openReview(page, { onQueue: body => { sent = body; } });
  // touch the timing control instead, so there is something to queue
  await page.locator('#sceneBypass').check();
  await page.getByRole('button', { name: /^Render/ }).click();
  await expect.poll(() => sent).not.toBeNull();
  expect(sent.edits[0].treatment).toBeUndefined();
});

test('with treatments off the panel says so instead of offering a dead control',
  async ({ page }) => {
    await openReview(page, {
      review: { treatments: { mode: 'off', default: 'dry', catalogue: [] } },
      scene: { treatment: { preset: 'dry', origin: 'none', outcome: 'bypassed',
        capability: 'supported', tail: 0, makeup: 0,
        reason: 'acoustic treatment is off for this run' } },
    });
    const panel = page.locator('.scene-space');
    await expect(panel).toContainText('Acoustic treatment is off for this run');
    await expect(page.locator('#sceneTreatment')).toHaveCount(0);
  });

test('a treatment finding is offered for listening rather than corrected away',
  async ({ page }) => {
    await openReview(page, { scene: { treatment_findings: [
      { code: 'treatment_transition', severity: 'info',
        note: 'the space changes between these two lines; the earlier tail is kept' }] } });
    await expect(page.locator('.scene-space')).toContainText('treatment transition');
    await expect(page.locator('.scene-space')).toContainText('the earlier tail is kept');
  });

// --- the exported file (Plan 05) -------------------------------------------

test('the export measurement is shown beside the review counts and never as approval',
  async ({ page }) => {
    await openReview(page);
    const summary = page.locator('#reviewSummary');
    await expect(summary).toContainText('export checks passed with warnings');
    await expect(summary).toContainText('-18.2 LUFS');
    await expect(summary).toContainText('measured, no target set');
    await expect(summary).toContainText('Technical checks are not a listening pass');
  });

test('a failed export says so plainly in the summary', async ({ page }) => {
  await openReview(page, { review: { delivery: {
    state: 'failed', publishable: false, output_name: 'episode.mkv',
    profile: { target_lufs: null }, loudness: {},
    findings: [{ code: 'duration_mismatch', severity: 'failure', detail: '', evidence: {} }],
  } } });
  await expect(page.locator('#reviewSummary')).toContainText('export checks FAILED');
});

test('a run with no export report simply says nothing about one', async ({ page }) => {
  await openReview(page, { review: { delivery: {} } });
  const summary = await page.locator('#reviewSummary').textContent();
  expect(summary).not.toContain('export checks');
  expect(summary).toContain('flagged for review');
});

