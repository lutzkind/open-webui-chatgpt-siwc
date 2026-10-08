// Browser-level connection page checks with synthetic identities and responses.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const origin = process.argv[2];

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    let connected = false;
    let pairCalls = 0;
    let verifyCalls = 0;

    await page.route('**/siwc/status', route => route.fulfill({json: {
      configured: connected,
      connected,
      companion_available: true,
      companion_notarized: false
    }}));
    await page.route('**/siwc/pair', async route => {
      pairCalls += 1;
      assert.equal(route.request().headers().authorization, undefined);
      assert.match(route.request().postDataJSON().challenge, /^[a-f0-9]{64}$/);
      await route.fulfill({json: {ticket: 'T'.repeat(43)}});
    });
    await page.route('http://127.0.0.1:54321/connect**', route =>
      route.fulfill({body: 'Synthetic companion callback'}));
    await page.route('**/siwc/verify', async route => {
      verifyCalls += 1;
      await route.fulfill({json: {configured: true, models: ['synthetic-model']}});
    });

    await page.goto(`${origin}/siwc/connect#${new URLSearchParams({
      challenge: 'a'.repeat(64),
      port: '54321',
      state: 's'.repeat(43)
    })}`);
    await page.getByRole('status').filter({hasText: 'Not connected'}).waitFor();
    assert.equal(await page.locator('#install').isVisible(), false);
    assert.equal(await page.evaluate(() => location.hash), '');
    await page.getByRole('button', {name: 'Continue with ChatGPT'}).click();
    await page.waitForURL('http://127.0.0.1:54321/connect**');
    assert.equal(pairCalls, 1);

    connected = true;
    await page.goto(`${origin}/siwc/connect?verify=1`);
    await page.getByRole('status').filter({hasText: 'Connected'}).waitFor();
    await page.getByText(/Live subscription model discovery succeeded/).waitFor();
    assert.equal(await page.getByRole('button', {name: 'Continue with ChatGPT'}).isVisible(), false);
    assert.equal(verifyCalls, 1);

    const text = await page.locator('body').innerText();
    assert.match(text, /Sign into Open WebUI/);
    assert.match(text, /Use a ChatGPT subscription for inference/);
    console.log('Browser connection flow passed with synthetic pairing, connected-state and model-discovery responses.');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
