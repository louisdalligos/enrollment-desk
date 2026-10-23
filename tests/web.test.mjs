import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const html = await readFile(new URL('../src/enrollment_desk/web.html', import.meta.url), 'utf8');
const app = await readFile(new URL('../src/enrollment_desk/web.js', import.meta.url), 'utf8');
const auth = await readFile(new URL('../src/enrollment_desk/auth.js', import.meta.url), 'utf8');
let serial = 0;
async function boot(mode, signedIn = false) {
  // Load the real HTML's element IDs so missing bootstrap targets fail the test.
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], {
    textContent: '', innerHTML: '', hidden: false, disabled: false, value: '',
    handlers: {}, addEventListener(type, callback) { this.handlers[type] = callback; }, scrollIntoView() {}
  }]));
  const saved = new Map();
  if (signedIn) saved.set('enrollment.session', JSON.stringify({accessToken: 'test-access', expiresAt: Date.now() + 60000}));
  globalThis.sessionStorage = {getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key)};
  globalThis.document = {getElementById: id => nodes.get(id) || null, hidden: false};
  globalThis.location = {origin: 'https://desk.example', search: ''};
  globalThis.setInterval = () => 0;
  globalThis.fetch = async (url, options) => {
    if (url === '/config.json') return {ok: true, json: async () => ({mode, domain: 'https://enrollment-test.auth.ap-southeast-2.amazoncognito.com', clientId: 'test-client'})};
    assert.equal(url, '/api/cases');
    if (mode === 'aws') assert.equal(options.headers.get('Authorization'), 'Bearer test-access');
    return {ok: true, json: async () => ({cases: [{case_id: 'end-20261021-123456', reference_number: 'END-20261021-123456', company_id: 'CMP-DALISAY', source_document: 'clean.csv', received_at: '2026-10-21T00:00:00Z', status: 'active', members: []}], companies: [{company_id: 'CMP-DALISAY', name: 'Dalisay'}], demo_date: '2026-10-15'})};
  };
  const authUrl = 'data:text/javascript;base64,' + Buffer.from(auth + '\n// boot ' + serial).toString('base64');
  const script = app.replace("'./auth.js'", JSON.stringify(authUrl)) + '\n// boot ' + serial++;
  await import('data:text/javascript;base64,' + Buffer.from(script).toString('base64'));
  for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
  assert.notEqual(nodes.get('message').className, 'error', nodes.get('message').textContent);
  return nodes;
}

test('cloud boot displays sign-in and AWS labels without accessing protected cases', async () => {
  const nodes = await boot('aws');
  assert.equal(nodes.get('workspace').textContent, 'AWS WORKSPACE');
  assert.equal(nodes.get('mode').textContent, 'Insurer simulator · AWS workflow');
  assert.equal(nodes.get('auth').textContent, 'Sign in');
  assert.equal(nodes.get('empty').textContent, 'Sign in to view your enrollments.');
  assert.equal(nodes.get('submit').disabled, true);
  assert.match(nodes.get('simulation').textContent, /real timestamps/);
});

test('authenticated cloud boot loads protected cases and assigned companies', async () => {
  const nodes = await boot('aws', true);
  assert.equal(nodes.get('auth').textContent, 'Sign out');
  assert.equal(nodes.get('total').textContent, 1);
  assert.match(nodes.get('cases').innerHTML, /END-20261021-123456/);
  assert.equal(nodes.get('submit').disabled, false);
});

test('local boot still loads demo cases without a token', async () => {
  const nodes = await boot('local');
  assert.equal(nodes.get('total').textContent, 1);
  assert.equal(nodes.get('demoDate').textContent, '2026-10-15');
  assert.equal(nodes.get('submit').disabled, false);
});


test('local column review requires explicit approve before workflow request', async () => {
  const nodes = await boot('local');
  nodes.get('reviewMapping').checked = true;
  const file = new Blob(['Payroll reference\nSYN-1']); file.name = 'test.csv';
  nodes.get('file').files = [file];
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push(url);
    assert.match(url, /^\/api\/mapping-preview/);
    return {ok: true, json: async () => ({job_id: 'abc', header_row: 0,
      headers: ['Payroll reference'], mapping: {'0': null}, fields: ['employee_id'],
      rows: [{index: 0, labels: ['Payroll reference']}]})};
  };
  await nodes.get('upload').handlers.submit({preventDefault() {}});
  assert.equal(nodes.get('mappingPanel').hidden, false);
  assert.equal(calls.length, 1);
  nodes.get('mappingColumns').querySelectorAll = () => [{dataset: {column: '0'}, value: 'employee_id'}];
  globalThis.fetch = async (url, options) => {
    calls.push(url);
    assert.equal(url, '/api/mapping-approve');
    const body = JSON.parse(options.body);
    assert.equal(body.confirm, true);
    assert.deepEqual(body.mapping, {'0': 'employee_id'});
    return {ok: false, status: 400, json: async () => ({error: 'Keep reviewing'})};
  };
  nodes.get('approveMapping').handlers.click();
  for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 2);
  assert.equal(nodes.get('mappingPanel').hidden, false);
  assert.match(nodes.get('mappingMessage').textContent, /Keep reviewing/);
});

test('manual-only review hides Bedrock suggestions and explains approval', async () => {
  const nodes = await boot('local');
  nodes.get('reviewMapping').checked = true;
  const file = new Blob(['Payroll reference\nSYN-1']); file.name = 'unknown.csv';
  nodes.get('file').files = [file];
  globalThis.fetch = async () => ({ok: true, json: async () => ({job_id: 'abc', header_row: 0,
    headers: ['Payroll reference'], mapping: {'0': null}, fields: ['employee_id'],
    rows: [{index: 0, labels: ['Payroll reference']}], bedrock_enabled: false})});
  await nodes.get('upload').handlers.submit({preventDefault() {}});
  assert.equal(nodes.get('suggestMapping').hidden, true);
  assert.match(nodes.get('mappingHelp').textContent, /Bedrock is disabled/);
  assert.equal(nodes.get('mappingPanel').hidden, false);
});
