"""Shared lab inputs for the unit and disposable Debian target tests."""
from pathlib import Path
import subprocess

PRIVATE = ("ca.crt member.crt member.key client.crt client.key admin-kek console-token "
           "vws-access-key vws-secret-key api-etcd-key.pem api-mtls-key.pem aws-source "
           "iam-etcd-key.pem iam-key.pem region-kek region.key").split()
PUBLIC = "etcd-ca.pem api-etcd.pem internal-ca.pem api-mtls.pem iam-etcd.pem iam.pem".split()


def prepare(root):
    for directory in ("credentials/iam-kek", "packages", "bin", "state"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in PRIVATE + PUBLIC + ["iam-kek/active", "iam-kek/key1"]:
        (root / "credentials" / name).write_text("fixture-private-" + name.replace("/", "-") + "\n")
    (root / "hosts.yaml").write_text("""profile: lab
hosts:
  - name: lab-1
    address: 192.0.2.1
    roles: [etcd, global, region]
vip:
  address: 192.0.2.10/24
""")
    (root / "global.yaml").write_text("""iam:
  etcd_endpoints: [https://127.0.0.1:2379]
  tls:
    ca_file: /etc/vws/etcd-ca.pem
credentials:
  kek_file: /run/credentials/vws-console-api.service/admin-kek
default_project: lab
""")
    for name in ("vws-etcd", "vws-global", "vws-region"):
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
            script = helper / ("check-global-ready" if name == "vws-global" else "check-region-ready")
        script.write_text('#!/bin/sh\ntest ! -f /var/lib/vws-install-fail\n')
        script.chmod(0o755)
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zgzip", "--build", str(package),
                        str(root / "packages" / (name + "_0.0.0-1_all.deb"))],
                       check=True, stdout=subprocess.DEVNULL, timeout=30)


if __name__ == "__main__":
    import sys
    prepare(Path(sys.argv[1]))
