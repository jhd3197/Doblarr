// Confirm a rendered comparison actually plays before anyone is asked to listen.
//
//     node scripts/verify_comparison_page.js work/benchmarks/quality-final/<id>
//
// Opens the comparison page in a real browser, clicks every playback control,
// and waits for the audio element to reach a non-zero position. A comparison
// whose files are on disk but do not decode is not a listening test, and
// finding that out from the listener is the wrong way round.
//
// Local only, like everything else under work/benchmarks.

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

import { chromium } from '@playwright/test';

const root = process.argv[2];
if (!root) {
  console.error('usage: node scripts/verify_comparison_page.js <comparison directory>');
  process.exit(2);
}

const page_url = pathToFileURL(path.resolve(root, 'index.html')).href;
const manifest = JSON.parse(readFileSync(path.resolve(root, 'manifest.json'), 'utf8'));

const browser = await chromium.launch();
const page = await browser.newPage();
const problems = [];
page.on('pageerror', e => problems.push(`page error: ${e.message}`));
await page.goto(page_url);

// Every scene, every source, actually decoded and played. The page renders
// one scene at a time, so this walks them rather than reading one screen.
const scenes = await page.locator('.scene').count();
console.log(`${manifest.comparison_id}: ${scenes} scenes`);
if (!scenes) problems.push('the page offers no scenes');
let played = 0;
for (let n = 0; n < scenes; n += 1) {
  await page.locator('.scene').nth(n).click();
  await page.waitForTimeout(150);
  const title = await page.locator('#sceneTitle').textContent();
  const sources = await page.locator('.src').count();
  for (let i = 0; i < sources; i += 1) {
    const button = page.locator('.src').nth(i);
    const label = (await button.textContent()).trim().replace(/\s+/g, ' ');
    await button.click();
    const ok = await page.waitForFunction(() => {
      const a = document.querySelector('audio') || window.__player;
      return a && a.readyState >= 2 && isFinite(a.duration) && a.duration > 1;
    }, null, { timeout: 15000 }).then(() => true).catch(() => false);
    if (!ok) { problems.push(`scene ${n + 1} / ${label} did not decode`); continue; }
    await page.evaluate(() => (document.querySelector('audio') || window.__player).play());
    const moved = await page.waitForFunction(() => {
      const a = document.querySelector('audio') || window.__player;
      return a.currentTime > 0.15;
    }, null, { timeout: 10000 }).then(() => true).catch(() => false);
    await page.evaluate(() => (document.querySelector('audio') || window.__player).pause());
    if (!moved) problems.push(`scene ${n + 1} / ${label} decoded but did not play`);
    else played += 1;
  }
  console.log(`  scene ${n + 1}: ${sources} sources play · ${title}`);
}
console.log(`  ${played} playable sources in total`);

// The judgement controls are the point of the page, not decoration.
await page.locator('.scene').first().click();
await page.getByRole('button', { name: 'Version B', exact: true }).first().click();
if (!(await page.locator('#judged').textContent()).startsWith('1 '))
  problems.push('a verdict was not recorded');
await page.locator('#markNow').click();
if (await page.locator('.mark').count() !== 1) problems.push('a moment was not marked');
if (await page.locator('.pin').count() !== 1) problems.push('a marked moment has no pin');

// Keyboard, pressed straight after touching a control, because that is where
// shortcuts have broken before.
await page.keyboard.press('2');
await page.waitForTimeout(200);
const pressed = await page.locator('.src[aria-pressed="true"]').first().textContent();
if (!pressed.includes('Version B')) problems.push('the keyboard did not switch versions');
else console.log('  verdict, moment and keyboard all work');

// A reload must not lose a judgement somebody already made.
await page.reload();
await page.waitForTimeout(500);
if (!(await page.locator('#judged').textContent()).startsWith('1 '))
  problems.push('judgements did not survive a reload');

// The label mapping is a fairness aid, never a secret.
await page.getByRole('button', { name: 'Reveal which is which' }).click();
const mapping = (await page.locator('#mapping').textContent()).trim();
for (const [label, name] of Object.entries(manifest.labels)) {
  if (!mapping.includes(`${label} = ${name}`)) problems.push(`label ${label} is not revealable`);
}
console.log(`  mapping revealed: ${mapping}`);

await browser.close();
if (problems.length) {
  console.error('\nNOT ready to hand over:');
  for (const problem of problems) console.error(`  ! ${problem}`);
  process.exit(1);
}
console.log('\nEvery variant decodes and plays. Whether it sounds better is not '
  + 'established by any of this.');
