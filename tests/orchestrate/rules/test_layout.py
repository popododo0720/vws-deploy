"""The guard paths cover the rules/ layout (vws-deploy#501 AC6).

- Rule D's SERIAL_MERGE_RE: a PR that touches only .github/orchestrate/rules/<x>.py merges one at a time
  across repos, as docs/factory-orchestrator.md says (everything that was orchestrate.py stays serial);
- the scripts/guard-agents.sh comments that point at orchestrate.py name helpers orch still has;
- CODEOWNERS: none in this repo; if one is added it must cover rules/;
- trusted_read_lint.py checks every rules/*.py, and orchestrate-update.py syntax-checks them before a
  fast-forward.
Run: python3 -m unittest discover -s tests/orchestrate"""
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest

from orch_testlib import ROOT, orch
from rules.rules_testlib import read
from test_merge import merges, reasons, run, snap
from test_merge_order import TWO, pr

RULE_FILE = ".github/orchestrate/rules/merge.py"


def load(name, rel):
    """Load a script as a module without writing __pycache__ next to it (test_status_issue reads box/*)."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    return mod


class SerialMerge(unittest.TestCase):
    def test_rule_modules_are_serial_paths(self):
        for path in (RULE_FILE, ".github/orchestrate/rules/common.py", ".github/orchestrate/rules/__init__.py",
                     ".github/orchestrate/rules/new_rule.py", ".github/orchestrate/orchestrate.py"):
            self.assertTrue(orch.serial_merge_pr({"files": [path]}), path)
        for path in (".github/orchestrate/rules/sub/x.py", ".github/orchestrate/rules/README.md",
                     "tests/orchestrate/rules/test_merge.py", ".github/orchestrate/trusted_read.py"):
            self.assertFalse(orch.serial_merge_pr({"files": [path]}), path)

    def test_a_pr_touching_only_a_rule_module_merges_one_at_a_time(self):
        a = pr("vws-deploy", 500, [RULE_FILE], 1)
        b = pr("vws-iam", 600, ["AGENTS.md"], 3)
        r = run(snap(a, b, merge=TWO))
        self.assertEqual(merges(r), ["vws-iam#600"])
        self.assertEqual(reasons(r, "vws-deploy#500"), ["serial_after:vws-iam#600"])

    def test_the_docs_describe_the_same_serial_set(self):
        docs = read("docs/factory-orchestrator.md")
        row = next(line for line in docs.split("\n") if line.startswith("| serial set (vws-deploy#397)"))
        for path in ("`.github/orchestrate/orchestrate.py`", "`.github/orchestrate/rules/*.py`", "`AGENTS.md`",
                     "`.github/workflows/repo-guard*.yml`"):
            self.assertIn(path, row)


class GuardAgents(unittest.TestCase):
    def test_orchestrate_references_resolve(self):
        text = read("scripts/guard-agents.sh")
        flat = re.sub(r"\n\s*#\s*", " ", text)
        names = set(re.findall(r"(\w+) in \.github/orchestrate/orchestrate\.py", flat))
        names |= set(re.findall(r"\.github/orchestrate/orchestrate\.py (\w+) is its", flat))
        self.assertEqual(names, {"PART_OF_RE", "parse_ac", "visible_text"})
        for name in names:
            self.assertTrue(hasattr(orch, name), name)
        for path in set(re.findall(r"\.github/orchestrate/[\w./-]+\.py", text)):
            self.assertTrue(os.path.exists(os.path.join(ROOT, path)), path)


def codeowners_cover(text, path):
    """True when a CODEOWNERS line with an owner has a pattern that matches path (prefix or glob)."""
    for line in text.split("\n"):
        parts = line.split("#", 1)[0].split()
        if len(parts) < 2:
            continue
        pat = parts[0].lstrip("/")
        rx = "^" + re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + ("" if pat.endswith("/") else "(/|$)")
        if pat == "*" or re.match(rx, path):
            return True
    return False


class CodeOwners(unittest.TestCase):
    def test_the_matcher(self):
        self.assertTrue(codeowners_cover("/.github/orchestrate/ @o\n", RULE_FILE))
        self.assertTrue(codeowners_cover(".github/orchestrate/rules/*.py @o\n", RULE_FILE))
        self.assertFalse(codeowners_cover(".github/orchestrate/orchestrate.py @o\n", RULE_FILE))
        self.assertFalse(codeowners_cover("# .github/ @o\n", RULE_FILE))

    def test_any_codeowners_covers_rules(self):
        for rel in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
            if os.path.exists(os.path.join(ROOT, rel)):
                text = read(rel)
                if codeowners_cover(text, ".github/orchestrate/orchestrate.py"):
                    self.assertTrue(codeowners_cover(text, RULE_FILE), rel)


class TrustedReadLint(unittest.TestCase):
    def test_a_marker_match_in_a_rule_module_fails(self):
        lint = load("trusted_read_lint_501", ".github/drift/trusted_read_lint.py")
        self.assertEqual(lint.lint(ROOT), [])
        with tempfile.TemporaryDirectory() as d:
            for rel in (".github/orchestrate/rules", ".github/workflows"):
                os.makedirs(os.path.join(d, rel))
            for rel in (".github/orchestrate/trusted_read.py", ".github/orchestrate/orchestrate.py"):
                shutil.copy(os.path.join(ROOT, rel), os.path.join(d, rel))
            with open(os.path.join(d, ".github/orchestrate/rules/x.py"), "w", encoding="utf-8") as f:
                f.write("def seen(body):\n    return WAKE_MARKER in body\n")
            self.assertEqual(lint.lint(d), [".github/orchestrate/rules/x.py:2: marker-match: compares text with a marker "
                                            "outside .github/orchestrate/trusted_read.py; add a function there"])


class FakeGit:
    def __init__(self, files):
        self.files = files

    def run(self, *args, **kw):
        if args[0] == "ls-tree":
            d = args[-1]
            return 0, "\n".join(sorted(p for p in self.files if p.startswith(d)))
        rel = args[1].split(":", 1)[1]
        return (0, self.files[rel]) if rel in self.files else (128, "")


class SelfUpdateSyntaxCheck(unittest.TestCase):
    def test_a_rule_module_that_does_not_compile_is_refused(self):
        upd = load("orchestrate_update_501", ".github/orchestrate/box/orchestrate-update.py")
        files = {rel: read(rel) for rel in upd.BASH_CHECK + upd.PY_CHECK}
        files[RULE_FILE] = "NAME = 'merge'\n"
        files[".github/orchestrate/rules/README.md"] = "not python ("
        self.assertEqual(upd.candidate_problem(FakeGit(files), "abc"), "")
        files[RULE_FILE] = "def decide(:\n"
        self.assertEqual(upd.candidate_problem(FakeGit(files), "abc"), f"{RULE_FILE}: does not compile")


if __name__ == "__main__":
    unittest.main()
