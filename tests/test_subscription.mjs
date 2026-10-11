import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('../mita/subscription.html', import.meta.url), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.equal(scripts.length, 2, 'bootstrap and application scripts must remain inline');
const source = scripts[1][1];

test('loads minimalist font with system fallback and respects device appearance', () => {
  assert.match(html, /family=Manrope/);
  assert.match(html, /family=Press\+Start\+2P/);
  assert.match(html, /font-family: "Press Start 2P"/);
  assert.match(html, /--pink:\s*#[0-9a-f]{6}/);
  assert.match(html, /background: var\(--pink\)/);
  assert.match(html, /font-family: "Manrope".*system-ui, sans-serif;/);
  assert.match(html, /@media\s*\(prefers-color-scheme:\s*dark\)/);
  assert.match(html, /name="theme-color".*prefers-color-scheme: light/);
  assert.match(html, /name="theme-color".*prefers-color-scheme: dark/);
  assert.match(html, /@keyframes reveal/);
  assert.match(html, /\.step:nth-child\(2\).*animation-delay/);
  assert.match(html, /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
  assert.match(html, /animation:\s*none;/);
});

test('minimal design uses flat sections with restrained corners', () => {
  assert.match(html, /--radius:\s*5px;/);
  assert.match(html, /\.step\s*\{[\s\S]*?border:\s*1px solid var\(--border\)/);
  assert.match(html, /border-left:\s*3px solid var\(--pink\)/);
  assert.match(html, /\.choice\s*\{[\s\S]*?background:\s*transparent/);
  assert.doesNotMatch(html, /box-shadow\s*:/);
  assert.doesNotMatch(html, /border-radius:\s*(?:999px|1[0-9]px|[2-9][0-9]px)/);
  assert.match(html, /class="step-num" aria-hidden="true">01</);
  assert.match(html, /class="step-num" aria-hidden="true">02</);
});

test('mobile setup prioritizes two actions within the first viewport', () => {
  assert.match(html, /@media\s*\(max-width:\s*640px\)/);
  assert.match(html, /min-height:\s*100dvh/);
  assert.match(html, /\.choice-grid\s*\{[^}]*grid-template-columns:/);
  assert.match(html, /\.step-heading p\s*\{\s*display:\s*none;/);
  assert.match(html, /\.footer\s*\{\s*display:\s*none;/);
  assert.match(html, /<details id="advanced">/);
  assert.match(html, /<summary>Другие способы подключения<\/summary>/);
  assert.match(html, /<option value="" disabled hidden>Платформа<\/option>/);
});

test('iPhone typography is larger and cards use available viewport height', () => {
  assert.match(html, /font-size:\s*clamp\(16px,\s*4\.2vw,\s*20px\)/);
  assert.match(html, /\.step-heading h2\s*\{\s*font-size:\s*clamp\(11px,\s*3vw,\s*12px\)/);
  assert.match(html, /\.button\s*\{\s*min-height:\s*50px;/);
  assert.match(html, /font-size:\s*15px;/);
  assert.match(html, /main\s*\{\s*display:\s*flex;\s*flex:\s*1;/);
  assert.match(html, /grid-template-rows:\s*repeat\(2,\s*minmax\(max-content,\s*1fr\)\)/);
  assert.match(html, /\.step > \.button\s*\{\s*margin-top:\s*auto;/);
  assert.match(html, /@media\s*\(max-width:\s*640px\) and \(max-height:\s*740px\)/);
  assert.match(html, /\.steps\s*\{\s*flex:\s*none;\s*grid-template-rows:\s*none;/);
});

test('background gradient adapts to theme and step motion slides in from the right', () => {
  assert.match(html, /--gradient-glow:\s*rgba\(/);
  assert.match(html, /--gradient-base:\s*#fff4fa;/);
  assert.match(html, /--gradient-base:\s*#1a1119;/);
  assert.match(html, /background-image:\s*radial-gradient\([\s\S]*?linear-gradient\(/);
  assert.match(html, /@keyframes stepReveal\s*\{\s*from\s*\{\s*opacity:\s*0;\s*transform:\s*translate3d\(26px,\s*0,\s*0\)/);
  assert.match(html, /@keyframes stepReveal[\s\S]*?to\s*\{\s*opacity:\s*1;\s*transform:\s*translate3d\(0,\s*0,\s*0\)/);
  assert.match(html, /\.steps\s*>\s*\.step\s*\{\s*animation:\s*stepReveal\s*\.68s/);
  assert.match(html, /\.step:nth-child\(1\)\s*\{\s*animation-delay:\s*200ms/);
  assert.match(html, /\.step:nth-child\(2\)\s*\{\s*animation-delay:\s*380ms/);
  assert.match(html, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.steps\s*>\s*\.step\s*\{\s*animation:\s*none;/);
});

function setup(platform = 'ios', browser = {}) {
  const ids = [
    'clientSelect', 'platformSelect', 'hwidBadge', 'installBtn', 'missingInstall',
    'alternatives', 'alternativeLinks', 'installLabel', 'installSource',
    'terminalInstall', 'terminalCommand', 'copyTerminal', 'terminalFeedback',
    'openProfile', 'openLabel', 'openHint', 'copySecondary', 'copyFeedback',
  ];
  const nodes = new Map();
  const makeNode = () => ({
    hidden: false,
    value: '',
    href: '',
    textContent: '',
    children: [],
    events: {},
    style: {},
    addEventListener(name, callback) { this.events[name] = callback; },
    removeAttribute(name) { delete this[name]; },
    replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); },
    select() {},
    remove() {},
  });
  for (const id of ids) nodes.set(id, makeNode());
  const clientNode = nodes.get('clientSelect');
  clientNode.value = 'clashmi';
  clientNode.options = [...html.matchAll(/<option value="(clashmi|karing|flclashx|koala)">/g)]
    .map(([, value]) => ({ value, disabled: false, hidden: false }));
  Object.defineProperty(clientNode, 'selectedOptions', {
    get() { return clientNode.options.filter(option => option.value === clientNode.value); },
  });

  let clipboard = '';
  const document = {
    getElementById(id) { return nodes.get(id); },
    createElement() { return makeNode(); },
    body: { appendChild() {} },
    execCommand(command) { return command === 'copy'; },
  };
  const context = {
    window: { __SUBSCRIPTION__: {
      username: 'tester',
      url: 'https://vpn.example.com/sub/' + 'T'.repeat(32),
      platform,
    }},
    URL,
    URLSearchParams,
    document,
    navigator: {
      userAgent: browser.userAgent || '',
      platform: browser.platform || '',
      maxTouchPoints: browser.maxTouchPoints || 0,
      clipboard: { async writeText(value) { clipboard = value; } },
    },
  };
  vm.runInNewContext(source, context, { filename: 'subscription.html' });

  return {
    nodes,
    get(id) { return nodes.get(id); },
    change(id, value) { const node = nodes.get(id); node.value = value; node.events.change(); },
    get clipboard() { return clipboard; },
  };
}

test('Firefox on iPhone remains iOS even when server classified UA as macOS', () => {
  const view = setup('macos', {
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) FxiOS/143.0 Mobile/15E148 Safari/605.1.15',
    platform: 'MacIntel',
    maxTouchPoints: 5,
  });
  assert.equal(view.get('platformSelect').value, 'ios');
  assert.match(view.get('installBtn').href, /^https:\/\/apps\.apple\.com/);
  assert.equal(view.get('clientSelect').options.find(x => x.value === 'koala').disabled, true);
});

test('iPad desktop mode is identified from touch support', () => {
  const view = setup('macos', {
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15',
    platform: 'MacIntel',
    maxTouchPoints: 5,
  });
  assert.equal(view.get('platformSelect').value, 'ios');
});

test('real macOS remains macOS without multi-touch', () => {
  const view = setup('macos', {
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Firefox/144.0',
    platform: 'MacIntel',
    maxTouchPoints: 0,
  });
  assert.equal(view.get('platformSelect').value, 'macos');
});

test('shows download as step one and app import as step two for iPhone', () => {
  const view = setup();
  assert.equal(view.get('platformSelect').value, 'ios');
  assert.equal(view.get('installBtn').hidden, false);
  assert.match(view.get('installBtn').href, /^https:\/\/apps\.apple\.com/);
  assert.match(view.get('openProfile').href, /^clashmi:\/\/install-config\?/);
  const importUrl = new URL(view.get('openProfile').href);
  assert.equal(importUrl.searchParams.get('xhwid'), 'true');
  const rawUrl = new URL(importUrl.searchParams.get('url'));
  assert.equal(rawUrl.searchParams.get('raw'), '1');
  assert.equal(rawUrl.pathname, '/sub/' + 'T'.repeat(32));
});

test('changes installation and app import together when selecting Karing', () => {
  const view = setup();
  view.change('clientSelect', 'karing');
  assert.match(view.get('installBtn').href, /apps\.apple\.com\/us\/app\/karing/);
  assert.match(view.get('openProfile').href, /^karing:\/\/install-config\?/);
  assert.equal(view.get('hwidBadge').hidden, false);
});

test('preserves verified download routing and platform alternatives', () => {
  const view = setup('android');
  view.change('clientSelect', 'flclashx');
  assert.match(view.get('installBtn').href, /\/download\/flclashx\/android-arm64$/);
  assert.match(view.get('openProfile').href, /^flclashx:\/\//);
  assert.equal(view.get('hwidBadge').hidden, false);
  view.change('platformSelect', 'linux');
  assert.equal(view.get('alternatives').hidden, false);
  assert.ok(view.get('alternativeLinks').children.length > 0);
  assert.match(view.get('installBtn').href, /\/download\/flclashx\/linux-amd64-appimage$/);
});

test('Linux installation displays a terminal command without exposing subscription tokens', async () => {
  const view = setup('linux');
  assert.equal(view.get('terminalInstall').hidden, false);
  const command = view.get('terminalCommand').textContent;
  assert.match(command, /raw\.githubusercontent\.com\/lendysx16-new\/mita-ha\/main\/mita\/install-linux\.sh/);
  assert.match(command, /bash \.\/mita-install-linux\.sh clashmi$/);
  assert.doesNotMatch(command, /vpn\.example\.com|\/sub\/|T{32}/);
  await view.get('copyTerminal').events.click();
  assert.equal(view.clipboard, command);
  assert.equal(view.get('terminalFeedback').hidden, false);
  view.change('clientSelect', 'koala');
  assert.match(view.get('terminalCommand').textContent, /install-linux\.sh koala$/);
});

test('Terminal instructions stay hidden on iPhone and reappear when switching to Linux', () => {
  const view = setup('ios');
  assert.equal(view.get('terminalInstall').hidden, true);
  view.change('platformSelect', 'linux');
  assert.equal(view.get('terminalInstall').hidden, false);
  assert.match(view.get('terminalCommand').textContent, /install-linux\.sh clashmi$/);
});

test('manual subscription link remains available in advanced settings', async () => {
  const view = setup('windows');
  view.change('clientSelect', 'koala');
  assert.match(view.get('openProfile').href, /^koala-clash:\/\//);
  await view.get('copySecondary').events.click();
  assert.equal(view.clipboard, 'https://vpn.example.com/sub/' + 'T'.repeat(32));
  assert.equal(view.get('copyFeedback').hidden, false);
});

test('picker contains only HWID-capable clients and no generic option', () => {
  const view = setup('ios');
  const values = view.get('clientSelect').options.map(option => option.value);
  assert.deepEqual(values, ['clashmi', 'karing', 'flclashx', 'koala']);
  assert.doesNotMatch(html, /<option value="(?:flclash|verge|generic)">/);
});

test('FlClashX supports HWID on Android and uses its registered deep link', () => {
  const view = setup('android');
  view.change('clientSelect', 'flclashx');
  assert.match(view.get('installBtn').href, /\/download\/flclashx\/android-arm64$/);
  assert.match(view.get('openProfile').href, /^flclashx:\/\/install-config\?/);
  assert.equal(view.get('hwidBadge').hidden, false);
  const url = new URL(view.get('openProfile').href);
  assert.equal(url.searchParams.get('xhwid'), null, 'FlClashX sends HWID natively');
});

test('Koala Clash supports HWID and has a verified scheme on desktop', () => {
  const view = setup('windows');
  view.change('clientSelect', 'koala');
  assert.match(view.get('installBtn').href, /\/download\/koala\/windows-amd64$/);
  assert.match(view.get('openProfile').href, /^koala-clash:\/\/install-config\?/);
  assert.equal(view.get('hwidBadge').hidden, false);
});

test('iOS hides clients that have no iOS installation option', () => {
  const view = setup('ios');
  const clients = view.get('clientSelect').options;
  assert.equal(clients.find(item => item.value === 'koala').disabled, true);
  assert.equal(clients.find(item => item.value === 'flclashx').hidden, true);
  assert.equal(clients.find(item => item.value === 'karing').disabled, false);
  view.change('platformSelect', 'windows');
  assert.equal(clients.find(item => item.value === 'koala').hidden, false);
  assert.equal(clients.find(item => item.value === 'flclashx').disabled, false);
});

test('requires device selection when OS is unknown', () => {
  const view = setup('unknown');
  assert.equal(view.get('installBtn').hidden, true);
  assert.match(view.get('missingInstall').textContent, /Выберите своё устройство/);
  view.change('platformSelect', 'windows');
  assert.equal(view.get('installBtn').hidden, false);
});
