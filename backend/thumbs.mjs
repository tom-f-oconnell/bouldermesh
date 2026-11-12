// Draws meshes' pictures for thumbs.py, with the site's own viewer in a browser with no screen (headless Chromium, by
// playwright-core: PLAYWRIGHT_CORE says where it is, PLAYWRIGHT_BROWSERS_PATH where its browser is). Reads a job a line
// from stdin, {url, out, width, height, timeout}, opens url (/m/<id>?thumb=...), waits for the page to say it's drawn
// (html[data-thumb]), saves the view as a JPEG at out, and prints {ok, error, seconds} a line each. The browser stays
// open between jobs, until stdin closes.
import { createInterface } from 'node:readline';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE || '/opt/playwright-core');
// WebGL with no GPU: SwiftShader, Chromium's own software renderer
const ARGS = (process.env.THUMB_CHROMIUM_ARGS || '--no-sandbox --disable-dev-shm-usage --use-angle=swiftshader --enable-unsafe-swiftshader --ignore-gpu-blocklist')
  .split(' ').filter(Boolean);
const say = o => process.stdout.write(JSON.stringify(o) + '\n');
let browser = null;
for await (const line of createInterface({ input: process.stdin })) {
  if (!line.trim()) continue;
  const began = Date.now(), seconds = () => (Date.now() - began) / 1000;
  let page = null, job;
  try {
    job = JSON.parse(line);
    if (!browser?.isConnected()) browser = await chromium.launch({ headless: true, args: ARGS });
    page = await browser.newPage({ viewport: { width: job.width, height: job.height }, deviceScaleFactor: 1 });
    const end = began + (job.timeout || 120) * 1000, left = () => Math.max(1000, end - Date.now());   // all of it, in that
    await page.goto(job.url, { waitUntil: 'load', timeout: left() });
    await page.waitForSelector('html[data-thumb]', { state: 'attached', timeout: left() });
    if (await page.getAttribute('html', 'data-thumb') !== 'ready') throw new Error("the viewer couldn't show it");
    await page.screenshot({ path: job.out, type: 'jpeg', quality: 85, timeout: left() });   // slow while the server's busy
    say({ ok: true, seconds: seconds() });
  } catch (e) {
    say({ ok: false, error: String(e?.message || e).split('\n')[0].slice(0, 300), seconds: seconds() });
  } finally {
    await page?.close().catch(() => {});
  }
}
await browser?.close().catch(() => {});
