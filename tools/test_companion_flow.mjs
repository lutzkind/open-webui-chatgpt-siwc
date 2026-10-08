// Synthetic local browser-to-companion pairing test; no OAuth or account data.
import assert from 'node:assert/strict';
import http from 'node:http';
import {once} from 'node:events';
import {prepareWebConnection} from './web_connect.mjs';

const syntheticRecord = {
  id_token: 'synthetic-id-token',
  access_token: 'synthetic-access-token',
  refresh_token: 'synthetic-refresh-token'
};
const hostId = 'urn:uuid:00000000-0000-4000-8000-000000000001';
const nonce = 'N'.repeat(43);
let expectedGrant;
let completionBody;
let browserLaunches = 0;

const adapter = http.createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {};
  if (request.url === '/siwc/handoff/prepare' && request.method === 'POST') {
    expectedGrant = request.headers.authorization;
    assert.match(expectedGrant, /^Pair [A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}$/);
    response.writeHead(200, {'content-type': 'application/json'});
    response.end(JSON.stringify({host_id: hostId, nonce}));
    return;
  }
  if (request.url === '/siwc/handoff/complete' && request.method === 'POST') {
    assert.equal(request.headers.authorization, expectedGrant);
    completionBody = body;
    response.writeHead(200, {'content-type': 'application/json'});
    response.end('{"configured":true}');
    return;
  }
  response.writeHead(404).end();
});
adapter.listen(0, '127.0.0.1');
await once(adapter, 'listening');

try {
  const port = adapter.address().port;
  const connectUrl = `http://127.0.0.1:${port}/siwc/connect`;
  const connection = await prepareWebConnection(connectUrl, {
    timeoutMs: 3000,
    async openBrowser(value) {
      browserLaunches += 1;
      const url = new URL(value);
      if (browserLaunches > 1) {
        assert.equal(url.pathname, '/siwc/connect');
        assert.equal(url.searchParams.get('verify'), '1');
        return;
      }
      assert.equal(url.search, '');
      const fragment = new URLSearchParams(url.hash.slice(1));
      assert.match(fragment.get('challenge'), /^[a-f0-9]{64}$/);
      const callbackOrigin = `http://127.0.0.1:${fragment.get('port')}`;
      const page = await fetch(`${callbackOrigin}/connect`, {redirect: 'error'});
      assert.equal(page.status, 200);
      const callback = await fetch(`${callbackOrigin}/handoff`, {
        method: 'POST',
        headers: {
          Origin: callbackOrigin,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          state: fragment.get('state'),
          ticket: 'T'.repeat(43)
        })
      });
      assert.equal(callback.status, 200);
    }
  });

  assert.equal(connection.hostId, hostId);
  assert.equal(connection.nonce, nonce);
  await connection.transfer(syntheticRecord);
  assert.deepEqual(completionBody, syntheticRecord);
  assert.equal(browserLaunches, 2);
  console.log('Companion pairing and protected handoff passed with synthetic credentials.');
} finally {
  adapter.close();
  await once(adapter, 'close');
}
