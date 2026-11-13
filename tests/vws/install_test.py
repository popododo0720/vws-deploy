#!/usr/bin/env python3
import json
import os
from pathlib import Path
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
    name = next(n for n in ("vws-etcd", "vws-global", "vws-region") if n in c)
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
                    good.replace("roles: [etcd, global, region]", "roles: [etcd]"),
                    good + "password: hidden\n", good + "---\nprofile: lab\n",
                    good.replace("roles: [etcd, global, region]", "roles: [{secret: hidden}]"),
                    "profile: lab\nhosts: &loop [*loop]\n",
                    good.replace("name: lab-1", "? [bad, key]\n    : value"),
                    good.replace("roles: [etcd, global, region]", "roles: [etcd, global, region, region]")):
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
        for group, mib in (("global", 1024), ("region", 512), ("etcd", 512)):
            slice_name = f"vws-lab-{group}.slice"
            self.assertTrue(any(f"/{slice_name}.d/50-vws-memory.conf" in row["command"]
                                and f"MemoryMax={mib}M\nMemorySwapMax=0\n" in row["data"]
                                for row in units), group)
        for service, group in (("vws-iam", "global"), ("vws-aws-proxy", "global"),
                               ("vws-console-api", "global"), ("vws-console", "global"),
                               ("vws-region", "region"), ("vws-etcd", "etcd"),
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
        path.write_text(path.read_text() + "memory_mib: {global: 768, region: 512, etcd: 256}\n")
        self.env["INSTALL_FAIL"] = "systemctl daemon-reload"
        result = self.run_install()
        self.assertEqual(result.returncode, 1, result.stderr)
        log = (self.work / "state/argv").read_text()
        self.assertNotIn("enable --now keepalived", log)
        self.env.pop("INSTALL_FAIL")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MemoryMax=768M", (self.work / "state/units").read_text())
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
        self.assertNotIn("enable --now keepalived", log)

    def test_member_name_boundaries(self):
        path = self.work / "hosts.yaml"
        good = path.read_text()
        for name in ("a", "0", "a" * 63):
            with self.subTest(name=name):
                path.write_text(good.replace("name: lab-1", "name: " + name))
                result = self.run_install()
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_credential(self):
        (self.work / "credentials/admin-kek").unlink()
        result = self.refused()
        self.assertEqual(result.stderr, "vws: required regular file: admin-kek\n")

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
        self.assertIn("keepalived changed", result.stdout)
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
        self.assertLess(log.index("check-global-ready"), log.index("check-region-ready"))

    def test_readiness_failure(self):
        for probe in ("etcd/check-ready", "check-global-ready api", "check-region-ready"):
            with self.subTest(probe=probe):
                (self.work / "state/argv").unlink(missing_ok=True)
                self.env["INSTALL_FAIL"] = probe
                result = self.run_install()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("lab-1:", result.stderr)
                self.assertIn("readiness failed", result.stderr)
                log = (self.work / "state/argv").read_text()
                self.assertNotIn("enable --now keepalived", log)
                self.assertIn("disable --now keepalived", log)
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
