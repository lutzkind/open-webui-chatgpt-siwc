"""Lightweight public-tree scan for credential patterns and local secret files."""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
ALLOWED_SYNTHETIC = re.compile(r"^(?:test|synthetic|dummy|replace-with|example)[-_]", re.IGNORECASE)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
    r"\s*[:=]\s*[\"']([^\"']{12,})[\"']"
)
KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{20,}|gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[a-z0-9-]{20,})\b"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
URL_CREDENTIALS = re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
LONG_VALUE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9])")


def entropy(value: str) -> float:
    counts = {char: value.count(char) for char in set(value)}
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".zip", ".enc", ".pyc"}:
            continue
        yield path


def main() -> int:
    failures: list[str] = []
    for path in files():
        relative = path.relative_to(ROOT)
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            failures.append(f"{relative}: private environment file")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if PRIVATE_KEY.search(content):
            failures.append(f"{relative}: private key block")
        if URL_CREDENTIALS.search(content):
            failures.append(f"{relative}: URL contains user credentials")
        if KNOWN_TOKEN.search(content):
            failures.append(f"{relative}: credential token pattern")
        for match in SECRET_ASSIGNMENT.finditer(content):
            value = match.group(1).strip()
            if not ALLOWED_SYNTHETIC.match(value) and "placeholder" not in value.lower():
                failures.append(f"{relative}: credential-like assignment")
                break
        if path.name != "package-lock.json":
            for match in LONG_VALUE.finditer(content):
                value = match.group(0).rsplit("=", 1)[-1]
                if entropy(value) >= 4.4 and not ALLOWED_SYNTHETIC.match(value):
                    failures.append(f"{relative}: high-entropy literal")
                    break
    if failures:
        print("Public-tree security scan failed:")
        print("\n".join(sorted(set(failures))))
        return 1
    print("Public-tree security scan passed: no private env files, credential patterns, or high-entropy literals found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
