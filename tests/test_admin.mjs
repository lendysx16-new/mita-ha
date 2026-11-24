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

function setup(overrides = {}) {
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
  const state = { outbound_enabled: overrides.outboundEnabled, users: [{
    username: 'alice', allow_private_ip: false, allow_loopback_ip: false,
    legacy_enabled: true, device_limit: 2, subscription_url: 'https://vpn.example/sub/SECRET_TOKEN',
    devices: [{id: '0123456789abcdef', name: 'Laptop', hwid: 'sensitive-hwid',
      last_seen: '2026-10-10T13:00:00Z', os: 'Linux', os_version: '6'}],
  }, ...(overrides.extraUsers || [])], settings: {
    public_mieru_host: 'vpn.example', public_mieru_port: 8443,
    subscription_base_url: 'https://vpn.example', contact_email: '',
    log_level: 'INFO', postgres_dsn: '',
  }};
  const document = {
    title: '', hidden: false,
    addEventListener() {},
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
    location: { href: 'http://127.0.0.1:8098/' },
    fetch: async url => ({ ok: true, json: async () =>
      String(url).includes('api/backup-status') ? {status:{enabled:false}} : state }),
    Intl, Date, URL,
    alert() {}, confirm() { return true; }, addEventListener() {},
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

test('rendered markup escapes user-controlled values', async () => {
  const {ctx,element} = setup({extraUsers: [{
    username: 'bob', allow_private_ip: true, allow_loopback_ip: false, legacy_enabled: false, device_limit: 0,
    subscription_url: 'https://vpn.example/sub/OTHER',
    devices: [{id: 'fedcba9876543210', name: '<img src=x onerror=alert(1)>', hwid: 'hw"id', last_seen: '', os: '', os_version: ''}],
  }]});
  await ctx.load();
  const devices = element('#deviceUsers').innerHTML;
  assert.doesNotMatch(devices, /<img/);
  assert.match(devices, /&lt;img src=x/);
  assert.match(devices, /hw&quot;id/);
  assert.match(element('#users').innerHTML, />LAN</);
  assert.match(element('#users').innerHTML, />HWID only</);
});

test('routing writes a deep-linkable hash and restores it', async () => {
  const {ctx,element,panels} = setup();
  await ctx.load();
  ctx.openUser('alice');
  assert.equal(ctx.location.hash, '#/users/alice');
  ctx.setTab('trafficTab');
  assert.equal(ctx.location.hash, '#/traffic');
  ctx.location.hash = '#/users/alice';
  ctx.applyRoute();
  assert.equal(panels.find(p=>p.id==='userDetailTab').classList.active, true);
  ctx.location.hash = '#/users/nobody';
  ctx.applyRoute();
  assert.equal(panels.find(p=>p.id==='usersTab').classList.active, true);
  ctx.location.hash = '#/settings';
  ctx.applyRoute();
  assert.equal(panels.find(p=>p.id==='confTab').classList.active, true);
  assert.equal(element('#pageTitle').textContent, 'Settings');
});

test('destructive controls respect backend invariants', async () => {
  const {ctx,element,state} = setup();
  state.users[0].legacy_enabled = false;
  await ctx.load();
  ctx.openUser('alice');
  assert.equal(element('#detailDelete').disabled, true, 'last remaining user cannot be deleted');
  assert.match(element('#detailDeviceUsers').innerHTML, /data-remove-device="0123456789abcdef"[^>]* disabled/);
});

test('device directory filters by owner, name or HWID', async () => {
  const {ctx,element} = setup({extraUsers: [{
    username: 'bob', allow_private_ip: false, allow_loopback_ip: false, legacy_enabled: true, device_limit: 0,
    subscription_url: 'https://vpn.example/sub/OTHER',
    devices: [{id: 'fedcba9876543210', name: 'Phone', hwid: 'phone-hwid', last_seen: '2026-10-11T13:00:00Z', os: 'iOS', os_version: '18'}],
  }]});
  await ctx.load();
  assert.equal(element('#deviceCount').textContent, '2 devices');
  assert.ok(element('#deviceUsers').innerHTML.indexOf('Phone') < element('#deviceUsers').innerHTML.indexOf('Laptop'), 'most recent first');
  element('#deviceSearch').value = 'sensitive';
  ctx.renderDeviceDirectory();
  assert.match(element('#deviceUsers').innerHTML, /Laptop/);
  assert.doesNotMatch(element('#deviceUsers').innerHTML, /Phone/);
  element('#deviceSearch').value = 'zzz';
  ctx.renderDeviceDirectory();
  assert.match(element('#deviceUsers').innerHTML, /No devices match/);
});

test('mutations send JSON content type; user settings and password reset use new endpoints', async () => {
  const {ctx,element,state} = setup({extraUsers: [{
    username: 'bob', allow_private_ip: false, allow_loopback_ip: false, legacy_enabled: true, device_limit: 0,
    subscription_url: 'https://vpn.example/sub/OTHER', devices: [],
  }]});
  await ctx.load();
  const calls = [];
  ctx.fetch = async (url, opts = {}) => {
    calls.push({url: String(url), method: opts.method || 'GET', type: opts.headers?.['Content-Type'], body: opts.body});
    const path = String(url);
    const payload = path.endsWith('/password') ? {user: state.users[0], generated_password: 'NEW-SECRET'} : state;
    return {ok: true, json: async () => payload};
  };
  ctx.openUser('alice');
  const form = {
    dataset: {deviceForm: 'alice'},
    elements: {device_limit: {value: '3'}, legacy_enabled: {checked: true},
      allow_private_ip: {checked: true}, allow_loopback_ip: {checked: false}},
    querySelector() { return element('#formSubmit'); },
  };
  await ctx.saveDeviceSettings(form);
  const patch = calls.find(c => c.method === 'PATCH');
  assert.equal(patch.url, 'http://127.0.0.1:8098/api/users/alice');
  assert.equal(patch.type, 'application/json');
  assert.deepEqual(JSON.parse(patch.body), {device_limit: 3, legacy_enabled: true, allow_private_ip: true});

  await ctx.resetPassword('alice');
  assert.ok(calls.some(c => c.method === 'POST' && c.url.endsWith('/api/users/alice/password')));
  assert.equal(element('#detailPassword').value, 'NEW-SECRET');
  assert.equal(element('#detailPasswordPanel').hidden, false);
  ctx.openUser('bob');
  assert.equal(element('#detailPassword').value, '', 'password is cleared when switching users');

  await ctx.removeUser('bob');
  const del = calls.find(c => c.method === 'DELETE');
  assert.equal(del.type, 'application/json', 'DELETE must carry the CSRF-safe content type');
});

test('users on the placeholder password are flagged with a reset shortcut', async () => {
  const {ctx,element,state} = setup();
  state.users[0].default_password = true;
  await ctx.load();
  assert.match(element('#users').innerHTML, /badge-warning">Default password/);
  ctx.openUser('alice');
  assert.equal(element('#detailPasswordWarning').hidden, false);
  assert.doesNotMatch(element('#detailAccess').textContent, /Default password/);
  state.users[0].default_password = false;
  await ctx.load();
  assert.equal(element('#detailPasswordWarning').hidden, true);
});

test('routing tab shows Everything else and the generated sing-box route', async () => {
  const {ctx, element, state} = setup({outboundEnabled: true});
  state.routing_active = true;
  state.sing_box_running = true;
  state.sing_box_available = true;
  state.settings.outbounds = [{name: 'nl', port: 1081, outbound: {type: 'trojan', server: 't.example', server_port: 443}}];
  state.settings.routing_final = 'nl';
  state.sing_box_route = {final: 'direct', rules: [{inbound: ['router'], outbound: 'out-0'}]};
  await ctx.load();
  assert.match(element('#routingFinal').innerHTML, /value="direct"[\s\S]*value="block"[\s\S]*value="nl" selected/);
  assert.match(element('#routePreviewJson').textContent, /"outbound": "out-0"/);
  assert.match(element('#singBoxStatus').textContent, /sing-box routes all traffic with 1 outbound/);
  state.sing_box_running = false;
  await ctx.load();
  assert.equal(element('#singBoxStatus').dataset.state, 'error');
  state.routing_active = false;
  state.sing_box_route = null;
  await ctx.load();
  assert.match(element('#singBoxStatus').textContent, /Routing is off/);
  assert.match(element('#routePreviewJson').textContent, /Routing is off/);
  assert.ok(html.includes('id="finalForm"'));
});

test('account settings show and save a named outbound when routing is available', async () => {
  for (const enabled of [false, true]) {
    const {ctx, element, state} = setup({outboundEnabled: enabled});
    state.proxy_outbounds = ['east', 'west'];
    state.users[0].proxy_outbound = 'west';
    await ctx.load();
    ctx.openUser('alice');
    const details = element('#detailDeviceUsers').innerHTML;
    if (!enabled) {
      assert.doesNotMatch(details, /name="proxy_outbound"/);
      continue;
    }
    assert.match(details, /<option value="east">east<\/option>/);
    assert.match(details, /<option value="west" selected>west<\/option>/);
    let payload;
    const fetchState = ctx.fetch;
    ctx.fetch = async (url, options) => {
      if (options?.method === 'PATCH') {
        payload = JSON.parse(options.body);
        return {ok: true, json: async () => ({})};
      }
      return fetchState(url, options);
    };
    await ctx.saveDeviceSettings({
      dataset: {deviceForm: 'alice'},
      elements: {device_limit: {value: '2'}, legacy_enabled: {checked: true},
        allow_private_ip: {checked: false}, allow_loopback_ip: {checked: false},
        proxy_outbound: {value: 'east'}},
      querySelector() { return element('#formSubmit'); },
    });
    assert.equal(payload.proxy_outbound, 'east');
  }
});

test('Geo source import preserves suffix and CIDR selectors and rejects unsupported matches', () => {
  const {ctx} = setup();
  const source = {version: 3, rules: [
    {domain_suffix: ['Example.com.', '_dmarc.example.com', 'пример.рф'], domain: ['exact.example'], domain_regex: ['.*']},
    {ip_cidr: ['198.51.100.0/24']},
  ]};
  const parsed = ctx.readGeoRuleSet(source);
  assert.deepEqual([...parsed.domains], ['example.com']);
  assert.deepEqual([...parsed.ipRanges], ['198.51.100.0/24']);
  assert.deepEqual([...parsed.skipped], ['domain', 'domain_regex', 'invalid domain suffixes']);
  assert.throws(() => ctx.readGeoRuleSet({version: 3, rules: [{domain: ['example.com']}]}), /no compatible/);
  assert.throws(() => ctx.readGeoRuleSet({version: 3, rules: [
    {domain_suffix: ['example.com'], ip_cidr: ['198.51.100.0/24']},
  ]}), /no compatible/);
});

test('GeoIP import keeps valid networks and skips what the server would reject', () => {
  const {ctx} = setup();
  const parsed = ctx.readGeoRuleSet({version: 2, rules: [{ip_cidr: [
    '1.0.1.0/24', ' 10.0.0.1/8 ', '2001:DB8::/32', '::ffff:192.0.2.0/120', '::/1',
    '0.0.0.0/0', '::/0', 'nope', '1.2.3.4', '256.1.1.1/8', '1.2.3/24', '1.2.3.4/33',
    '2001:db8::1::/64', '2001:db8:1:2:3:4:5:6:7/64', '1.2.3.4/24/1', 42,
    '192.0.2.1::/64', '01.2.3.4/24', '::ffff:192.000.2.1/120', '1.2.3.4::5/64',
  ]}]});
  assert.deepEqual([...parsed.ipRanges],
    ['1.0.1.0/24', '10.0.0.1/8', '2001:db8::/32', '::ffff:192.0.2.0/120', '::/1']);
  assert.deepEqual([...parsed.skipped], ['invalid CIDRs']);
  assert.throws(() => ctx.readGeoRuleSet({version: 2, rules: [{ip_cidr: ['0.0.0.0/0']}]}), /no compatible/);
});

test('account outbound select offers only live outbounds and keeps the current choice', async () => {
  // `state` is the mocked /api/state response that load() reads.
  const {ctx, element, state} = setup({outboundEnabled: true});
  state.proxy_outbounds = ['east'];
  state.pending_proxy_outbounds = ['north'];
  state.users[0].proxy_outbound = 'west';
  await ctx.load();
  ctx.openUser('alice');
  const details = element('#detailDeviceUsers').innerHTML;
  assert.match(details, /<option value="east">east<\/option>/);
  assert.match(details, /<option value="west" selected>west<\/option>/);
  assert.doesNotMatch(details, /<option value="north"/);
  assert.match(details, /Restart Mita before assigning: north/);
  delete state.pending_proxy_outbounds;  // older backends omit the field
  await ctx.load();
  ctx.openUser('alice');
  assert.doesNotMatch(element('#detailDeviceUsers').innerHTML, /data-pending-outbounds/);
});

test('outbounds and routing policies render and policies are built from the editor', async () => {
  const {ctx, element, state} = setup({outboundEnabled: true});
  state.sing_box_available = true;
  state.sing_box_running = true;
  state.settings.outbounds = [
    {name: 'nl', port: 1081, outbound: {type: 'hysteria2', server: 'hy.example', server_port: 8443, password: 'SECRET'}},
    {name: 'de', port: 1082, outbound: {type: 'vless', server: 'v.example', server_port: 443, uuid: 'u', tls: {enabled: true, reality: {enabled: true}}}},
  ];
  state.settings.geo_snapshots = {'https://raw.githubusercontent.com/o/r/main/g.json': {domains: 120, ip_ranges: 3},
    'https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/sing/geo/geosite/youtube.json': {domains: 10, ip_ranges: 0}};
  state.settings.egress = {
    proxies: [],
    rules: [{name: 'Streaming <b>', domainNames: ['video.example', 'cdn.example'], ipRanges: ['198.51.100.0/24'], action: 'PROXY', proxyNames: ['nl'],
             geoUrls: ['https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/sing/geo/geosite/youtube.json', 'https://raw.githubusercontent.com/o/r/main/g.json']},
      {domainNames: ['ads.example'], ipRanges: [], action: 'REJECT', proxyNames: []}],
  };
  state.users[0].proxy_outbound = 'nl';
  await ctx.load();
  assert.equal(element('.tab[data-tab="outboundsTab"]').hidden, false);
  assert.equal(element('.tab[data-tab="routingTab"]').hidden, false);
  assert.match(element('#singBoxStatus').textContent, /Routing is off|routes all traffic/);
  const outbounds = element('#outboundRows').innerHTML;
  assert.match(outbounds, /Hysteria2[\s\S]*hy\.example:8443/);
  assert.match(outbounds, /VLESS REALITY/);
  assert.doesNotMatch(outbounds, /SECRET/);
  assert.match(outbounds, /1 policy · 1 account/);
  assert.match(outbounds, /data-routing="delete-outbound" data-name="nl" disabled/, 'used outbound cannot be deleted');
  assert.match(outbounds, /data-routing="delete-outbound" data-name="de" title/, 'unused outbound can be deleted');
  const lists = element('#listRows').innerHTML;
  assert.match(lists, /Streaming &lt;b&gt;/);
  assert.match(lists, /2 domains · 1 network · geosite:youtube, https:\/\/raw\.githubusercontent\.com\/o\/r\/main\/g\.json \(133 entries\)/);
  assert.match(lists, />Block</);
  assert.match(lists, /Unnamed policy/);
  assert.match(lists, /data-step="-1" disabled/);

  const fields = {name: {value: ''}, target: {value: '', innerHTML: ''}, domains: {value: ''}, ipRanges: {value: ''}, geoUrls: {value: ''}};
  fields.name.focus = () => {};
  Object.assign(element('#listEditor'), {elements: fields, querySelector: () => ({textContent: ''})});
  ctx.openListEditor(-1);
  assert.match(fields.target.innerHTML, /value="proxy:nl"[\s\S]*value="proxy:de"[\s\S]*value="DIRECT"[\s\S]*value="REJECT"/);
  fields.name.value = ' Work ';
  fields.target.value = 'proxy:de';
  fields.domains.value = 'corp.example\n\n corp.example \nmail.example';
  fields.geoUrls.value = 'geosite:telegram\ngeoip:telegram';
  const egress = ctx.listFromEditor();
  assert.deepEqual(JSON.parse(JSON.stringify(egress.rules[2])), {
    name: 'Work', domainNames: ['corp.example', 'mail.example'], ipRanges: [],
    action: 'PROXY', proxyNames: ['de'], geoUrls: ['geosite:telegram', 'geoip:telegram'],
  });
  fields.target.value = 'DIRECT';
  assert.equal(ctx.listFromEditor().rules[2].action, 'DIRECT');
  fields.domains.value = '';
  assert.equal(ctx.listFromEditor().rules[2].domainNames.length, 0, 'a GitHub-only policy is allowed');
  fields.geoUrls.value = '';
  assert.throws(() => ctx.listFromEditor(), /domain, an IP network or a GeoSite \/ GeoIP category/);

  ctx.openListEditor(0);
  assert.equal(fields.target.value, 'proxy:nl');
  assert.equal(fields.domains.value, 'video.example\ncdn.example');
  assert.equal(fields.geoUrls.value, 'geosite:youtube\nhttps://raw.githubusercontent.com/o/r/main/g.json');
});

test('outbound editor shows the saved sing-box JSON and the status explains a missing binary', async () => {
  const {ctx, element, state} = setup({outboundEnabled: true});
  state.sing_box_available = false;
  state.settings.outbounds = [{name: 'nl', port: 1081, outbound: {type: 'trojan', server: 't.example', server_port: 443, password: 'p'}}];
  await ctx.load();
  assert.match(element('#singBoxStatus').textContent, /not installed/);
  assert.equal(element('#addOutbound').disabled, true);
  const fields = {name: {value: ''}, config: {value: ''}};
  fields.name.focus = fields.config.focus = () => {};
  Object.assign(element('#outboundEditor'), {elements: fields});
  ctx.openOutboundEditor('nl');
  assert.equal(fields.name.value, 'nl');
  assert.equal(fields.name.readOnly, true);
  assert.deepEqual(JSON.parse(fields.config.value), {type: 'trojan', server: 't.example', server_port: 443, password: 'p'});
  ctx.openOutboundEditor('');
  assert.equal(fields.name.readOnly, false);
  assert.equal(fields.config.value, '');
});

test('outbounds and routing are separate tabs; policies only pick saved outbounds', () => {
  const outboundsTab = /<div id="outboundsTab"[\s\S]*?(?=<div id="routingTab")/.exec(html)?.[0] || '';
  const routingTab = /<div id="routingTab"[\s\S]*?(?=<div id="confTab")/.exec(html)?.[0] || '';
  assert.match(outboundsTab, /id="outboundEditor"/);
  assert.match(outboundsTab, /name="config"/);
  assert.doesNotMatch(outboundsTab, /name="host"|id="singBoxForm"/);
  assert.doesNotMatch(outboundsTab, /id="listEditor"/);
  assert.match(routingTab, /id="listEditor"/);
  assert.match(routingTab, /<select name="target" required>/);
  assert.match(routingTab, /name="geoUrls"/);
  assert.doesNotMatch(routingTab, /id="outboundEditor"|name="config"/);
});

test('outbound status shows the last check and Test refreshes it', async () => {
  const {ctx, element, state} = setup({outboundEnabled: true});
  state.sing_box_available = true;
  state.sing_box_running = true;
  state.settings.outbounds = [
    {name: 'nl', port: 1081, outbound: {type: 'hysteria2', server: 'hy.example', server_port: 443, password: 'p'}},
    {name: 'de', port: 1082, outbound: {type: 'trojan', server: 't.example', server_port: 443, password: 'p'}},
    {name: 'fi', port: 1083, outbound: {type: 'trojan', server: 'f.example', server_port: 443, password: 'p'}},
  ];
  state.outbound_health = {
    nl: {ok: true, latency_ms: 142, error: '', checked_at: '2026-11-24T20:00:00Z'},
    de: {ok: false, latency_ms: null, error: 'the outbound timed out <x>', checked_at: '2026-11-24T20:00:00Z'},
  };
  await ctx.load();
  const rows = element('#outboundRows').innerHTML;
  assert.match(rows, /data-state="ok"[^>]*>OK · 142 ms/);
  assert.match(rows, /data-state="error"[^>]*>the outbound timed out &lt;x&gt;/);
  assert.match(rows, /Not checked yet/);
  assert.match(rows, /data-routing="test-outbound" data-name="fi"/);

  let tested;
  const fetchState = ctx.fetch;
  ctx.fetch = async (url, options) => {
    if (String(url).endsWith('api/outbounds/fi/test')) {
      tested = options.method;
      return {ok: true, json: async () => ({health: {ok: true, latency_ms: 77, error: '', checked_at: '2026-11-24T20:01:00Z'}})};
    }
    return fetchState(url, options);
  };
  await ctx.onRoutingClick({target: {closest: selector => selector === '[data-routing]'
    ? {dataset: {routing: 'test-outbound', name: 'fi'}, disabled: false, setAttribute() {}} : null}});
  assert.equal(tested, 'POST');
  assert.match(element('#outboundRows').innerHTML, /OK · 77 ms/);
  assert.match(element('#msg').textContent, /fi works \(77 ms\)/);
});

test('restart bar and Geo sources reflect the server state', async () => {
  const {ctx, element, state} = setup({outboundEnabled: true});
  state.restart_pending = true;
  state.settings.geo_sources = {geosite: 'https://github.com/o/r/releases/latest/download/geosite.dat'};
  state.geo_dat = {geosite: {url: 'https://github.com/o/r/releases/latest/download/geosite.dat', downloaded_at: '2026-11-24T20:00:00Z', bytes: 3145728}};
  await ctx.load();
  for (const id of ['#restartBarOutbounds', '#restartBarRouting']) {
    assert.equal(element(id).dataset.state, 'warn');
    assert.match(element(id).innerHTML, /needs a restart[\s\S]*data-routing="restart-mita"/);
  }
  assert.match(element('[data-dat-status="geosite"]').textContent, /^Downloaded .* · 3\.0 MiB\.$/);
  assert.equal(element('[data-dat-status="geoip"]').textContent, 'Using MetaCubeX geoip categories.');

  state.restart_pending = false;
  state.geo_dat = {};
  await ctx.load();
  assert.equal(element('#restartBarRouting').dataset.state, 'ok');
  assert.match(element('#restartBarRouting').innerHTML, /Policy changes apply instantly/);
  assert.equal(element('[data-dat-status="geosite"]').textContent, 'Not downloaded yet.');

  let restarted;
  const fetchState = ctx.fetch;
  ctx.fetch = async (url, options) => {
    if (String(url).endsWith('api/mita/restart')) { restarted = options.method; return {ok: true, json: async () => ({restarted: true})}; }
    return fetchState(url, options);
  };
  await ctx.onRoutingClick({target: {closest: selector => selector === '[data-routing]'
    ? {dataset: {routing: 'restart-mita'}, disabled: false, setAttribute() {}} : null}});
  assert.equal(restarted, 'POST');
  assert.match(element('#msg').textContent, /Mita restarted/);
});
