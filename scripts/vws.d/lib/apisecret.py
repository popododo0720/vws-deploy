#!/usr/bin/env python3
"""apisecret.py: the Secrets vws apply creates, with values made here and never written anywhere else.

  apisecret.py names VALUES   one "NAME<TAB>KIND" per line for the enabled services of charts/vws/values.yaml
                            overlaid with VALUES (the rendered overlay): KIND api for
                            services.*.apiCredentials.existingSecret, kek for services.*.kekSecret (#243).
  apisecret.py manifest NAME [KIND]  a Secret manifest (JSON) on stdout with fresh random values.
                            api (default): console-token, access-key, secret-key (console-api).
                            kek: kek (base64 of 32 random bytes) and key-id kek-1 (vws-iam, AES-256-GCM).

The manifest goes only to the stdin of the create call in vws apply: no value is in an argument, a file or a log.
Values come from the secrets module (os.urandom), like the installer's crypto/rand password (#146).
Exit 2 on bad usage, 3 when a repo file is missing.
"""
import base64
import json
import secrets
import string
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
VALUES = ROOT / "charts/vws/values.yaml"
ACCESS_KEY_ALPHABET = string.ascii_uppercase + string.digits


def names(overlay_path: str) -> list[str]:
    try:
        base = yaml.safe_load(VALUES.read_text()) or {}
    except OSError as err:
        print(f"apisecret.py: {err}", file=sys.stderr)
        sys.exit(3)
    overlay = yaml.safe_load(Path(overlay_path).read_text()) or {}
    services = dict(base.get("services") or {})
    for name, spec in (overlay.get("services") or {}).items():
        merged = dict(services.get(name) or {})
        merged.update(spec or {})
        services[name] = merged
    found = set()
    for spec in services.values():
        spec = spec or {}
        if spec.get("enabled", True) is False:
            continue
        secret = (spec.get("apiCredentials") or {}).get("existingSecret")
        if secret:
            found.add(f"{secret}\tapi")
        if spec.get("kekSecret"):
            found.add(f"{spec['kekSecret']}\tkek")
    return sorted(found)


def kek_manifest(name: str) -> dict:
    """The vws-iam key-encryption key. Created once; vws apply never replaces it (no silent rotation)."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": "vws", "app.kubernetes.io/managed-by": "vws-apply"}},
        "type": "Opaque",
        "stringData": {"kek": base64.b64encode(secrets.token_bytes(32)).decode(), "key-id": "kek-1"},
    }


def manifest(name: str) -> dict:
    access_key = "VWS" + "".join(secrets.choice(ACCESS_KEY_ALPHABET) for _ in range(17))
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": "vws", "app.kubernetes.io/managed-by": "vws-apply"}},
        "type": "Opaque",
        "stringData": {
            "console-token": secrets.token_urlsafe(32),
            "access-key": access_key,
            "secret-key": secrets.token_urlsafe(32),
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "names":
        print("\n".join(names(argv[1])))
        return 0
    if len(argv) in (2, 3) and argv[0] == "manifest":
        kind = argv[2] if len(argv) == 3 else "api"
        if kind not in ("api", "kek"):
            print(f"apisecret.py: unknown kind {kind}", file=sys.stderr)
            return 2
        json.dump(kek_manifest(argv[1]) if kind == "kek" else manifest(argv[1]), sys.stdout)
        return 0
    print("usage: apisecret.py names VALUES | manifest NAME [api|kek]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
