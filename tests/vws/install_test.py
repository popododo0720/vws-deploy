#!/usr/bin/env python3
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from install_fixture import prepare, PRIVATE

ROOT = Path(__file__).resolve().parents[2]
SSH = r"""#!/usr/bin/env python3
import hashlib, json, os, pathlib, shlex, sys
s = pathlib.Path(os.environ["INSTALL_STATE"])
c = shlex.split(sys.argv[-1])[-1]
d = sys.stdin.buffer.read()
# One state directory serves every host of an inventory: package, service and write markers are per host.
h = next(a for a in sys.argv if "@" in a).split("@")[1] + "-"
with (s / "argv").open("a") as f:
    f.write(json.dumps(sys.argv) + "\n")
with (s / "payload").open("ab") as f:
    f.write(d)
if "/etc/systemd/system/" in c:
    with (s / "units").open("a") as f:
        f.write(json.dumps({"command": c, "data": d.decode()}) + "\n")
if os.environ.get("INSTALL_FAIL") and os.environ["INSTALL_FAIL"] in c:
    sys.stderr.write("fixture-private-admin-kek")
    sys.exit(1)
if c == "test ! -f /var/lib/vws-install/pending || echo pending":
    if (s / "pending").exists(): print("pending")
elif c == "rm -f /var/lib/vws-install/pending":
    (s / "pending").unlink(missing_ok=True)
elif "dpkg-query" in c:
    name = next(n for n in ("vws-etcd", "vws-global", "vws-region") if n in c)
    (s / "current-package").write_text(name)
    if (s / (h + name)).exists():
        status = os.environ.get("INSTALL_PACKAGE_STATE", "ii ") if name == "vws-global" else "ii "
        print(status + "\t0.0.0-1", end="")
elif "apt-get install" in c:
    (s / (h + (s / "current-package").read_text())).touch()
    (s / "pending").touch()
elif "systemctl is-enabled" in c:
    key = s / (h + "service-" + c.split("is-enabled --quiet ")[1].split()[0])
    print("changed" if not key.exists() or "systemctl restart" in c else "unchanged")
    key.touch()
elif "/readyz" in c:
    name = c.split("/var/lib/vws-region/")[1].split("/")[0]
    print("registered" if (s / (h + "registered-" + name)).exists() else "unregistered")
elif "echo changed" in c:
    key = s / hashlib.sha256(h.encode() + c.encode() + d).hexdigest()
    changed = not key.exists()
    if changed: (s / "pending").touch()
    key.touch()
    if changed and os.environ.get("INSTALL_LOST") and os.environ["INSTALL_LOST"] in c:
        sys.exit(255)
    print("changed" if changed else "unchanged")
"""


# Names vws install creates when the credentials directory lacks them.
GENERATED = ("admin-kek ca.crt member.crt member.key client.crt client.key etcd-ca.pem "
             "api-etcd.pem api-etcd-key.pem iam-etcd.pem iam-etcd-key.pem internal-ca.pem iam.pem "
             "iam-key.pem api-mtls.pem api-mtls-key.pem").split()


# A region instance on the global host: appended to the hosts.yaml of prepare().
REGION_ON_GLOBAL = "    regions:\n      - name: pve-seoul-1\n        port: 8443\n"
SEPARATE_HOST = """  - name: region-1
    address: 192.0.2.20
    roles: [region]
    regions:
      - name: pve-busan-1
        port: 8443
"""


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        prepare(self.work)
        shim = self.work / "bin/ssh"
        shim.write_text(SSH)
        shim.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.work / "bin") + ":" + os.environ["PATH"],
                        INSTALL_STATE=str(self.work / "state"))
        self.args = ["--profile", "lab", "--inventory", str(self.work / "hosts.yaml"),
                     "--packages", str(self.work / "packages"),
                     "--credentials", str(self.work / "credentials")]

    def run_install(self, args=None):
        return subprocess.run(["bash", str(ROOT / "scripts/vws"), "install",
                               *(self.args if args is None else args)], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def refused(self, args=None):
        result = self.run_install(args)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.work / "state/argv").exists())
        return result

    def test_arguments(self):
        for args in ([], ["--unknown", "private-value"], ["--profile", "small", *self.args[2:]],
                     ["--profile", "ha", *self.args[2:]]):
            with self.subTest(args=args):
                self.refused(args)

    def test_inventory(self):
        path = self.work / "hosts.yaml"
        good = path.read_text()
        for bad in ("", "---", "[", "hosts: []", good + "unknown: value\n",
                    good + "profile: lab\n", good.replace("name: lab-1", "token: hidden"),
                    good.replace("name: lab-1", "name: Lab-1"),
                    good.replace("name: lab-1", "name: lab-"),
                    good.replace("name: lab-1", "name: client"),
                    good.replace("name: lab-1", "name: " + "a" * 64),
                    good.replace("name: lab-1", "name: '-oProxyCommand=bad'"),
                    good.replace("address: 192.0.2.1", "address: 192.0.2.1;bad"),
                    good.replace("roles: [etcd, global]", "roles: [etcd]"),
                    good.replace("roles: [etcd, global]", "roles: [region]"),
                    good.replace("roles: [etcd, global]", "roles: [global]"),
                    good.replace("roles: [etcd, global]", "roles: [etcd, region]"),
                    good.replace("roles: [etcd, global]", "roles: [etcd, global, region]"),
                    good + "vip:\n  address: 192.0.2.10/24\n",
                    good.replace("roles: [etcd, global]", "roles: [etcd, global, region]")
                    + "vip:\n  address: 192.0.2.10/24\n",
                    good.replace("roles: [etcd, global]", "roles: [etcd, global, global]"),
                    good + "password: hidden\n", good + "---\nprofile: lab\n",
                    good.replace("roles: [etcd, global]", "roles: [{secret: hidden}]"),
                    "profile: lab\nhosts: &loop [*loop]\n",
                    good.replace("name: lab-1", "? [bad, key]\n    : value")):
            with self.subTest(bad=bad):
                path.write_text(bad)
                self.refused()

    def test_global_config(self):
        for data in ("secret: hidden\n", "---", "? [a, b]\n: value\n"):
            with self.subTest(data=data):
                (self.work / "global.yaml").write_text(data)
                self.refused()

    def test_lab_memory_budget(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        units = [json.loads(line) for line in (self.work / "state/units").read_text().splitlines()]
        for group, mib in (("global", 1024), ("etcd", 512)):
            slice_name = f"vws-lab-{group}.slice"
            self.assertTrue(any(f"/{slice_name}.d/50-vws-memory.conf" in row["command"]
                                and f"MemoryMax={mib}M\nMemorySwapMax=0\n" in row["data"]
                                for row in units), group)
        # A global host has no region slice and no unit of the removed per-node vws-region.service.
        self.assertFalse(any("region" in row["command"] for row in units))
        for service, group in (("vws-iam", "global"), ("vws-aws-proxy", "global"),
                               ("vws-console-api", "global"), ("vws-console", "global"),
                               ("vws-etcd", "etcd"),
                               ("vws-etcd-snapshot", "etcd"), ("vws-etcd-defrag", "etcd")):
            self.assertTrue(any(f"/{service}.service.d/50-vws-memory.conf" in row["command"]
                                and row["data"] == f"[Service]\nSlice=vws-lab-{group}.slice\n"
                                for row in units), service)
        log = (self.work / "state/argv").read_text()
        self.assertLess(log.index("systemctl daemon-reload"), log.index("systemctl is-enabled"))

    def test_memory_input_boundaries(self):
        path = self.work / "hosts.yaml"
        good = path.read_text()
        for memory in ("{global: 1025, region: 512, etcd: 512}",
                       "{global: 0, region: 512, etcd: 512}",
                       "{global: -1, region: 512, etcd: 512}",
                       "{global: '512;bad', region: 512, etcd: 512}",
                       "{global: true, region: 512, etcd: 512}",
                       "{global: 512, region: 512}", "null",
                       "{global: 512, region: 512, etcd: 512, extra: 1}"):
            with self.subTest(memory=memory):
                path.write_text(good + "memory_mib: " + memory + "\n")
                self.refused()

    def test_custom_budget_and_reload_failure_recovery(self):
        path = self.work / "hosts.yaml"
        path.write_text(path.read_text() + "memory_mib: {global: 768, region: 768, etcd: 256}\n")
        self.env["INSTALL_FAIL"] = "systemctl daemon-reload"
        result = self.run_install()
        self.assertEqual(result.returncode, 1, result.stderr)
        log = (self.work / "state/argv").read_text()
        self.env.pop("INSTALL_FAIL")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MemoryMax=768M", (self.work / "state/units").read_text())
        units = [json.loads(line) for line in (self.work / "state/units").read_text().splitlines()]
        self.assertTrue(any("/vws-iam.service.d/50-vws-memory.conf" in row["command"]
                            and row["data"] == "[Service]\nSlice=vws-lab-global.slice\n"
                            for row in units))
        self.assertFalse((self.work / "state/pending").exists())
        again = self.run_install(["-o", "json", *self.args])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(all(s["status"] == "unchanged" for s in json.loads(again.stdout)))

    def test_memory_controller_unavailable(self):
        self.env["INSTALL_FAIL"] = "/sys/fs/cgroup/cgroup.controllers"
        result = self.run_install()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("memory controller failed", result.stderr)
        log = (self.work / "state/argv").read_text()
        self.assertNotIn("apt-get install", log)

    def test_member_name_boundaries(self):
        path = self.work / "hosts.yaml"
        good = path.read_text()
        for name in ("a", "0", "a" * 63):
            with self.subTest(name=name):
                path.write_text(good.replace("name: lab-1", "name: " + name))
                result = self.run_install()
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_credential(self):
        (self.work / "credentials/console-token").unlink()
        result = self.refused()
        self.assertEqual(result.stderr, "vws: required regular file: console-token\n")

    def remove_generated(self):
        source = self.work / "credentials"
        for name in GENERATED:
            (source / name).unlink(missing_ok=True)
        shutil.rmtree(source / "iam-kek")
        return source

    def secrets(self, source):
        return {str(p.relative_to(source)): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in source.rglob("*") if p.is_file()}

    def test_first_install_generates_missing_secrets_once(self):
        source = self.remove_generated()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        first = self.secrets(source)
        for name in ("admin-kek", "iam-kek/k1"):
            self.assertEqual(len(base64.b64decode(first[name][0], validate=False)), 32, name)
        self.assertEqual(first["iam-kek/active"][0], b"k1\n")
        supplied = {"console-token", "vws-access-key", "vws-secret-key", "aws-source"}
        self.assertEqual({name for name, (_, mode) in first.items() if mode != 0o600}, supplied)
        for ca, leaf, san in (("ca.crt", "member.crt", "IP Address:192.0.2.1, IP Address:127.0.0.1, DNS:lab-1"),
                              ("etcd-ca.pem", "api-etcd.pem", ""), ("etcd-ca.pem", "iam-etcd.pem", ""),
                              ("internal-ca.pem", "iam.pem", "IP Address:127.0.0.1, DNS:iam"),
                              ("internal-ca.pem", "api-mtls.pem", "")):
            subprocess.run(["openssl", "verify", "-CAfile", source / ca, source / leaf],
                           check=True, capture_output=True)
            shown = subprocess.run(["openssl", "x509", "-in", source / leaf, "-noout", "-ext",
                                    "subjectAltName"], capture_output=True, text=True).stdout
            self.assertIn(san, shown, leaf)
        self.assertNotEqual(first["ca.crt"][0], first["internal-ca.pem"][0])
        # The CA keys stay on the operator host; the generated KEK reaches the target on stdin only.
        payload = (self.work / "state/payload").read_bytes()
        self.assertIn(first["admin-kek"][0], payload)
        for state in ("etcd-tls", "internal-tls"):
            self.assertNotIn(first[state + "/ca.key"][0], payload)
        again = self.run_install(["-o", "json", *self.args])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(all(s["status"] == "unchanged" for s in json.loads(again.stdout)), again.stdout)
        self.assertEqual(self.secrets(source), first)
        output = (result.stdout + result.stderr + again.stdout + again.stderr
                  + (self.work / "state/argv").read_text())
        self.assertNotIn(first["admin-kek"][0].decode().strip(), output)

    def test_interrupted_generation_resumes_without_new_keys(self):
        source = self.remove_generated()
        self.assertEqual(self.run_install().returncode, 0)
        first = self.secrets(source)
        # Power loss between two links: the state directory is complete, some names are not linked yet.
        for name in ("api-etcd.pem", "iam-key.pem", "admin-kek"):
            (source / name).unlink()
        self.assertEqual(self.run_install().returncode, 0)
        after = self.secrets(source)
        self.assertNotEqual(after.pop("admin-kek"), first.pop("admin-kek"))
        self.assertEqual(after, first)

    def test_supplied_tls_set_is_never_completed_with_generated_files(self):
        source = self.work / "credentials"
        (source / "member.key").unlink()
        (source / "iam.pem").unlink()
        before = self.secrets(source)
        result = self.refused()
        self.assertEqual(result.stderr, "vws: required regular file: member.key\n")
        self.assertEqual(self.secrets(source), before)

    def test_replaced_key_is_not_completed_from_retained_state(self):
        source = self.remove_generated()
        self.assertEqual(self.run_install().returncode, 0)
        other = self.work / "other"
        subprocess.run(["bash", str(ROOT / "packaging/vws-etcd/vws-etcd-tls"), str(other), "iam=127.0.0.1"],
                       check=True, capture_output=True)
        # The operator replaces the key and the cert is absent: the old state must not make a new cert.
        (source / "iam-key.pem").write_bytes((other / "iam/member.key").read_bytes())
        (source / "iam.pem").unlink()
        before = self.secrets(source)
        calls = (self.work / "state/argv").read_text()
        result = self.run_install()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, "vws: iam-key.pem differs from internal-tls/; replace the whole "
                         "internal-tls set or delete its files and internal-tls/\n")
        self.assertEqual(self.secrets(source), before)
        self.assertEqual((self.work / "state/argv").read_text(), calls)

    def test_resume_needs_every_present_credential_in_the_retained_state(self):
        source = self.remove_generated()
        self.assertEqual(self.run_install().returncode, 0)
        other = self.work / "other"
        subprocess.run(["bash", str(ROOT / "packaging/vws-etcd/vws-etcd-tls"), str(other), "iam=127.0.0.1"],
                       check=True, capture_output=True)
        # The IAM leaf is gone from the retained state, the operator supplies a leaf from another CA and
        # the client certificate is absent: nothing may be filled in around the foreign pair.
        shutil.rmtree(source / "internal-tls/iam")
        (source / "iam.pem").write_bytes((other / "iam/member.crt").read_bytes())
        (source / "iam-key.pem").write_bytes((other / "iam/member.key").read_bytes())
        (source / "api-mtls.pem").unlink()
        before = self.secrets(source)
        calls = (self.work / "state/argv").read_text()
        result = self.run_install()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, "vws: iam.pem differs from internal-tls/; replace the whole "
                         "internal-tls set or delete its files and internal-tls/\n")
        self.assertEqual(self.secrets(source), before)
        self.assertEqual((self.work / "state/argv").read_text(), calls)

    def test_supplied_member_certificate_is_reissued_under_the_own_ca(self):
        hosts = self.work / "hosts.yaml"
        hosts.write_text(hosts.read_text().replace("192.0.2.1", "192.0.2.9"))
        result = self.refused()
        self.assertEqual(result.stderr, "vws: member.crt does not name 192.0.2.9. Reissue it under your own CA "
                         "with 192.0.2.9 and 127.0.0.1 in its subject alternative names, then replace "
                         "member.crt and member.key together\n")
        self.assertNotIn("delete", result.stderr)

    def test_certificate_and_key_must_be_one_pair(self):
        source = self.work / "credentials"
        other = self.work / "other"
        subprocess.run(["bash", str(ROOT / "packaging/vws-etcd/vws-etcd-tls"), str(other), "iam=127.0.0.1"],
                       check=True, capture_output=True)
        (source / "iam-key.pem").write_bytes((other / "iam/member.key").read_bytes())
        result = self.refused()
        self.assertEqual(result.stderr, "vws: iam.pem and iam-key.pem are not one key pair\n")

    def test_new_inventory_address_needs_a_member_certificate_for_it(self):
        source = self.remove_generated()
        self.assertEqual(self.run_install().returncode, 0)
        ca = (source / "ca.crt").read_bytes()
        calls = (self.work / "state/argv").read_text()
        hosts = self.work / "hosts.yaml"
        hosts.write_text(hosts.read_text().replace("192.0.2.1", "192.0.2.9"))
        result = self.run_install()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, "vws: member.crt does not name 192.0.2.9. To reissue it from the same CA, "
                         "delete member.crt, member.key and etcd-tls/lab-1/, then run again\n")
        self.assertEqual((self.work / "state/argv").read_text(), calls)
        # The documented fix keeps the CA and names the new address and loopback.
        for name in ("member.crt", "member.key"):
            (source / name).unlink()
        shutil.rmtree(source / "etcd-tls/lab-1")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual((source / "ca.crt").read_bytes(), ca)
        for address in ("192.0.2.9", "127.0.0.1"):
            subprocess.run(["openssl", "verify", "-CAfile", source / "ca.crt", "-verify_ip", address,
                            source / "member.crt"], check=True, capture_output=True)

    def test_separate_client_certificate_is_kept_when_the_member_pair_is_reissued(self):
        source = self.remove_generated()
        self.assertEqual(self.run_install().returncode, 0)
        state = source / "etcd-tls"
        tmp = self.work / "reissue"
        tmp.mkdir()

        def issue(base, subject, extension, days="825"):
            subprocess.run(["openssl", "req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-subj", "/CN=" + subject, "-keyout", tmp / (base + ".key"),
                            "-out", tmp / (base + ".csr")], check=True, capture_output=True)
            (tmp / (base + ".ext")).write_text(extension)
            subprocess.run(["openssl", "x509", "-req", "-in", tmp / (base + ".csr"), "-CA", state / "ca.crt",
                            "-CAkey", state / "ca.key", "-days", days, "-set_serial", "0x" + base.encode().hex(),
                            "-extfile", tmp / (base + ".ext"), "-out", tmp / (base + ".crt")],
                           check=True, capture_output=True)

        issue("api", "console-api", "extendedKeyUsage=clientAuth\n")
        (source / "api-etcd.pem").write_bytes((tmp / "api.crt").read_bytes())
        (source / "api-etcd-key.pem").write_bytes((tmp / "api.key").read_bytes())
        self.assertEqual(self.run_install().returncode, 0)
        hosts = self.work / "hosts.yaml"
        hosts.write_text(hosts.read_text().replace("192.0.2.1", "192.0.2.9"))
        calls = (self.work / "state/argv").read_text()
        result = self.run_install()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, "vws: member.crt does not name 192.0.2.9. Deleting files will not work "
                         "because the credentials you supplied differ from etcd-tls/. Reissue the member pair "
                         "under the same CA with 192.0.2.9 and 127.0.0.1 in its subject alternative names, "
                         "then replace member.crt and member.key, keeping your separate client credentials\n")
        self.assertEqual((self.work / "state/argv").read_text(), calls)
        # Following the message works: same CA, new pair, separate client credentials untouched.
        issue("member", "lab-1", "extendedKeyUsage=serverAuth,clientAuth\n"
              "subjectAltName=IP:192.0.2.9,IP:127.0.0.1,DNS:lab-1\n")
        ca = (source / "ca.crt").read_bytes()
        (source / "member.crt").write_bytes((tmp / "member.crt").read_bytes())
        (source / "member.key").write_bytes((tmp / "member.key").read_bytes())
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual((source / "ca.crt").read_bytes(), ca)
        self.assertEqual((source / "api-etcd.pem").read_bytes(), (tmp / "api.crt").read_bytes())
        subprocess.run(["openssl", "verify", "-CAfile", source / "ca.crt", "-verify_ip", "192.0.2.9",
                        source / "member.crt"], check=True, capture_output=True)

    def test_generation_without_openssl_is_refused(self):
        self.remove_generated()
        (self.work / "bin/openssl").write_text("#!/bin/sh\nexit 1\n")
        (self.work / "bin/openssl").chmod(0o755)
        result = self.refused()
        self.assertEqual(result.stderr, "vws: cannot generate etcd-tls; openssl and flock are required\n")
        self.assertFalse((self.work / "credentials/ca.crt").exists())

    def test_installed_units_are_enabled_and_started(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.work / "state/argv").read_text()
        for unit in ("vws-etcd", "vws-etcd-snapshot.timer", "vws-etcd-defrag.timer", "vws-iam",
                     "vws-aws-proxy", "vws-console-api", "vws-console"):
            self.assertIn(f"lab-1 {unit} changed\n", result.stdout)
            self.assertIn(f"systemctl enable {unit} ", log)
            self.assertIn(f"systemctl start {unit} ", log)

    def test_global_host_gets_no_region_and_no_keepalived(self):
        source = self.remove_generated()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        for unit in ("vws-etcd", "vws-etcd-snapshot.timer", "vws-etcd-defrag.timer", "vws-iam",
                     "vws-aws-proxy", "vws-console-api", "vws-console"):
            self.assertIn(f"lab-1 {unit} changed\n", result.stdout)
        log = (self.work / "state/argv").read_text()
        for absent in ("keepalived", "vws-region", "region.", "region-kek", "global-ca.pem", "registration",
                       "check-region-ready", "address show"):
            self.assertNotIn(absent, result.stdout + log, absent)
        self.assertFalse((source / "region-kek").exists())
        again = self.run_install(["-o", "json", *self.args])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(all(s["status"] == "unchanged" for s in json.loads(again.stdout)), again.stdout)

    def test_vip_and_keepalived_are_refused(self):
        """The per-node region layout is gone: no inventory key asks for a VIP or keepalived."""
        path = self.work / "hosts.yaml"
        good = path.read_text() + REGION_ON_GLOBAL
        good = good.replace("roles: [etcd, global]", "roles: [etcd, global, region]")
        for extra in ("vip:\n  address: 192.0.2.10/24\n", "keepalived:\n  priority: 100\n"):
            for bad in (good + extra, good.replace("    roles:", "    " + extra.replace("\n  ", "\n      ").rstrip() + "\n    roles:", 1),
                        good.replace("        port: 8443\n", "        port: 8443\n        " + extra.split(":")[0] + ": true\n")):
                with self.subTest(bad=bad):
                    path.write_text(bad)
                    self.assertEqual(self.refused().stderr, "vws: invalid inventory\n")

    def test_global_host_readiness_failure(self):
        self.env["INSTALL_FAIL"] = "check-global-ready api"
        result = self.run_install()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("lab-1: api readiness failed", result.stderr)

    def test_symlink_and_traversal(self):
        path = self.work / "credentials/admin-kek"
        path.unlink()
        path.symlink_to(self.work / "global.yaml")
        self.refused()
        path.unlink()
        path.write_text("private")
        (self.work / "credentials/iam-kek/key1").unlink()
        (self.work / "credentials/iam-kek/key1").symlink_to(self.work / "global.yaml")
        self.refused()
        args = self.args[:-1] + [str(self.work / "credentials/../credentials")]
        self.refused(args)

    def test_package_count(self):
        (self.work / "packages/vws-etcd_extra.deb").write_bytes(b"bad")
        self.refused()

    def test_symlinked_credential_directory(self):
        source = self.work / "credentials"
        source.rename(self.work / "realcredentials")
        source.symlink_to(self.work / "realcredentials", target_is_directory=True)
        self.refused()

    def test_happy_rerun_and_no_leak(self):
        before = [(p, p.read_bytes()) for p in (self.work / "hosts.yaml", self.work / "global.yaml")]
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        again = self.run_install(["-o", "json", *self.args])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(all(s["status"] == "unchanged" for s in json.loads(again.stdout)), again.stdout)
        output = result.stdout + result.stderr + again.stdout + again.stderr
        output += (self.work / "state/argv").read_text()
        for path, contents in before:
            self.assertEqual(path.read_bytes(), contents)
            output += path.read_text()
        for name in PRIVATE + ["iam-kek/active", "iam-kek/key1"]:
            self.assertNotIn((self.work / "credentials" / name).read_text().strip(), output)
        log = (self.work / "state/argv").read_text()
        self.assertLess(log.index("etcd/check-ready"), log.index("check-global-ready"))

    def test_readiness_failure(self):
        for probe in ("etcd/check-ready", "check-global-ready api"):
            with self.subTest(probe=probe):
                (self.work / "state/argv").unlink(missing_ok=True)
                self.env["INSTALL_FAIL"] = probe
                result = self.run_install()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("lab-1:", result.stderr)
                self.assertIn("readiness failed", result.stderr)
                log = (self.work / "state/argv").read_text()
                self.assertNotIn("fixture-private", result.stdout + result.stderr + log)

    def test_interrupted_install_restarts_services(self):
        self.assertEqual(self.run_install().returncode, 0)
        (self.work / "state/pending").touch()
        (self.work / "state/argv").unlink()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl restart vws-etcd", (self.work / "state/argv").read_text())
        self.assertFalse((self.work / "state/pending").exists())

    def test_lost_write_reply_still_restarts_services(self):
        self.assertEqual(self.run_install().returncode, 0)
        (self.work / "credentials/admin-kek").write_text("replacement-private-fixture")
        self.env["INSTALL_LOST"] = "/etc/vws/credentials/admin-kek"
        (self.work / "state/argv").unlink()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl restart vws-etcd", (self.work / "state/argv").read_text())
        self.assertNotIn("replacement-private-fixture", result.stdout + result.stderr)

    def test_unconfigured_package_is_installed_again(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.env["INSTALL_PACKAGE_STATE"] = "iU "
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("vws-global package changed", result.stdout)
        self.env.pop("INSTALL_PACKAGE_STATE")
        again = self.run_install(["-o", "json", *self.args])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(all(s["status"] == "unchanged" for s in json.loads(again.stdout)))

    def use(self, text):
        (self.work / "hosts.yaml").write_text("profile: lab\nhosts:\n" + text)

    GLOBAL = "  - name: lab-1\n    address: 192.0.2.1\n    roles: [etcd, global%s]\n"

    def on_global(self, regions="pve-seoul-1:8443"):
        entries = "".join(f"      - name: {r.split(':')[0]}\n        port: {r.split(':')[1]}\n"
                          for r in regions.split())
        self.use(self.GLOBAL % ", region" + "    regions:\n" + entries)

    def rows(self, result):
        return [line.split(" ", 2) for line in result.stdout.splitlines()]

    def test_region_instance_on_the_global_host(self):
        source = self.remove_generated()
        self.on_global()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        # One run is enough: the instance runs and says it is not registered yet.
        self.assertIn("lab-1 vws-region@pve-seoul-1 registration unregistered\n", result.stdout)
        self.assertIn("lab-1 vws-region package changed\n", result.stdout)
        self.assertIn("lab-1 vws-region@pve-seoul-1 changed\n", result.stdout)
        log = (self.work / "state/argv").read_text()
        commands = "\n".join(json.loads(line)[-1] for line in log.splitlines())
        payload = (self.work / "state/payload").read_bytes()
        for path, mode in (("/etc/vws-region/pve-seoul-1.env", "644"), ("/etc/vws-region/pve-seoul-1/region.yaml", "644"),
                           ("/etc/vws-region/pve-seoul-1/region.crt", "644"),
                           ("/etc/vws-region/pve-seoul-1/global-ca.pem", "644"),
                           ("/etc/vws-region/pve-seoul-1/region.key", "600")):
            self.assertIn(f'stat -c %u:%g:%a {path})" = 0:0:{mode}', commands, path)
        self.assertIn(b"VWS_REGION_ADDR=:8443\nVWS_REGION_OPS_ADDR=127.0.0.1:9443\n", payload)
        self.assertIn((ROOT / "packaging/vws-region/region.yaml").read_bytes(), payload)
        folder = source / "regions/pve-seoul-1"
        certificate, key = folder / "region.crt", folder / "region.key"
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        # The certificate covers the host address, loopback and the name, serves only, and chains to the CA
        # the global layer trusts and the instance uses for its clients.
        shown = subprocess.run(["openssl", "x509", "-in", certificate, "-noout", "-ext",
                                "subjectAltName,extendedKeyUsage"], capture_output=True, text=True).stdout
        self.assertIn("IP Address:192.0.2.1, IP Address:127.0.0.1, DNS:pve-seoul-1", shown)
        self.assertIn("TLS Web Server Authentication", shown)
        self.assertNotIn("Client", shown)
        for check in (["-verify_ip", "192.0.2.1"], ["-verify_ip", "127.0.0.1"], ["-verify_hostname", "pve-seoul-1"]):
            subprocess.run(["openssl", "verify", "-CAfile", source / "internal-ca.pem", *check, certificate],
                           check=True, capture_output=True)
        self.assertIn((source / "internal-ca.pem").read_bytes(), payload)
        # The memory budget of the region group applies to the instance.
        units = (self.work / "state/units").read_text()
        self.assertIn("vws-lab-region.slice.d/50-vws-memory.conf", units)
        self.assertIn("vws-region@pve-seoul-1.service.d/50-vws-memory.conf", units)
        # Secrets stay out of every output; the CA key never leaves the operator host.
        seen = result.stdout + result.stderr + log
        self.assertNotIn(key.read_text().strip(), seen)
        self.assertNotIn((source / "internal-tls/ca.key").read_bytes(), payload)
        # A rerun changes nothing and restarts nothing.
        before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        (self.work / "state/argv").unlink()
        again = self.run_install(["-o", "json", *self.args])
        self.assertEqual(again.returncode, 0, again.stderr)
        steps = json.loads(again.stdout)
        self.assertEqual([s for s in steps if s["status"] != "unchanged"],
                         [{"host": "lab-1", "step": "vws-region@pve-seoul-1 registration", "status": "unregistered"}])
        self.assertNotIn("restart", (self.work / "state/argv").read_text())
        self.assertEqual({str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}, before)

    def test_region_instance_on_a_separate_host(self):
        self.remove_generated()
        self.use(self.GLOBAL % "" + SEPARATE_HOST)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("region-1 vws-region@pve-busan-1 registration unregistered\n", result.stdout)
        # The global host gets no instance; the region host gets no global or etcd file, package or unit.
        self.assertNotIn("lab-1 vws-region", result.stdout)
        self.assertFalse([r for r in self.rows(result) if r[0] == "region-1"
                          and any(w in r[1] for w in ("vws-etcd", "vws-global", "vws-iam", "/etc/vws/"))])
        calls = [json.loads(line) for line in (self.work / "state/argv").read_text().splitlines()]
        region = [c for c in calls if "root@192.0.2.20" in c]
        self.assertTrue(region)
        text = "\n".join(c[-1] for c in region)
        for absent in ("/etc/vws/", "vws-etcd", "vws-global", "vws-iam"):
            self.assertNotIn(absent, text.replace("/etc/vws-region", ""), absent)
        self.assertIn("vws-lab-region.slice.d", text)
        self.assertNotIn("vws-lab-global.slice", text)
        source = self.work / "credentials"
        certificate = source / "regions/pve-busan-1/region.crt"
        shown = subprocess.run(["openssl", "x509", "-in", certificate, "-noout", "-ext", "subjectAltName"],
                               capture_output=True, text=True).stdout
        self.assertIn("IP Address:192.0.2.20, IP Address:127.0.0.1, DNS:pve-busan-1", shown)
        again = self.run_install(["-o", "json", *self.args])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual([s["status"] for s in json.loads(again.stdout) if s["status"] != "unchanged"],
                         ["unregistered"])

    def test_instances_on_both_hosts_and_two_on_one_host(self):
        self.remove_generated()
        self.use(self.GLOBAL % ", region" + "    regions:\n      - {name: pve-seoul-1, port: 8443}\n"
                 "      - {name: pve-seoul-2, port: 8444, ops_port: 9500}\n" + SEPARATE_HOST)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in ("lab-1 vws-region@pve-seoul-1 registration unregistered", "lab-1 vws-region@pve-seoul-2 registration unregistered",
                     "region-1 vws-region@pve-busan-1 registration unregistered", "region-1 vws-region package changed"):
            self.assertIn(line + "\n", result.stdout)
        payload = (self.work / "state/payload").read_bytes()
        self.assertIn(b"VWS_REGION_ADDR=:8444\nVWS_REGION_OPS_ADDR=127.0.0.1:9500\n", payload)
        # An edit of one instance's config restarts that instance only.
        (self.work / "regions").mkdir()
        (self.work / "regions/pve-seoul-2.yaml").write_text("read_only: true\ndefault_project: other\n")
        (self.work / "state/argv").unlink()
        again = self.run_install()
        self.assertEqual(again.returncode, 0, again.stderr)
        log = (self.work / "state/argv").read_text()
        self.assertIn("systemctl restart vws-region@pve-seoul-2", log)
        self.assertNotIn("restart vws-region@pve-seoul-1", log)
        self.assertNotIn("restart vws-region@pve-busan-1", log)

    def test_lost_write_reply_restarts_the_instance(self):
        self.remove_generated()
        self.on_global()
        self.assertEqual(self.run_install().returncode, 0)
        (self.work / "regions").mkdir()
        (self.work / "regions/pve-seoul-1.yaml").write_text("read_only: true\ndefault_project: lost\n")
        self.env["INSTALL_LOST"] = "/etc/vws-region/pve-seoul-1/region.yaml"
        (self.work / "state/argv").unlink()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl restart vws-region@pve-seoul-1", (self.work / "state/argv").read_text())

    def test_registered_instance_must_be_ready(self):
        self.remove_generated()
        self.on_global()
        self.assertEqual(self.run_install().returncode, 0)
        (self.work / "state/192.0.2.1-registered-pve-seoul-1").touch()
        result = self.run_install(["-o", "json", *self.args])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("registered", [s["status"] for s in json.loads(result.stdout)])
        self.env["INSTALL_FAIL"] = "/readyz"
        failed = self.run_install()
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertIn("lab-1: vws-region@pve-seoul-1 readiness failed", failed.stderr)
        self.assertNotIn("fixture-private", failed.stdout + failed.stderr)

    def test_region_inventory_boundaries(self):
        two = "      - {name: %s, port: %s%s}\n"
        on = self.GLOBAL % ", region" + "    regions:\n"
        used = "port is used by another instance or service on host lab-1"
        cases = (
            # a duplicate name, on one host and across hosts
            (on + two % ("pve-a-1", 8443, "") + two % ("pve-a-1", 8444, ""), "region name pve-a-1 appears twice"),
            (on + two % ("pve-a-1", 8443, "") + SEPARATE_HOST.replace("pve-busan-1", "pve-a-1"),
             "region name pve-a-1 appears twice"),
            # a duplicate port; an ops port that is another instance's port, or its own; the default ops port
            # (port + 1000) of one instance that is the port of another
            (on + two % ("pve-a-1", 8443, "") + two % ("pve-a-2", 8443, ""), used),
            (on + two % ("pve-a-1", 8443, "") + two % ("pve-a-2", 8444, ", ops_port: 8443"), used),
            (on + two % ("pve-a-1", 8443, ", ops_port: 8443"), used),
            (on + two % ("pve-a-1", 8443, "") + two % ("pve-a-2", 9443, ""), used),
            # a port of the global host's own services, below 1024, above 65535, or not a number
            (on + two % ("pve-a-1", 8083, ""), used),
            (on + two % ("pve-a-1", 2379, ""), used),
            (on + two % ("pve-a-1", 443, ""), "ports must be 1024 to 65535"),
            (on + two % ("pve-a-1", 65536, ""), "ports must be 1024 to 65535"),
            (on + two % ("pve-a-1", 64900, ""), "ports must be 1024 to 65535"),
            (on + two % ("pve-a-1", "'8443;x'", ""), "ports must be 1024 to 65535"),
            (on + "      - {name: pve-a-1}\n", "ports must be 1024 to 65535"),
            # a name the unit refuses
            *((on + two % (n, 8443, ""), "invalid region name")
              for n in ("seoul-1", "pve-Seoul-1", "pve-seoul", "pve-seoul-1-", "'pve-a-1;x'", "pve-" + "a" * 60 + "-1")),
            # the role and the list go together
            (self.GLOBAL % ", region", "the region role and regions go together"),
            (self.GLOBAL % "" + "    regions:\n" + two % ("pve-a-1", 8443, ""), "the region role and regions go together"),
            (self.GLOBAL % "" + SEPARATE_HOST.replace("    regions:\n      - name: pve-busan-1\n        port: 8443\n", ""),
             "the region role and regions go together"),
            # one global host, unique hosts and addresses, known roles, no extra keys
            (self.GLOBAL % "" + SEPARATE_HOST.replace("roles: [region]", "roles: [etcd, global, region]"),
             "lab inventory requires one global host"),
            (self.GLOBAL % "" + SEPARATE_HOST.replace("192.0.2.20", "192.0.2.1"), "host name and address must be unique"),
            (self.GLOBAL % "" + SEPARATE_HOST.replace("region-1", "lab-1"), "host name and address must be unique"),
            (SEPARATE_HOST, "lab inventory requires one global host"),
            (self.GLOBAL % "" + SEPARATE_HOST.replace("roles: [region]", "roles: [etcd, region]"), "invalid lab host"),
            (self.GLOBAL % "" + SEPARATE_HOST.replace("      - name: pve-busan-1", "      - name: pve-busan-1\n        token: x"),
             "forbidden key"))
        for bad, message in cases:
            with self.subTest(bad=bad):
                self.use(bad)
                self.assertIn(message, self.refused().stderr)
                self.assertFalse((self.work / "credentials/regions").exists())

    def test_same_port_on_two_hosts_is_allowed(self):
        self.remove_generated()
        self.use(self.GLOBAL % ", region" + "    regions:\n      - {name: pve-seoul-1, port: 8443}\n" + SEPARATE_HOST)
        self.assertEqual(self.run_install().returncode, 0)

    def region_leaf(self, other, folder, extensions):
        """Write a leaf signed by the CA in `other` with these extensions as the region pair in `folder`."""
        (other / "leaf.ext").write_text(extensions)
        for command in (["req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                         "-subj", "/CN=pve-seoul-1", "-keyout", str(other / "leaf.key"), "-out", str(other / "leaf.csr")],
                        ["x509", "-req", "-in", str(other / "leaf.csr"), "-CA", str(other / "ca.crt"),
                         "-CAkey", str(other / "ca.key"), "-days", "30", "-set_serial", "0x" + os.urandom(8).hex(),
                         "-extfile", str(other / "leaf.ext"), "-out", str(other / "leaf.crt")]):
            subprocess.run(["openssl", *command], check=True, capture_output=True)
        (folder / "region.crt").write_bytes((other / "leaf.crt").read_bytes())
        (folder / "region.key").write_bytes((other / "leaf.key").read_bytes())

    def test_supplied_region_certificate_is_used_and_checked(self):
        source = self.work / "credentials"
        self.on_global()
        # No retained CA key and no certificate: nothing can be issued.
        result = self.refused()
        self.assertIn("regions/pve-seoul-1/ is missing and internal-tls/ holds no key", result.stderr)
        # An operator pair under internal-ca.pem is installed as it is.
        other = self.work / "other"
        subprocess.run(["bash", str(ROOT / "packaging/vws-etcd/vws-etcd-tls"), str(other), "pve-seoul-1=192.0.2.1"],
                       check=True, capture_output=True)
        folder = source / "regions/pve-seoul-1"
        folder.mkdir(parents=True)
        for name, src in (("region.crt", "pve-seoul-1/member.crt"), ("region.key", "pve-seoul-1/member.key")):
            (folder / name).write_bytes((other / src).read_bytes())
        # A certificate of another CA is refused: the global layer would not trust it.
        result = self.refused()
        self.assertIn("region.crt is not issued under internal-ca.pem", result.stderr)
        (source / "internal-ca.pem").write_bytes((other / "ca.crt").read_bytes())
        # Regionapi authenticates clients against this CA, so only a server-only leaf is accepted. The etcd
        # member certificate (serverAuth and clientAuth), no EKU, anyExtendedKeyUsage, a CA and a missing
        # basicConstraints are refused before SSH with one fixed diagnostic.
        san = "subjectAltName=IP:192.0.2.1,IP:127.0.0.1,DNS:pve-seoul-1\n"
        fixed = ("vws: regions/pve-seoul-1/region.crt must be a server-only leaf: extendedKeyUsage exactly serverAuth "
                 "(no clientAuth or anyExtendedKeyUsage) and basicConstraints CA:FALSE. Reissue it under "
                 "internal-ca.pem as a server certificate, or delete regions/pve-seoul-1/ to issue it from the CA "
                 "kept in internal-tls/\n")
        self.assertEqual(self.refused().stderr, fixed)
        bc = "basicConstraints=critical,CA:FALSE\n"
        for label, ext in (("no EKU", bc + san),
                           ("clientAuth only", bc + "extendedKeyUsage=clientAuth\n" + san),
                           ("dual use", bc + "extendedKeyUsage=serverAuth,clientAuth\n" + san),
                           ("anyExtendedKeyUsage", bc + "extendedKeyUsage=serverAuth,anyExtendedKeyUsage\n" + san),
                           ("CA:TRUE", "basicConstraints=critical,CA:TRUE\nextendedKeyUsage=serverAuth\n" + san),
                           ("no basicConstraints", "extendedKeyUsage=serverAuth\n" + san)):
            with self.subTest(label):
                self.region_leaf(other, folder, ext)
                self.assertEqual(self.refused().stderr, fixed)
        self.region_leaf(other, folder, bc + "extendedKeyUsage=serverAuth\n" + san)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual((folder / "region.crt").read_bytes(), (other / "leaf.crt").read_bytes())
        # A key that is not the certificate's is refused.
        subprocess.run(["bash", str(ROOT / "packaging/vws-etcd/vws-etcd-tls"), str(other), "pve-seoul-2=192.0.2.1"],
                       check=True, capture_output=True)
        (folder / "region.key").write_bytes((other / "pve-seoul-2/member.key").read_bytes())
        self.assertIn("are not one key pair", self.refused_after_install().stderr)

    def test_region_certificate_is_reissued_for_a_new_address(self):
        source = self.remove_generated()
        self.use(self.GLOBAL % "" + SEPARATE_HOST)
        self.assertEqual(self.run_install().returncode, 0)
        ca = (source / "internal-ca.pem").read_bytes()
        old = (source / "regions/pve-busan-1/region.crt").read_bytes()
        hosts = self.work / "hosts.yaml"
        hosts.write_text(hosts.read_text().replace("192.0.2.20", "192.0.2.21"))
        result = self.refused_after_install()
        self.assertEqual(result.stderr, "vws: regions/pve-busan-1/region.crt does not name 192.0.2.21, 127.0.0.1 and "
                         "pve-busan-1. To reissue it from the CA kept in internal-tls/, delete regions/pve-busan-1/ "
                         "and run again\n")
        # Deleting the folder, as the message says, keeps the CA and names the new address.
        shutil.rmtree(source / "regions/pve-busan-1")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual((source / "internal-ca.pem").read_bytes(), ca)
        self.assertNotEqual((source / "regions/pve-busan-1/region.crt").read_bytes(), old)
        subprocess.run(["openssl", "verify", "-CAfile", source / "internal-ca.pem", "-verify_ip", "192.0.2.21",
                        source / "regions/pve-busan-1/region.crt"], check=True, capture_output=True)

    def test_interrupted_certificate_issue_leaves_no_half_pair(self):
        source = self.remove_generated()
        self.on_global()
        (self.work / "bin/openssl").write_text("#!/bin/sh\ncase \"$*\" in *region.ext*) exit 1;; esac\nexec "
                                               + shutil.which("openssl") + ' "$@"\n')
        (self.work / "bin/openssl").chmod(0o755)
        self.assertEqual(self.refused().stderr, "vws: cannot issue the certificate of pve-seoul-1; openssl is required\n")
        self.assertFalse((source / "regions/pve-seoul-1").exists())
        # A staged directory left by a crash does not block the next run.
        (source / "regions/.vws-install.crash").mkdir()
        (self.work / "bin/openssl").unlink()
        self.assertEqual(self.run_install().returncode, 0)
        self.assertTrue((source / "regions/pve-seoul-1/region.key").is_file())

    def test_region_config_comes_from_beside_the_inventory(self):
        self.remove_generated()
        self.on_global()
        (self.work / "regions").mkdir()
        (self.work / "regions/pve-seoul-1.yaml").write_text("read_only: true\ndefault_project: seoul\n")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertIn(b"default_project: seoul\n", (self.work / "state/payload").read_bytes())
        for bad in ("[unbalanced", "- a\n- b\n", "x: " + "y" * 70000):
            with self.subTest(bad=bad[:20]):
                (self.work / "regions/pve-seoul-1.yaml").write_text(bad)
                self.refused_after_install()
        (self.work / "regions/pve-seoul-1.yaml").unlink()
        (self.work / "regions/pve-seoul-1.yaml").symlink_to(self.work / "global.yaml")
        self.refused_after_install()

    def refused_after_install(self):
        calls = (self.work / "state/argv").read_text()
        result = self.run_install()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.work / "state/argv").read_text(), calls)
        return result

    def test_operator_docs_describe_no_per_node_layout(self):
        import re
        for path in sorted((ROOT / "docs/operator").glob("*.md")):
            text = path.read_text()
            self.assertIsNone(re.search(r"keepalived|\bVIP\b|every PVE node|each PVE node", text, re.I), path.name)


# The remote shell of one step, run for real with sh against a fake systemctl that keeps unit state in files.
SYSTEMCTL = r"""#!/bin/sh
printf '%s\n' "$*" >> "$UNITS/calls"
verb=$1
shift
test "$1" = --quiet && shift
case "$verb" in
    is-enabled|is-active) test -f "$UNITS/$verb-$1" ;;
    enable) touch "$UNITS/is-enabled-$1" ;;
    start|restart) touch "$UNITS/is-active-$1" ;;
    *) exit 1 ;;
esac
"""


class RemoteShell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR", ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.units = Path(self.tmp.name)
        (self.units / "systemctl").write_text(SYSTEMCTL)
        (self.units / "systemctl").chmod(0o755)
        sys.path.insert(0, str(ROOT / "scripts/vws.d/lib"))
        self.addCleanup(sys.path.remove, str(ROOT / "scripts/vws.d/lib"))
        import install
        env = dict(os.environ, PATH=str(self.units) + ":" + os.environ["PATH"], UNITS=str(self.units))
        original = install.local
        install.local = lambda args, data=None: subprocess.run(["sh", "-c", args[-1]], input=data or b"",
                                                               capture_output=True, env=env, timeout=30)
        self.addCleanup(setattr, install, "local", original)
        self.steps = []
        self.target = install.Target({"name": "lab-1", "address": "192.0.2.1"}, "json", self.steps)

    def calls(self):
        return (self.units / "calls").read_text().splitlines()

    def test_active_disabled_unit_restarts_for_a_change(self):
        # A running instance that was disabled by hand keeps running; a changed config must restart it.
        unit = "vws-region@pve-seoul-1"
        (self.units / ("is-active-" + unit)).touch()
        self.target.service(unit, changed=True)
        self.assertIn("enable " + unit, self.calls())
        self.assertIn("restart " + unit, self.calls())
        self.assertTrue((self.units / ("is-enabled-" + unit)).exists())
        self.assertEqual(self.steps[-1]["status"], "changed")

    def test_unit_state_converges(self):
        unit = "vws-iam"
        self.target.service(unit)
        self.assertEqual([c for c in self.calls() if "--quiet" not in c], ["enable " + unit, "start " + unit])
        # Enabled and running with nothing changed: nothing restarts.
        (self.units / "calls").unlink()
        self.target.service(unit)
        self.assertNotIn("restart " + unit, self.calls())
        self.assertEqual(self.steps[-1]["status"], "unchanged")
        # Enabled but stopped: it starts, without a restart.
        (self.units / ("is-active-" + unit)).unlink()
        self.target.service(unit, changed=True)
        self.assertIn("start " + unit, self.calls())
        self.assertNotIn("restart " + unit, self.calls())


if __name__ == "__main__":
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(case) for case in (Install, RemoteShell))
    result = unittest.TextTestRunner().run(suite)
    print(f"{result.testsRun} cases, {len(result.failures) + len(result.errors)} failed")
    raise SystemExit(not result.wasSuccessful())
