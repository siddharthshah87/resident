#!/usr/bin/env node
// agentos-browse: read pages through the resident agent's system browser.
// Connects to the headless Chrome run by agent-browser.service over CDP
// (127.0.0.1:9222), so logins in the persistent profile are reused.
// Every call appends one line to visits.jsonl.
//
// Usage:
//   agentos-browse <url>                      main text (title + final URL first; 8k cap)
//   agentos-browse <url> --screenshot [path]  PNG → shots/<epoch>.png or path
//   agentos-browse <url> --links              up to 50 links: text → href
//   agentos-browse --tabs                     list open CDP targets
// Flags: --keep (leave the tab open), --timeout <ms> (default 20000)

import { createRequire } from 'node:module';
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const CDP = process.env.AGENTOS_CDP || 'http://127.0.0.1:9222';
// Where to load Playwright from: $RESIDENT_PW_ROOT (a dir containing node_modules/playwright), else next to this file.
const PW_ROOT = process.env.RESIDENT_PW_ROOT || process.env.AGENTOS_PW_ROOT || (HERE + '/');
const VISITS = join(HERE, 'visits.jsonl');
const SHOTS = join(HERE, 'shots');
const TEXT_CAP = 8000;
const LINK_CAP = 50;

const { chromium } = createRequire(PW_ROOT)('playwright');

// ---- args ------------------------------------------------------------------
const argv = process.argv.slice(2);
let url = null, mode = 'text', shotPath = null, keep = false, timeout = 20000;
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--tabs') mode = 'tabs';
  else if (a === '--links') mode = 'links';
  else if (a === '--screenshot') {
    mode = 'screenshot';
    if (argv[i + 1] && !argv[i + 1].startsWith('--')) shotPath = argv[++i];
  }
  else if (a === '--keep') keep = true;
  else if (a === '--timeout') timeout = Number(argv[++i]) || timeout;
  else if (a === '-h' || a === '--help') { usage(); process.exit(0); }
  else if (!a.startsWith('--') && !url) url = a;
  else { console.error(`unknown argument: ${a}`); usage(); process.exit(2); }
}
if (mode !== 'tabs' && !url) { usage(); process.exit(2); }
if (url && !/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) url = 'https://' + url;

function usage() {
  console.error('usage: agentos-browse <url> [--screenshot [path] | --links] [--keep] [--timeout ms]\n       agentos-browse --tabs');
}

function logVisit(rec) {
  try {
    appendFileSync(VISITS, JSON.stringify({
      ts: new Date().toISOString(),
      url: rec.url ?? null,
      final_url: rec.final_url ?? null,
      title: rec.title ?? null,
      intent: process.env.AGENTOS_INTENT || null,
      mode,
      ...(rec.error ? { error: rec.error } : {}),
    }) + '\n');
  } catch (e) { console.error(`(visit log failed: ${e.message})`); }
}

// ---- main ------------------------------------------------------------------
let browser;
try {
  browser = await chromium.connectOverCDP(CDP, { timeout: 5000 });
} catch (e) {
  console.error(`cannot reach the system browser at ${CDP}: ${e.message}\n` +
    `  is it running?  systemctl --user status agent-browser.service`);
  process.exit(3);
}

try {
  const context = browser.contexts()[0] || await browser.newContext();

  if (mode === 'tabs') {
    const pages = context.pages();
    if (!pages.length) console.log('(no open tabs)');
    for (const p of pages) {
      let t = ''; try { t = await p.title(); } catch {}
      console.log(`${p.url()}\t${t}`);
    }
    logVisit({});
    process.exit(0);
  }

  const page = await context.newPage();
  page.setDefaultTimeout(timeout);
  let title = '', finalUrl = url;
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
    // Give SPAs a moment to hydrate, but never block on perpetual network chatter.
    await page.waitForLoadState('networkidle', { timeout: Math.min(5000, timeout) }).catch(() => {});
    finalUrl = page.url();
    title = await page.title().catch(() => '');

    if (mode === 'text') {
      const text = await page.evaluate(() => {
        const KILL = 'script,style,noscript,svg,nav,header,footer,aside,iframe,template,[hidden],[aria-hidden="true"]';
        const root = (document.querySelector('main, article, [role="main"]') || document.body).cloneNode(true);
        root.querySelectorAll(KILL).forEach(n => n.remove());
        // innerText needs the node attached to lay out; use a hidden host.
        const host = document.createElement('div');
        host.style.cssText = 'position:absolute;left:-99999px;top:0;width:1280px';
        host.appendChild(root); document.body.appendChild(host);
        let t = root.innerText || root.textContent || '';
        host.remove();
        return t.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
      });
      console.log(`# ${title || '(untitled)'}\n${finalUrl}\n`);
      console.log(text.length > TEXT_CAP ? text.slice(0, TEXT_CAP) + `\n\n[... truncated at ${TEXT_CAP} chars of ${text.length}]` : text);
    }

    else if (mode === 'links') {
      const links = await page.evaluate((cap) => {
        const seen = new Set(), out = [];
        for (const a of document.querySelectorAll('a[href]')) {
          const href = a.href; if (!href || href.startsWith('javascript:') || seen.has(href)) continue;
          seen.add(href);
          out.push([(a.innerText || a.getAttribute('aria-label') || a.title || '').trim().replace(/\s+/g, ' ').slice(0, 80), href]);
          if (out.length >= cap) break;
        }
        return out;
      }, LINK_CAP);
      console.log(`# ${title || '(untitled)'}\n${finalUrl}\n`);
      for (const [t, h] of links) console.log(`${t || '(no text)'} → ${h}`);
    }

    else if (mode === 'screenshot') {
      let out = shotPath ? resolve(shotPath) : join(SHOTS, `${Date.now()}.png`);
      mkdirSync(dirname(out), { recursive: true });
      await page.screenshot({ path: out, fullPage: false });
      console.log(out);
    }

    logVisit({ url, final_url: finalUrl, title });
  } catch (e) {
    logVisit({ url, final_url: finalUrl, title, error: e.message.split('\n')[0] });
    console.error(`browse failed: ${e.message.split('\n')[0]}`);
    process.exitCode = 1;
  } finally {
    if (!keep) await page.close().catch(() => {});
  }
} finally {
  // Disconnect only; never close the shared service browser.
  await browser.close().catch(() => {});
}
