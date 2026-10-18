# Open WebUI ChatGPT SIWC

Use an eligible ChatGPT subscription with self-hosted Open WebUI through OpenAI's official Sign in with ChatGPT (SIWC) flow and Responses API.

This project brings an eligible ChatGPT subscription into a self-hosted Open WebUI deployment through the official ChatGPT Plus or Pro authorization flow. Plan eligibility and availability are controlled by OpenAI. It is an independent, community-maintained adapter and is not affiliated with, endorsed by, or sponsored by OpenAI or Open WebUI. See OpenAI's [SIWC quickstart](https://developers.openai.com/siwc/quickstart).

## Architecture

```text
Open WebUI
    |
    | OpenAI-compatible Responses API
    v
SIWC Adapter
    |
    | official Sign in with ChatGPT
    v
OpenAI
```

The adapter holds encrypted SIWC credentials, discovers the models available to the connected account, and forwards supported Responses API requests. It does not implement an agent framework, a model gateway, a database, or a vector store.

Two upstream transports share that interface:

- `RESPONSES_TRANSPORT=http` (default) relays one independent HTTP/SSE Responses request per inbound request.
- `RESPONSES_TRANSPORT=websocket` keeps one authenticated Responses WebSocket per account and continues a conversation with `previous_response_id` plus only the new input items, falling back to the HTTP path whenever the socket cannot serve the request.

## Two separate sign-ins

- **Sign into Open WebUI** with the identity provider configured by your Open WebUI administrator. This gives you access to your self-hosted Open WebUI account. An administrator identity may be required to connect or inspect the shared provider.
- **Use a ChatGPT subscription for inference** by connecting an eligible ChatGPT account through OpenAI's official SIWC authorization page. This is a separate account and consent flow.

An OpenAI API key is not used to authorize ChatGPT subscription access. This project is not an API-key bypass; it uses the official SIWC OAuth flow and its eligibility rules.

## Eligibility and requirements

- A ChatGPT plan and account that OpenAI currently makes eligible for SIWC. Availability, plan eligibility, usage limits, and supported regions are controlled by OpenAI and can change. Check the [SIWC quickstart](https://developers.openai.com/siwc/quickstart) before deploying.
- A current self-hosted Open WebUI instance with an OpenAI-compatible provider that supports the Responses API.
- Docker Compose and a private Docker network shared by Open WebUI and the adapter.
- A public HTTPS origin for Open WebUI. The companion's OAuth callback runs on the computer with the browser, including when Open WebUI is hosted on a remote VM.
- A Mac running macOS 13.5 or later to use the published local connection companion.

The companion is currently distributed as an unsigned, non-notarized community build unless a release explicitly says otherwise. Review the release checksum and source before opening it. macOS may require you to approve an unsigned application.

## Docker quick start

1. Create a private Docker network shared with your Open WebUI service. Find its existing network name with your deployment tooling.
2. Copy the example settings and replace every placeholder:

   ```sh
   cp .env.example .env
   ```

3. Generate unique values for the adapter API key, Fernet encryption key, and stable host UUID. Keep the API key and encryption key in your deployment secret manager. Do not change the host UUID after authorizing the adapter.

   ```sh
   python -c 'import secrets; print(secrets.token_urlsafe(48))'
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   python -c 'import uuid; print(uuid.uuid4())'
   ```

   Set `SIWC_HOST_ID` to a newly generated UUID, optionally prefixed with `urn:uuid:`. Set `OPEN_WEBUI_PUBLIC_ORIGIN`, `CONNECT_PUBLIC_URL`, `CONNECT_ADMIN_EMAILS`, and `OPEN_WEBUI_DOCKER_NETWORK` for your installation.

4. Configure your existing authenticated reverse proxy to route `/siwc/*` to the adapter. Protect the connection, status, config, verification, download, and pairing page routes with the same Open WebUI sign-in and administrator policy you use for the shared provider. Strip any user-supplied identity header before authentication and set `X-Auth-Request-Email` only from the trusted authenticated identity. Keep `/v1/*` on the private service network.
5. Start the service:

   ```sh
   docker compose up -d --build
   docker compose logs --tail=50 siwc-adapter
   ```

6. Verify `https://<your-open-webui-origin>/siwc/connect` is reachable after signing into Open WebUI as an allowed administrator. The adapter API should not be exposed directly to the public internet.

The sample Compose file creates a named persistent volume. For an existing deployment, mount its established persistent data directory at `DATA_DIR`; back up the encrypted credential file and encryption key together, and protect the key separately.

For the current SIWC flow, keep `OPENAI_API_BASE_URL=https://api.openai.com/v1` and `OPENAI_AUTH_BASE_URL=https://auth.openai.com`. OpenAI's SIWC resource contract currently does not support a custom API proxy.

## Configure Open WebUI

In Open WebUI's existing OpenAI-compatible connection settings, add one provider:

- **Base URL:** the adapter's private service address followed by `/v1`, for example `http://siwc-adapter:8080/v1` on the shared Docker network.
- **API key:** the same value configured as `ADAPTER_API_KEY`.
- **API type:** Responses.
- **Model IDs:** leave dynamic model discovery enabled; do not enter a hard-coded model list.
- **Headers (recommended for the WebSocket transport):** add
  `{"X-OpenWebUI-Chat-Id": "{{CHAT_ID}}"}` so every request of one conversation
  carries the same Open WebUI chat identifier. With the header present, the
  adapter correlates tool-loop rounds by that explicit conversation ID; without
  it, it falls back to proving that each request is a strict extension of a
  recent one. The raw identifier is never logged or stored — only a truncated
  digest is used as the in-memory lane key.

Use the provider once. Model discovery returns only models the connected ChatGPT account is allowed to list. Open WebUI labels can vary by release; keep the provider's API type set to Responses. The adapter accepts `X-OpenWebUI-Chat-Id` by default; set `WS_CONVERSATION_HEADER` to look for a different header name.


The connection page's **Connect ChatGPT** action downloads or opens the macOS companion. Confirm the Open WebUI address shown by the companion, then sign in at the official OpenAI authorization page. After consent, the companion sends the credentials over HTTPS to the paired adapter. Return to Open WebUI and select a discovered model.

### Open WebUI hosted on a VM

The OAuth callback uses `http://127.0.0.1:<port>/auth/callback` on the computer that opened the browser. When Open WebUI runs on a remote VM, the local companion handles that callback on your Mac and securely hands the result to the paired VM adapter. Do not forward the OAuth callback to the VM or paste tokens into a browser address.

## Responses API compatibility

- The adapter queries OpenAI for the connected account's model catalog and maps visible entries to OpenAI-compatible model records. Model availability is account-specific and can change.
- The adapter uses upstream Responses SSE with `store: false`; streaming callers receive SSE, while non-streaming callers receive the completed Responses object as JSON. With `RESPONSES_TRANSPORT=websocket` the same contract is preserved: the adapter translates the socket's events into the same Responses SSE stream and JSON response objects.
- The adapter maps Open WebUI's legacy `reasoning_effort` field to Responses API `reasoning.effort` and preserves the native reasoning object where supported.
- Native Open WebUI function tools are passed through in the Responses request. Tool execution and approvals remain controlled by Open WebUI.
- Open WebUI system-role input items are converted to the Responses-compatible developer role. Fields outside the current SIWC plan surface are removed.
- ChatGPT subscription access is not the same product surface as the OpenAI API. Some API parameters and features are not available through SIWC. See OpenAI's [models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) and [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) documentation.

### Open WebUI reasoning control

Open WebUI v0.12.0 and newer provide the canonical reasoning selector as native **Model Controls**: a per-model control (for example a "Thinking" menu) whose options set the model's `reasoning_effort`. Configure one option per live-supported effort level on each SIWC model; users pick a level from the controls menu next to the model selector, and the choice is remembered per model. The adapter continues to translate Open WebUI's flat `reasoning_effort` field to the Responses API `reasoning.effort` object, so this normalization is required and must not be removed.

The earlier stock Open WebUI Filter Function at [`integrations/open-webui/functions/siwc_think.py`](integrations/open-webui/functions/siwc_think.py) is **retired** and superseded by the native Model Controls. It remains in the repository only for older Open WebUI versions (pre-v0.12.0) that do not have Model Controls; do not run it alongside native Model Controls. See the integration [notes](integrations/open-webui/README.md).

## Prompt caching, tool loops and plan usage

SIWC plan usage has no stored conversation state: every request resends the full `input`, and each round of a client-side tool loop (model → tools → model) is a separate `/v1/responses` call billed at full size. Current OpenAI caching semantics (GPT-5.6 and later) apply: prompt caching is automatic, cache writes and reads are reported per request, and reuse requires a matching prefix at an eligible boundary.

- **Explicit cache breakpoints (`CACHE_BREAKPOINTS`).** The adapter marks the stable head (the leading developer/system block) and every tool result with `prompt_cache_breakpoint`: `{"mode": "explicit"}`. The marks are deterministic — every round re-applies the same marks at the same prefix positions — so later tool rounds can reuse the earlier cached prefix instead of re-billing it as fresh input. This follows OpenAI's documented multi-turn agent pattern of "a breakpoint after each tool result". No model-visible content changes. Disable if a resource rejects the field.
  - **Live status (2026-10-18): not activatable on the current SIWC resource.** Direct production requests show the resource rejects both `prompt_cache_breakpoint` (on developer-message and function-call-output content parts) and the request-level `prompt_cache_options` with HTTP 400 `invalid_parameter` (`error_param` naming the field), independently of plan-usage quota. The option therefore remains off by default; re-test against the resource before enabling, and keep it off if the fields are still rejected.
- **Conversation cache routing (`CACHE_ROUTING_KEY`).** When a caller does not send `prompt_cache_key`, the adapter can derive one from the conversation anchor (model plus the first user-role input item). This is **off by default**: on GPT-5.6 and later OpenAI routes caches automatically, and keys are only used for separate cache accounting; on models before GPT-5.6 a stable key is recommended, so enable it there.
- **Append-only request construction.** Request serialization is deterministic (stable tool ordering), so consecutive tool rounds share a byte-identical prefix and differ only by appended items.

Round-trip count itself is controlled by the model and Open WebUI: each assistant tool-call batch costs one model turn, parallel calls inside one batch are executed together, and the adapter performs exactly one upstream request per inbound request (no retries, no replay). High reasoning effort remains available through the Model Controls; nothing in the adapter lowers it.

## WebSocket transport (`RESPONSES_TRANSPORT`)

`RESPONSES_TRANSPORT=websocket` keeps one authenticated Responses WebSocket
(`wss://api.openai.com/v1/responses`) per SIWC account and continues a
conversation on that socket with `previous_response_id` plus only the items the
caller appended. Open WebUI keeps its existing Responses HTTP interface; the
browser never opens an OpenAI socket.

How a conversation is continued:

- The adapter maintains one in-memory *lane* per conversation plus connection
  generation. A lane holds only derived state: the latest upstream response ID,
  per-item digests of the last request, output-item comparison signatures, the
  connection generation and a last-used timestamp. No prompt text, tool
  arguments, tool results or credentials are stored, and there is no durable
  adapter conversation store — Open WebUI remains the source of conversation
  history.
- The first request of a chain sends the full sanitized context. Every later
  request is continued only when the adapter can prove it is a strict,
  byte-stable extension of the lane's previous request: same model, same
  instructions, same tool definitions, same connection generation, and every
  earlier input item identical. Replayed response output items (for example the
  `function_call` items Open WebUI stores on the assistant message) are removed
  from the delta because they already live in the upstream conversation state.
- Anything else — edited or reordered history, a removed item, changed
  instructions or tools, a new conversation, an idle lane, a lost connection or
  a rejected `previous_response_id` — falls back to a full-context request on
  the same socket. The adapter never guesses.
- Connections rotate before the documented 60-minute server limit; a closed,
  expired or token-rotated socket is replaced, the next request per conversation
  resends full context from Open WebUI's history, and continuation resumes from
  the new chain.

SIWC-specific behavior observed in live acceptance (2026-10-18) and reflected in
the implementation:

- The plan route accepts the WebSocket and same-socket `previous_response_id`
  continuation, which HTTP does not.
- The plan route rejects the WebSocket-only `stream_id` parameter
  (`invalid_request_error`, "Unsupported parameter: stream_id"), so a connection
  carries a single implicit lane. The adapter serializes requests per connection
  and uses the bounded HTTP fallback for a request that cannot get the lane in
  time.
- On a continuation the route reports the retained prefix as cached input tokens
  (`usage.input_tokens_details.cached_tokens`), which the HTTP route never did;
  the bytes actually retransmitted drop from the full context to the appended
  items only.

Fallback behavior is deliberate and bounded: if the socket cannot be established
(or is disabled, unauthorized, cooling down after failures, or busy), the
request is served by the existing HTTP/SSE path and the adapter records the
reason in telemetry. Model, quota and policy errors are never silently switched
between transports; they are returned to the caller unchanged.

Configuration: `RESPONSES_TRANSPORT=http|websocket`, `WS_CONVERSATION_HEADER`,
`WS_CONNECTION_MAX_AGE_SECONDS`, `WS_EVENT_TIMEOUT_SECONDS`,
`WS_OPEN_TIMEOUT_SECONDS`, `WS_LOCK_TIMEOUT_SECONDS`, `WS_LANE_IDLE_TTL_SECONDS`.
See [.env.example](.env.example). `store: false` is preserved on every request
over both transports.


## Request telemetry

The adapter writes one structured, content-free JSON log line per `/v1/responses` request to stdout (`docker compose logs siwc-adapter`, logger `siwc.telemetry`, disable with `REQUEST_TELEMETRY=false`). Each line includes the locally generated request ID, model, reasoning effort, input item count and byte sizes, tool-definition count/bytes, per-item structural fingerprints (length plus truncated SHA-256), the stable-prefix hash, byte accounting by category (messages, function calls, tool results, reasoning), a comparison with the previous/best-matching recent request (common prefix items/bytes, first difference position, strict-extension flag), upstream status, duration, response ID, token usage including cached and cache-write counters, and non-secret rate-limit headers.

Telemetry never logs prompts, assistant content, tool arguments or results, credentials, or headers that can carry secrets. The adapter process keeps at most the last 32 request fingerprints in memory for prefix comparison; the buffer resets on restart.

Every line also carries a `transport` record so HTTP and WebSocket behavior can
be compared numerically: `kind` (http/websocket), `lane` (opaque conversation
digest), `generation`, `mode` (`full` or `continuation`),
`previous_response_used`, `previous_response_id`, `sent_items`/`sent_bytes` and
`sent_tool_bytes` (what was actually transmitted upstream),
`received_items`/`received_bytes`/`received_tool_bytes` (what the caller sent),
`stripped_items`, `wire_reduction`, `reconnects`, `fallback_reason`,
`response_id`, `terminal_event` and the provider usage counters. `fallback`
marks requests served by the HTTP path because the socket was unavailable.


## Credential storage and security

The adapter encrypts the SIWC credential record with Fernet and atomically stores it in `DATA_DIR/credentials.enc` with restrictive file permissions. The encryption key is supplied separately through `SIWC_CREDENTIAL_KEY`. Refresh-token rotation is persisted back to the encrypted file. The local companion does not retain a plaintext credential file.

Use HTTPS for the public Open WebUI origin and private networking for the adapter API. Protect all `/siwc/*` administrative routes behind Open WebUI authentication and enforce the administrator allowlist. The trusted email header is safe only when the reverse proxy removes client-provided copies and sets it after authentication. Use a long, unique adapter API key and store both keys in a secret manager. See [SECURITY.md](SECURITY.md) for the security model and vulnerability reporting.

## Upgrading

Read the release notes before upgrading. Back up the encrypted credential file and encryption key as a matched pair. Keep `SIWC_HOST_ID`, the credential key, adapter API key, data directory, and provider URL stable. Build and deploy a reviewed release, verify `/health` and `/v1/models`, and keep the prior image available for rollback. A code upgrade should not require SIWC reauthorization when the existing encrypted record and configuration remain valid.

## Troubleshooting

- **Connection page says sign-in is required:** sign into Open WebUI and verify that the authenticated proxy sets the trusted identity header for the adapter.
- **Administrator is not allowed:** check the private `CONNECT_ADMIN_EMAILS` allowlist and ensure the proxy supplies the same authenticated email identity.
- **The companion cannot reach the adapter:** confirm the browser-facing connection URL is HTTPS and reachable from the Mac. Do not use an internal container hostname as `CONNECT_PUBLIC_URL`.
- **OAuth callback does not complete:** allow the local companion to bind to loopback, keep the callback on the same Mac that opened the browser, and restart the connection flow to create a fresh state and PKCE challenge.
- **No models appear:** confirm the account is SIWC-eligible, the adapter can reach OpenAI, and the Open WebUI provider uses the adapter's private `/v1` address and API key.
- **Credential validation fails after moving hosts:** keep the configured host UUID stable. SIWC credentials are bound to the host ID used during authorization.
- **Refresh fails:** check system time and outbound access to OpenAI's authentication and API endpoints. Reauthorization may be required if the refresh grant has expired or been revoked.

## Known limitations

- SIWC plan eligibility, availability, quotas, and model visibility are determined by OpenAI and may change.
- This adapter currently targets the official SIWC Responses API flow. It does not provide the full OpenAI API surface.
- Hosted tools such as file search, Code Interpreter, computer use, and hosted MCP are outside this adapter's supported flow. Background responses, stored conversation state, and unsupported Responses parameters are not provided. Native Open WebUI functions can be sent to the model; Open WebUI performs the tool execution.
- The current SIWC preview does not cover audio/video inputs or the Files and transcription APIs. Check OpenAI's [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) for the current supported surface.
- The published companion is macOS-only. The OAuth callback requires the computer that opened the browser to run the companion.
- SIWC uses account authorization; it does not create or manage Open WebUI user accounts.

## Development

Requirements: Python 3.12+, Node.js 24+, and Docker. Install and run the synthetic test suite:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m compileall -q app integrations tests tools
ruff check app integrations tests tools --per-file-ignores "integrations/open-webui/functions/siwc_think.py:I001"
python -m pytest -q
npm --prefix tools ci
node --check tools/authorize.mjs
node --check tools/web_connect.mjs
node --test tools/test_oauth_flow.test.mjs
node tools/test_companion_flow.mjs
```

WebSocket transport tests run against an in-process scripted Responses server
(`tests/ws_fakes.py`), so the whole suite stays offline and deterministic.

Build the adapter image with `docker build -t open-webui-chatgpt-siwc:local .`. The macOS companion is built by the release workflow on macOS; its source and build steps are in `companion/` and `tools/`. Tests use synthetic credentials only. Live SIWC consent is a manual acceptance step and is not simulated by CI; `tools/ws_acceptance.py` performs the bounded three-phase live acceptance (trivial response, same-conversation continuation, incremental tool result) against a running adapter.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), follow the pull request template, and include tests for changes to OAuth, credential handling, request compatibility, or browser pairing. Never submit credentials, production configuration, private deployment files, account identifiers, or real authentication captures.

## License

Distributed under the MIT License. See [LICENSE](LICENSE).

## Disclaimer

This is independent community software. OpenAI, ChatGPT, and Open WebUI are names of their respective owners. This project is not affiliated with or endorsed by OpenAI or Open WebUI. Use is subject to the applicable provider terms, account eligibility, and local law.
