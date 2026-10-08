"""Serve the adapter with synthetic settings and run the portable browser test."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="siwc-browser-test-") as data_dir:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        os.environ.update(
            {
                "ADAPTER_API_KEY": "synthetic-browser-test-adapter-key-000000000000000000000000",
                "SIWC_CREDENTIAL_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
                "SIWC_HOST_ID": "00000000-0000-4000-8000-000000000001",
                "CONNECT_ADMIN_EMAILS": "admin@example.test",
                "OPEN_WEBUI_PUBLIC_ORIGIN": origin,
                "CONNECT_PUBLIC_URL": origin + "/siwc/connect",
                "DATA_DIR": data_dir,
            }
        )
        config = uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not server.started:
                time.sleep(0.05)
            if not server.started:
                raise RuntimeError("Synthetic browser-test server did not start.")
            result = subprocess.run(
                ["node", str(ROOT / "tools/test_connect_page.cjs"), origin],
                cwd=ROOT,
                env={**os.environ, "PLAYWRIGHT_MODULE": str(ROOT / "tests/browser/node_modules/playwright")},
                check=False,
            )
            return result.returncode
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
