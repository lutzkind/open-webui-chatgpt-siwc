// Small, independently testable helpers for OpenAI's official SIWC flow.
import crypto from 'node:crypto';
import {createRemoteJWKSet, jwtVerify} from 'jose';

export const AUTH_BASE = 'https://auth.openai.com';
export const RESOURCE = 'https://api.openai.com/v1';
export const SCOPES = [
  'openid',
  'profile',
  'email',
  'offline_access',
  'resource.invoke',
  'chatgpt.tokens.use.direct'
];
export const REQUIRED_SCOPES = new Set(SCOPES);

export function randomState() {
  return crypto.randomBytes(32).toString('base64url');
}

export function createPkce() {
  const verifier = crypto.randomBytes(64).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  return {verifier, challenge};
}

export function buildAuthorizationUrl({redirectUri, hostId, state, nonce, challenge}) {
  const url = new URL(`${AUTH_BASE}/api/accounts/authorize`);
  url.search = new URLSearchParams({
    client_id: 'dynamic_agent_client',
    agent_name_hint: 'Open WebUI ChatGPT SIWC',
    ext_agent_host_id: hostId,
    response_type: 'code',
    redirect_uri: redirectUri,
    scope: SCOPES.join(' '),
    resource: RESOURCE,
    state,
    nonce,
    code_challenge_method: 'S256',
    code_challenge: challenge
  }).toString();
  return url;
}

export function parseAuthorizationCallback(params, expectedState) {
  if (params.get('state') !== expectedState) throw new Error('OAuth state validation failed.');
  if (params.has('error')) throw new Error('ChatGPT authorization was declined or failed.');
  const code = params.get('code');
  const clientId = params.get('client_id');
  if (!code || !clientId || clientId === 'dynamic_agent_client') {
    throw new Error('Authorization did not return an issued client ID and code.');
  }
  return {code, clientId};
}

export function validateGrantedScopes(value) {
  const scopes = new Set(String(value || '').split(/\s+/).filter(Boolean));
  if ([...REQUIRED_SCOPES].some(scope => !scopes.has(scope))) {
    throw new Error('Authorization did not grant the required SIWC permissions.');
  }
  return scopes;
}

export async function verifyIdToken(idToken, clientId, nonce, jwks) {
  const keySet = jwks || createRemoteJWKSet(new URL(`${AUTH_BASE}/.well-known/jwks.json`));
  const {payload} = await jwtVerify(idToken, keySet, {
    issuer: AUTH_BASE,
    audience: clientId
  });
  if (payload.nonce !== nonce || typeof payload.sub !== 'string' || !payload.sub ||
      typeof payload.exp !== 'number' || payload.exp <= Math.floor(Date.now() / 1000)) {
    throw new Error('OpenAI ID token validation failed.');
  }
  return payload;
}
