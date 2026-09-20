import { test, expect } from '@playwright/test';

async function showPage(page) {
  await page.route('**/api/library', route => route.fulfill({ json: { items: [{
    title: 'Mushi-Shi', tvdb_id: 79214, media_type: 'show', source: 'Sonarr · Shows',
    original: 'ja', path: '/shows/Mushi-Shi', audio_langs: ['en', 'ja'], label: 'available',
  }], target_languages: ['en', 'es'], counts: {} } }));
  await page.route('**/api/series/79214/episodes?*', route => {
    const target = new URL(route.request().url()).searchParams.get('target_lang');
    return route.fulfill({ json: { title: 'Mushi-Shi', total: 3, downloaded: 1, dubbed: target === 'en' ? 1 : 0,
      episodes: [
        { id: 1, season: 1, episode: 1, title: 'The Green Seat', downloaded: true,
          path: '/shows/Mushi-Shi/first.mkv', audio_langs: ['en', 'ja'], status: target === 'en' ? 'audio-present' : 'needs-dub', dubbed: target === 'en' },
        { id: 2, season: 1, episode: 2, title: 'The Light of the Eyelid', downloaded: false, audio_langs: [], status: 'not-downloaded' },
        { id: 3, season: 2, episode: 1, title: 'Banquet', downloaded: false, audio_langs: [], status: 'not-downloaded' },
      ] } });
  });
  await page.route('**/api/voices', route => route.fulfill({ json: { voices: [{ id: 'warm', name: 'Warm storyteller' }] } }));
  await page.goto('/title/tvdb-79214');
  await expect(page.getByRole('heading', { name: 'Episodes', exact: true })).toBeVisible();
}

test('show page lists seasons and queues explicit episodes in the chosen language', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await showPage(page);
  await expect(page.getByText('TV Show', { exact: true })).toBeVisible();
  await expect(page.getByText('1 downloaded of 3 episodes', { exact: false })).toBeVisible();
  await page.getByLabel('Dub language', { exact: true }).selectOption('es');
  await expect(page.locator('[data-episode="1"]')).toContainText('Needs dub');
  await expect(page.locator('[data-episode="2"]')).toContainText('Not downloaded');
  await page.getByText('Season 2', { exact: false }).click();
  await expect(page.getByText('Banquet', { exact: true })).toBeVisible();
  let request;
  await page.route('**/api/series/79214/queue', route => {
    request = route.request().postDataJSON();
    return route.fulfill({ json: { queued: [{ episode_id: 1, job_id: 'test' }], skipped: [] } });
  });
  await page.getByRole('button', { name: 'Queue missing dubs', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('1 file queued.');
  expect(request).toEqual({ episode_ids: [1], target_lang: 'es', kind: 'full', missing_only: true });
  await page.screenshot({ path: 'test-results/episodes-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: 'test-results/episodes-mobile.png' });
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390);
  expect(errors).toEqual([]);
});

test('narrator choice and direction persist and episode cast uses the file identity', async ({ page }) => {
  await showPage(page);
  await page.locator('[data-episode="1"]').getByRole('button', { name: 'Voices', exact: true }).click();
  await expect(page.locator('#castScope')).toContainText('S1E1');
  await page.getByLabel('Voice', { exact: true }).selectOption('warm');
  await page.getByLabel('Voice engine', { exact: true }).selectOption('qwen');
  await page.getByLabel('Delivery direction').fill('Warm and calm, with gentle pauses.');
  await page.getByRole('button', { name: 'Save narrator', exact: true }).click();
  await expect(page.locator('#narratorStatus')).toHaveText('Saved ✓');
  await page.reload();
  await page.getByRole('button', { name: 'Speakers & voices', exact: true }).click();
  await expect(page.getByLabel('Voice', { exact: true })).toHaveValue('warm');
  await expect(page.getByLabel('Delivery direction')).toHaveValue('Warm and calm, with gentle pauses.');
});
