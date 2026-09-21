import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/library', route => route.fulfill({json:{items:[{title:'Test Film',tmdb_id:42,original:'ko',source:'Radarr',path:'/local/film.mkv',media_type:'movie',label:'needs-dub',audio_langs:['ko']}],target_languages:['en','es'],counts:{}}}));
});

test('recipe import waits for the saved plan before accepting a file', async ({ page }) => {
  let releasePlan;
  const planReady = new Promise(resolve => { releasePlan = resolve; });
  await page.route('**/api/plan?*', async route => {
    await planReady;
    await route.fulfill({ json: { plan: { target_lang: 'es' } } });
  });
  await page.goto('/title/tmdb-42/recipes');
  await expect(page.locator('#titleTabBody')).toHaveText('Loading saved recipe settings…');
  await expect(page.getByLabel('Import recipe file')).toHaveCount(0);
  releasePlan();
  await page.getByLabel('Import recipe file').setInputFiles({
    name: 'bad.dobdub', mimeType: 'application/json', buffer: Buffer.from('{bad'),
  });
  await expect(page.locator('#recipeStatus')).toContainText('not a valid JSON');
  await expect(page.locator('#recipeApply')).toHaveCount(0);
});

test('recipe export downloads settings only and import requires a preview before saving', async ({ page, request }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/title/tmdb-42/recipes');
  await expect(page.getByRole('heading', {name:'Dub recipes'})).toBeVisible();
  await page.locator('#recipeCreator').fill('Community creator');
  await page.locator('#recipeNotes').fill('Quiet delivery for this release.');
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', {name:'Export recipe',exact:true}).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toMatch(/\.dobdub$/);
  const recipe = JSON.parse(await readFile(await download.path(), 'utf8'));
  expect(recipe).toMatchObject({format:'doblarr-recipe',schema_version:1,mode:'recipe-only',creator:'Community creator'});
  expect(recipe.media.tmdb_id).toBe(42);
  expect(JSON.stringify(recipe)).not.toContain('film.mkv');
  expect(recipe.settings).not.toHaveProperty('dub.line_edits');
  recipe.target_language = 'es';
  recipe.characters = [{speaker_id:'S0', label:'Village elder',category:'elderly_m',voice:{name:'Missing voice',engine:'qwen',delivery:'Slow, thoughtful delivery'}}];
  const before = await (await request.get('/api/jobs')).json();
  await page.getByLabel('Import recipe file').setInputFiles({name:'test.dobdub',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(recipe))});
  await expect(page.getByRole('heading',{name:'Review recipe'})).toBeVisible();
  await expect(page.getByLabel('Local voice for Village elder (S0)')).toHaveValue('');
  await page.getByRole('button',{name:'Apply recipe to this movie'}).click();
  await expect(page.locator('#recipeStatus')).toContainText('Recipe applied');
  expect(await (await request.get('/api/jobs')).json()).toEqual(before);
  await page.getByRole('button',{name:'Speakers & voices',exact:true}).click();
  await expect(page.getByLabel('Character name', {exact:true})).toHaveValue('Village elder');
  await page.getByRole('button',{name:'Recipes',exact:true}).click();
  await page.reload();
  await expect(page.getByRole('heading',{name:'Dub recipes'})).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth',390);
  await page.screenshot({path:'test-results/recipes-mobile.png'});
  expect(errors).toEqual([]);
});

test('invalid or mismatched recipe never exposes an apply action', async ({ page }) => {
  await page.goto('/title/tmdb-42/recipes');
  await page.getByLabel('Import recipe file').setInputFiles({name:'bad.dobdub',mimeType:'application/json',buffer:Buffer.from('{bad')});
  await expect(page.locator('#recipeStatus')).toContainText('not a valid JSON');
  await expect(page.locator('#recipeApply')).toHaveCount(0);
  await page.getByLabel('Import recipe file').setInputFiles({name:'huge.dobdub',mimeType:'application/json',buffer:Buffer.alloc(129*1024)});
  await expect(page.locator('#recipeStatus')).toContainText('smaller than 128 KB');
  await expect(page.locator('#recipeApply')).toHaveCount(0);
});
