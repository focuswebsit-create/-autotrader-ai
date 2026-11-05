// End-to-end check of /terminal against a RUNNING stack (desktop + iPhone 14).
//
//   E2E_BASE=http://localhost:3000 E2E_USER=owner E2E_PASSWORD=... \
//   E2E_ALLOW_START=1 E2E_SHOTS=./shots node e2e/terminal.e2e.cjs
//
// Uses Playwright (not a dependency of the app; install it separately or use a machine
// where `require('playwright')` works). Without E2E_ALLOW_START=1 it never presses
// "start" and only checks the anonymous / logged-in views. Everything is PAPER.
const { chromium, devices } = require("playwright");
const fs = require("fs");

const BASE = process.env.E2E_BASE ?? "http://localhost:3000";
const USER = process.env.E2E_USER ?? "owner";
const PW = process.env.E2E_PASSWORD ?? "";
const SHOTS = process.env.E2E_SHOTS ?? "";
const ALLOW_START = process.env.E2E_ALLOW_START === "1";
const STATE = '[aria-label^="מצב הבוט"]';

function check(cond, msg) {
  if (!cond) throw new Error(`E2E: ${msg}`);
  console.log(`ok - ${msg}`);
}
const overflow = (p) => p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
const shot = async (p, name, full = true) => {
  if (SHOTS) {
    fs.mkdirSync(SHOTS, { recursive: true });
    await p.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: full });
  }
};
const waitState = (p, he) =>
  p.waitForFunction(([sel, t]) => document.querySelector(sel)?.textContent?.includes(t), [STATE, he], { timeout: 30000 });

(async () => {
  const browser = await chromium.launch();
  const desk = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await desk.newPage();
  const pageErrors = [];
  p.on("pageerror", (e) => pageErrors.push(String(e)));

  await p.goto(`${BASE}/terminal`);
  await p.waitForSelector("text=PAPER TRADING");
  check(await p.locator("text=מסחר ניסיוני ווירטואלי בלבד").count() === 1, "experimental warning visible");
  await p.waitForFunction(() => document.body.innerText.includes("מחובר"), null, { timeout: 20000 });
  const start = p.locator("button.t-btn-start");
  if ((await start.count()) > 0) check(await start.isDisabled(), "anonymous: start is disabled");
  check((await overflow(p)) <= 0, "desktop: no horizontal overflow");
  await shot(p, "desktop-anon");

  if (PW) {
    await p.goto(`${BASE}/login?next=/terminal`);
    await p.fill("input[name=username]", USER);
    await p.fill("input[name=password]", "definitely-wrong-password");
    await p.click("button[type=submit]");
    await p.waitForSelector("form [role=alert]");
    check((await p.locator("form [role=alert]").innerText()).length > 0, "wrong password is refused");
    await p.fill("input[name=password]", PW);
    await p.click("button[type=submit]");
    await p.waitForURL("**/terminal");
    await p.waitForFunction((u) => document.body.innerText.includes(`${u} · ADMIN`), USER, { timeout: 15000 });
    check(true, "admin logged in");
    const state = await p.locator(STATE).first().innerText();
    if (ALLOW_START && state.includes("כבוי")) {
      await p.waitForFunction(() => !document.querySelector("button.t-btn-start")?.disabled, null, { timeout: 30000 });
      await start.click();
      const confirm = p.locator("[role=dialog] button.t-btn-start");
      check(await confirm.isDisabled(), "start needs the explicit acknowledgement");
      await p.check("[role=dialog] input[type=checkbox]");
      await confirm.click();
      await waitState(p, "פועל");
      check(true, "bot RUNNING after start (worker executed the command)");
      await p.click('button:has-text("השהיה")');
      await waitState(p, "מושהה");
      check(true, "pause");
      await p.click('button:has-text("חידוש")');
      await waitState(p, "פועל");
      check(true, "resume");
    }
    await shot(p, "desktop-admin");
  }
  check(pageErrors.length === 0, `no page errors (${pageErrors.join("; ")})`);

  const phone = await browser.newContext({ ...devices["iPhone 14"] });
  await phone.addCookies(await desk.cookies());
  const m = await phone.newPage();
  await m.goto(`${BASE}/terminal`);
  await m.waitForSelector("text=PAPER TRADING");
  await m.waitForTimeout(4000);
  check((await overflow(m)) <= 0, "iPhone: no horizontal overflow");
  const kill = await m.locator("button.t-btn-kill").first().boundingBox();
  check(!kill || kill.height >= 44, "iPhone: touch targets >= 44px");
  await shot(m, "iphone-top", false);
  await shot(m, "iphone-full");
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
