import { test, expect } from '@playwright/test';

// The server refuses /api/jobs until it sees the key, so the app asks for it
// in its own dialog instead of window.prompt.
async function lockedJobs(page) {
  const seen = [];
  await page.route('**/api/config', route => route.fulfill({ json: {} }));
  await page.route('**/api/jobs', route => {
    const key = route.request().headers()['x-api-key'] || '';
    seen.push(key);
    if (key !== 'harbor-key') return route.fulfill({ status: 401, json: { error: 'Unauthorized' } });
    return route.fulfill({ json: { counts: {}, paused: false, jobs: [] } });
  });
  page.on('dialog', native => { throw new Error(`unexpected native ${native.type()}`); });
  return seen;
}

test('a 401 opens the API-key dialog; confirming stores the key and retries', async ({ page }) => {
  const seen = await lockedJobs(page);
  await page.goto('/dubs');
  const dialog = page.getByRole('dialog', { name: 'API key' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('textbox')).toBeFocused();
  await dialog.getByRole('textbox').fill('harbor-key');
  await dialog.getByRole('button', { name: 'OK' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('tbody .empty')).toContainText('No jobs yet');
  expect(await page.evaluate(() => localStorage.getItem('doblarr_api_key'))).toBe('harbor-key');
  expect(seen).toContain('harbor-key');
});

test('Enter in the dialog confirms it', async ({ page }) => {
  await lockedJobs(page);
  await page.goto('/dubs');
  await page.getByRole('textbox', { name: 'API key' }).fill('harbor-key');
  await page.keyboard.press('Enter');
  await expect(page.locator('tbody .empty')).toContainText('No jobs yet');
});

test('cancelling the dialog stores nothing', async ({ page }) => {
  await lockedJobs(page);
  await page.goto('/dubs');
  await page.getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByRole('dialog', { name: 'API key' })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('doblarr_api_key'))).toBeNull();
});

test('Escape closes the dialog and returns focus', async ({ page }) => {
  await lockedJobs(page);
  await page.goto('/dubs');
  await expect(page.getByRole('dialog', { name: 'API key' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'API key' })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('doblarr_api_key'))).toBeNull();
});

test('the Overview shows a Loading status while the library loads, then an empty state', async ({ page }) => {
  await page.route('**/api/config', route => route.fulfill({ json: {} }));
  await page.route('**/api/jobs', route => route.fulfill({ json: { counts: {}, paused: false, jobs: [] } }));
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/library', async route => {
    await gate;
    await route.fulfill({ json: { items: [], counts: {} } });
  });
  await page.goto('/');
  await expect(page.getByRole('status').filter({ hasText: 'Loading the library…' })).toBeVisible();
  release();
  await expect(page.getByRole('status').filter({ hasText: 'Loading the library…' })).toHaveCount(0);
  await expect(page.getByText('No titles are flagged')).toBeVisible();
});
