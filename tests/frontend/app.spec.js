import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/library', route => route.fulfill({ json: {
    target_languages: ['en', 'es'], counts: { needs_dub: 1 }, warnings: [],
    items: [{ title: 'Test Film', year: 2024, tmdb_id: 42, original: 'ko',
      source: 'Radarr', label: 'needs-dub', missing: ['en'], audio_langs: ['ko'] }],
  } }));
});

test('nested routes load modules and save real settings', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/settings/voices');
  const field = page.locator('.frow').filter({ hasText: 'Voice approach' });
  await field.getByRole('button', { name: 'preset', exact: true }).click();
  const saved = page.waitForResponse(r => r.url().endsWith('/api/config') && r.request().method() === 'POST');
  await page.locator('#saveSettings').click();
  expect((await (await saved).json()).config.dub.voice_mode).toBe('preset');
  await expect(page.locator('#saveStatus')).toHaveText('Saved ✓');
  await page.reload();
  await expect(field.getByRole('button', { name: 'preset', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(errors).toEqual([]);
});

test('translation region and named versions persist in settings', async ({ page }) => {
  await page.goto('/settings/translate');
  await page.locator('.frow').filter({ hasText: 'Spanish region' })
    .getByRole('button', { name: 'es-419', exact: true }).click();
  let saved = page.waitForResponse(r => r.url().endsWith('/api/config') && r.request().method() === 'POST');
  await page.locator('#saveSettings').click();
  expect((await (await saved).json()).config.translate.locale).toBe('es-419');
  await page.goto('/settings/output');
  await page.locator('.frow').filter({ hasText: 'Version name' }).locator('input').fill('LATAM quiet');
  saved = page.waitForResponse(r => r.url().endsWith('/api/config') && r.request().method() === 'POST');
  await page.locator('#saveSettings').click();
  const config = (await (await saved).json()).config;
  expect(config.dub.version_name).toBe('LATAM quiet');
  expect(config.dub.preserve_versions).toBe(true);
  await page.reload();
  await expect(page.locator('.frow').filter({ hasText: 'Version name' }).locator('input')).toHaveValue('LATAM quiet');
});

test('finished dubs show independent script and dub identities', async ({ page }) => {
  await page.route('**/api/jobs', route => route.fulfill({ json: { counts: { done: 1 }, jobs: [{
    id: 'version-test', title: 'Test Film', source_lang: 'en', target_lang: 'es',
    status: 'done', progress: 100, version_name: 'LATAM <quiet>',
    version_id: 'abc123456789ffff', translation_id: 'def123456789ffff',
  }] } }));
  await page.goto('/dubs');
  await expect(page.locator('#dubsBody')).toContainText('LATAM <quiet> · abc123456789');
  await expect(page.locator('#dubsBody')).toContainText('Script def123456789');
  await expect(page.locator('#dubsBody quiet')).toHaveCount(0);
});

test('failed manual enqueue keeps the form open and displays the server error', async ({ page }) => {
  await page.route('**/api/jobs', route => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { error: 'Queue unavailable' } }) : route.continue());
  await page.goto('/dubs');
  await page.locator('#newDubBtn').click();
  await page.locator('#ndTitle').fill('Test Film');
  await page.locator('#ndQueue').click();
  await expect(page.locator('#newDubModal')).toBeVisible();
  await expect(page.locator('#ndStatus')).toHaveText('Queue unavailable');
  await expect(page.locator('#ndQueue')).toBeEnabled();
});

test('every editable settings field has a backend config key', async ({ page, request }) => {
  await page.goto('/settings');
  const config = await (await request.get('/api/config')).json();
  const keys = await page.evaluate(async () => {
    const { FIELD_BY_KEY } = await import('/js/settings-model.js');
    return Object.keys(FIELD_BY_KEY);
  });
  for (const key of keys) {
    const [section, name] = key.split('.');
    expect(config[section], key).toHaveProperty(name);
  }
});

test('title plans save and accompany queued jobs from a deep link', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/title/tmdb-42');
  const field = page.locator('#planFields .frow').filter({ hasText: 'Voice approach' });
  const saved = page.waitForResponse(r => r.url().endsWith('/api/plan') && r.request().method() === 'PUT');
  await field.getByRole('button', { name: 'preset', exact: true }).click();
  expect((await saved).ok()).toBe(true);
  await expect(page.locator('#planStatus')).toHaveText('Saved ✓');
  const queued = page.waitForResponse(r => r.url().endsWith('/api/jobs') && r.request().method() === 'POST');
  await page.locator('#tpQueue').click();
  expect((await (await queued).json()).job.overrides).toEqual({ 'dub.voice_mode': 'preset' });
  await expect(page.locator('#tpQueue')).toHaveText('Queued ✓');
  await page.reload();
  await expect(field.getByRole('button', { name: 'preset', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(errors).toEqual([]);
});


test('movie tabs have persistent deep links and normalize unsupported tabs', async ({ page }) => {
  await page.goto('/title/tmdb-42');
  await expect(page).toHaveURL(/\/title\/tmdb-42\/plan$/);
  for (const tab of ['voices', 'jobs', 'meta', 'plan']) {
    await page.locator(`[data-dtab="${tab}"]`).click();
    await expect(page).toHaveURL(new RegExp(`/title/tmdb-42/${tab}$`));
    await page.reload();
    await expect(page.locator(`[data-dtab="${tab}"]`)).toHaveAttribute('aria-current', 'page');
  }
  await page.goto('/title/tmdb-42/episodes');
  await expect(page).toHaveURL(/\/title\/tmdb-42\/plan$/);
});
