#!/usr/bin/env python3
"""Seed shipped configuration with references the repository secret guard misses."""
import pathlib
import subprocess
import sys

checker, sudoers = sys.argv[1:3]
root = pathlib.Path(sys.argv[3]) / "policy"
root.mkdir()
tokens = ("user@pve!region=" + "01234567-89ab-cdef-0123-456789abcdef",
          "pve-token", "/etc/pve/priv/vws/")
failures = []
for name in ("nested/other.service", "other.target", "other.socket",
             "other.service.d/override.conf", "keepalived.conf", "region.yaml"):
    candidate = root / name
    candidate.parent.mkdir(parents=True, exist_ok=True)
    for token in tokens:
        candidate.write_text("# reference: " + token + "\n")
        result = subprocess.run(["python3", checker, sudoers, str(root)],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0 or str(candidate) not in result.stderr:
            failures.append(f"{name}: forbidden reference accepted ({token})")
        elif token in result.stderr:
            failures.append(f"{name}: diagnostic exposed the forbidden value")
    candidate.unlink()
assert not failures, "\n".join(failures)
print("packaged token policy: 18 negative cases rejected without exposing values")
