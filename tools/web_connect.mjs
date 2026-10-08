// Pair a local OAuth companion with the administrator's browser over HTTPS.
// Neither the pairing ticket nor OAuth credentials are put in a request URL.
import http from 'node:http';
import crypto from 'node:crypto';
import open from 'open';

const TOKEN = /^[A-Za-z0-9_-]{43}$/;
const HOST_ID = /^(?:urn:uuid:)?[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function validateConnectUrl(raw) {
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error('The Open WebUI connection address is invalid.');
  }
  const localHttp = url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if ((!localHttp && url.protocol !== 'https:') || !url.hostname || url.username || url.password ||
      url.pathname !== '/siwc/connect' || url.search || url.hash) {
    throw new Error('Use the HTTPS /siwc/connect address shown by your Open WebUI administrator.');
  }
  return url;
}

export async function prepareWebConnection(
  rawConnectUrl,
  {openBrowser = open, fetchImpl = fetch, timeoutMs = 600000} = {}
) {
  const connectUrl = validateConnectUrl(rawConnectUrl);
  const verifier = crypto.randomBytes(32).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('hex');
  const state = crypto.randomBytes(32).toString('base64url');
  let resolveTicket;
  let rejectTicket;
  const ticketPromise = new Promise((resolve, reject) => {
    resolveTicket = resolve;
    rejectTicket = reject;
  });
  let used = false;

  const listener = http.createServer((req, res) => {
    const expectedHost = `127.0.0.1:${listener.address()?.port}`;
    if (req.headers.host !== expectedHost) {
      res.writeHead(400).end('Invalid local connection.');
      return;
    }

    if (req.method === 'GET' && req.url === '/connect') {
      const scriptNonce = crypto.randomBytes(18).toString('base64url');
      const html = `<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Connect ChatGPT</title><p>Completing the secure connection…</p><script nonce="${scriptNonce}">(async()=>{const values=new URLSearchParams(location.hash.slice(1));history.replaceState(null,"","/connect");const body={state:values.get("state"),ticket:values.get("ticket"),status:values.get("status")};try{const r=await fetch("/handoff",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),cache:"no-store"});if(!r.ok)throw Error();document.querySelector("p").textContent="Connection received. Return to the companion."}catch{document.querySelector("p").textContent="Connection expired. Return to Open WebUI and try again."}})()</script>`;
      res.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': `default-src 'none'; script-src 'nonce-${scriptNonce}'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
      }).end(html);
      return;
    }

    if (req.method !== 'POST' || req.url !== '/handoff' ||
        req.headers.origin !== `http://127.0.0.1:${listener.address()?.port}` ||
        !String(req.headers['content-type'] || '').toLowerCase().startsWith('application/json') || used) {
      res.writeHead(400).end('Invalid local connection.');
      return;
    }
    const chunks = [];
    let size = 0;
    req.on('data', chunk => {
      size += chunk.length;
      if (size > 1024) req.destroy();
      else chunks.push(chunk);
    });
    req.on('end', () => {
      let body;
      try {
        body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      } catch {
        res.writeHead(400).end('Invalid local connection.');
        return;
      }
      const validState = body.state === state;
      const alreadyConnected = body.status === 'connected' && !body.ticket;
      const validTicket = TOKEN.test(body.ticket || '');
      if (!validState || (!alreadyConnected && !validTicket)) {
        res.writeHead(400).end('Invalid local connection.');
        return;
      }
      used = true;
      res.writeHead(200, {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer'
      }).end('{"ok":true}');
      resolveTicket(alreadyConnected ? 'already-connected' : body.ticket);
    });
  });

  listener.on('error', () => rejectTicket(new Error('Could not prepare the local connection.')));
  await new Promise((resolve, reject) => {
    listener.once('error', reject);
    listener.listen(0, '127.0.0.1', resolve);
  });

  const timeout = setTimeout(
    () => rejectTicket(new Error('Connection expired. Return to Open WebUI and try again.')),
    timeoutMs
  );
  const browserUrl = new URL(connectUrl);
  browserUrl.hash = new URLSearchParams({
    challenge,
    port: String(listener.address().port),
    state
  }).toString();

  let ticket;
  try {
    await openBrowser(browserUrl.toString());
    ticket = await ticketPromise;
  } finally {
    clearTimeout(timeout);
    listener.close();
  }

  const verifyUrl = new URL('/siwc/connect?verify=1', connectUrl.origin);
  if (ticket === 'already-connected') {
    await openBrowser(verifyUrl.toString());
    return {alreadyConnected: true};
  }

  const pairGrant = `Pair ${ticket}.${verifier}`;
  async function send(action, body) {
    let response;
    try {
      response = await fetchImpl(new URL(`/siwc/handoff/${action}`, connectUrl.origin), {
        method: 'POST',
        redirect: 'error',
        signal: AbortSignal.timeout(45000),
        headers: {Authorization: pairGrant, 'Content-Type': 'application/json'},
        body: JSON.stringify(body || {}),
        cache: 'no-store'
      });
    } catch {
      throw new Error('The secure connection could not reach Open WebUI.');
    }
    if (!response.ok) {
      throw new Error('Connection expired or could not be verified. Return to Open WebUI.');
    }
    return response.json();
  }

  const prepared = await send('prepare');
  if (!HOST_ID.test(prepared.host_id || '') || !TOKEN.test(prepared.nonce || '')) {
    throw new Error('Open WebUI returned an invalid connection request.');
  }
  return {
    hostId: prepared.host_id,
    nonce: prepared.nonce,
    async transfer(record) {
      const result = await send('complete', record);
      if (result.configured !== true) {
        throw new Error('Open WebUI did not confirm encrypted credential storage.');
      }
      await openBrowser(verifyUrl.toString());
    }
  };
}
