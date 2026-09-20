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
    return route.fulfill({ json: { title: 'The Green Gathering', flagged: 1, editable: true, segments: [
      { index: 0, start: 192.1, end: 194.7, speaker: 'Ginko', text_src: 'Someone is coming.',
        text_translated: 'Alguien viene hacia nosotros.', has_audio: true, issues: ['timing_overflow'] },
      { index: 1, start: 197, end: 199.4, speaker: 'Shinra', text_src: 'I see.',
        text_translated: 'Entiendo.', has_audio: false, issues: [] },
    ] } });
  });
  await page.goto('/dubs');
  const open = page.getByRole('button', { name: 'Review (1)' });
  await open.click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Line 1' })).toBeVisible();
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
  expect(queued.edits[0]).toMatchObject({ index: 0, text: 'Alguien viene.', regenerate: true });
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
