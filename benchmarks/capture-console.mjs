// R1 "real ops" capture. Logs into the local console (port 3001, pointed at the local
// backend that holds the seeded incidents) and screenshots the dashboard, incident list, and
// one incident detail (AI summary). High-DPI for crisp blog hero images.
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const BASE = 'http://localhost:3001';
const EMAIL = process.argv[2];
const PASSWORD = process.argv[3] || 'benchpass123';
const OUT = 'C:/Users/ashmi/Documents/log0/log0-refs/blog-assets/screenshots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({ channel: 'chrome' });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 });
const page = await ctx.newPage();

console.log('login...');
await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
await page.fill('input[name="email"]', EMAIL);
await page.fill('input[name="password"]', PASSWORD);
await Promise.all([
  page.waitForLoadState('networkidle'),
  page.click('button[type="submit"]'),
]);
await page.waitForTimeout(2500);
console.log('after login url:', page.url());

async function shot(path, file) {
  console.log('capture', path);
  await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${OUT}/${file}`, fullPage: true });
}

await shot('/dashboard', 'r1-dashboard.png');
await shot('/incidents', 'r1-incidents.png');

// First incident detail (AI summary view), if any row links out.
try {
  await page.goto(`${BASE}/incidents`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  const link = page.locator('a[href*="/incidents/"]').first();
  if (await link.count()) {
    await link.click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2500);
    await page.screenshot({ path: `${OUT}/r1-incident-detail.png`, fullPage: true });
    console.log('capture incident detail:', page.url());
  }
} catch (e) { console.log('detail capture skipped:', e.message); }

await browser.close();
console.log('DONE ->', OUT);
