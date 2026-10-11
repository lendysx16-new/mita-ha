import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('../mita/admin.html', import.meta.url), 'utf8');
const script = /<script>([\s\S]*?)<\/script>/.exec(html)?.[1];
assert.ok(script, 'Admin UI must expose its client-side script');

test('overview is a compact directory, while account actions are on user detail', () => {
  const directory = /<div id="usersTab"[\s\S]*?(?=<div id="userDetailTab")/.exec(html)?.[0] || '';
  const detail = /<div id="userDetailTab"[\s\S]*?(?=<div id="devicesTab")/.exec(html)?.[0] || '';
  const deviceDirectory = /<div id="devicesTab"[\s\S]*?(?=<div id="trafficTab")/.exec(html)?.[0] || '';
  const settings = html.slice(html.indexOf('<div id="confTab"'));
  assert.match(directory, /<table class="user-table">/);
  assert.match(directory, /id="toggleAddUser"/);
  assert.match(directory, /id="addUserPanel" hidden/);
  assert.doesNotMatch(directory, /Subscription base URL|id="detailRotate"|id="detailDelete"|id="host"/);
  assert.match(detail, /id="detailSubscription"/);
  assert.match(detail, /id="detailCopy"/);
  assert.match(detail, /id="detailRotate"/);
  assert.match(detail, /id="detailDelete"/);
  assert.match(detail, /id="detailDeviceUsers"/);
  assert.match(deviceDirectory, /<table class="device-directory-table">/);
  assert.match(settings, /id="host"/);
  assert.match(settings, /id="contactEmail"/);
  assert.match(html, /@media \(max-width: 700px\)[\s\S]*?\.user-table tr \{ display: grid/);
});

function setup() {
  const values = new Map();
  function element(key) {
    if (!values.has(key)) {
      const node = {
        hidden: false, value: '', textContent: '', innerHTML: '', dataset: {},
        style: {}, onclick: null, disabled: false,
        classList: { active: false, toggle(name, value) { if (name === 'active') this.active = value; } },
        addEventListener() {}, querySelectorAll() { return []; },
        scrollIntoView() {}, focus() {}, setAttribute() {},
      };
      values.set(key, node);
    }
    return values.get(key);
  }
  const panels = ['usersTab','userDetailTab','devicesTab','trafficTab','confTab']
    .map((id, index) => ({ id, classList: { active: index === 0, toggle(name, value) { if(name === 'active') this.active=value; } } }));
  const tabs = ['usersTab','devicesTab','trafficTab','confTab'].map(tab=>({
    dataset: {tab}, classList: { active: false, toggle(name,value) { if(name==='active')this.active=value; } },
    setAttribute() {},
  }));
  const state = { users: [{
    username: 'alice', allow_private_ip: false, allow_loopback_ip: false,
    legacy_enabled: true, device_limit: 2, subscription_url: 'https://vpn.example/sub/SECRET_TOKEN',
    devices: [{id: '0123456789abcdef', name: 'Laptop', hwid: 'sensitive-hwid',
      last_seen: '2026-10-10T13:00:00Z', os: 'Linux', os_version: '6'}],
  }], settings: {
    public_mieru_host: 'vpn.example', public_mieru_port: 8443,
    subscription_base_url: 'https://vpn.example', contact_email: '',
    log_level: 'INFO', postgres_dsn: '',
  }};
  const document = {
    title: '',
    querySelector(selector) {
      if (selector === '.tab-panel.active') return panels.find(p=>p.classList.active);
      return element(selector);
    },
    querySelectorAll(selector) {
      if (selector === '[data-tab]' || selector === '.tab') return tabs;
      if (selector === '.tab-panel') return panels;
      return [];
    },
  };
  const ctx = vm.createContext({
    document,
    navigator: { languages: ['en-US'], clipboard: { async writeText(v) { ctx.copied = v; } } },
    location: { href: 'https://ha.example/ingress/' },
    fetch: async url => ({ ok: true, json: async () =>
      String(url).includes('api/backup-status') ? {status:{enabled:false}} : state }),
    Intl, Date, URL,
    alert() {}, confirm() { return true; },
    setTimeout() { return 1; }, clearTimeout() {},
    setInterval() { return 1; }, clearInterval() {},
  });
  vm.runInContext(script, ctx, {filename:'admin.html'});
  return { ctx, element, panels, tabs, state };
}

test('user directory hides tokens; profile and device table link to owner', async () => {
  const {ctx,element,panels,tabs} = setup();
  await ctx.load();
  assert.match(element('#users').innerHTML, /data-open-user="alice"/);
  assert.doesNotMatch(element('#users').innerHTML, /SECRET_TOKEN|Rotate token|Delete user|data-copy/);
  assert.match(element('#deviceUsers').innerHTML, /data-open-user="alice"/);
  assert.match(element('#deviceUsers').innerHTML, /sensitive-hwid/);

  ctx.openUser('alice');
  assert.equal(panels.find(p=>p.id==='userDetailTab').classList.active, true);
  assert.equal(tabs.find(t=>t.dataset.tab==='usersTab').classList.active, true);
  assert.equal(element('.stat-grid').hidden, true);
  assert.equal(element('#detailUsername').textContent, 'alice');
  assert.match(element('#detailSubscription').textContent, /SECRET_TOKEN/);
  assert.match(element('#detailDeviceUsers').innerHTML, /sensitive-hwid/);

  await element('#detailCopy').onclick();
  assert.equal(ctx.copied, 'https://vpn.example/sub/SECRET_TOKEN');
  element('#backToUsers').onclick();
  assert.equal(panels.find(p=>p.id==='usersTab').classList.active, true);
  assert.equal(element('.stat-grid').hidden, false);
});

test('user creation form stays hidden until requested; device directory opens owner', async () => {
  const {ctx,element,panels} = setup();
  await ctx.load();
  assert.equal(element('#addUserPanel').hidden, false, 'mock DOM has no markup parser');
  element('#addUserPanel').hidden = true;
  element('#toggleAddUser').onclick();
  assert.equal(element('#addUserPanel').hidden, false);
  element('#cancelAddUser').onclick();
  assert.equal(element('#addUserPanel').hidden, true);
  ctx.setTab('devicesTab');
  ctx.openUser('alice');
  assert.equal(panels.find(p=>p.id==='userDetailTab').classList.active, true);
  element('#backToUsers').onclick();
  assert.equal(panels.find(p=>p.id==='devicesTab').classList.active, true);
});
