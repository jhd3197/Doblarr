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

const buttons = await page.locator('button[data-src]').all();
console.log(`${manifest.comparison_id}: ${buttons.length} playback controls`);
if (!buttons.length) problems.push('the page offers nothing to play');

for (const button of buttons) {
  const label = (await button.textContent()).trim();
  const src = await button.getAttribute('data-src');
  await button.click();
  // Wait for real decoded audio, not just a src attribute that was set.
  const ok = await page.waitForFunction(() => {
    const player = document.getElementById('player');
    return player.readyState >= 2 && Number.isFinite(player.duration) && player.duration > 1;
  }, null, { timeout: 15000 }).then(() => true).catch(() => false);
  if (!ok) { problems.push(`${label} (${src}) did not decode`); continue; }
  const duration = await page.evaluate(() => document.getElementById('player').duration);
  await page.evaluate(() => document.getElementById('player').play());
  const moved = await page.waitForFunction(() => {
    return document.getElementById('player').currentTime > 0.15;
  }, null, { timeout: 10000 }).then(() => true).catch(() => false);
  await page.evaluate(() => document.getElementById('player').pause());
  if (!moved) problems.push(`${label} (${src}) decoded but did not play`);
  console.log(`  ${moved ? 'plays' : 'SILENT'}  ${label} · ${duration.toFixed(2)}s · ${src}`);
}

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
