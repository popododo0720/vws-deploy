#!/usr/bin/env python3
"""vws config validate through scripts/vws. The refusal table repeats the case names and inputs of
vws-console-api internal/globalconfig TestParseRefusesWhatIsNotBootstrap, so both parsers give one verdict."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES = FAILED = 0
MAX_BYTES = 64 << 10
SECRETS = ["hunter2", "q83vEjRWeJA", "MIIEvQ", "abc123"]
VALID = """
iam:
  etcd_endpoints: [https://10.0.0.1:2379, https://10.0.0.2:2379]
  tls:
    ca_file: /etc/vws/etcd-ca.pem
    cert_file: /run/credentials/vws-console-api/etcd.crt
    key_file: /run/credentials/vws-console-api/etcd.key
listeners:
  console: ":8080"
  ec2: 0.0.0.0:8081
  ops: 127.0.0.1:9090
credentials:
  kek_file: /run/credentials/vws-console-api/admin-kek
  mtls:
    ca_file: /etc/vws/internal-ca.pem
    cert_file: /run/credentials/vws-console-api/mtls.crt
    key_file: /run/credentials/vws-console-api/mtls.key
default_project: default
"""


def check(name, condition, detail=""):
    global CASES, FAILED
    CASES += 1
    if condition:
        print("ok   " + name)
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


def run(*args, stdin=None, output="text"):
    env = dict(os.environ)
    cmd = [str(ROOT / "scripts/vws")] + (["-o", "json"] if output == "json" else []) + ["config", *args]
    p = subprocess.run(cmd, input=stdin, capture_output=True, env=env, check=False)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


def validate(text, output="text"):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
    try:
        return run("validate", f.name, output=output)
    finally:
        os.unlink(f.name)


def with_(old, repl):
    assert old in VALID, old
    return VALID.replace(old, repl, 1)


MTLS_KEY = "key_file: /run/credentials/vws-console-api/mtls.key"
KEK_FILE = "kek_file: /run/credentials/vws-console-api/admin-kek"
PEM = "|\n      -----BEGIN PRIVATE KEY-----\n      MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n      -----END PRIVATE KEY-----"
REFUSED = [
    ("regions", VALID + "regions:\n  - name: pve-seoul-1\n    pve_api_url: https://10.0.0.5:8006\n", "registration"),
    ("one region", VALID + "region: vws-1\n", "registration"),
    ("aws accounts", VALID + "aws_accounts:\n  - aws_account_id: \"123456789012\"\n", "registration"),
    ("capitalized registration", VALID + "Regions: []\n", "registration"),
    ("token secret", VALID + "token_secret: abc123\n", "inline-secret"),
    ("inline kek", with_(KEK_FILE, "kek: q83vEjRWeJA="), "inline-secret"),
    ("inline tls key", with_("key_file: /run/credentials/vws-console-api/etcd.key", "key: " + PEM), "inline-secret"),
    ("pem in a file field", with_(MTLS_KEY, "key_file: " + PEM), "inline-secret"),
    ("kek value in a file field", with_(KEK_FILE, "kek_file: q83vEjRWeJA="), "inline-secret"),
    ("relative file", with_("ca_file: /etc/vws/etcd-ca.pem", "ca_file: etcd-ca.pem"), "inline-secret"),
    ("etcd password in url", with_("https://10.0.0.1:2379", "https://root:hunter2@10.0.0.1:2379"), "inline-secret"),
    ("etcd password key", with_("  etcd_endpoints:", "  etcd_password: hunter2\n  etcd_endpoints:"), "inline-secret"),
    ("unknown key", VALID + "log_level: debug\n", "invalid"),
    ("unknown nested key", with_("  ops: 127.0.0.1:9090", "  ops: 127.0.0.1:9090\n  admin: :9443"), "invalid"),
    ("duplicate key", VALID + "default_project: other\n", "invalid"),
    ("two documents", VALID + "---\ndefault_project: other\n", "invalid"),
    ("empty", "", "invalid"),
    ("not a mapping", "- a\n- b\n", "invalid"),
    ("plaintext etcd", with_("https://10.0.0.1:2379", "http://10.0.0.1:2379"), "invalid"),
    ("etcd without port", with_("https://10.0.0.1:2379", "https://10.0.0.1"), "invalid"),
    ("no etcd", with_("  etcd_endpoints: [https://10.0.0.1:2379, https://10.0.0.2:2379]\n", ""), "invalid"),
    ("no ca", with_("    ca_file: /etc/vws/etcd-ca.pem\n", ""), "invalid"),
    ("no kek", with_("  kek_file: /run/credentials/vws-console-api/admin-kek\n", ""), "invalid"),
    ("cert without key", with_("    key_file: /run/credentials/vws-console-api/mtls.key\n", ""), "invalid"),
    ("bad listener", with_('console: ":8080"', "console: localhost"), "invalid"),
    ("listener port out of range", with_('console: ":8080"', 'console: ":70000"'), "invalid"),
    ("no default project", with_("default_project: default\n", ""), "invalid"),
    ("default project with a space", with_("default_project: default", "default_project: a b"), "invalid"),
    ("oversized", VALID + "#" + "x" * MAX_BYTES + "\n", "invalid"),
    ("mistyped field quoting a secret", with_(
        "etcd_endpoints: [https://10.0.0.1:2379, https://10.0.0.2:2379]", "etcd_endpoints: hunter2"), "invalid"),
    ("unterminated quote around a secret", with_("default_project: default", 'default_project: "hunter2'), "invalid"),
    ("listener quoting a secret", with_('console: ":8080"', "console: hunter2"), "invalid"),
    ("default project quoting a secret", with_("default_project: default", "default_project: hunter2!"), "invalid"),
]

rc, out, err = validate(VALID)
check("the console-api fixture is valid", rc == 0 and out.startswith("OK "), f"rc={rc} {out}{err}")
rc, out, err = run("validate", str(ROOT / "packaging/vws-global/global.yaml"))
check("the shipped vws-global global.yaml is valid", rc == 0, f"rc={rc} {err}")
rc, out, err = run("validate", "-", stdin=VALID.encode())
check("stdin is accepted", rc == 0, f"rc={rc} {err}")
rc, out, _ = validate(VALID, output="json")
check("json reports valid", rc == 0 and json.loads(out) == {"valid": True}, out)
rc, out, err = run("validate", rc_path := str(ROOT / "no-such-dir/global.yaml"))
check("an unreadable file exits 3", rc == 3 and rc_path not in err, f"rc={rc} {err}")
for args in ([], ["validate"], ["show", "x"], ["validate", "a", "b"]):
    rc, _, err = run(*args)
    check(f"usage {args} exits 2", rc == 2 and "usage" in err, f"rc={rc} {err}")

for name, text, kind in REFUSED:
    rc, out, err = validate(text)
    leaked = [s for s in SECRETS if s in out + err]
    check(f"refuses {name}", rc == 2 and not out and not leaked and err.startswith("vws: "),
          f"rc={rc} leaked={leaked} {out}{err}")
    rc, out, err = validate(text, output="json")
    try:
        got = json.loads(out)
    except ValueError:
        got = {}
    leaked = [s for s in SECRETS if s in out + err]
    check(f"json kind of {name} is {kind}", rc == 2 and got.get("valid") is False and got.get("kind") == kind
          and not leaked, f"rc={rc} leaked={leaked} {out}")

print(f"{CASES} cases, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
