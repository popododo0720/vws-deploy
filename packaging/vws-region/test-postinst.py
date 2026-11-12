#!/usr/bin/env python3
"""Exercise postinst with filesystem paths redirected into a disposable fixture."""
import os
import pathlib
import shutil
import subprocess
import sys
import unittest

source = pathlib.Path(sys.argv[1]).read_text()
root = pathlib.Path(sys.argv[2]) / "postinst"
root.mkdir()
pve = root / "pve"
pve.mkdir()
credentials = root / "credentials"
script = root / "postinst"
script.write_text(source.replace("/etc/pve/vws", str(pve / "vws"))
                  .replace("/etc/vws/credentials", str(credentials))
                  .replace("/run/systemd/system", str(root / "no-systemd")))
bin_dir = root / "bin"
bin_dir.mkdir()
for name, body in {
    "getent": "exit 0",
    "adduser": "exit 1",
    "install": 'for arg do :; done\nexec mkdir -p "$arg"',
    "chmod": "exit 1",
    "chown": "exit 1",
}.items():
    command = bin_dir / name
    command.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
    command.chmod(0o755)
mounts = root / "mounts"
env = {key: value for key, value in os.environ.items()
       if key not in ("VWS_REGION_TEST", "VWS_REGION_TEST_MOUNTS")}
env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]


def configure(extra):
    return subprocess.run(["sh", str(script), "configure"], env={**env, **extra},
                          capture_output=True, text=True, timeout=10)


fixture = {"VWS_REGION_TEST": "1", "VWS_REGION_TEST_MOUNTS": str(mounts)}


class PostinstTests(unittest.TestCase):
    def setUp(self):
        mounts.write_text("/dev/fuse /etc/pve fuse rw,nosuid,nodev 0 0\n")

    def tearDown(self):
        lease = pve / "vws"
        if lease.is_symlink():
            lease.unlink()
        elif lease.exists():
            shutil.rmtree(lease)

    def test_non_pmxcfs_unchanged(self):
        for line in ("", "/dev/fuse /etc/pve ext4 rw 0 0\n",
                     "none /etc/pve fuse rw 0 0\n",
                     "/dev/fuse /etc/pve/sub fuse rw 0 0\n",
                     "/dev/fuse /etc/pve\\040other fuse rw 0 0\n",
                     "/dev/fuse /other\\040path fuse rw 0 0\n"):
            with self.subTest(line=line):
                mounts.write_text(line)
                result = configure(fixture)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((result.stdout, result.stderr), ("", ""))
                self.assertFalse((pve / "vws").exists())

    def test_override_requires_explicit_test_mode(self):
        for extra in ({"VWS_REGION_TEST_MOUNTS": str(mounts)},
                      {"VWS_REGION_TEST": "0", "VWS_REGION_TEST_MOUNTS": str(mounts)},
                      {"VWS_REGION_TEST": "1", "VWS_REGION_TEST_MOUNTS": ""}):
            with self.subTest(extra=extra):
                result = configure(extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("mounts override requires", result.stderr)
                self.assertFalse((pve / "vws").exists())

    def test_pmxcfs_creation_and_restart(self):
        result = configure(fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((pve / "vws").is_dir())
        sentinel = pve / "vws" / "lease.json"
        sentinel.write_text("lease sentinel\n")
        before = (sentinel.read_bytes(), sentinel.stat())
        result = configure(fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, (sentinel.read_bytes(), sentinel.stat()))

    def test_symlink_refused(self):
        target = root / "target"
        target.mkdir()
        (pve / "vws").symlink_to(target, target_is_directory=True)
        result = configure(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be a symlink", result.stderr)
        self.assertEqual(list(target.iterdir()), [])


unittest.main(argv=[sys.argv[0]], verbosity=2)
