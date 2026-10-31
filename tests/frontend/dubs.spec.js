import { test, expect } from '@playwright/test';

const RUNNING = { id: 'r1', title: 'Harbor Lights S01E03', status: 'running', kind: 'full', stage: 'mix',
  progress: 40, source_lang: 'ja', target_lang: 'es', updated_at: '2026-10-03T21:11:00' };

test('cancelling a running dub leaves its button usable', async ({ page }) => {
  let job = RUNNING;
  await page.route('**/api/jobs', route => route.fulfill({ json: { counts: {}, paused: false, jobs: [job] } }));
  await page.route('**/api/config', route => route.fulfill({ json: {} }));
  await page.route('**/api/jobs/r1', route => {
    // The server cancels a running job rather than removing it, so the row stays.
    job = { ...RUNNING, status: 'cancelled', message: 'cancelled by user' };
    return route.fulfill({ json: { ok: true, cancelled: true } });
  });
  await page.goto('/dubs');
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByRole('button', { name: 'Remove' })).toBeEnabled();
});


test('the dubs table fits a phone screen and scrolls inside its card', async ({ page }) => {
  const done = { ...RUNNING, id: 'd1', status: 'done', title: 'Harbor Lights S01E04 — a long episode title that runs on',
    version_id: 'abcdef1234567890', translation_id: '0123456789abcdef', version_name: 'Kaito and Mina, warmer' };
  await page.route('**/api/jobs', route => route.fulfill({ json: { counts: {}, paused: false, jobs: [RUNNING, done] } }));
  await page.route('**/api/config', route => route.fulfill({ json: {} }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/dubs');
  await expect(page.getByText('Harbor Lights S01E03')).toBeVisible();
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390);
});

test('an empty search says no jobs match, an empty queue says no jobs yet', async ({ page }) => {
  let jobs = [];
  await page.route('**/api/jobs', route => route.fulfill({ json: { counts: {}, paused: false, jobs } }));
  await page.route('**/api/config', route => route.fulfill({ json: {} }));
  await page.goto('/dubs');
  await expect(page.getByText('No jobs yet')).toBeVisible();
  jobs = [RUNNING];
  await page.goto('/dubs');
  await page.locator('#globalSearch').fill('no such title');
  await expect(page.getByText('No jobs match.')).toBeVisible();
  await expect(page.getByText('No jobs yet')).toHaveCount(0);
});

test('a library card opens its title through a real link', async ({ page }) => {
  await page.route('**/api/library', route => route.fulfill({ json: { target_languages: ['es'], counts: {}, warnings: [],
    items: [{ title: 'Harbor Lights', year: 2021, tvdb_id: 81234, media_type: 'show', source: 'Sonarr',
      status: 'needs-dub', label: 'needs-dub' }] } }));
  await page.route('**/api/jobs', route => route.fulfill({ json: { counts: {}, jobs: [] } }));
  await page.goto('/library');
  const link = page.getByRole('link', { name: 'Harbor Lights' });
  await expect(link).toHaveAttribute('href', '/title/tvdb-81234/episodes');   // a show opens on its episodes
  await link.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/title\/tvdb-81234/);
});

test('a failed queue action is shown on the page, not in a blocking alert', async ({ page }) => {
  page.on('dialog', dialog => { throw new Error(`unexpected ${dialog.type()}: ${dialog.message()}`); });
  await page.route('**/api/jobs', route => route.fulfill({ json: { counts: {}, paused: false, jobs: [] } }));
  await page.route('**/api/config', route => route.fulfill({ json: {} }));
  await page.route('**/api/queue/pause', route => route.fulfill({ status: 503, json: { error: 'The worker is not running' } }));
  await page.goto('/dubs');
  await page.getByRole('button', { name: 'Pause queue' }).click();
  await expect(page.getByRole('alert')).toContainText('The worker is not running');
  await page.getByRole('button', { name: 'Dismiss' }).click();
  await expect(page.locator('#appNotice')).toHaveCount(0);
});
