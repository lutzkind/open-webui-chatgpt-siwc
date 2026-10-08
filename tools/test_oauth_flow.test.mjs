import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import test from 'node:test';
import {createLocalJWKSet, exportJWK, generateKeyPair, SignJWT} from 'jose';
import {
  AUTH_BASE,
  REQUIRED_SCOPES,
  SCOPES,
  buildAuthorizationUrl,
  createPkce,
  parseAuthorizationCallback,
  randomState,
  validateGrantedScopes,
  verifyIdToken
} from './oauth_flow.mjs';

const HOST_ID = 'urn:uuid:00000000-0000-4000-8000-000000000001';
const CLIENT_ID = 'synthetic-issued-client';
const NONCE = 'synthetic-nonce';

async function signer(issuer = AUTH_BASE, audience = CLIENT_ID, nonce = NONCE, expiration = '5m') {
  const {privateKey, publicKey} = await generateKeyPair('RS256', {modulusLength: 2048});
  const kid = 'synthetic-signing-key';
  const jwk = await exportJWK(publicKey);
  jwk.kid = kid;
  jwk.alg = 'RS256';
  jwk.use = 'sig';
  const keySet = createLocalJWKSet({keys: [jwk]});
  const token = await new SignJWT({nonce, sub: 'synthetic-subject'})
    .setProtectedHeader({alg: 'RS256', kid})
    .setIssuer(issuer)
    .setAudience(audience)
    .setIssuedAt()
    .setExpirationTime(expiration)
    .sign(privateKey);
  return {token, keySet};
}

test('PKCE S256, unpredictable OAuth state, and official authorization parameters', () => {
  const {verifier, challenge} = createPkce();
  const state = randomState();
  assert.match(verifier, /^[A-Za-z0-9_-]{86}$/);
  assert.equal(challenge, crypto.createHash('sha256').update(verifier).digest('base64url'));
  assert.match(challenge, /^[A-Za-z0-9_-]{43}$/);
  assert.match(state, /^[A-Za-z0-9_-]{43}$/);
  assert.notEqual(randomState(), state);

  const url = buildAuthorizationUrl({
    redirectUri: 'http://127.0.0.1:49152/auth/callback',
    hostId: HOST_ID,
    state,
    nonce: NONCE,
    challenge
  });
  assert.equal(url.origin, AUTH_BASE);
  assert.equal(url.searchParams.get('client_id'), 'dynamic_agent_client');
  assert.equal(url.searchParams.get('ext_agent_host_id'), HOST_ID);
  assert.equal(url.searchParams.get('redirect_uri'), 'http://127.0.0.1:49152/auth/callback');
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256');
  assert.equal(url.searchParams.get('code_challenge'), challenge);
  assert.equal(url.searchParams.get('nonce'), NONCE);
  assert.deepEqual(new Set(url.searchParams.get('scope').split(' ')), REQUIRED_SCOPES);
});

test('OAuth callback rejects wrong state, provider errors, and unissued client IDs', () => {
  const callback = new URLSearchParams({state: 'state-1', code: 'synthetic-code', client_id: CLIENT_ID});
  assert.deepEqual(parseAuthorizationCallback(callback, 'state-1'), {
    code: 'synthetic-code',
    clientId: CLIENT_ID
  });
  assert.throws(() => parseAuthorizationCallback(callback, 'state-2'), /state validation failed/);
  assert.throws(
    () => parseAuthorizationCallback(new URLSearchParams({state: 'state-1', error: 'denied'}), 'state-1'),
    /declined or failed/
  );
  assert.throws(
    () => parseAuthorizationCallback(new URLSearchParams({state: 'state-1', code: 'x', client_id: 'dynamic_agent_client'}), 'state-1'),
    /issued client ID/
  );
});

test('scope validation requires the complete SIWC grant', () => {
  assert.equal(validateGrantedScopes(SCOPES.join(' ')).size, REQUIRED_SCOPES.size);
  assert.throws(() => validateGrantedScopes('openid profile email'), /required SIWC permissions/);
});

test('ID token signature, issuer, audience, expiration, subject and nonce validate', async () => {
  const signed = await signer();
  const claims = await verifyIdToken(signed.token, CLIENT_ID, NONCE, signed.keySet);
  assert.equal(claims.sub, 'synthetic-subject');
  await assert.rejects(verifyIdToken(signed.token, 'other-client', NONCE, signed.keySet));
  await assert.rejects(verifyIdToken(signed.token, CLIENT_ID, 'other-nonce', signed.keySet));

  const badIssuer = await signer('https://issuer.example.test');
  await assert.rejects(verifyIdToken(badIssuer.token, CLIENT_ID, NONCE, badIssuer.keySet));
  const expired = await signer(AUTH_BASE, CLIENT_ID, NONCE, '0s');
  await assert.rejects(verifyIdToken(expired.token, CLIENT_ID, NONCE, expired.keySet));
});
