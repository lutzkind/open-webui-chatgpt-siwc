'use strict';

const state = document.querySelector('#state');
const message = document.querySelector('#message');
const connect = document.querySelector('#connect');
const params = new URLSearchParams(location.hash.slice(1));
const shouldVerify = new URLSearchParams(location.search).get('verify') === '1';
let verificationStarted = false;
const companion = {
  challenge: params.get('challenge'),
  port: params.get('port'),
  state: params.get('state')
};
history.replaceState(null, '', '/siwc/connect');

const companionReady = /^[a-f0-9]{64}$/.test(companion.challenge || '') &&
  /^[A-Za-z0-9_-]{43}$/.test(companion.state || '') && /^\d{1,5}$/.test(companion.port || '') &&
  Number(companion.port) >= 1024 && Number(companion.port) <= 65535;

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      headers: {...(options.headers || {})}
    });
  } catch {
    throw new Error('The connection could not reach the SIWC adapter.');
  }
  if (!response.ok) {
    if (response.status === 401) throw new Error('Sign into Open WebUI with an allowed administrator account, then return here.');
    if (response.status === 403) throw new Error('This Open WebUI account is not allowed to connect the shared provider.');
    if (response.status === 409) throw new Error('ChatGPT is already connected. Existing credentials were preserved.');
    throw new Error(`The connection request failed (HTTP ${response.status}).`);
  }
  return response.json();
}

function localCallback(values) {
  const url = new URL(`http://127.0.0.1:${companion.port}/connect`);
  url.hash = new URLSearchParams(values).toString();
  location.href = url.toString();
}

async function verify() {
  const checks = document.querySelector('#checks');
  document.querySelector('#verification').hidden = false;
  checks.replaceChildren();
  const item = document.createElement('li');
  try {
    const result = await api('/siwc/verify', {method: 'POST'});
    item.textContent = result.models.length
      ? `Live subscription model discovery succeeded (${result.models.length} models).`
      : 'No subscription models are currently available to this ChatGPT account.';
  } catch (error) {
    item.textContent = error.message;
  }
  checks.append(item);
}

async function refresh() {
  try {
    const statusResult = await api('/siwc/status');
    state.textContent = statusResult.connected ? 'Connected' : statusResult.configured ? 'Connection needs attention' : 'Not connected';
    connect.hidden = statusResult.configured;
    document.querySelector('#install').hidden = statusResult.configured || companionReady;
    document.querySelector('#download').hidden = !statusResult.companion_available;
    document.querySelector('#distribution').hidden = statusResult.companion_notarized;
    document.querySelector('#back').hidden = !statusResult.connected;

    if (statusResult.connected) {
      if (companionReady) {
        localCallback({status: 'connected', state: companion.state});
        return;
      }
      message.textContent = 'Credentials are stored encrypted by the adapter. Select a discovered model in Open WebUI to start a chat.';
      if (shouldVerify && !verificationStarted) {
        verificationStarted = true;
        await verify();
      }
      return;
    }

    message.textContent = companionReady
      ? 'Continue to pair this browser with the local macOS companion.'
      : 'Connect ChatGPT to this Open WebUI instance. The OAuth callback runs on your Mac.';
  } catch (error) {
    state.textContent = 'Sign-in required';
    message.textContent = error.message;
  }
}

connect.addEventListener('click', async () => {
  connect.disabled = true;
  try {
    if (!companionReady) {
      const config = await api('/siwc/config');
      const launch = new URL('open-webui-chatgpt-siwc://connect');
      launch.searchParams.set('connect_url', config.connect_url);
      location.href = launch.toString();
      document.querySelector('#install').hidden = false;
      message.textContent = 'Approve the Open WebUI address in Connect ChatGPT. If the companion is not installed, download it below.';
      connect.disabled = false;
      return;
    }

    const result = await api('/siwc/pair', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({challenge: companion.challenge})
    });
    localCallback({ticket: result.ticket, state: companion.state});
  } catch (error) {
    message.textContent = error.message;
    connect.disabled = false;
  }
});

refresh();
setInterval(() => {
  if (!document.hidden && !verificationStarted) refresh();
}, 10000);
