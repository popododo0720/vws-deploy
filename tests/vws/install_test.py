#!/usr/bin/env python3
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from install_fixture import prepare, PRIVATE

ROOT = Path(__file__).resolve().parents[2]
SSH = r"""#!/usr/bin/env python3
import hashlib, json, os, pathlib, shlex, sys
s = pathlib.Path(os.environ["INSTALL_STATE"])
c = shlex.split(sys.argv[-1])[-1]
d = sys.stdin.buffer.read()
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
    name = next(n for n in ("vws-etcd", "vws-global") if n in c)
    (s / "current-package").write_text(name)
    if (s / name).exists():
        status = os.environ.get("INSTALL_PACKAGE_STATE", "ii ") if name == "vws-global" else "ii "
        print(status + "\t0.0.0-1", end="")
elif "apt-get install" in c:
    (s / (s / "current-package").read_text()).touch()
    (s / "pending").touch()
elif "systemctl is-enabled" in c:
    key = s / ("service-" + c.split("is-enabled --quiet ")[1].split()[0])
    print("changed" if not key.exists() or "systemctl restart" in c else "unchanged")
    key.touch()
elif "echo changed" in c:
    key = s / hashlib.sha256(c.encode() + d).hexdigest()
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
            self.assertIn(f"systemctl enable --now {unit} ", log)

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

    def test_region_role_and_vip_are_refused(self):
        """The per-node region layout is gone: the lab path installs no vws-region and no keepalived."""
        path = self.work / "hosts.yaml"
        good = path.read_text()
        for bad in (good.replace("roles: [etcd, global]", "roles: [etcd, global, region]")
                    + "vip:\n  address: 192.0.2.10/24\n", good + "vip:\n  address: 192.0.2.10/24\n"):
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


if __name__ == "__main__":
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Install))
    print(f"{result.testsRun} cases, {len(result.failures) + len(result.errors)} failed")
    raise SystemExit(not result.wasSuccessful())
