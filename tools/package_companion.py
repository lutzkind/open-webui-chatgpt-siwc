"""Build the self-contained macOS app on macOS; no setup on the user's Mac.

Builder uses locked npm dependencies, pinned Node runtime and optional Developer
ID identity / keychain notary profile. Credentials remain in the build keychain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = 'v24.15.0'


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def download(url, target):
    # TLS defaults remain enabled; redirects must also stay HTTPS.
    with urllib.request.urlopen(url, timeout=90) as response:
        if not response.url.startswith('https://'):
            raise RuntimeError('Insecure runtime redirect rejected')
        target.write_bytes(response.read())


def build(output: Path, identity: str | None, notary_profile: str | None):
    if os.uname().sysname != 'Darwin':
        raise RuntimeError('Native compilation, signing and artifact checks require macOS')
    if bool(identity) != bool(notary_profile):
        raise RuntimeError('Developer ID identity and notary keychain profile must be supplied together')
    with tempfile.TemporaryDirectory(prefix='siwc-companion-') as tmp:
        stage = Path(tmp)
        app = stage / 'Connect ChatGPT.app'
        contents = app / 'Contents'
        resources = contents / 'Resources'
        macos = contents / 'MacOS'
        tools = resources / 'tools'
        tools.mkdir(parents=True)
        macos.mkdir()
        for name in ('authorize.mjs', 'oauth_flow.mjs', 'web_connect.mjs', 'package.json', 'package-lock.json'):
            shutil.copy2(ROOT / 'tools' / name, tools / name)
        (stage / 'npm-user').write_text('')
        (stage / 'npm-global').write_text('')
        run('npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund', cwd=tools, env={**os.environ, 'NODE_OPTIONS': '', 'NPM_CONFIG_USERCONFIG': str(stage / 'npm-user'), 'NPM_CONFIG_GLOBALCONFIG': str(stage / 'npm-global'), 'NPM_CONFIG_REGISTRY': 'https://registry.npmjs.org/'})
        base = f'https://nodejs.org/dist/{NODE_VERSION}'
        sums = stage / 'SHASUMS256.txt'
        download(f'{base}/SHASUMS256.txt', sums)
        checksums = dict(line.split()[::-1] for line in sums.read_text().splitlines())
        for arch, target in (('arm64', 'arm64-apple-macosx13.5'), ('x64', 'x86_64-apple-macosx13.5')):
            asset = f'node-{NODE_VERSION}-darwin-{arch}.tar.gz'
            archive = stage / asset
            download(f'{base}/{asset}', archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != checksums[asset]:
                raise RuntimeError('Official runtime checksum mismatch')
            with tarfile.open(archive) as tar:
                tar.extractall(stage / arch, filter='data')
            runtime = resources / 'runtime' / arch / 'bin'
            runtime.mkdir(parents=True)
            shutil.copy2(stage / arch / asset.removesuffix('.tar.gz') / 'bin' / 'node', runtime / 'node')
            run('swiftc', '-O', '-target', target, '-framework', 'AppKit', str(ROOT / 'companion/ConnectChatGPT.swift'), '-o', str(stage / f'launcher-{arch}'))
        run('lipo', '-create', str(stage / 'launcher-arm64'), str(stage / 'launcher-x64'), '-output', str(macos / 'ConnectChatGPT'))
        info = {'CFBundleIdentifier': 'io.github.lutzkind.openwebui-chatgpt-siwc', 'CFBundleName': 'Connect ChatGPT', 'CFBundleDisplayName': 'Connect ChatGPT', 'CFBundleExecutable': 'ConnectChatGPT', 'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '0.1.0', 'CFBundleVersion': '1', 'LSMinimumSystemVersion': '13.5', 'CFBundleURLTypes': [{'CFBundleURLName': 'io.github.lutzkind.openwebui-chatgpt-siwc', 'CFBundleURLSchemes': ['open-webui-chatgpt-siwc']}]}
        (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
        for runtime in sorted((resources / 'runtime').rglob('node')):
            run('codesign', '--force', '--sign', identity or '-', '--options', 'runtime', '--entitlements', str(ROOT / 'companion/entitlements.plist'), *(['--timestamp'] if identity else []), str(runtime))
        (resources / 'distribution.json').write_text(json.dumps({'developer_id_signed_and_notarized': bool(identity)}))
        pins = {str(p.relative_to(resources)): hashlib.sha256(p.read_bytes()).hexdigest() for p in resources.rglob('*') if p.is_file()}
        (resources / 'bundle-manifest.json').write_text(json.dumps(pins, sort_keys=True, indent=2)+'\n')
        run('codesign', '--force', '--sign', identity or '-', '--options', 'runtime', *(['--timestamp'] if identity else []), str(app))
        run('codesign', '--verify', '--deep', '--strict', str(app))
        # Test the actual app in a clean home before packaging, with no source auth.
        clean = stage / 'clean-home'
        clean.mkdir()
        run(str(resources / 'runtime' / ('arm64' if os.uname().machine == 'arm64' else 'x64') / 'bin/node'), '--check', str(tools / 'authorize.mjs'), env={'HOME': str(clean), 'PATH':'/usr/bin:/bin'})
        run(str(resources / 'runtime' / ('arm64' if os.uname().machine == 'arm64' else 'x64') / 'bin/node'), '--check', str(tools / 'oauth_flow.mjs'), env={'HOME': str(clean), 'PATH':'/usr/bin:/bin'})
        output.parent.mkdir(parents=True, exist_ok=True)
        run('ditto', '-c', '-k', '--keepParent', str(app), str(output))
        if identity:
            run('xcrun', 'notarytool', 'submit', str(output), '--keychain-profile', notary_profile, '--wait')
            run('xcrun', 'stapler', 'staple', str(app))
            run('spctl', '--assess', '--type', 'execute', '--verbose=2', str(app))
            run('ditto', '-c', '-k', '--keepParent', str(app), str(output))
        print(json.dumps({'artifact': output.name, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'developer_id_signed_and_notarized': bool(identity)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--identity')
    parser.add_argument('--notary-profile')
    args = parser.parse_args()
    build(args.output.resolve(), args.identity, args.notary_profile)
