# Contributing

Thanks for helping improve Open WebUI ChatGPT SIWC.

## Before opening a pull request

- Open an issue for substantial changes so the scope can be discussed.
- Keep deployment-specific configuration out of the repository. Do not submit environment files, secrets, account identifiers, private hostnames, production captures, or deployment logs.
- Keep the adapter focused on the official SIWC and Responses API integration. Do not add a second gateway, database, scheduler, agent runtime, or unrelated service.
- Use synthetic values in tests. Never use a real OAuth session or ChatGPT account in automated tests.
- Add or update focused tests for behavior changes, especially OAuth validation, pairing, encryption, refresh, streaming, model discovery, reasoning, and function tools.

## Local checks

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m compileall -q app tests tools
ruff check app tests tools
python -m pytest -q
npm --prefix tools ci
node --check tools/authorize.mjs
node --check tools/web_connect.mjs
node --test tools/test_oauth_flow.test.mjs
node tools/test_companion_flow.mjs
docker build -t open-webui-chatgpt-siwc:local .
git diff --check
```

The companion app is compiled and checked on macOS by CI. Live SIWC browser consent is a manual acceptance step and must not be represented by a synthetic test.

## Pull requests

Use the pull request template. Keep changes reviewable, describe compatibility and security effects, and link any related issue. Maintainers may request changes before merging.
