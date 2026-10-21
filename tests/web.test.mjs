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
    addEventListener() {}, scrollIntoView() {}
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
