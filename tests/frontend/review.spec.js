import { test, expect } from '@playwright/test';

test('review edits one line, validates timing, and queues only the change', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/library', route => route.fulfill({ json: { items: [], counts: {} } }));
  await page.route('**/api/jobs', route => route.fulfill({ json: {
    jobs: [{ id: 'review-1', title: 'The Green Gathering', status: 'done', source_lang: 'ja', target_lang: 'es',
      has_review: true, review_count: 1, progress: 100 }], counts: { done: 1 }, paused: false,
  } }));
  await page.route('**/api/voices', route => route.fulfill({ json: { voices: [{ id: 'ginko', name: 'Ginko' }] } }));
  let queued;
  await page.route('**/api/jobs/review-1/review', route => {
    if (route.request().method() === 'POST') {
      queued = route.request().postDataJSON();
      return route.fulfill({ json: { ok: true, job: { id: 'next' } } });
    }
    return route.fulfill({ json: { title: 'The Green Gathering', flagged: 1, editable: true,
      cue_schema: 1, revision: 'rev-1', segments: [
        { index: 0, start: 192.1, end: 194.7, speaker: 'Ginko', text_src: 'Someone is coming.',
          text_translated: 'Alguien viene hacia nosotros.', has_audio: true, issues: ['timing_overflow'],
          cue: { cue_id: 'cue-aaa', source: { spans: [{ start: 192.1, end: 194.7, domain: 'source' }] },
            preparation: { decision: 'trimmed', lead: 0.42, tail: 0.18, active_duration: 2.1,
              onset: 0.25, silences: [{ start: 1, end: 1.2, domain: 'clip' }] },
            audio: { takes: [{ take_id: 't1' }], renders: [{ role: 'fitted', proven: true, available: true }, { role: 'edged', proven: true, available: true }] } } },
        { index: 1, start: 197, end: 199.4, speaker: 'Shinra', text_src: 'I see.',
          text_translated: 'Entiendo.', has_audio: false, issues: [],
          cue: { cue_id: 'cue-bbb', source: { spans: [] },
            preparation: { decision: 'uncertain', reason: 'only 4.0 dB between speech and noise' },
            audio: { takes: [], renders: [] } } },
      ] } });
  });
  await page.goto('/dubs');
  const open = page.getByRole('button', { name: 'Review (1)' });
  await open.click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Line 1' })).toBeVisible();
  // Provenance is visible: which cue this is, its source interval and what was rendered.
  await expect(page.getByText(/Cue cue-aaa .* 1 take .* rendered from edged/)).toBeVisible();
  // Boundary decisions are visible per line, including what was kept.
  await expect(page.getByText(/Boundaries: trimmed 0.42s lead \/ 0.18s tail .* 2.10s of speech .* speaks 0.25s after the cue starts .* 1 internal pause kept .* edges faded/)).toBeVisible();
  // A take that could not be analyzed says so instead of implying it was fine.
  await page.locator('#reviewFlagged').uncheck();
  await page.locator('#reviewLines [data-line="1"]').click();
  await expect(page.getByText('Boundaries: uncertain — only 4.0 dB between speech and noise')).toBeVisible();
  await page.locator('#reviewLines [data-line="0"]').click();
  await page.getByLabel('Dubbed dialogue').fill('Alguien viene.');
  await page.getByLabel('Generate a new take').check();
  await page.getByLabel('Start (seconds)').fill('200');
  await page.getByRole('button', { name: 'Render 1 changed line', exact: true }).click();
  await expect(page.locator('#reviewStatus')).toContainText('end time after its start');
  expect(queued).toBeUndefined();
  await page.getByLabel('Start (seconds)').fill('192.1');
  await expect(page.getByRole('option', { name: 'Ginko', exact: true })).toBeAttached();
  await page.screenshot({ path: 'test-results/review-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'test-results/review-mobile.png' });
  const bounds = await page.getByRole('dialog').boundingBox();
  expect(bounds.width).toBeLessThanOrEqual(390);
  await page.getByRole('button', { name: 'Render 1 changed line', exact: true }).click();
  await expect(page.locator('#reviewStatus')).toContainText('Queued.');
  expect(queued.edits).toHaveLength(1);
  expect(queued.edits[0]).toMatchObject({ index: 0, cue: 'cue-aaa', text: 'Alguien viene.', regenerate: true });
  // Edits carry the snapshot they were made against, so a moved-on review is rejected.
  expect(queued.base_revision).toBe('rev-1');
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(open).toBeFocused();
  expect(errors).toEqual([]);
});

test('generation settings validate dictionaries before saving', async ({ page }) => {
  await page.goto('/settings/translate');
  await page.getByLabel('Names and terminology').fill('not JSON');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.locator('#saveStatus')).toContainText('enter a JSON object');
  await page.getByLabel('Names and terminology').fill('{"Ginko":"Ginko"}');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.locator('#saveStatus')).toHaveText('Saved ✓');
  await page.reload();
  await expect(page.getByLabel('Names and terminology')).toHaveValue(/Ginko/);
});
