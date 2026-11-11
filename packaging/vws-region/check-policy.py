#!/usr/bin/env python3
"""Check command permissions; visudo checks syntax, not least privilege."""
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
helper = "/usr/lib/vws/vws-region-helper"
expected = []
for operation in ("registration-write", "token-write", "token-read", "lease-write"):
    expected.append(f"vws-region ALL=(root) NOPASSWD: {helper} {operation}")
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
    valid = ("a" * 64 if verb in ("lock", "unlock")
             else "vws-01234567-89ab-cdef-0123-456789abcdef-user.yaml")
    assert re.fullmatch(arguments, f"{verb} {valid}"), arguments
    for invalid in ("", "../escape", "valid extra", "wrong.yaml"):
        assert re.fullmatch(arguments, f"{verb} {invalid}") is None, invalid
print(f"{path}: fixed helper policy OK")
