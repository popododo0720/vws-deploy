"""verify-queue.yml is vendored byte for byte into every vws-* repo, so .github/drift/vendored.tsv pins its
sha256 (vws-deploy#439 class 4). Like scripts/guard-agents.sh.sha256 (R13), a change to the file must update
the pin in the same commit. This runs in scripts/guard.sh R13 on every pull request."""
import hashlib
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VQ = ".github/workflows/verify-queue.yml"


class VendoredPin(unittest.TestCase):
    def test_verify_queue_matches_its_pin(self):
        with open(os.path.join(ROOT, VQ), "rb") as f:
            got = hashlib.sha256(f.read()).hexdigest()
        rows = [ln.split("\t") for ln in open(os.path.join(ROOT, ".github", "drift", "vendored.tsv"))
                if ln.strip() and not ln.startswith("#")]
        want = [r[2] for r in rows if r[0] == VQ and r[1] == "*"]
        self.assertEqual(want, [got], "update .github/drift/vendored.tsv: %s\t*\t%s" % (VQ, got))


if __name__ == "__main__":
    unittest.main()
