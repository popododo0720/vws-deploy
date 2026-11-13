#!/usr/bin/env python3
"""Check helper permissions and PVE token references in shipped configuration."""
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
helper = "/usr/lib/vws/vws-region-helper"
expected = [f"vws-region ALL=(root) NOPASSWD: {helper} registration-write"]
for operation in ("token-write", "token-read", "token-delete"):
    expected.append(f"vws-region ALL=(root) NOPASSWD: {helper} ^{operation} [0-9a-f]{{64}}$")
expected.append(f"vws-region ALL=(root) NOPASSWD: {helper} lease-write")
for operation in ("lock", "unlock", "snippet-write", "snippet-delete"):
    name = (r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}" if operation in ("lock", "unlock")
            else r"vws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-user[.]yaml")
    expected.append(f"vws-region ALL=(root) NOPASSWD: {helper} ^{operation} {name}$")
actual = [line.strip() for line in path.read_text().splitlines()
          if line.strip() and not line.lstrip().startswith("#")]
for line in actual:
    if f"{helper} " not in line:
        continue
    arguments = line.split(f"{helper} ", 1)[1]
    anchored = arguments.startswith("^") and arguments.endswith("$")
    if not anchored and (arguments.startswith("^") or arguments.endswith("$")
                         or any(char in arguments for char in "*?[")):
        sys.exit(f"{path}: helper argument patterns must be anchored regexes")
if actual != expected:
    sys.exit(f"{path}: sudoers must allow only the fixed helper operations")
for line in expected:
    arguments = line.split(f"{helper} ", 1)[1]
    if not arguments.startswith("^"):
        continue
    verb = arguments.split(" ", 1)[0][1:]
    valid = ("a" * 64 if verb in ("lock", "unlock", "token-write", "token-read", "token-delete")
             else "vws-01234567-89ab-cdef-0123-456789abcdef-user.yaml")
    assert re.fullmatch(arguments, f"{verb} {valid}"), arguments
    for invalid in ("", "../escape", "valid extra", "wrong.yaml"):
        assert re.fullmatch(arguments, f"{verb} {invalid}") is None, invalid
packaging = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path(__file__).resolve().parent.parent
# guard.sh secrets already rejects the authorization prefix in every tracked file.
token = re.compile(
    r"pve-token|/etc/pve/priv/vws/|"
    r"[^\s@=!]+@[^\s@=!]+![^\s@=!]+="
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)
suffixes = {".service", ".target", ".socket", ".mount", ".swap", ".timer",
            ".path", ".slice", ".scope", ".automount", ".device", ".conf", ".yaml"}
for candidate in sorted(packaging.rglob("*")):
    if candidate.is_file() and candidate.suffix in suffixes:
        for number, line in enumerate(candidate.read_text().splitlines(), 1):
            if token.search(line):
                sys.exit(f"{candidate}:{number}: packaged configuration must not contain a PVE token or token path")
print(f"{path}: fixed helper policy and packaged PVE token policy OK")
