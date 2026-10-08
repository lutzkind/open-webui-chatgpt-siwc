import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
import {chromium} from '../tests/browser/node_modules/playwright/index.mjs';

const root = resolve(new URL('..', import.meta.url).pathname);
const browser = await chromium.launch({headless: true});
try {
  const page = await browser.newPage({viewport: {width: 1280, height: 640}, deviceScaleFactor: 1});
  await page.goto(pathToFileURL(resolve(root, '.github/social-preview.svg')).href);
  await page.screenshot({path: resolve(root, '.github/social-preview.png')});
} finally {
  await browser.close();
}
