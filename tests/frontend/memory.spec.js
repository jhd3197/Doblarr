import { test, expect } from '@playwright/test';

test('review saves a private translation and memory page can retire it', async ({ page }) => {
  await page.route('**/api/library', route => route.fulfill({ json: { items: [], counts: {} } }));
  await page.route('**/api/jobs', route => route.fulfill({ json: {
    jobs: [{ id: 'memory-review', title: 'Authored scene', status: 'done', source_lang: 'en',
      target_lang: 'es', has_review: true, review_count: 0, progress: 100 }],
    counts: { done: 1 }, paused: false,
  } }));
  await page.route('**/api/voices', route => route.fulfill({ json: { voices: [] } }));
  await page.route('**/api/jobs/memory-review/review', route => route.fulfill({ json: {
    title: 'Authored scene', source_language: 'en', locale: 'es-VE', flagged: 0, editable: true,
    segments: [{ index: 0, start: 0, end: 3, speaker: 'narrator', text_src: 'Hello, Mira.',
      text_translated: 'Hola, Mira.', has_audio: false, issues: [],
      memory_context: { register: 'neutral', speaker: 'narrator', before: [], after: [], settings: {}, glossary: {} } }],
  } }));
  await page.goto('/dubs');
  await page.getByRole('button', { name: 'Review', exact: true }).click();
  await page.getByRole('button', { name: 'Save translation for reuse' }).click();
  await page.getByLabel('Reviewer', { exact: true }).fill('Test reviewer');
  await page.getByLabel('Source meaning verified').check();
  await page.getByLabel('Regional wording verified').check();
  await page.getByLabel('Timing verified by listening').check();
  await page.getByRole('button', { name: 'Save privately' }).click();
  await expect(page.locator('.memory-status')).toContainText('Saved privately.');
  await page.goto('/knowledge');
  await page.getByRole('button', { name: 'Translation memory', exact: true }).click();
  await expect(page.locator('.knowledge-body')).toContainText('Hola, Mira.');
  await page.locator('.memory-retire').first().click();
  await expect(page.locator('.knowledge-body')).toContainText('retired');
});

test('installed packs view shows real installed releases', async ({ page }) => {
  await page.goto('/knowledge');
  await page.getByRole('button', { name: 'Installed packs', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Install file', exact: true })).toBeVisible();
  await expect(page.getByLabel('Pack ID from configured distribution')).toBeVisible();
});
