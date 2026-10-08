# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities through GitHub's **Report a vulnerability** action on this repository. This creates a private security advisory visible to maintainers. Do not open a public issue for an unpatched vulnerability. Include affected versions, a concise reproduction, and impact; do not include real tokens or personal account data.

## Security design

- **Credential storage:** SIWC credentials are encrypted with Fernet and stored in `DATA_DIR/credentials.enc`. Writes are atomic and set file mode `0600`. The key is provided independently through `SIWC_CREDENTIAL_KEY`; protect it separately and back it up only with controls appropriate for a secret.
- **Plaintext handling:** OAuth tokens exist in process memory during exchange and transfer, and in protected HTTPS request bodies between the companion and adapter. The companion does not write a plaintext credential file, and the adapter does not retain a plaintext credential file. Do not enable request-body or authorization-header logging in the reverse proxy, application server, or tracing system.
- **OAuth protections:** The companion uses PKCE with `S256`, a cryptographically random OAuth `state`, and a nonce supplied by the paired adapter. It validates the ID token signature using OpenAI's JWKS and checks issuer, audience, expiration, subject, and nonce before handoff. The adapter repeats ID-token validation before encrypting the record.
- **Callback and pairing:** The OAuth redirect URI is a loopback callback on the computer running the companion. For a remote Open WebUI VM, a short-lived pairing challenge connects the browser to the local companion; the companion transfers the result to the adapter over HTTPS. Pairing grants are bound to a verifier and expire. Access, refresh, and ID tokens and pairing grants are never placed in request URLs. The authorization-code grant returns a short-lived code on the loopback callback query as required by OAuth; the companion consumes it immediately. The browser uses a URL fragment for the local pairing handoff and removes it before sending the handoff body.
- **Browser storage:** The connection page does not store access tokens, refresh tokens, or adapter API keys in `localStorage` or browser storage.
- **Transport:** Use HTTPS for every browser-facing Open WebUI origin and adapter handoff. The only HTTP exceptions are loopback development/callback addresses. Do not expose the adapter's `/v1` API to an untrusted network.
- **Adapter authentication:** `/v1/*` requires the configured bearer API key. Administrative `/siwc/*` endpoints require a trusted identity header and an allowlisted administrator identity. A reverse proxy must remove any incoming client-supplied copy of that header and set it only after authenticating the Open WebUI user. Enforce Open WebUI sign-in and administrator access at the proxy as well.
- **Network placement:** Run the adapter on a private network shared with Open WebUI. Publish only the authenticated connection page through the existing reverse proxy. Keep the API key and Fernet key in a deployment secret manager; do not commit them or put them in logs.
- **Refresh tokens:** The adapter refreshes tokens when needed and atomically persists rotated credentials in encrypted form. Failed refreshes do not include provider response bodies or token values in API errors.

## Scope

CI and automated tests use synthetic identities and credentials. Live account authorization is a manual acceptance test and is not performed in CI. Please do not submit production credentials, real account identifiers, private hostnames, or deployment evidence in issues or pull requests.
