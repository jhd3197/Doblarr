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

