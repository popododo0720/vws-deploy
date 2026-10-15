"""Unit tests for .github/drift/drift_check.py (vws-deploy#300). Run: python3 -m unittest discover -s tests/drift"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("drift_check", os.path.join(ROOT, ".github", "drift", "drift_check.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

COMMON = {"linters": {"enable": ["govet", "lll"]}}
GOOD = """linters:
  enable: [govet, lll]
  settings:
    lll:
      line-length: 120
formatters:
  enable: [gofumpt, goimports]
"""


class LintDrift(unittest.TestCase):
    def test_good_config(self):
        self.assertEqual(dc.lint_drift(GOOD, COMMON), [])

    def test_empty_config(self):
        for cfg in ("", "   \n", "# only a comment\n"):
            self.assertEqual(dc.lint_drift(cfg, COMMON), ["config is empty or not a YAML mapping"])

    def test_not_a_mapping(self):
        for cfg in ("- a\n- b\n", "just a string\n", "42\n"):
            self.assertEqual(dc.lint_drift(cfg, COMMON), ["config is empty or not a YAML mapping"])

    def test_malformed_yaml(self):
        out = dc.lint_drift("linters: [unclosed\n", COMMON)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("config does not parse:"))

    def test_wrong_shapes(self):
        self.assertEqual(dc.lint_drift("linters: [govet]\n", COMMON), ["config 'linters' is not a mapping"])
        out = dc.lint_drift("linters:\n  enable: govet\n  settings: x\nformatters: 3\n", COMMON)
        self.assertIn("missing linters: govet,lll", out)
        self.assertIn("lll line-length is None, not 120", out)
        self.assertIn("formatter gofumpt is off", out)


class LoadCommon(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return dc.load_common(f.name)

    def test_valid(self):
        self.assertEqual(self.load("linters:\n  enable: [govet]\n")["linters"]["enable"], ["govet"])

    def test_bad(self):
        for text in ("", "linters: [x\n", "- a\n", "linters:\n  enable: []\n", "linters: {}\n"):
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                self.load(text)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("drift-check:", err.getvalue())

    def test_missing_file(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            dc.load_common("/nonexistent/golangci-common.yml")
        self.assertEqual(cm.exception.code, 2)

    def test_repo_baseline(self):
        dc.load_common(os.path.join(ROOT, ".github", "golangci-common.yml"))


class ExitStatus(unittest.TestCase):
    def test_drift_is_not_a_crash_status(self):
        # 1 is what Python returns on a traceback; drift must not share it with a crash.
        self.assertEqual(dc.DRIFT_EXIT, 3)


if __name__ == "__main__":
    unittest.main()
