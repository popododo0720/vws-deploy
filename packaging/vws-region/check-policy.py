#!/usr/bin/env python3
"""Check command permissions; visudo checks syntax, not least privilege."""
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
helper = "/usr/lib/vws/vws-region-helper"
expected = []
for operation in ("registration-write", "token-write", "token-read", "lease-write",
                  "lock *", "unlock *", "snippet-write *", "snippet-delete *"):
    expected.append(f"vws-region ALL=(root) NOPASSWD: {helper} {operation}")
actual = [line.strip() for line in path.read_text().splitlines()
          if line.strip() and not line.lstrip().startswith("#")]
if actual != expected:
    sys.exit(f"{path}: sudoers must allow only the fixed helper operations")
print(f"{path}: fixed helper policy OK")
