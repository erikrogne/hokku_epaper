/* Fixture-only browser QA. Requires an existing Playwright installation.
 * Start tools/ui_fixture_server.py on 18084 (baseline template) and 18085
 * (current template, --worker-version-file output/playwright/worker-version.txt).
 * Run: node tools/ui_browser_qa.cjs
 * Never point this script at a live app: all paths are fixed loopback fixtures.
 */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const output = path.resolve('output/playwright');
const evidence = path.resolve('docs/mobile-review');
const versionFile = path.join(output, 'worker-version.txt');
const checks = [];
function check(name, condition) { assert.ok(condition, name); checks.push(name); }

(async () => {
  fs.mkdirSync(output, {recursive: true});
  fs.mkdirSync(evidence, {recursive: true});
  fs.writeFileSync(versionFile, 'hokku-public-v1');
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true});
  const page = await context.newPage();
  const errors = [];
  const writes = [];
  page.on('pageerror', error => errors.push(error.message));
  context.on('request', request => {
    if (!['GET', 'HEAD'].includes(request.method())) writes.push(request.url());
  });
  try {
    await page.goto('http://127.0.0.1:18084/hokku/ui');
    await page.waitForLoadState('networkidle');
    await page.screenshot({path: path.join(evidence, 'before-mobile.png')});
    await page.locator('.image-select').first().check();
    await page.waitForTimeout(5200);
    check('Baseline reproduces selection loss on poll', !await page.locator('.image-select').first().isChecked());
    const desktopContext = await browser.newContext({viewport: {width: 1440, height: 900}});
    const desktopPage = await desktopContext.newPage();
    await desktopPage.goto('http://127.0.0.1:18084/hokku/ui');
    await desktopPage.waitForLoadState('networkidle');
    await desktopPage.screenshot({path: path.join(evidence, 'before-desktop.png')});

    await page.setViewportSize({width: 390, height: 844});
    await page.goto('http://127.0.0.1:18085/hokku/ui');
    await page.waitForLoadState('networkidle');
    check('Initial connection and configuration loaded', await page.locator('#connection-status').isHidden());
    await page.screenshot({path: path.join(evidence, 'after-mobile.png')});
    for (const [width, height] of [[320, 740], [375, 812], [390, 844], [430, 932], [768, 1024], [1440, 900], [844, 390]]) {
      await page.setViewportSize({width, height});
      check(`No page overflow at ${width}×${height}`, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await page.setViewportSize({width: 390, height: 844});
    check('Phone navigation and actions have 44px touch height', await page.locator('.section-nav a, .section-nav button, .image-card .btn, .image-select-target, .delete-btn').evaluateAll(nodes => nodes.every(n => n.getBoundingClientRect().height >= 44)));
    await page.locator('.image-select').first().check();
    await page.locator('#bulk-collection-select').selectOption(['sample']);
    await page.waitForTimeout(10200);
    check('Photo selection survives two polls', await page.locator('.image-select').first().isChecked());
    check('Bulk collection selection survives two polls', await page.locator('#bulk-collection-select').inputValue() === 'sample');
    await page.locator('#image-grid-more').click();
    check('Progressive reveal retains photo selection', await page.locator('.image-select').first().isChecked());
    check('Progressive reveal displays twenty photos', await page.locator('.image-card').count() === 20);
    await page.locator('#collection-filter').selectOption('empty');
    check('Empty collection offers a next action', (await page.locator('.gallery-empty').innerText()).includes('choose another collection'));
    await page.locator('#collection-filter').selectOption('all');
    check('Changing collection resets progressive reveal', await page.locator('.image-card').count() === 10);
    await page.locator('.image-card').first().scrollIntoViewIfNeeded();
    await page.screenshot({path: path.join(evidence, 'after-mobile-gallery.png')});

    for (let i = 0; i < 3; i++) {
      await page.locator('.details-btn').first().click();
      await page.getByRole('dialog').waitFor();
      await page.waitForLoadState('networkidle');
      if (i === 0) await page.screenshot({path: path.join(evidence, 'after-mobile-dialog.png')});
      check(`Details dialog focuses inside on opening ${i + 1}`, await page.evaluate(() => document.querySelector('.modal-backdrop.show .modal').contains(document.activeElement)));
      await page.keyboard.press('Shift+Tab');
      check(`Details dialog traps keyboard focus ${i + 1}`, await page.evaluate(() => document.querySelector('.modal-backdrop.show .modal').contains(document.activeElement)));
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('.modal-backdrop.show'));
      check(`Details dialog restores focus ${i + 1}`, await page.locator('.details-btn').first().evaluate(el => el === document.activeElement));
    }
    await page.locator('.delete-btn').first().click();
    await page.getByRole('dialog').waitFor();
    check('Delete confirmation starts on Cancel', await page.locator('#confirm-cancel').evaluate(el => el === document.activeElement));
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => !document.querySelector('.modal-backdrop.show'));
    check('Enter on Cancel does not delete a photo', writes.length === 0);
    check('Confirmation restores focus to its trigger', await page.locator('.delete-btn').first().evaluate(el => el === document.activeElement));
    await page.getByRole('link', {name: 'Frames', exact: true}).click();
    await page.screenshot({path: path.join(evidence, 'after-mobile-frames.png')});
    await page.getByRole('button', {name: 'Config', exact: true}).click();
    await page.locator('#screen-display-name-input').fill('Unsaved fixture edit');
    await page.waitForTimeout(5200);
    check('Frame-name draft survives poll', await page.locator('#screen-display-name-input').inputValue() === 'Unsaved fixture edit');
    await page.keyboard.press('Escape');
    await page.getByRole('button', {name: 'Admin', exact: true}).first().click();
    check('Admin disclosure works and exposes expanded state', await page.locator('#admin-toggle').getAttribute('aria-expanded') === 'true');
    await page.locator('#poll-interval').fill('17');
    await page.waitForTimeout(5200);
    check('Configuration draft survives poll', await page.locator('#poll-interval').inputValue() === '17');

    let failStatus = true;
    await page.route('**/hokku/api/status', route => failStatus ? route.fulfill({status: 503, contentType: 'application/json', body: '{"error":"fixture interruption"}'}) : route.continue());
    await page.evaluate(() => refreshStatus());
    check('HTTP errors expose stale-data status and retry', await page.locator('#connection-status').isVisible() && (await page.locator('#connection-message').innerText()).includes('unreachable'));
    failStatus = false;
    await page.locator('#connection-retry').click();
    await page.waitForFunction(() => document.getElementById('connection-status').hidden);
    check('Retry recovers after server interruption', await page.locator('#connection-status').isHidden());
    check('Retry leaves unsaved form edits intact', await page.locator('#poll-interval').inputValue() === '17');
    await page.unroute('**/hokku/api/status');
    let concurrentRequests = 0;
    await page.route('**/hokku/api/status', async route => {
      concurrentRequests++;
      await new Promise(resolve => setTimeout(resolve, 250));
      await route.continue();
    });
    await page.evaluate(() => Promise.all([refreshStatus(), refreshStatus()]));
    check('Concurrent status refreshes use one request', concurrentRequests === 1);
    await page.unroute('**/hokku/api/status');
    await context.setOffline(true);
    await page.waitForFunction(() => !document.getElementById('connection-status').hidden);
    check('Offline event is visible without a reload', await page.locator('#connection-status').isVisible());
    await context.setOffline(false);
    await page.waitForFunction(() => document.getElementById('connection-status').hidden);

    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.reload();
    await page.waitForLoadState('networkidle');
    check('Service worker controls a reopened page', await page.evaluate(() => !!navigator.serviceWorker.controller));
    const cachePaths = await page.evaluate(async () => {
      const requests = await Promise.all((await caches.keys()).map(async name => (await (await caches.open(name)).keys()).map(request => new URL(request.url).pathname)));
      return requests.flat();
    });
    const allowed = ['/hokku/static/offline.html', '/hokku/static/mobile.css', '/hokku/static/mobile.js', '/hokku/static/icon-192.png', '/hokku/static/icon-512.png'];
    check('Only the five public app assets are cached', cachePaths.length === 5 && cachePaths.every(p => allowed.includes(p)));
    await page.getByRole('button', {name: 'Admin', exact: true}).first().click();
    await page.locator('#poll-interval').fill('19');
    fs.writeFileSync(versionFile, 'hokku-public-v2');
    await page.evaluate(async () => (await navigator.serviceWorker.getRegistration()).update());
    await page.waitForFunction(() => !document.getElementById('app-update').hidden);
    check('App update waits without reloading an unsaved form', await page.locator('#poll-interval').inputValue() === '19');
    await page.close();
    const reopened = await context.newPage();
    await reopened.goto('http://127.0.0.1:18085/hokku/ui');
    await reopened.waitForLoadState('networkidle');
    await reopened.waitForFunction(async () => (await caches.keys()).includes('hokku-public-v2'));
    check('Reopening activates update and removes old public cache', await reopened.evaluate(async () => (await caches.keys()).join(',') === 'hokku-public-v2'));
    await context.setOffline(true);
    await reopened.reload();
    check('Offline navigation shows generic reconnect page', (await reopened.locator('h1').innerText()) === 'Hokku is unreachable');
    check('Offline page contains no private runtime data', await reopened.locator('.image-card, input').count() === 0);
    await reopened.screenshot({path: path.join(evidence, 'after-mobile-offline.png')});
    await context.setOffline(false);
    await reopened.getByRole('link', {name: 'Try again', exact: true}).click();
    await reopened.waitForLoadState('networkidle');
    check('Offline navigation recovers on retry', await reopened.locator('.image-card').count() === 10);
    await desktopPage.goto('http://127.0.0.1:18085/hokku/ui');
    await desktopPage.waitForLoadState('networkidle');
    check('Standard desktop context fits without overflow', await desktopPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await desktopPage.screenshot({path: path.join(evidence, 'after-desktop.png')});
    await desktopContext.close();

    // Separate fresh context to verify initial config failures don't allow defaults to be saved.
    const failedContext = await browser.newContext({viewport: {width: 390, height: 844}});
    const failedPage = await failedContext.newPage();
    await failedPage.route('**/hokku/api/config', route => route.fulfill({status: 503, body: '{}'}));
    await failedPage.goto('http://127.0.0.1:18085/hokku/ui');
    await failedPage.waitForLoadState('networkidle');
    check('Initial configuration failure is visible', (await failedPage.locator('#connection-message').innerText()).includes('Configuration could not load'));
    await failedPage.evaluate(() => saveConfig());
    check('Uninitialized settings cannot be saved', (await failedPage.locator('#toast').innerText()).includes('Wait for configuration'));
    await failedPage.unroute('**/hokku/api/config');
    await failedPage.locator('#connection-retry').click();
    await failedPage.waitForFunction(() => document.getElementById('connection-status').hidden);
    check('Initial configuration failure can recover', await failedPage.locator('#connection-status').isHidden());
    await failedContext.close();

    // Reserved hostname, intercepted entirely by the fixture: representative
    // HTTP LAN trust context, without DNS, TLS changes or a real phone.
    const insecureContext = await browser.newContext({viewport: {width: 390, height: 844}});
    const insecurePage = await insecureContext.newPage();
    await insecurePage.route('http://hokku-fixture.invalid/**', async route => {
      const url = new URL(route.request().url());
      const response = await insecureContext.request.get(`http://127.0.0.1:18085${url.pathname}${url.search}`);
      await route.fulfill({response});
    });
    await insecurePage.goto('http://hokku-fixture.invalid/hokku/ui');
    await insecurePage.waitForLoadState('networkidle');
    await insecurePage.locator('#install-help-toggle').click();
    check('HTTP LAN context explains offline limitations', !await insecurePage.evaluate(() => isSecureContext) && (await insecurePage.locator('#install-context').innerText()).includes('offline support needs HTTPS'));
    await insecurePage.screenshot({path: path.join(evidence, 'after-mobile-install-http.png')});
    await insecureContext.close();
    check('No application write requests occurred', writes.length === 0);
    check('No browser JavaScript errors', errors.length === 0);
    fs.writeFileSync(path.join(output, 'qa-result.json'), JSON.stringify({browser: browser.version(), platform: process.platform, mode: 'headless Mac Chromium; viewport and touch emulation, not an iPhone', checks, cachePaths, errors, writes}, null, 2));
    console.log(`${checks.length} browser checks passed. Fixture screenshots: ${evidence}`);
  } finally {
    fs.writeFileSync(versionFile, 'hokku-public-v1');
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
