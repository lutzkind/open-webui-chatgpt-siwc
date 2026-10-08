#!/usr/bin/env node
// Complete SIWC OAuth on the local Mac and send credentials directly to the
// paired adapter over HTTPS. Credentials are never written to a local file.
import http from 'node:http';
import open from 'open';
import {prepareWebConnection} from './web_connect.mjs';
import {
  AUTH_BASE,
  RESOURCE,
  buildAuthorizationUrl,
  createPkce,
  parseAuthorizationCallback,
  randomState,
  validateGrantedScopes,
  verifyIdToken
} from './oauth_flow.mjs';

function argument(name) {
  const index = process.argv.indexOf(name);
  return index < 0 ? null : process.argv[index + 1] || null;
}


let server;
let approvalTimer;
let tokenResponse;
let tokens;
let credentialRecord;

try {
  const connectUrl = argument('--connect-url');
  if (!connectUrl || !process.argv.includes('--web-connect')) {
    throw new Error('Start Connect ChatGPT from your Open WebUI connection page.');
  }

  const webConnection = await prepareWebConnection(connectUrl);
  if (webConnection.alreadyConnected) {
    console.log('This adapter is already connected. Existing credentials were preserved.');
    process.exit(0);
  }

  const state = randomState();
  const nonce = webConnection.nonce;
  const {verifier, challenge} = createPkce();

  let resolveCallback;
  let rejectCallback;
  const callbackResult = new Promise((resolve, reject) => {
    resolveCallback = resolve;
    rejectCallback = reject;
  });

  server = http.createServer((request, response) => {
    let callback;
    try {
      callback = new URL(request.url || '/', 'http://127.0.0.1');
    } catch {
      response.writeHead(400).end('Invalid callback.');
      rejectCallback(new Error('Invalid OAuth callback.'));
      return;
    }
    if (request.method !== 'GET' || callback.pathname !== '/auth/callback') {
      response.writeHead(404).end('Not found');
      return;
    }
    const redirectUri = `http://127.0.0.1:${server.address().port}/auth/callback`;
    response.writeHead(200, {
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
      'Referrer-Policy': 'no-referrer',
      'X-Content-Type-Options': 'nosniff'
    }).end('ChatGPT authorization received. You can close this tab.');
    server.close();
    resolveCallback({params: callback.searchParams, redirectUri});
  });
  server.on('error', () => rejectCallback(new Error('Could not start the loopback OAuth listener.')));

  await new Promise((resolve, reject) => {
    server.once('listening', resolve);
    server.once('error', reject);
    server.listen(0, '127.0.0.1');
  });

  const redirectUri = `http://127.0.0.1:${server.address().port}/auth/callback`;
  const authorizationUrl = buildAuthorizationUrl({
    redirectUri,
    hostId: webConnection.hostId,
    state,
    nonce,
    challenge
  });

  console.log('Opening the official ChatGPT authorization page in your browser…');
  await open(authorizationUrl.toString());
  const approvalExpired = new Promise((_, reject) => {
    approvalTimer = setTimeout(
      () => reject(new Error('ChatGPT approval expired. Return to Open WebUI and try again.')),
      600000
    );
  });
  const {params, redirectUri: callbackUri} = await Promise.race([callbackResult, approvalExpired]);
  clearTimeout(approvalTimer);
  approvalTimer = undefined;

  const {code, clientId} = parseAuthorizationCallback(params, state);

  try {
    const tokenResponseHttp = await fetch(`${AUTH_BASE}/api/accounts/oauth/token`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        Accept: 'application/json'
      },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        client_id: clientId,
        code,
        code_verifier: verifier,
        redirect_uri: callbackUri,
        resource: RESOURCE
      }),
      redirect: 'error',
      cache: 'no-store',
      signal: AbortSignal.timeout(45000)
    });
    if (!tokenResponseHttp.ok) {
      throw new Error(`OAuth token exchange failed (HTTP ${tokenResponseHttp.status}).`);
    }
    try {
      tokenResponse = await tokenResponseHttp.json();
    } catch {
      throw new Error('OAuth token response could not be parsed.');
    }
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('OAuth token exchange failed')) throw error;
    throw new Error('OAuth token exchange could not be completed.');
  }

  tokens = tokenResponse;
  if (!tokens.id_token || !tokens.access_token || !tokens.refresh_token) {
    throw new Error('OAuth token response is missing a required credential.');
  }

  const payload = await verifyIdToken(tokens.id_token, clientId, nonce);

  const grantedScopes = validateGrantedScopes(tokens.scope || params.get('scope'));

  credentialRecord = {
    issuer: AUTH_BASE,
    subject: payload.sub,
    client_id: clientId,
    ext_agent_host_id: webConnection.hostId,
    resource: RESOURCE,
    id_token: tokens.id_token,
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token,
    token_type: tokens.token_type || 'Bearer',
    expires_in: tokens.expires_in || 3600,
    earliest_refresh_at: tokens.earliest_refresh_at,
    scopes: [...grantedScopes].sort(),
    saved_at: new Date().toISOString()
  };
  await webConnection.transfer(credentialRecord);
  console.log('Connected. Return to Open WebUI to finish checking the provider.');
} catch (error) {
  if (server?.listening) server.close();
  const message = error instanceof Error ? error.message : 'SIWC authorization failed.';
  console.error(message);
  process.exitCode = 1;
} finally {
  if (approvalTimer) clearTimeout(approvalTimer);
  if (server?.listening) server.close();
  tokenResponse = undefined;
  tokens = undefined;
  credentialRecord = undefined;
}
