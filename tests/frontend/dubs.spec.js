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
