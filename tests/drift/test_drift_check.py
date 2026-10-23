"""Unit tests for .github/drift/drift_check.py (vws-deploy#300). Run: python3 -m unittest discover -s tests/drift"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import os
import sys
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
    """caller-pending.tsv pins, per repo, the sha256 of the full repo-guard.yml it still carries (vws-deploy#402)."""

    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_load(self):
        p = self.write("# c\nvws-lib\t%s\tfull copy until its caller PR lands\n\n" % ("a" * 64))
        self.assertEqual(dc.load_pending(p), {"vws-lib": "a" * 64})

    def test_missing_file_means_none(self):
        self.assertEqual(dc.load_pending("/nonexistent/caller-pending.tsv"), {})

    def test_bad_rows(self):
        h = "a" * 64
        for text in ("vws-lib\n", "vws-lib\t\n", "\treason\n", "vws-lib\treason\n", "vws-lib\t%s\n" % h,
                     "vws-lib\t%s\t \n" % h, "vws-lib\t%s\tr\n" % h[:63], "vws-lib\t%s\tr\n" % h.upper(),
                     "a\t%s\tr\na\t%s\tr\n" % (h, h), "a\t%s\tr\tx\n" % h):
            with self.subTest(text=text), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    dc.load_pending(self.write(text))
                self.assertEqual(cm.exception.code, 2)

    def test_repo_file_never_lists_vws_deploy(self):
        pending = dc.load_pending(os.path.join(ROOT, ".github", "drift", "caller-pending.tsv"))
        self.assertNotIn("vws-deploy", pending)
        self.assertTrue(pending)


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
    """Every open PR is listed, page by page; a list that cannot be read in full is None (drift)."""

    @staticmethod
    def pages(*pages):
        """A fake gh api: page n of the open PR list is pages[n-1], later pages are empty, anything else None."""
        def api(path):
            base, _, page = path.partition("&page=")
            if base != "repos/o/r/pulls?state=open&per_page=100" or not page.isdigit():
                return None
            n = int(page)
            return pages[n - 1] if n <= len(pages) else []
        return api

    @staticmethod
    def prs(first, count):
        return [{"number": n, "head": {"sha": "%040x" % n}} for n in range(first, first + count)]

    def test_lists_number_and_head(self):
        api = self.pages([{"number": 7, "head": {"sha": SHA}}])
        self.assertEqual(dc.open_pr_heads("o", "r", api=api), [("#7", SHA)])

    def test_reads_every_page(self):
        got = dc.open_pr_heads("o", "r", api=self.pages(self.prs(1, 100), self.prs(101, 100), self.prs(201, 3)))
        self.assertEqual(len(got), 203)
        self.assertEqual(got[0], ("#1", "%040x" % 1))
        self.assertEqual(got[-1], ("#203", "%040x" % 203))

    def test_a_full_last_page_is_followed_by_an_empty_one(self):
        self.assertEqual(len(dc.open_pr_heads("o", "r", api=self.pages(self.prs(1, 100)))), 100)

    def test_api_error_is_none(self):
        self.assertIsNone(dc.open_pr_heads("o", "r", api=lambda path: None))
        self.assertIsNone(dc.open_pr_heads("o", "r", api=lambda path: {"message": "x"}))

    def test_error_on_a_later_page_is_none(self):
        first = self.pages(self.prs(1, 100))

        def api(path):
            return None if path.endswith("&page=2") else first(path)
        self.assertIsNone(dc.open_pr_heads("o", "r", api=api))

    def test_malformed_entries_are_none(self):
        for bad in ([{"number": 7}], [{"number": 7, "head": {"sha": "main"}}], [{"head": {"sha": SHA}}], ["x"]):
            with self.subTest(bad=bad):
                self.assertIsNone(dc.open_pr_heads("o", "r", api=self.pages(bad)))

    def test_endless_pages_are_none(self):
        # A list that never ends (or a fake that ignores page) is not read forever.
        self.assertIsNone(dc.open_pr_heads("o", "r", api=lambda path: self.prs(1, 100)))


RG = ".github/workflows/repo-guard.yml"
DPW = ".github/workflows/direct-push-watch.yml"
# A stand-in for the full repo-guard.yml a pending repo still carries; only its bytes matter.
FULL = b"""name: repo-guard
on:
  pull_request:
    types: [opened, edited, reopened, synchronize, labeled, unlabeled]
jobs:
  pr-hygiene:
    runs-on: ubuntu-latest
    steps: [{run: scripts/guard-agents.sh pr-body}]
  verify-gate:
    runs-on: ubuntu-latest
    steps: [{run: scripts/guard-agents.sh verify-gate}]
"""
FULL_SHA = hashlib.sha256(FULL).hexdigest()
PENDING = {"vws-lib": FULL_SHA}
DPW_TEXT = b"name: direct-push-watch\non: push\njobs:\n  direct-push-watch: {runs-on: ubuntu-latest, steps: [{run: x}]}\n"
BYPASS_SHELL = pinned().replace("permissions: {}\n", "permissions: {}\ndefaults: {run: {shell: \"bash -c 'exit 0' {0}\"}}\n", 1)
AT_MAIN = pinned().replace("@" + SHA, "@main")


def tree(rg=FULL, **more):
    """{path: bytes} of .github/workflows at one ref: repo-guard.yml (None = absent), direct-push-watch.yml, more."""
    out = {DPW: DPW_TEXT}
    if rg is not None:
        out[RG] = rg if isinstance(rg, bytes) else rg.encode()
    for name, text in more.items():
        out[".github/workflows/" + name.replace("_", "-").replace("-yaml", ".yaml").replace("-yml", ".yml")] = (
            text if text is None or isinstance(text, bytes) else text.encode())
    return out


def home_tree():
    d = os.path.join(ROOT, ".github", "workflows")
    out = {}
    for n in sorted(os.listdir(d)):
        with open(os.path.join(d, n), "rb") as f:
            out[".github/workflows/" + n] = f.read()
    return out


class PendingExemption(unittest.TestCase):
    """A pending repo is exempt only while repo-guard.yml is byte for byte the full copy that
    caller-pending.tsv pins; anything else is checked as a caller (round 2 blocker)."""

    def check(self, files, repo="vws-lib", pending=PENDING):
        return dc.ref_issues(files, home=(repo == "vws-deploy"), full_copy=pending.get(repo))

    def test_unchanged_full_copy_passes(self):
        self.assertEqual(self.check(tree()), ([], "pending"))

    def test_bypass_caller_in_a_pending_repo_is_flagged(self):
        for what, text in (("defaults.run.shell", BYPASS_SHELL), ("@main pin", AT_MAIN)):
            with self.subTest(what=what):
                issues, pin = self.check(tree(rg=text))
                self.assertTrue(issues)
                self.assertIsNone(pin)
                self.assertIn("not the full copy", issues[0])
        self.assertIn("top-level keys", self.check(tree(rg=BYPASS_SHELL))[0][0])
        self.assertIn("@main", self.check(tree(rg=AT_MAIN))[0][0])

    def test_edited_full_copy_is_flagged(self):
        for edited in (FULL + b"#\n", FULL.replace(b"\n", b"\r\n"), FULL.replace(b"pr-body", b"true   ")):
            with self.subTest(edited=edited[-20:]):
                issues, pin = self.check(tree(rg=edited))
                self.assertTrue(issues)
                self.assertIsNone(pin)

    def test_good_caller_in_a_pending_repo_returns_its_pin(self):
        self.assertEqual(self.check(tree(rg=pinned())), ([], SHA))

    def test_full_copy_outside_caller_pending_is_flagged(self):
        issues, pin = self.check(tree(), pending={})
        self.assertTrue(issues)
        self.assertIsNone(pin)

    def test_deleted_or_unreadable_repo_guard_is_flagged(self):
        self.assertEqual(self.check(tree(rg=None))[0], ["no .github/workflows/repo-guard.yml"])
        files = tree()
        files[RG] = None
        self.assertEqual(self.check(files)[0], ["cannot read .github/workflows/repo-guard.yml"])
        self.assertEqual(self.check(tree(rg=b"\xff\xfe"))[1], None)
        self.assertEqual(self.check(None), (["cannot list .github/workflows"], None))

    def test_home_repo_tree_is_clean(self):
        self.assertEqual(self.check(home_tree(), repo="vws-deploy"), ([], "local"))


class OtherWorkflowFiles(unittest.TestCase):
    """Only repo-guard.yml may call the repo-guard jobs or report the bare pr-hygiene and verify-gate checks,
    in every repo, pending or not, on the default branch and at PR heads."""

    def issues(self, files, repo="vws-lib"):
        return dc.ref_issues(files, home=(repo == "vws-deploy"), full_copy=PENDING.get(repo))[0]

    def test_second_caller_with_a_bad_pin_is_flagged(self):
        out = self.issues(tree(rg2_yml=AT_MAIN))
        self.assertTrue(any("rg2.yml" in i and "only repo-guard.yml may call" in i for i in out), out)

    def test_second_caller_with_a_good_pin_is_flagged(self):
        out = self.issues(tree(rg2_yml=pinned()))
        self.assertTrue(any("rg2.yml" in i for i in out), out)

    def test_renamed_caller_is_flagged(self):
        out = self.issues(tree(rg=None, guard_yaml=pinned()))
        self.assertIn("no .github/workflows/repo-guard.yml", out)
        self.assertTrue(any("guard.yaml" in i for i in out), out)

    def test_local_path_call_is_flagged(self):
        for uses in ("./.github/workflows/repo-guard-pr-hygiene.yml",
                     "popododo0720/vws-deploy/.github/workflows/repo-guard-verify-gate.yml@" + SHA,
                     "popododo0720/vws-deploy/.github/workflows/Repo-Guard-Verify-Gate.YML@main"):
            text = "on: pull_request\njobs:\n  x:\n    uses: %s\n" % uses
            with self.subTest(uses=uses):
                self.assertTrue(any("x.yml" in i for i in self.issues(tree(x_yml=text))))

    def test_job_that_reports_a_required_check_name_is_flagged(self):
        for job in ("pr-hygiene: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}",
                    "x: {name: verify-gate, runs-on: ubuntu-latest, steps: [{run: 'true'}]}",
                    "x: {name: PR-Hygiene, runs-on: ubuntu-latest, steps: [{run: 'true'}]}",
                    "x: {name: ' verify-gate ', runs-on: ubuntu-latest, steps: [{run: 'true'}]}",
                    "x: {name: \"${{ 'verify-gate' }}\", runs-on: ubuntu-latest, steps: [{run: 'true'}]}",
                    "x: {name: \"verify-${{ 'gate' }}\", runs-on: ubuntu-latest, steps: [{run: 'true'}]}"):
            for name in ("spoof_yml", "spoof_yaml"):
                text = "on: [pull_request]\njobs:\n  %s\n" % job
                with self.subTest(job=job, file=name):
                    out = self.issues(tree(**{name: text}))
                    self.assertTrue(any("required check name" in i for i in out), out)

    def test_ordinary_workflows_pass(self):
        ok = ("on: pull_request\njobs:\n  test:\n    name: test (${{ matrix.go }})\n    runs-on: ubuntu-latest\n"
              "    strategy: {matrix: {go: ['1.22']}}\n    steps: [{run: go test}]\n"
              "  hygiene: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n")
        called = "on: workflow_call\njobs:\n  pr-hygiene: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n"
        called2 = "on: {workflow_call: {}}\njobs:\n  verify-gate: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n"
        self.assertEqual(self.issues(tree(ci_yml=ok, lib_yml=called, lib2_yml=called2)), [])

    def test_unreadable_or_broken_files_are_flagged(self):
        for text in (None, b"\xff", b"jobs: [unclosed\n", b"a: 1\na: 2\n", b"- a list\n"):
            with self.subTest(text=text):
                self.assertTrue(any("x.yml" in i for i in self.issues(tree(x_yml=text))))


class WorkflowFiles(unittest.TestCase):
    """workflow_files reads every .yml/.yaml file in .github/workflows at one ref through gh api."""

    @staticmethod
    def api(entries, files, ref=None):
        q = "?ref=%s" % ref if ref else ""

        def get(path):
            if path == "repos/o/r/contents/.github/workflows" + q:
                return entries
            for p, b in files.items():
                if path == "repos/o/r/contents/" + p + q:
                    return {"content": base64.b64encode(b).decode()}
            return None
        return get

    def test_reads_files_at_a_ref(self):
        entries = [{"type": "file", "name": "repo-guard.yml", "path": RG},
                   {"type": "file", "name": "buf.yaml", "path": ".github/workflows/buf.yaml"},
                   {"type": "file", "name": "README.md", "path": ".github/workflows/README.md"},
                   {"type": "dir", "name": "sub", "path": ".github/workflows/sub"}]
        files = {RG: FULL, ".github/workflows/buf.yaml": b"x"}
        got = dc.workflow_files("o", "r", ref=SHA, api=self.api(entries, files, ref=SHA))
        self.assertEqual(got, files)
        got = dc.workflow_files("o", "r", api=self.api(entries, files))
        self.assertEqual(got, files)

    def test_errors(self):
        self.assertIsNone(dc.workflow_files("o", "r", api=lambda p: None))
        self.assertIsNone(dc.workflow_files("o", "r", api=lambda p: {"message": "x"}))
        entries = [{"type": "file", "name": "a.yml", "path": ".github/workflows/a.yml"},
                   {"type": "symlink", "name": "b.yml", "path": ".github/workflows/b.yml"}]
        got = dc.workflow_files("o", "r", api=self.api(entries, {}))
        self.assertEqual(got, {".github/workflows/a.yml": None, ".github/workflows/b.yml": None})


class HeadCallers(unittest.TestCase):
    """The workflows at a PR head are checked like the default branch ones, against the fleet pin (AC3)."""

    def issues(self, heads, repo="vws-lib", pending=PENDING, common=SHA, main=(SHA,)):
        return dc.head_issues(repo, heads, home=(repo == "vws-deploy"), pending=pending, common=common,
                              on_main=lambda sha: sha in main)

    def test_same_pin_is_clean(self):
        self.assertEqual(self.issues([("#7", SHA, tree(rg=pinned()))]), [])

    def test_pr_that_changes_the_pin_is_flagged(self):
        out = self.issues([("#7", OTHER, tree(rg=pinned(OTHER)))], main=(SHA, OTHER))
        self.assertEqual(len(out), 1)
        self.assertIn("#7", out[0])
        self.assertIn("not the common pin", out[0])

    def test_pin_not_on_main_is_flagged(self):
        out = self.issues([("#7", OTHER, tree(rg=pinned(OTHER)))], common=None)
        self.assertEqual(len(out), 1)
        self.assertIn("not on vws-deploy main", out[0])

    def test_pending_repo_head(self):
        # (b) the unchanged full copy is not drift; (a) a bypass caller at the head of a migration PR is.
        self.assertEqual(self.issues([("#7", OTHER, tree())]), [])
        for text in (BYPASS_SHELL, AT_MAIN, FULL.decode() + "#\n"):
            with self.subTest(text=text[-30:]):
                out = self.issues([("#7", OTHER, tree(rg=text))])
                self.assertEqual(len(out), 1, out)
                self.assertTrue(out[0].startswith("#7 %s:" % OTHER[:12]), out)

    def test_pending_repo_head_with_a_second_caller_or_spoof_is_flagged(self):
        spoof = "on: pull_request\njobs:\n  pr-hygiene: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n"
        self.assertTrue(self.issues([("#7", OTHER, tree(rg2_yml=AT_MAIN))]))
        self.assertTrue(self.issues([("#7", OTHER, tree(x_yml=spoof))]))
        self.assertTrue(self.issues([("#7", OTHER, tree(rg=None))]))

    def test_unlisted_head_is_flagged(self):
        self.assertIn("cannot list .github/workflows", self.issues([("#7", OTHER, None)])[0])

    def test_home_repo_head(self):
        self.assertEqual(self.issues([("#9", OTHER, home_tree())], repo="vws-deploy"), [])
        self.assertTrue(self.issues([("#9", OTHER, tree(rg=pinned()))], repo="vws-deploy"))

    def test_label_names_the_head(self):
        out = self.issues([("--head", OTHER, tree(rg=None))])
        self.assertTrue(out[0].startswith("--head %s:" % OTHER[:12]), out)


def _b64(b):
    return {"content": base64.b64encode(b).decode()}


class MainWithFakeGh(unittest.TestCase):
    """main() end to end with a fake gh api: the default branch and PR heads of a pending repo (no network)."""

    def run_main(self, lib_default, lib_prs=(), lib_heads=None):
        with open(os.path.join(ROOT, "scripts", "guard-agents.sh"), "rb") as f:
            guard = f.read()
        prot = {"required_status_checks": {"contexts": list(dc.COMMON_CHECKS)}, "enforce_admins": {"enabled": True},
                "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False}}
        trees = {("vws-deploy", None): home_tree(), ("vws-lib", None): lib_default}
        trees.update({("vws-lib", sha): t for sha, t in (lib_heads or {}).items()})
        prs = {"vws-deploy": [], "vws-lib": [{"number": n, "head": {"sha": sha}} for n, sha in lib_prs]}

        def gh(path):
            path, _, q = path.partition("?")
            parts = path.split("/")
            repo = parts[2]
            if len(parts) == 3:
                return {"archived": False}
            rest = "/".join(parts[3:])
            if rest == "branches/main/protection":
                return prot
            if rest == "pulls":
                return prs[repo] if q.endswith("&page=1") else []
            if rest.startswith("compare/"):
                return {"status": "identical"}
            ref = q[4:] if q.startswith("ref=") else None
            if rest.startswith("contents/.github/workflows"):
                t = trees.get((repo, ref))
                if t is None:
                    return None
                if rest == "contents/.github/workflows":
                    return [{"type": "file", "name": p.rsplit("/", 1)[1], "path": p} for p in t]
                b = t.get(rest[len("contents/"):])
                return None if b is None else _b64(b)
            return {"contents/scripts/guard-agents.sh": _b64(guard), "contents/AGENTS.md": _b64(b"x"),
                    "contents/QUALITY.md": _b64(b"x")}.get(rest)

        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.unlink(os.path.join(d, f)) for f in os.listdir(d)] and os.rmdir(d))
        repos = os.path.join(d, "repos.tsv")
        with open(repos, "w") as f:
            f.write("vws-deploy\tother\t\t\nvws-lib\tother\t\t\n")
        pend = os.path.join(d, "pending.tsv")
        with open(pend, "w") as f:
            f.write("vws-lib\t%s\tfull copy\n" % FULL_SHA)
        argv = ["drift_check.py", "--owner", "o", "--repos", repos, "--caller-pending", pend,
                "--guard-variants", os.path.join(d, "none.tsv")]
        out = io.StringIO()
        with mock.patch.object(dc, "gh", gh), mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            rc = dc.main()
        return rc, out.getvalue()

    def test_pending_full_copy_is_clean(self):
        rc, out = self.run_main(tree())
        self.assertEqual(rc, 0, out)
        self.assertIn("pending (full copy)", out)

    def test_default_branch_second_caller_with_a_bad_pin_is_flagged(self):
        # (c) a pending repo whose default branch has another workflow file calling repo-guard-*.yml
        rc, out = self.run_main(tree(rg2_yml=AT_MAIN))
        self.assertEqual(rc, dc.DRIFT_EXIT, out)
        self.assertIn("rg2.yml", out)

    def test_default_branch_edited_full_copy_is_flagged(self):
        rc, out = self.run_main(tree(rg=FULL + b"#\n"))
        self.assertEqual(rc, dc.DRIFT_EXIT, out)
        self.assertIn("not the full copy", out)

    def test_pr_head_bypass_in_a_pending_repo_is_flagged(self):
        rc, out = self.run_main(tree(), lib_prs=[(5, OTHER)], lib_heads={OTHER: tree(rg=BYPASS_SHELL)})
        self.assertEqual(rc, dc.DRIFT_EXIT, out)
        self.assertIn("| vws-lib | #5 %s:" % OTHER[:12], out)


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
