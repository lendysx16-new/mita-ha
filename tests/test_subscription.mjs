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

function setup(platform = 'ios') {
  const ids = [
    'clientSelect', 'platformSelect', 'hwidBadge', 'installBtn', 'missingInstall',
    'alternatives', 'alternativeLinks', 'installLabel', 'installSource',
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
    navigator: { clipboard: { async writeText(value) { clipboard = value; } } },
  };
  vm.runInNewContext(source, context, { filename: 'subscription.html' });

  return {
    nodes,
    get(id) { return nodes.get(id); },
    change(id, value) { const node = nodes.get(id); node.value = value; node.events.change(); },
    get clipboard() { return clipboard; },
  };
}

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
