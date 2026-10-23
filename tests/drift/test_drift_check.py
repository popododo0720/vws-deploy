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


class CallerPending(unittest.TestCase):
    """caller-pending.tsv lists repos that still carry the full repo-guard.yml (vws-deploy#402)."""

    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_load(self):
        p = self.write("# c\nvws-lib\tfull copy until its caller PR lands\n\n")
        self.assertEqual(dc.load_pending(p), {"vws-lib": "full copy until its caller PR lands"})

    def test_missing_file_means_none(self):
        self.assertEqual(dc.load_pending("/nonexistent/caller-pending.tsv"), {})

    def test_bad_rows(self):
        for text in ("vws-lib\n", "vws-lib\t\n", "\treason\n", "a\tb\tc\n", "a\tr\na\tr\n"):
            with self.subTest(text=text), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    dc.load_pending(self.write(text))
                self.assertEqual(cm.exception.code, 2)

    def test_repo_file_never_lists_vws_deploy(self):
        pending = dc.load_pending(os.path.join(ROOT, ".github", "drift", "caller-pending.tsv"))
        self.assertNotIn("vws-deploy", pending)


SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "f" * 40
HOME_CALLER = open(os.path.join(ROOT, ".github", "workflows", "repo-guard.yml")).read()


def pinned(sha=SHA):
    """This repo's caller as another repo carries it: each local call pinned to sha."""
    out = []
    for line in HOME_CALLER.splitlines():
        if line.startswith("    uses: ./.github/workflows/"):
            line = line.replace("./", "popododo0720/vws-deploy/", 1) + "@" + sha
        out.append(line)
    return "\n".join(out) + "\n"


class HeadArgs(unittest.TestCase):
    """--head REPO=SHA names a PR head to check, from the workflow event (vws-deploy#402)."""

    def test_parse(self):
        self.assertEqual(dc.parse_heads(["vws-lib=" + SHA, "vws-deploy=" + OTHER], {"vws-lib", "vws-deploy"}),
                         {"vws-lib": [SHA], "vws-deploy": [OTHER]})
        self.assertEqual(dc.parse_heads([], {"vws-lib"}), {})

    def test_bad(self):
        for arg in ("vws-lib", "vws-lib=", "vws-lib=main", "vws-lib=" + SHA[:12], "vws-lib=" + SHA.upper(),
                    "=" + SHA, "vws-unknown=" + SHA):
            with self.subTest(arg=arg), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    dc.parse_heads([arg], {"vws-lib"})
                self.assertEqual(cm.exception.code, 2)


class OpenPrHeads(unittest.TestCase):
    def test_lists_number_and_head(self):
        api = {"repos/o/r/pulls?state=open&per_page=100": [{"number": 7, "head": {"sha": SHA}}]}.get
        self.assertEqual(dc.open_pr_heads("o", "r", api=api), [("#7", SHA)])

    def test_api_error_is_none(self):
        self.assertIsNone(dc.open_pr_heads("o", "r", api=lambda path: None))
        self.assertIsNone(dc.open_pr_heads("o", "r", api=lambda path: {"message": "x"}))


class HeadCallers(unittest.TestCase):
    """The caller at a PR head is checked like the default branch one, against the fleet pin (AC3)."""

    def issues(self, heads, repo="vws-lib", pending=(), common=SHA, main=(SHA,)):
        return dc.head_issues(repo, heads, home=(repo == "vws-deploy"), pending=set(pending), common=common,
                              on_main=lambda sha: sha in main)

    def test_same_pin_is_clean(self):
        self.assertEqual(self.issues([("#7", SHA, pinned())]), [])

    def test_pr_that_changes_the_pin_is_flagged(self):
        out = self.issues([("#7", OTHER, pinned(OTHER))], main=(SHA, OTHER))
        self.assertEqual(len(out), 1)
        self.assertIn("#7", out[0])
        self.assertIn("not the common pin", out[0])

    def test_pin_not_on_main_is_flagged(self):
        out = self.issues([("#7", OTHER, pinned(OTHER))], common=None)
        self.assertEqual(len(out), 1)
        self.assertIn("not on vws-deploy main", out[0])

    def test_missing_or_broken_caller_is_flagged(self):
        self.assertIn("no .github/workflows/repo-guard.yml", self.issues([("#7", OTHER, None)])[0])
        broken = pinned().replace("permissions: {}\n", "permissions: {}\ndefaults: {run: {shell: sh}}\n", 1)
        out = self.issues([("#7", OTHER, broken)])
        self.assertEqual(len(out), 1)
        self.assertIn("top-level keys", out[0])

    def test_pending_repo(self):
        # A repo still on the full copy is not drift at a PR head either, but its migration PR is checked.
        self.assertEqual(self.issues([("#7", OTHER, "name: repo-guard\njobs: {}\n")], pending=["vws-lib"]), [])
        self.assertEqual(self.issues([("#7", OTHER, pinned())], pending=["vws-lib"]), [])
        out = self.issues([("#7", OTHER, pinned(OTHER))], pending=["vws-lib"], main=(SHA, OTHER))
        self.assertIn("not the common pin", out[0])

    def test_home_repo_head(self):
        self.assertEqual(self.issues([("#9", OTHER, HOME_CALLER)], repo="vws-deploy"), [])
        self.assertTrue(self.issues([("#9", OTHER, pinned())], repo="vws-deploy"))

    def test_label_names_the_head(self):
        out = self.issues([("--head", OTHER, None)])
        self.assertTrue(out[0].startswith("--head %s:" % OTHER[:12]), out)


class HeadWiring(unittest.TestCase):
    def test_workflow_passes_the_event_head(self):
        import yaml
        wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "drift-check.yml")))
        step = next(s for s in wf["jobs"]["drift"]["steps"] if s.get("name") == "Compare every repo against the baseline")
        self.assertEqual(step["env"]["PR_HEAD"], "${{ github.event.pull_request.head.sha }}")
        self.assertIn('--head "vws-deploy=${PR_HEAD}"', step["run"])
        self.assertIn('--head "${IN_REPO}=${IN_SHA}"', step["run"])
        self.assertNotIn("${{ inputs", step["run"])  # inputs reach the script through env only
        on = wf.get("on", wf.get(True))
        self.assertEqual(sorted(on["workflow_dispatch"]["inputs"]), ["repo", "sha"])


if __name__ == "__main__":
    unittest.main()
