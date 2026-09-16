import { chromium } from '@playwright/test';
import { build } from 'esbuild';
import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import assert from 'node:assert/strict';

await build({ entryPoints: ['tests/browser/main.jsx'], bundle: true, format: 'esm', jsx: 'automatic',
  alias: { '@hermes/plugin-sdk': './tests/browser/sdk.jsx' }, outfile: 'tests/browser/bundle.js' });
const probe = createServer();
await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
const port = probe.address().port;
await new Promise(resolve => probe.close(resolve));
const server = spawn(process.env.PYTHON || 'python', ['tests/browser/server.py', '--port', String(port)], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
let output = '';
server.stdout.on('data', x => { output += x; }); server.stderr.on('data', x => { output += x; });
const base = `http://127.0.0.1:${port}`;
let browser;
try {
  let ready = false;
  for (let i = 0; i < 60; i++) {
    if (server.exitCode !== null) throw new Error(output);
    try { if ((await fetch(base)).ok) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  assert.ok(ready, 'Preview API did not start: ' + output);
  browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
  const page = await browser.newPage({ viewport: { width: 1000, height: 760 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(base);
  await page.getByRole('status').filter({ hasText: '4 files changed' }).waitFor();
  await page.getByRole('button', { name: /session.ts modified/ }).click();
  await page.getByLabel('File diff').filter({ hasText: '+  if (!response.ok)' }).waitFor();
  await page.getByRole('button', { name: 'Copy patch', exact: true }).click();
  assert.match(await page.evaluate(() => window.copiedPatch), /Session unavailable/);
  await page.evaluate(() => { window.failClipboard = true; });
  await page.getByRole('button', { name: 'Copied', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'Could not copy' }).waitFor();
  assert.equal(await page.getByLabel('File diff').isVisible(), true);
  await page.evaluate(() => { window.failClipboard = false; });
  await page.getByRole('button', { name: 'Retry copy', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'Could not copy' }).waitFor({ state: 'hidden' });
  await page.route('**/history?*', route => route.fulfill({ status: 503, body: 'unavailable' }));
  await page.getByRole('button', { name: 'Edit history', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'Could not load edit history' }).waitFor();
  assert.equal(await page.getByLabel('File diff').isVisible(), true);
  await page.unroute('**/history?*');
  await page.getByRole('button', { name: 'Retry history', exact: true }).click();
  await page.getByLabel('Edit history', { exact: true }).waitFor();
  await page.getByRole('alert').filter({ hasText: 'Could not load edit history' }).waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: /scratch.txt restored/ }).click();
  await page.getByText('The file matches its original contents.', { exact: false }).waitFor();
  await page.getByRole('button', { name: 'Edit history', exact: true }).click();
  await page.getByRole('button', { name: /write_file · file tool/ }).click();
  await page.getByLabel('File diff').filter({ hasText: '+temporary experiment' }).waitFor();
  // Exercise actual host token names, then their absence on older hosts. The
  // previous preview invented a hover token and concealed the shipped defect.
  for (const dark of [false, true]) {
    await page.evaluate(dark => document.body.classList.toggle('dark', dark), dark);
    for (const fallback of [false, true]) {
      await page.evaluate(fallback => {
        for (const name of ['row-hover', 'row-active', 'control-hover', 'control-active']) {
          document.body.style.setProperty(`--ui-${name}-background`, fallback ? 'initial' : '');
        }
      }, fallback);
      for (const target of [page.locator('.file[aria-pressed=false]').first(), page.locator('.file[aria-pressed=true]'), page.locator('.history button[aria-pressed=false]').first(), page.locator('.history button[aria-pressed=true]'), page.getByRole('button', { name: 'Copy patch', exact: true })]) {
        await target.hover();
        const minimum = await target.evaluate(root => {
          const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
          const ctx = canvas.getContext('2d', { willReadFrequently: true });
          const rgba = color => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].map((v, i) => i === 3 ? v / 255 : v); };
          const blend = (top, bottom) => top.slice(0, 3).map((v, i) => v * top[3] + bottom[i] * (1 - top[3]));
          const luminance = rgb => rgb.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
          return Math.min(...[root, ...root.querySelectorAll('*')].filter(el => el.textContent.trim()).map(el => {
            const ancestors = []; for (let parent = el; parent; parent = parent.parentElement) ancestors.unshift(parent);
            let bg = [255, 255, 255];
            for (const ancestor of ancestors) bg = blend(rgba(getComputedStyle(ancestor).backgroundColor), bg);
            const ink = blend(rgba(getComputedStyle(el).color), bg);
            const a = luminance(ink), b = luminance(bg);
            return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
          }));
        });
        assert.ok(minimum >= 4.5, `Hover contrast ${minimum.toFixed(2)}; dark=${dark}, fallback=${fallback}`);
      }
    }
  }
  await page.locator('.history button[aria-pressed=true]').hover();
  await mkdir('test-results', { recursive: true });
  await page.screenshot({ path: 'test-results/history-hover.png', fullPage: true });
  await page.evaluate(() => document.body.classList.remove('dark'));
  await page.getByRole('button', { name: 'Show net diff' }).click();
  await page.getByLabel('Filter files').fill('nothing-matches');
  await page.getByText('No matching files.').waitFor();
  await page.getByLabel('Filter files').fill('');
  await page.getByRole('button', { name: /session.ts modified/ }).click();
  await page.getByLabel('File diff').filter({ hasText: 'Session unavailable' }).waitFor();
  await mkdir('test-results', { recursive: true });
  await page.screenshot({ path: 'test-results/desktop.png', fullPage: true });
  await page.evaluate(() => document.body.classList.add('dark'));
  await page.screenshot({ path: 'test-results/dark.png', fullPage: true });
  await page.setViewportSize({ width: 380, height: 760 });
  await page.screenshot({ path: 'test-results/narrow.png', fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.equal(await page.getByRole('button', { name: 'Copy patch', exact: true }).isVisible(), true);
  await page.getByLabel('Compare').selectOption('all');
  await page.getByRole('status').filter({ hasText: '4 files changed' }).waitFor();
  await page.evaluate(() => window.previewHost.state.focusedStoredSessionId.set('new-session'));
  await page.evaluate(() => window.previewHost.state.focusedSessionId.set('new-session'));
  await page.getByRole('heading', { name: 'No capture for this chat yet' }).waitFor();
  await page.getByLabel('Session', { exact: true }).selectOption('demo-session');
  await page.getByRole('status').filter({ hasText: '4 files changed' }).waitFor();
  await page.route('**/changes?*', route => route.fulfill({ status: 503, body: 'unavailable' }));
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'Could not load recorded changes' }).waitFor();
  await page.waitForResponse(r => r.url().endsWith('/sessions') && r.ok(), { timeout: 10000 });
  assert.equal(await page.getByRole('alert').filter({ hasText: 'Could not load recorded changes' }).isVisible(), true);
  await page.unroute('**/changes?*');
  await page.route('**/api/plugins/hermes-session-diff/**', route => route.fulfill({ status: 503, body: 'unavailable' }));
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('alert').first().waitFor();
  await page.unroute('**/api/plugins/hermes-session-diff/**');
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.getByRole('status').filter({ hasText: '4 files changed' }).waitFor();
  assert.deepEqual(errors, []);
  console.log('Browser checks passed: actual API, file diff, clipboard, restored history, filter, scope, focus, errors, recovery, light/dark and narrow layouts.');
} finally { if (browser) await browser.close(); server.kill(); }
