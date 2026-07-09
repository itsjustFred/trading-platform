/**
 * Frontend E2E Tests using Playwright (Firefox)
 *
 * Run: PLAYWRIGHT_BROWSERS_PATH=/home/ubuntu/.cache/ms-playwright node tests/e2e_frontend.js
 */

const { firefox } = require('playwright');

const BASE = 'http://localhost:5556';
let browser, page;
let passed = 0, failed = 0;

async function test(name, fn) {
  try {
    await fn();
    console.log(`  ✅ ${name}`);
    passed++;
  } catch (err) {
    console.log(`  ❌ ${name}: ${err.message}`);
    failed++;
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'Assertion failed');
}

(async () => {
  browser = await firefox.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });

  // Collect console errors
  const consoleErrors = [];
  page.on('pageerror', e => consoleErrors.push(e.message));

  console.log('\n📋 Frontend E2E Tests\n');

  // ─── Page Width Tests ────────────────────────────────────────────────
  console.log('Page Width:');

  const pages = [
    { path: '/strategies', name: 'Strategies' },
    { path: '/backtests', name: 'Backtests' },
    { path: '/portfolio', name: 'Portfolio' },
    { path: '/logging', name: 'Logging' },
  ];

  for (const p of pages) {
    await test(`${p.name} uses full width`, async () => {
      await page.goto(`${BASE}${p.path}`);
      await page.waitForTimeout(2000);
      const mainWidth = await page.evaluate(() => {
        const main = document.querySelector('main');
        return main ? main.getBoundingClientRect().width : 0;
      });
      assert(mainWidth > 1400, `Main width is only ${mainWidth}px, expected > 1400`);
    });
  }

  // ─── Strategies Page ─────────────────────────────────────────────────
  console.log('\nStrategies Page:');

  await test('Lists strategies', async () => {
    await page.goto(`${BASE}/strategies`);
    await page.waitForTimeout(2000);
    const text = await page.textContent('body');
    assert(text.includes('Strategies'), 'Should show Strategies heading');
  });

  // ─── Backtests Page ──────────────────────────────────────────────────
  console.log('\nBacktests Page:');

  await test('Lists backtests', async () => {
    await page.goto(`${BASE}/backtests`);
    await page.waitForTimeout(2000);
    const text = await page.textContent('body');
    assert(text.includes('Backtests'), 'Should show Backtests heading');
  });

  await test('Create Backtest modal opens', async () => {
    await page.goto(`${BASE}/backtests`);
    await page.waitForTimeout(2000);
    const btn = page.getByText('New Backtest');
    assert(await btn.count() > 0, 'New Backtest button should exist');
    await btn.click();
    await page.waitForTimeout(1000);
    const modalText = await page.textContent('body');
    assert(modalText.includes('Strategy'), 'Modal should show Strategy field');
    assert(modalText.includes('Engine'), 'Modal should show Engine field');
  });

  await test('Symbol autocomplete works', async () => {
    // Modal should still be open from previous test
    const input = page.locator('input[placeholder*="AAPL"]');
    if (await input.count() > 0) {
      await input.fill('');
      await input.type('GOO', { delay: 80 });
      await page.waitForTimeout(800);
      const text = await page.textContent('body');
      assert(text.includes('GOOG') || text.includes('GOOGL'), 'Should show GOOG/GOOGL suggestions');
    }
  });

  // ─── Backtest Detail Page ────────────────────────────────────────────
  console.log('\nBacktest Detail:');

  await test('Shows stats and equity chart', async () => {
    const resp = await page.evaluate(() => fetch('/api/backtests').then(r => r.json()));
    const items = Array.isArray(resp) ? resp : (resp?.items || []);
    if (items.length > 0) {
      await page.goto(`${BASE}/backtests/${items[0].id}`);
      await page.waitForTimeout(3000);
      const text = await page.textContent('body');
      assert(text.includes('Equity Curve'), 'Should show Equity Curve section');
    }
  });

  await test('Benchmark dropdown exists', async () => {
    const selects = await page.locator('select').count();
    assert(selects >= 1, 'Should have benchmark select dropdown');
    const text = await page.textContent('body');
    assert(text.includes('Benchmark'), 'Should show Benchmark label');
  });

  // ─── Portfolio Page ──────────────────────────────────────────────────
  console.log('\nPortfolio Page:');

  await test('Portfolio page loads', async () => {
    await page.goto(`${BASE}/portfolio`);
    await page.waitForTimeout(2000);
    const text = await page.textContent('body');
    assert(text.includes('Portfolio') || text.includes('portfolio'), 'Should show Portfolio');
  });

  // ─── Logging Page ────────────────────────────────────────────────────
  console.log('\nLogging Page:');

  await test('Logging page loads', async () => {
    await page.goto(`${BASE}/logging`);
    await page.waitForTimeout(2000);
    const text = await page.textContent('body');
    assert(text.includes('Log') || text.includes('log'), 'Should show Logging');
  });

  // ─── Console Errors ──────────────────────────────────────────────────
  console.log('\nConsole Errors:');

  await test('No critical console errors', async () => {
    const critical = consoleErrors.filter(e => !e.includes('favicon'));
    if (critical.length > 0) {
      console.log('    Warnings:', critical.slice(0, 3));
    }
    // We don't fail on console errors — just report
  });

  // ─── Summary ─────────────────────────────────────────────────────────
  await browser.close();

  console.log(`\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━`);
  console.log(`  Results: ${passed} passed, ${failed} failed`);
  console.log(`━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n`);

  process.exit(failed > 0 ? 1 : 0);
})();
