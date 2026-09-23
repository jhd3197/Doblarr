import { test, expect } from '@playwright/test';

test('star nudge shows once and remembers "Not now"', async ({ page }) => {
  await page.goto('/');
  const nudge = page.locator('#starNudge');
  await expect(page.locator('#brandStar')).toHaveAttribute('href', 'https://github.com/jhd3197/Doblarr');
  await expect(nudge).toBeVisible();
  if (process.env.STAR_SHOTS) {
    await page.locator('#brandStar').hover();
    await page.waitForTimeout(250);
    await page.locator('aside').screenshot({ path: process.env.STAR_SHOTS + '/side-light.png' });
    await page.click('[data-theme-set="dark"]');
    await page.locator('aside').screenshot({ path: process.env.STAR_SHOTS + '/side-dark.png' });
  }
  await nudge.locator('[data-star-later]').click();
  await expect(nudge).toBeHidden();
  await page.reload();
  await expect(nudge).toBeHidden();
});

test('starring hides the nudge for good', async ({ page, context }) => {
  await page.goto('/');
  const popup = context.waitForEvent('page');
  await page.locator('#starNudge [data-star-go]').click();
  await (await popup).close();
  await expect(page.locator('#starNudge')).toBeHidden();
  expect(await page.evaluate(() => localStorage.getItem('doblarr.starNudge.starred'))).toBe('1');
});
