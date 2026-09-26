import { firefox } from "playwright";

const baseUrl = process.env.BASE_URL || "http://localhost:5556";
const outDir = process.env.OUT_DIR || "../temp";

function joinUrl(path) {
  return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

async function snap(page, path, filename, waitFor) {
  await page.goto(joinUrl(path), { waitUntil: "networkidle" });
  if (waitFor) {
    await page.waitForSelector(waitFor, { timeout: 30000 });
  }
  const outPath = `${outDir}/${filename}`;
  await page.screenshot({ path: outPath, fullPage: true });
  return outPath;
}

async function main() {
  const browser = await firefox.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  const shots = [];
  shots.push(await snap(page, "/backtests", "backtests-overview.png"));

  await page.goto(joinUrl("/backtests"), { waitUntil: "networkidle" });
  const targetCard = page.locator('article:has-text("native-risk_parity")').first();
  if (await targetCard.count()) {
    await targetCard.click();
  } else {
    await page.locator("article").first().click();
  }
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${outDir}/backtest-detail.png`, fullPage: true });
  shots.push(`${outDir}/backtest-detail.png`);

  shots.push(await snap(page, "/portfolios", "portfolios-overview.png"));

  await browser.close();
  console.log(JSON.stringify({ baseUrl, shots }, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
