#!/usr/bin/env python3
"""Reject unsafe helper grants and packaged PVE token references."""
import pathlib
import subprocess
import sys

checker, sudoers = sys.argv[1:3]
root = pathlib.Path(sys.argv[3]) / "policy"
root.mkdir()
policy = pathlib.Path(sudoers).read_text()
result = subprocess.run(["python3", checker, sudoers, str(root)],
                        capture_output=True, text=True, timeout=10)
assert result.returncode == 0, (result.stdout, result.stderr)
for verb in ("token-write", "token-read", "token-delete"):
    arguments = f"^{verb} [0-9a-f]{{64}}$"
    assert arguments in policy, f"missing fingerprint policy for {verb}"
    candidate = root / "sudoers"
    for invalid in (verb, f"{verb} *", f"^{verb} *$", f"{verb} [0-9a-f]{{64}}",
                    arguments[1:], arguments[:-1], f"^{verb} [0-9a-f]{{63}}$"):
        candidate.write_text(policy.replace(arguments, invalid))
        result = subprocess.run(["python3", checker, str(candidate), str(root)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode != 0, f"unsafe token policy accepted: {invalid}"
        assert str(candidate) in result.stderr, result.stderr
    candidate.unlink()
print("token sudoers policy: exact fingerprint grants accepted; 21 unsafe grants rejected")
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
