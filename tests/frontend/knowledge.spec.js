import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

async function mockVoiceCatalog(page) {
  await page.route('**/api/voice-catalog', route => {
    if (route.request().method() !== 'GET') return route.fallback();
    return route.fulfill({ json: { warnings: [], voices: [
      { key: 'profile:ginko', name: 'Ginko clone', engine: 'chatterbox', language: 'es',
        kind: 'cloned', gender: 'unknown', age: 'unknown', description: '' },
    ] } });
  });
  await page.route('**/api/voice-catalog/preview', route =>
    route.fulfill({ json: { id: 'k1' } }));
  await page.route('**/api/voice-catalog/preview/k1', route =>
    route.fulfill({ json: { status: 'completed' } }));
  await page.route('**/api/voice-catalog/preview/k1/audio', route =>
    route.fulfill({ body: 'audio', contentType: 'audio/wav' }));
}

test('knowledge page lists the initial locales and a correction is created, heard and found', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await mockVoiceCatalog(page);
  await page.goto('/knowledge');
  for (const name of ['English', 'Spanish — Mexico', 'Spanish — Venezuela'])
    await expect(page.locator('.knowledge-cards').getByText(name, { exact: true })).toBeVisible();
  const card = page.locator('.knowledge-cards .panel').filter({ hasText: 'Spanish — Mexico' });
  await expect(card).toContainText('0 reviewed · 0 proposed');
  await card.getByRole('button', { name: 'Add a correction' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.locator('.correction-phrase').fill('Ginko');
  await dialog.locator('.correction-replacement').fill('Guin-ko');
  await dialog.locator('.correction-text').fill('Ginko camina.');
  await dialog.getByRole('button', { name: 'Preview matches' }).click();
  await expect(dialog.locator('.correction-preview-out')).toContainText('Guin-ko camina.');
  await dialog.getByRole('button', { name: 'Save correction' }).click();
  await expect(dialog.locator('.correction-status')).toContainText('Saved as proposed');
  await dialog.getByRole('button', { name: 'Close' }).click();
  await expect(card).toContainText('1 proposed');  // real count, still marked unreviewed
  await page.locator('.knowledge-locale').selectOption('es-MX');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await page.locator('.knowledge-row').filter({ hasText: 'Ginko' }).click();
  const detail = page.locator('.knowledge-detail');
  await expect(detail).toContainText('Guin-ko');
  await expect(detail).toContainText('Unreviewed');
  await detail.getByRole('button', { name: 'Listen' }).click();
  await expect(detail.locator('.knowledge-audio')).toBeVisible();
  await page.screenshot({ path: 'test-results/knowledge.png' });
  expect(errors).toEqual([]);
});

test('a correction from review re-renders affected lines with updated knowledge', async ({ page }) => {
  await page.route('**/api/library', route => route.fulfill({ json: { items: [], counts: {} } }));
  await page.route('**/api/jobs', route => route.fulfill({ json: {
    jobs: [{ id: 'review-1', title: 'The Green Gathering', status: 'done', source_lang: 'ja',
      target_lang: 'es', has_review: true, review_count: 0, progress: 100 }],
    counts: { done: 1 }, paused: false } }));
  await page.route('**/api/voices', route => route.fulfill({ json: { voices: [] } }));
  let queued;
  await page.route('**/api/jobs/review-1/review', route => {
    if (route.request().method() === 'POST') {
      queued = route.request().postDataJSON();
      return route.fulfill({ json: { ok: true, job: { id: 'next' } } });
    }
    return route.fulfill({ json: { title: 'The Green Gathering', flagged: 0, editable: true,
      locale: 'es-MX', title_ref: 'film-key', show_ref: 'series:1', segments: [
        { index: 0, start: 1, end: 2.5, speaker: 'Ginko', text_src: 'Someone is coming.',
          text_translated: 'Ginko llega.', has_audio: true, issues: [], profile: 'ginko-voice' },
        { index: 1, start: 3, end: 4, speaker: 'Shinra', text_src: 'I see.',
          text_translated: 'Entiendo.', has_audio: false, issues: [] },
      ] } });
  });
  await page.goto('/dubs');
  await page.getByRole('button', { name: 'Review' }).first().click();
  await page.getByRole('button', { name: 'Fix pronunciation' }).click();
  const dialog = page.getByRole('dialog').last();
  await expect(dialog.getByRole('heading', { name: 'Fix pronunciation' })).toBeVisible();
  await dialog.locator('.correction-phrase').fill('Ginko');
  await dialog.locator('.correction-replacement').fill('Guin-ko');
  await dialog.getByRole('button', { name: 'Save correction' }).click();
  await expect(dialog.locator('.correction-status')).toContainText('Saved as proposed');
  await dialog.getByRole('button', { name: 'Close' }).click();
  const box = page.locator('#reviewCorrection');
  await expect(box).toContainText('1 line');
  await box.getByRole('button', { name: 'Re-render affected lines' }).click();
  await expect(box).toContainText('Queued with the updated knowledge');
  expect(queued.use_updated_knowledge).toBe(true);
  expect(queued.edits).toEqual([{ index: 0, regenerate: true }]);
});

test('a v2 recipe exports and applies a knowledge overlay', async ({ page }) => {
  await page.route('**/api/library', route => route.fulfill({ json: {
    target_languages: ['en', 'es'], counts: {},
    items: [{ title: 'Test Film', year: 2024, tmdb_id: 42, original: 'ko', source: 'Radarr',
      label: 'needs-dub', path: '/films/test-film.mkv', audio_langs: ['ko'], media_type: 'movie' }],
  } }));
  await page.goto('/library');
  await page.request.put('/api/plan', { data: {
    path: '/films/test-film.mkv', title: 'Test Film',
    plan: { 'dub.cast_group': 'knowledge-grp', target_lang: 'es' },
  } });
  const entry = await (await page.request.post('/api/knowledge/entries', { data: {
    phrase: 'Ginko', kind: 'pronunciation', locale: 'es', scope: 'show',
    scope_ref: 'knowledge-grp', status: 'reviewed',
  } })).json();
  await page.request.post('/api/knowledge/realizations', { data: {
    entry_id: entry.entry.id, engine: 'chatterbox', replacement: 'Guin-ko', status: 'reviewed',
  } });
  await page.goto('/title/tmdb-42/recipes');
  await page.locator('#recipeSchema').selectOption('2');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export recipe' }).click();
  const recipe = JSON.parse(readFileSync(await (await downloadPromise).path(), 'utf8'));
  expect(recipe.schema_version).toBe(2);
  expect(recipe.target_language).toBe('es');
  expect(recipe.knowledge.entries.map(e => e.phrase)).toContain('Ginko');
  await page.locator('#recipeFile').setInputFiles({
    name: 'test.dobdub', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(recipe)),
  });
  await expect(page.locator('#recipePreview')).toContainText('Knowledge overlay: 1 rule(s)');
  await page.getByRole('button', { name: 'Apply recipe' }).click();
  await expect(page.locator('#recipeStatus')).toContainText('Recipe applied');
});
