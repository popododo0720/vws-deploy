"""Unit tests for .github/drift/drift_check.py (vws-deploy#300). Run: python3 -m unittest discover -s tests/drift"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from unittest import mock

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
WANT = ["what-happened", "impact", "detection", "root-cause", "catching-check", "follow-ups"]
GOOD_INCIDENT = "name: incident\nlabels: [incident]\nbody:\n" + "".join(
    f"  - type: textarea\n    id: {i}\n" for i in WANT
)


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


class IncidentDrift(unittest.TestCase):
    def drift(self, tpl=GOOD_INCIDENT, legacy=None):
        tpl = tpl.encode() if isinstance(tpl, str) else tpl
        return dc.incident_drift(tpl, legacy, WANT)

    def test_good_form(self):
        self.assertEqual(self.drift(), [])

    def test_no_form(self):
        self.assertEqual(self.drift(None), ["no .github/ISSUE_TEMPLATE/incident.yml"])

    def test_no_form_still_reports_legacy(self):
        self.assertEqual(self.drift(None, b"name: incident\n"),
                         ["no .github/ISSUE_TEMPLATE/incident.yml", "incident.md still exists"])

    def test_legacy_form(self):
        self.assertEqual(self.drift(legacy=b"name: incident\n"), ["incident.md still exists"])

    def test_labels_as_a_string(self):
        self.assertEqual(self.drift(GOOD_INCIDENT.replace("labels: [incident]", 'labels: "bug, incident"')), [])

    def test_missing_label(self):
        self.assertEqual(self.drift(GOOD_INCIDENT.replace("labels: [incident]", "labels: [bug]")),
                         ["does not apply the incident label"])

    def test_missing_field(self):
        self.assertEqual(self.drift(GOOD_INCIDENT.replace("    id: impact\n", "")),
                         ["missing fields: impact"])

    def test_malformed_yaml(self):
        out = self.drift("body: [unclosed\n")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("incident.yml does not parse:"))

    def test_not_a_mapping(self):
        for tpl in ("", "- a\n- b\n"):
            self.assertEqual(self.drift(tpl), ["incident.yml is not a YAML mapping"])

    def test_extra_field(self):
        self.assertEqual(self.drift(GOOD_INCIDENT + "  - type: textarea\n    id: extra\n"), [])

    def test_non_dict_body_item(self):
        out = self.drift("name: incident\nlabels: [incident]\nbody:\n  - just-a-string\n")
        self.assertEqual(out, ["missing fields: " + ",".join(WANT)])

    def test_bad_yaml_still_reports_legacy(self):
        out = self.drift("body: [unclosed\n", b"name: incident\n")
        self.assertTrue(out[0].startswith("incident.yml does not parse:"))
        self.assertEqual(out[1], "incident.md still exists")

    def test_non_mapping_still_reports_legacy(self):
        self.assertEqual(self.drift("- a\n", b"name: incident\n"),
                         ["incident.yml is not a YAML mapping", "incident.md still exists"])


class LoadIncident(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return dc.load_incident(f.name)

    def test_valid(self):
        self.assertEqual(self.load(GOOD_INCIDENT), WANT)

    def test_bad(self):
        for text in ("", "- a\n", "just a string\n", "body:\n  - type: textarea\n", "body: []\n"):
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                self.load(text)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("drift-check:", err.getvalue())

    def test_missing_file(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            dc.load_incident("/nonexistent/incident.yml")
        self.assertEqual(cm.exception.code, 2)

    def test_repo_baseline(self):
        self.assertEqual(dc.load_incident(os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "incident.yml")), WANT)


class LabelExists(unittest.TestCase):
    def test_present(self):
        with mock.patch.object(dc, "gh", return_value={"name": "incident"}):
            self.assertTrue(dc.label_exists("popododo0720", "vws-lib", "incident"))

    def test_missing(self):
        with mock.patch.object(dc, "gh", return_value=None):
            self.assertFalse(dc.label_exists("popododo0720", "vws-lib", "incident"))

    def test_asks_for_the_label_path(self):
        seen = []

        def fake(path):
            seen.append(path)
            return {}

        with mock.patch.object(dc, "gh", fake):
            dc.label_exists("o", "r", "incident")
        self.assertEqual(seen, ["repos/o/r/labels/incident"])


class GuardVariants(unittest.TestCase):
    WANT = "a" * 64
    VARIANT = "7070d89befc2ef534ea190006cad968d425a5793c416c33bc4daa275b5de2383"

    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return dc.load_variants(f.name)

    def test_canonical_sha_is_ok(self):
        self.assertEqual(dc.guard_status(self.WANT, self.WANT, self.VARIANT), (None, "ok"))

    def test_accepted_variant(self):
        self.assertEqual(dc.guard_status(self.VARIANT, self.WANT, self.VARIANT), (None, "accepted variant (7070d89befc2)"))

    def test_unexpected_sha_with_a_variant_is_drift(self):
        issue, _ = dc.guard_status("b" * 64, self.WANT, self.VARIANT)
        self.assertEqual(issue, "guard-agents.sh differs from vws-deploy")

    def test_repo_without_an_exception_is_drift(self):
        variants = self.load("vws-conformance\t%s\treason\n" % self.VARIANT)
        issue, _ = dc.guard_status(self.VARIANT, self.WANT, variants.get("vws-lib"))
        self.assertEqual(issue, "guard-agents.sh differs from vws-deploy")

    def test_load(self):
        text = "# comment\n\nvws-conformance\t%s\tcheck-run variant\n" % self.VARIANT
        self.assertEqual(self.load(text), {"vws-conformance": self.VARIANT})

    def test_missing_file_means_no_variants(self):
        self.assertEqual(dc.load_variants("/nonexistent/guard-variants.tsv"), {})

    def test_bad_rows(self):
        bad = (
            "vws-conformance\t%s\n" % self.VARIANT,  # no reason
            "vws-conformance\t%s\t \n" % self.VARIANT,  # blank reason
            "vws-conformance\t7070d89b\treason\n",  # short sha
            "vws-conformance\t%s\treason\n" % self.VARIANT.upper(),  # not lowercase
            "\t%s\treason\n" % self.VARIANT,  # no repo
            "vws-conformance\t%s\tr\nvws-conformance\t%s\tr\n" % (self.VARIANT, "c" * 64),  # repeated repo
        )
        for text in bad:
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                self.load(text)
            self.assertEqual(cm.exception.code, 2, text)
            self.assertIn("drift-check:", err.getvalue())

    def test_repo_file_pins_the_conformance_variant(self):
        variants = dc.load_variants(os.path.join(ROOT, ".github", "drift", "guard-variants.tsv"))
        self.assertEqual(variants, {"vws-conformance": self.VARIANT})


class ExitStatus(unittest.TestCase):
    def test_drift_is_not_a_crash_status(self):
        # 1 is what Python returns on a traceback; drift must not share it with a crash.
        self.assertEqual(dc.DRIFT_EXIT, 3)


if __name__ == "__main__":
    unittest.main()
