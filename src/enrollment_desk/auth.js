// Browser-only OAuth authorization-code flow with PKCE. No AWS keys or client secret.
let config = {mode: 'local'};
const TOKEN = 'enrollment.session';
const FLOW = 'enrollment.pkce';
const b64url = bytes => btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
const random = () => b64url(crypto.getRandomValues(new Uint8Array(32)));

export function isCloud() { return config.mode === 'aws'; }
export function authenticated() {
  if (!isCloud()) return true;
  try {
    const saved = JSON.parse(sessionStorage.getItem(TOKEN) || 'null');
    return Boolean(saved?.accessToken && saved.expiresAt > Date.now() + 10000);
  } catch { return false; }
}
export async function initializeAuth() {
  const response = await fetch('/config.json', {cache: 'no-store'});
  if (!response.ok) throw Error('Cannot load application configuration.');
  config = await response.json();
  if (!['local', 'aws'].includes(config.mode)) throw Error('Invalid application configuration.');
  if (!isCloud()) return;
  if (!/^https:\/\/[a-z0-9-]+\.auth\.[a-z0-9-]+\.amazoncognito\.com$/.test(config.domain) || !config.clientId) {
    throw Error('Sign-in configuration is invalid.');
  }
  const query = new URLSearchParams(location.search);
  if (query.has('error') || query.has('code')) {
    const flow = JSON.parse(sessionStorage.getItem(FLOW) || 'null');
    sessionStorage.removeItem(FLOW);
    history.replaceState({}, '', '/');
    if (!flow || flow.state !== query.get('state') || Date.now() - flow.created > 10 * 60 * 1000) {
      throw Error('Sign-in expired or could not be verified. Please sign in again.');
    }
    if (query.has('error')) throw Error('Sign-in was cancelled or denied. Please try again.');
    const tokenResponse = await fetch(config.domain + '/oauth2/token', {
      method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({grant_type: 'authorization_code', client_id: config.clientId,
        redirect_uri: location.origin + '/', code: query.get('code'), code_verifier: flow.verifier})
    });
    if (!tokenResponse.ok) throw Error('Sign-in failed. Please try again.');
    const tokens = await tokenResponse.json();
    if (!tokens.access_token || !tokens.expires_in) throw Error('Sign-in did not return a valid session.');
    sessionStorage.setItem(TOKEN, JSON.stringify({accessToken: tokens.access_token,
      expiresAt: Date.now() + Number(tokens.expires_in) * 1000}));
    // Deliberately discard ID and refresh tokens; sign in again after expiry.
  }
}
export async function signIn() {
  const verifier = random(), state = random();
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  sessionStorage.setItem(FLOW, JSON.stringify({verifier, state, created: Date.now()}));
  location.assign(config.domain + '/oauth2/authorize?' + new URLSearchParams({
    response_type: 'code', client_id: config.clientId, redirect_uri: location.origin + '/',
    scope: 'openid email enrollment/access', state, code_challenge: b64url(new Uint8Array(digest)),
    code_challenge_method: 'S256'
  }));
}
export function signOut() {
  sessionStorage.removeItem(TOKEN);
  sessionStorage.removeItem(FLOW);
  location.assign(config.domain + '/logout?' + new URLSearchParams({
    client_id: config.clientId, logout_uri: location.origin + '/'
  }));
}
export async function request(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (isCloud()) {
    if (!authenticated()) {
      sessionStorage.removeItem(TOKEN);
      throw Error('Your session has expired. Sign in again.');
    }
    headers.set('Authorization', 'Bearer ' + JSON.parse(sessionStorage.getItem(TOKEN)).accessToken);
  }
  const response = await fetch(url, {...options, headers, cache: 'no-store'});
  const data = await response.json();
  if (response.status === 401) sessionStorage.removeItem(TOKEN);
  if (!response.ok) throw Error(data.error || (response.status === 401 ? 'Please sign in again.' : 'Request failed. Please try again.'));
  return data;
}
