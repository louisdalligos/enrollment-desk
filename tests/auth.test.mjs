import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source = await readFile(new URL('../src/enrollment_desk/auth.js', import.meta.url), 'utf8');
let serial = 0;
const domain = 'https://enrollment-test.auth.ap-southeast-1.amazoncognito.com';
function storage() {
  const saved = new Map();
  return {getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key)};
}
async function setup(config = {mode: 'aws', domain, clientId: 'client'}) {
  globalThis.sessionStorage = storage();
  globalThis.location = {origin: 'https://desk.example', search: '', assign(url) { this.assigned = url; }};
  globalThis.history = {replaceState() { this.cleared = true; }};
  globalThis.fetch = async url => {
    assert.equal(url, '/config.json');
    return {ok: true, json: async () => config};
  };
  return import('data:text/javascript;base64,' + Buffer.from(source + `\n// test ${serial++}`).toString('base64'));
}

test('local mode requires no token and sends no authorization', async () => {
  const auth = await setup({mode: 'local'});
  await auth.initializeAuth();
  assert.equal(auth.isCloud(), false);
  globalThis.fetch = async (url, options) => {
    assert.equal(options.headers.has('Authorization'), false);
    return {ok: true, json: async () => ({ok: true})};
  };
  await auth.request('/api/cases');
});

test('PKCE uses S256, random state and a redirect restricted to this origin', async () => {
  const auth = await setup();
  await auth.initializeAuth();
  await auth.signIn();
  const url = new URL(location.assigned);
  assert.equal(url.origin, domain);
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256');
  assert.equal(url.searchParams.get('redirect_uri'), 'https://desk.example/');
  const flow = JSON.parse(sessionStorage.getItem('enrollment.pkce'));
  assert.equal(flow.verifier.length, 43);
  assert.equal(flow.state, url.searchParams.get('state'));
  assert.notEqual(flow.verifier, url.searchParams.get('code_challenge'));
});

test('unverified callback fails closed and clears callback URL', async () => {
  const auth = await setup();
  location.search = '?code=secret&state=wrong';
  await assert.rejects(auth.initializeAuth(), /could not be verified/);
  assert.equal(auth.authenticated(), false);
  assert.equal(history.cleared, true);
});

test('expired callback is rejected', async () => {
  const auth = await setup();
  sessionStorage.setItem('enrollment.pkce', JSON.stringify({state: 'ok', verifier: 'test', created: Date.now() - 700000}));
  location.search = '?code=secret&state=ok';
  await assert.rejects(auth.initializeAuth(), /expired/);
});

test('verified code exchanges verifier and stores only access token', async () => {
  const auth = await setup();
  await auth.initializeAuth();
  await auth.signIn();
  const flow = JSON.parse(sessionStorage.getItem('enrollment.pkce'));
  location.search = '?code=test-code&state=' + flow.state;
  globalThis.fetch = async (url, options) => {
    if (url === '/config.json') return {ok: true, json: async () => ({mode: 'aws', domain, clientId: 'client'})};
    assert.equal(url, domain + '/oauth2/token');
    assert.equal(options.body.get('code_verifier'), flow.verifier);
    return {ok: true, json: async () => ({access_token: 'access', id_token: 'discard-id', refresh_token: 'discard-refresh', expires_in: 3600})};
  };
  await auth.initializeAuth();
  assert.equal(auth.authenticated(), true);
  assert.equal(sessionStorage.getItem('enrollment.pkce'), null);
  assert.ok(!sessionStorage.getItem('enrollment.session').includes('discard'));
  globalThis.fetch = async (url, options) => {
    assert.equal(options.headers.get('Authorization'), 'Bearer access');
    return {ok: true, json: async () => ({ok: true})};
  };
  await auth.request('/api/cases');
});

test('expired token never reaches API, and 401 clears session', async () => {
  const auth = await setup();
  await auth.initializeAuth();
  sessionStorage.setItem('enrollment.session', JSON.stringify({accessToken: 'expired', expiresAt: 1}));
  await assert.rejects(auth.request('/api/cases'), /expired/);
  sessionStorage.setItem('enrollment.session', JSON.stringify({accessToken: 'fresh', expiresAt: Date.now() + 60000}));
  globalThis.fetch = async () => ({ok: false, status: 401, json: async () => ({})});
  await assert.rejects(auth.request('/api/cases'), /sign in/);
  assert.equal(sessionStorage.getItem('enrollment.session'), null);
});

test('configuration failures never fall back to unauthenticated cloud mode', async () => {
  const auth = await setup();
  globalThis.fetch = async () => ({ok: false});
  await assert.rejects(auth.initializeAuth(), /configuration/);
  const invalid = await setup({mode: 'aws', domain: 'https://untrusted.example', clientId: 'x'});
  await assert.rejects(invalid.initializeAuth(), /invalid/);
});

test('sign-out discards session and redirects to Cognito', async () => {
  const auth = await setup();
  await auth.initializeAuth();
  sessionStorage.setItem('enrollment.session', 'secret');
  auth.signOut();
  assert.equal(sessionStorage.getItem('enrollment.session'), null);
  const url = new URL(location.assigned);
  assert.equal(url.origin, domain);
  assert.equal(url.searchParams.get('logout_uri'), 'https://desk.example/');
});
