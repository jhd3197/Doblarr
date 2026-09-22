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
  available: { source: true, dub: true, line: true, take: true, note: '' },
  takes: [
    { take_id: 't1', origin: 'auto', state: 'generated', attempt: 0, direction: '',
      checks: { state: 'usable', duration: 2.4, rms_db: -18.2 }, selected: true,
      available: true },
    { take_id: 't2', origin: 'candidate', state: 'generated', attempt: 1, direction: 'hold back',
      checks: { state: 'defective', defects: ['clipping'] }, selected: false, available: true },
  ],
  selection: { take_id: 't1', reason: 'auto' },
};

async function openReview(page, { onDecision, onQueue, previews, versions } = {}) {
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
    return route.fulfill({ json: REVIEW });
  });
  const scenes = [];
  await page.route('**/api/jobs/review-1/scene/**', route => {
    scenes.push(new URL(route.request().url()).searchParams.get('context'));
    return route.fulfill({ json: SCENE });
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
