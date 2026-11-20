"""Shared lab inputs for the unit and disposable Debian target tests."""
from pathlib import Path
import subprocess
import tempfile

PRIVATE = ("ca.crt member.crt member.key client.crt client.key admin-kek console-token "
           "vws-access-key vws-secret-key api-etcd-key.pem api-mtls-key.pem aws-source "
           "iam-etcd-key.pem iam-key.pem").split()
PUBLIC = "etcd-ca.pem api-etcd.pem internal-ca.pem api-mtls.pem iam-etcd.pem iam.pem".split()


# Credential name -> file in the vws-etcd-tls output; the member is lab-1 at 192.0.2.1.
TLS = {"ca.crt": "etcd/ca.crt", "etcd-ca.pem": "etcd/ca.crt", "member.crt": "etcd/lab-1/member.crt",
       "member.key": "etcd/lab-1/member.key", "client.crt": "etcd/client/client.crt",
       "client.key": "etcd/client/client.key", "api-etcd.pem": "etcd/client/client.crt",
       "api-etcd-key.pem": "etcd/client/client.key", "iam-etcd.pem": "etcd/client/client.crt",
       "iam-etcd-key.pem": "etcd/client/client.key", "internal-ca.pem": "internal/ca.crt",
       "iam.pem": "internal/iam/member.crt", "iam-key.pem": "internal/iam/member.key",
       "api-mtls.pem": "internal/client/client.crt", "api-mtls-key.pem": "internal/client/client.key"}


def real_tls(credentials, address="192.0.2.1"):
    """Write a consistent operator-supplied TLS set made by the packaged generator."""
    tool = Path(__file__).resolve().parents[2] / "packaging/vws-etcd/vws-etcd-tls"
    with tempfile.TemporaryDirectory() as work:
        for state, members in (("etcd", "lab-1=" + address), ("internal", "iam=127.0.0.1")):
            subprocess.run(["bash", str(tool), work + "/" + state, members], check=True,
                           stdout=subprocess.DEVNULL, timeout=60)
        for name, path in TLS.items():
            (credentials / name).write_bytes((Path(work) / path).read_bytes())


def prepare(root):
    for directory in ("credentials/iam-kek", "packages", "bin", "state"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in [n for n in PRIVATE + PUBLIC if n not in TLS] + ["iam-kek/active", "iam-kek/key1"]:
        (root / "credentials" / name).write_text("fixture-private-" + name.replace("/", "-") + "\n")
    real_tls(root / "credentials")
    (root / "hosts.yaml").write_text("""profile: lab
hosts:
  - name: lab-1
    address: 192.0.2.1
    roles: [etcd, global]
""")
    (root / "global.yaml").write_text("""iam:
  etcd_endpoints: [https://127.0.0.1:2379]
  tls:
    ca_file: /etc/vws/etcd-ca.pem
credentials:
  kek_file: /run/credentials/vws-console-api.service/admin-kek
default_project: lab
""")
    for name in ("vws-etcd", "vws-global"):
        package = root / name
        (package / "DEBIAN").mkdir(parents=True)
        (package / "DEBIAN/control").write_text(
            f"Package: {name}\nVersion: 0.0.0-1\nArchitecture: all\n"
            "Maintainer: Test <test@example.org>\nDescription: installer boundary fixture\n")
        helper = package / "usr/lib/vws"
        helper.mkdir(parents=True)
        if name == "vws-etcd":
            helper /= "etcd"
            helper.mkdir()
            script = helper / "check-ready"
        else:
            script = helper / "check-global-ready"
        script.write_text('#!/bin/sh\ntest ! -f /var/lib/vws-install-fail\n')
        script.chmod(0o755)
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zgzip", "--build", str(package),
                        str(root / "packages" / (name + "_0.0.0-1_all.deb"))],
                       check=True, stdout=subprocess.DEVNULL, timeout=30)


if __name__ == "__main__":
    import sys
    prepare(Path(sys.argv[1]))
