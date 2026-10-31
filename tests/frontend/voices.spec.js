import { test, expect } from '@playwright/test';

const KAITO = {
  key: 'profile:vb-n', profile_id: 'vb-n', name: 'HarborLights-S01E02-scene-3-0f1e2d3c',
  engine: 'chatterbox', kind: 'cloned', language: 'es', gender: 'male', age: 'young',
  display_name: 'Kaito', character: 'KAITO', show: 'tvdb-81234', show_name: 'Harbor Lights',
};
const CLONE = { key: 'profile:vb-x', profile_id: 'vb-x', name: 'Episode-0123456789abcdef',
  engine: 'chatterbox', kind: 'cloned', language: 'es', gender: 'unknown', age: 'unknown' };
const PRESET = { key: 'preset:kokoro:em', preset_id: 'em', name: 'Alex', engine: 'kokoro',
  kind: 'preset', language: 'es', gender: 'male', age: 'unknown' };
const SHOW = { title: 'Harbor Lights', tvdb_id: 81234, media_type: 'show' };

async function routes(page, voice = KAITO) {
  await page.route('**/api/library', route => route.fulfill({ json: { items: [SHOW], counts: {} } }));
  await page.route('**/api/voice-catalog', route => route.fulfill({ json: { voices: [KAITO, CLONE, PRESET], warnings: [] } }));
  await page.route('**/api/voice-catalog/voice?*', route => route.fulfill({ json: {
    voice, used_in: [{ title_key: 'e02', title: 'S01E02', speaker: 'KAITO', label: 'Kaito',
      pitch_semitones: 0, formant_semitones: 2 }] } }));
}

test('the voices page lists each show’s characters with the voice they speak with', async ({ page }) => {
  await routes(page);
  await page.route('**/api/characters', route => route.fulfill({ json: { series_id: '', characters: [
    { id: 'chr-k', series_id: 'show:tvdb:81234', name: 'Kaito', aliases: [], voices: ['vb-n'] },
    { id: 'chr-m', series_id: 'show:tvdb:81234', name: 'Mina', aliases: [], voices: [] }] } }));
  await page.goto('/voices');
  await expect(page.locator('.voices-group h3')).toHaveCount(1);         // one show, its voice not listed twice
  await expect(page.locator('.voices-group h3')).toContainText('Harbor Lights');
  await expect(page.locator('.voice-tile')).toHaveCount(2);
  await expect(page.locator('.voice-tile', { hasText: 'Mina' })).toContainText('No voice yet');
  await page.getByRole('button', { name: /Working clones/ }).click();
  await expect(page.locator('.voices-table tbody tr')).toHaveCount(1);
  await expect(page.locator('.voices-table')).toContainText('Episode-0123456789abcdef');
  await page.getByRole('button', { name: /^Cast/ }).click();
  await page.locator('.voice-tile', { hasText: 'Kaito' }).click();
  await expect(page).toHaveURL(/\/voices\/character%3Achr-k$/);
});

test('a cast voice’s address opens its character: one page with the orb, the voice and how they sound', async ({ page, request }) => {
  const made = await (await request.post('/api/characters', { data: { series_id: 'show:tvdb:81234', name: 'Kaito' } })).json();
  await request.put(`/api/characters/${made.id}/assignments`, { data: { voice: 'vb-n', engine: 'chatterbox' } });
  await routes(page);
  let saved = null;
  await page.route('**/api/voice-catalog/traits', async route => {
    saved = route.request().postDataJSON();
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/voices/profile%3Avb-n');
  await expect(page).toHaveURL(new RegExp(`/voices/character:${made.id}$`));
  await expect(page.locator('.voice-hero h2')).toHaveText('Kaito');
  await expect(page.locator('.voice-orb, .voice-dot-xl')).toBeVisible();
  await expect(page.locator('.character-voice-line')).toContainText('Speaks with');
  await expect(page.locator('#voiceUsed').locator('..')).toContainText('+2 st formant');
  await expect(page.locator('#charTalks').locator('..')).toContainText('No analysed episode');
  await page.locator('[data-t="notes"]').fill('Loud and excitable.');
  await page.locator('.voice-color-input').fill('#f08a24');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(() => saved?.notes).toBe('Loud and excitable.');
  expect(saved).toMatchObject({ key: 'profile:vb-n', color: '#f08a24' });
  await page.getByRole('link', { name: '← All voices' }).click();
  await expect(page).toHaveURL(/\/voices$/);
});

test('a voice nobody cast yet can be cast as a character of a show', async ({ page }) => {
  await routes(page, CLONE);
  await page.route('**/api/characters', route => route.request().method() === 'POST'
    ? route.fulfill({ json: { id: 'chr-r', series_id: 'show:tvdb:81234', name: 'Ren' } })
    : route.fulfill({ json: { series_id: '', characters: [] } }));
  let cast = null, traits = null;
  await page.route('**/api/characters/chr-r/assignments', route => {
    cast = route.request().postDataJSON();
    return route.fulfill({ json: { assignments: [] } });
  });
  await page.route('**/api/voice-catalog/traits', route => {
    traits = route.request().postDataJSON();
    return route.fulfill({ json: { ok: true } });
  });
  await page.route('**/api/characters/chr-r/profile', route => route.fulfill({ status: 404, json: { detail: 'stop here' } }));
  await page.goto('/voices/profile%3Avb-x');
  await expect(page.locator('.voice-hero')).toContainText('Not cast as a character yet');
  await page.locator('[data-cast-series]').selectOption('show:tvdb:81234');
  await page.getByLabel('Cast as', { exact: true }).fill('Ren');
  await page.keyboard.press('Enter');
  await expect.poll(() => cast).toMatchObject({ voice: 'vb-x', engine: 'chatterbox' });
  expect(traits).toMatchObject({ key: 'profile:vb-x', character: 'Ren', show: 'tvdb-81234', show_name: 'Harbor Lights' });
  await expect(page).toHaveURL(/\/voices\/character%3Achr-r$/);
});

test('a character shows who voices them in published versions, as reference only', async ({ page, request }) => {
  const made = await (await request.post('/api/characters', { data: { series_id: 'show:tvdb:81234', name: 'Mina' } })).json();
  await routes(page);
  // The real profile is read through the test's own client: the page may drop
  // one of its two profile requests, which would dispose a route.fetch() response.
  const real = await (await request.get(`/api/characters/${made.id}/profile`)).json();
  real.character.published = { source: 'anilist', voice_actors: [
    { name: 'Aki Sora', language: 'Japanese', sources: ['anilist', 'ann'] },
    { name: 'Lucia Prado', language: 'Spanish', sources: ['ann'] }] };
  await page.route(`**/api/characters/${made.id}/profile`, route => route.fulfill({ json: real }));
  await page.goto(`/voices/character:${made.id}`);
  const block = page.locator('[data-published-voices]');
  await expect(block).toContainText('Japanese: Aki Sora per anilist, ann');
  await expect(block).toContainText('Spanish: Lucia Prado per ann');
  await expect(block).toContainText('never picks a voice');
});

test('a loose voice row opens from the keyboard with Space', async ({ page }) => {
  await routes(page);
  await page.route('**/api/characters', route => route.fulfill({ json: { series_id: '', characters: [] } }));
  await page.goto('/voices');
  await page.getByRole('button', { name: /Working clones/ }).click();
  await page.locator('.voices-table tbody tr').focus();
  await page.keyboard.press('Space');
  await expect(page).toHaveURL(/\/voices\/.*vb-x/);
});
