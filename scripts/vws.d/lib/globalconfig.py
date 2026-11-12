#!/usr/bin/env python3
"""Validates /etc/vws/global.yaml with the rules of vws-console-api internal/globalconfig.

global.yaml holds only the bootstrap settings of the global layer. Regions and AWS
accounts are registered through the admin API, and secrets are named by file path.
No message repeats a value from the file: a mistyped field may hold a secret.
"""
import json
import os
import posixpath
import re
import sys
import unicodedata

import yaml

MAX_BYTES = 64 << 10
REGISTRATION = "registration"
INLINE_SECRET = "inline-secret"
INVALID = "invalid"
MESSAGES = {
    REGISTRATION: "regions and AWS accounts are registered through the admin API, not global.yaml",
    INLINE_SECRET: "a secret is named by an absolute file path, never written inline",
    INVALID: "invalid global.yaml",
}
TLS = {"ca_file": str, "cert_file": str, "key_file": str}
SCHEMA = {
    "iam": {"etcd_endpoints": [str], "tls": TLS},
    "listeners": {"console": str, "ec2": str, "s3": str, "iam": str, "ops": str},
    "credentials": {"kek_file": str, "mtls": TLS},
    "default_project": str,
}


class Refused(Exception):
    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def line(node):
    return f"line {node.start_mark.line + 1}"


def registration_key(key):
    return key.lower() in ("region", "regions", "proxmox", "clusters", "account", "accounts",
                           "aws", "aws_accounts")


def secret_key(key):
    k = key.lower()
    return not k.endswith("_file") and any(w in k for w in ("secret", "password", "token", "kek", "key"))


def refuse_secret_keys(node, path):
    if not isinstance(node, yaml.MappingNode):
        return
    for key, value in node.value:
        if not isinstance(value, yaml.MappingNode) and secret_key(key.value):
            raise Refused(INLINE_SECRET, f'key "{path}{key.value}"')
        refuse_secret_keys(value, f"{path}{key.value}.")


def decode(node, schema):
    if isinstance(schema, dict):
        if isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:null":
            return {}
        if not isinstance(node, yaml.MappingNode):
            raise Refused(INVALID, f"malformed YAML at {line(node)}")
        out = {}
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.value not in schema:
                raise Refused(INVALID, f"malformed YAML at {line(key)}")
            if key.value in out:
                raise Refused(INVALID, f"malformed YAML at {line(key)}")
            out[key.value] = decode(value, schema[key.value])
        return out
    if isinstance(schema, list):
        if isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:null":
            return []
        if not isinstance(node, yaml.SequenceNode):
            raise Refused(INVALID, f"malformed YAML at {line(node)}")
        return [decode(item, schema[0]) for item in node.value]
    if not isinstance(node, yaml.ScalarNode):
        raise Refused(INVALID, f"malformed YAML at {line(node)}")
    return "" if node.tag == "tag:yaml.org,2002:null" else node.value


def host_port(s):
    """net.SplitHostPort plus a port from 1 to 65535."""
    if s.startswith("["):
        end = s.find("]")
        if end < 0 or s[end + 1:end + 2] != ":":
            return False
        host, port = s[1:end], s[end + 2:]
    else:
        i = s.rfind(":")
        if i < 0:
            return False
        host, port = s[:i], s[i + 1:]
        if ":" in host:
            return False
    if "[" in host or "]" in host or ":" in port:
        return False
    return bool(re.fullmatch(r"[+-]?[0-9]+", port)) and 0 < int(port) < 65536


def file_path(s):
    clean = posixpath.normpath(s) if s else "."
    if clean.startswith("//"):
        clean = "/" + clean.lstrip("/")
    return (s.startswith("/") and clean == s
            and not any(unicodedata.category(c) == "Cc" for c in s))


def check_endpoint(raw):
    m = re.fullmatch(r"([A-Za-z][A-Za-z0-9+.-]*):(//([^/?#]*))?([^?#]*)(\?[^#]*)?(#.*)?", raw, re.S)
    if not m or any(unicodedata.category(c) == "Cc" for c in raw):
        raise Refused(INVALID, "iam.etcd_endpoints holds an entry that is not a URL")
    scheme, authority, path, query, fragment = m.group(1), m.group(3) or "", m.group(4), m.group(5), m.group(6)
    if "@" in authority:
        raise Refused(INLINE_SECRET, "iam.etcd_endpoints carries user info")
    if (scheme.lower() != "https" or not host_port(authority) or path not in ("", "/")
            or query or fragment):
        raise Refused(INVALID, "iam.etcd_endpoints holds an entry that is not https://host:port")


def project_name(s):
    return (0 < len(s.encode()) <= 64
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", s) is not None)


def validate(cfg):
    iam = cfg.get("iam", {})
    tls = iam.get("tls", {})
    creds = cfg.get("credentials", {})
    mtls = creds.get("mtls", {})
    listeners = cfg.get("listeners", {})
    if not iam.get("etcd_endpoints"):
        raise Refused(INVALID, "iam.etcd_endpoints is empty")
    for endpoint in iam["etcd_endpoints"]:
        check_endpoint(endpoint)
    if not tls.get("ca_file"):
        raise Refused(INVALID, "iam.tls.ca_file is required")
    if not creds.get("kek_file"):
        raise Refused(INVALID, "credentials.kek_file is required")
    files = [("iam.tls." + k, tls.get(k, "")) for k in TLS]
    files.append(("credentials.kek_file", creds.get("kek_file", "")))
    files += [("credentials.mtls." + k, mtls.get(k, "")) for k in TLS]
    for name, value in files:
        if value and not file_path(value):
            raise Refused(INLINE_SECRET, name)
    for name, t in (("iam.tls", tls), ("credentials.mtls", mtls)):
        if bool(t.get("cert_file")) != bool(t.get("key_file")):
            raise Refused(INVALID, f"{name}.cert_file and {name}.key_file are set together")
    for name in SCHEMA["listeners"]:
        value = listeners.get(name, "")
        if value and not host_port(value):
            raise Refused(INVALID, f"listeners.{name} is not host:port")
    if not project_name(cfg.get("default_project", "")):
        raise Refused(INVALID, "default_project is not a Proxmox pool id")


def parse(data):
    if len(data) > MAX_BYTES:
        raise Refused(INVALID, f"larger than {MAX_BYTES} bytes")
    try:
        docs = list(yaml.compose_all(data.decode("utf-8"), Loader=yaml.SafeLoader))
    except UnicodeError:
        raise Refused(INVALID, "not UTF-8") from None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise Refused(INVALID, f"malformed YAML at line {mark.line + 1}" if mark else "malformed YAML") from None
    if not docs:
        raise Refused(INVALID, "empty")
    if len(docs) > 1:
        raise Refused(INVALID, "more than one document")
    root = docs[0]
    if isinstance(root, yaml.MappingNode):
        for key, _ in root.value:
            if registration_key(key.value):
                raise Refused(REGISTRATION, f'key "{key.value}"')
        refuse_secret_keys(root, "")
    cfg = decode(root, SCHEMA)
    validate(cfg)
    return cfg


def fail(message, code=2):
    print("vws: " + message, file=sys.stderr)
    raise SystemExit(code)


def main(argv):
    if len(argv) != 2 or argv[0] != "validate":
        fail("usage: vws config validate FILE|-; see docs/vws-cli.md")
    path = argv[1]
    try:
        if path == "-":
            data = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with open(path, "rb") as f:
                data = f.read(MAX_BYTES + 1)
    except OSError:
        fail("cannot read the config file", 3)
    json_output = os.environ.get("VWS_OUTPUT") == "json"
    try:
        parse(data)
    except Refused as e:
        if json_output:
            print(json.dumps({"valid": False, "kind": e.kind, "error": f"{MESSAGES[e.kind]}: {e.detail}"}))
        fail(f"{MESSAGES[e.kind]}: {e.detail}")
    print(json.dumps({"valid": True}) if json_output else "OK config bootstrap keys only")


if __name__ == "__main__":
    main(sys.argv[1:])
