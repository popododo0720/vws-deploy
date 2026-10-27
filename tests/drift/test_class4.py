"""Class 4 of vws-deploy#439 (docs/lessons/verify-findings-2026-10.md): copy drift of vendored shared files,
and a workflow that calls a guard-agents.sh subcommand the base-branch script does not have yet.

Every failing fixture is a real file from a past finding; every passing fixture is the file that should
have been there. Run: python3 -m unittest discover -s tests/drift
"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("drift_check", os.path.join(ROOT, ".github", "drift", "drift_check.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

FIX = os.path.join(ROOT, "tests", "drift", "fixtures", "class4")
VQ = ".github/workflows/verify-queue.yml"
CE = "scripts/check-english.sh"
RG = ".github/workflows/repo-guard.yml"


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def local(path):
    with open(os.path.join(ROOT, path), "rb") as f:
        return f.read()


def local_ce():
    """The pinned check-english.sh (vws-deploy has no copy of its own)."""
    return fixture("vws-lib-main-check-english.sh")


def repo_pins():
    return dc.load_vendored(os.path.join(ROOT, ".github", "drift", "vendored.tsv"))


class VendoredPins(unittest.TestCase):
    """vendored.tsv pins verify-queue.yml and check-english.sh by sha256, like guard-agents.sh.sha256."""

    def test_repo_file_pins_both_files(self):
        pins = repo_pins()
        self.assertEqual(sorted(pins), [VQ, CE])
        for path in pins:
            self.assertRegex(pins[path]["*"], r"^[0-9a-f]{64}$")

    def test_verify_queue_pin_is_this_repo_file(self):
        self.assertEqual(repo_pins()[VQ]["*"], hashlib.sha256(local(VQ)).hexdigest())

    def test_storage_140_copy_is_drift(self):
        # vws-storage#140 head 919f516: "copy unchanged" that dropped vws-deploy#471's skip list (review 5638072542).
        raw = fixture("vws-storage-140-919f516-verify-queue.yml")
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "1db525d00d9c5e1752509b763a952a2ba34eed5c097e61fb826fbecc0d96a0d0")
        issues, notes = dc.vendored_issues("vws-storage", {VQ: raw, CE: local_ce()}, repo_pins())
        self.assertEqual(len(issues), 1)
        self.assertIn("verify-queue.yml differs from the vws-deploy pin", issues[0])
        self.assertIn("got 1db525d00d9c", issues[0])
        self.assertEqual(notes, [])

    def test_byte_identical_copy_passes(self):
        self.assertEqual(dc.vendored_issues("vws-storage", {VQ: local(VQ), CE: local_ce()}, repo_pins()), ([], []))

    def test_check_english_copy_passes_and_one_byte_off_is_drift(self):
        raw = fixture("vws-lib-main-check-english.sh")
        self.assertEqual(dc.vendored_issues("vws-lib", {CE: raw}, repo_pins()), ([], []))
        issues, _ = dc.vendored_issues("vws-lib", {CE: raw + b"\n"}, repo_pins())
        self.assertEqual(len(issues), 1)
        self.assertIn("check-english.sh differs from the vws-deploy pin", issues[0])

    def test_absent_verify_queue_is_not_checked_here(self):
        # A missing verify-queue.yml is the workflows cell's drift, not the vendored cell's.
        raw = fixture("vws-lib-main-check-english.sh")
        self.assertEqual(dc.vendored_issues("vws-lib", {CE: raw}, repo_pins()), ([], []))
        self.assertEqual(dc.vendored_issues("vws-lib", {VQ: None, CE: raw}, repo_pins()), ([], []))

    def test_missing_check_english_is_drift(self):
        # vws-deploy#423: vws-compute had no scripts/check-english.sh and drift-check said nothing.
        for got in ({}, {CE: None}):
            with self.subTest(got=got):
                self.assertEqual(dc.vendored_issues("vws-compute", got, repo_pins()),
                                 (["no scripts/check-english.sh"], []))

    def test_vws_deploy_needs_no_check_english_copy(self):
        # vws-deploy runs the same check as scripts/guard.sh english (R22); it holds the pin, not a copy.
        self.assertEqual(dc.vendored_issues("vws-deploy", {CE: None}, repo_pins()), ([], []))

    def test_accepted_variant_is_a_note_only_for_its_repo(self):
        raw = b"variant\n"
        sha = hashlib.sha256(raw).hexdigest()
        pins = {VQ: {"*": "a" * 64, "vws-console-api": sha}}
        self.assertEqual(dc.vendored_issues("vws-console-api", {VQ: raw}, pins),
                         ([], [f"verify-queue.yml accepted variant ({sha[:12]})"]))
        issues, _ = dc.vendored_issues("vws-lib", {VQ: raw}, pins)
        self.assertEqual(len(issues), 1)

    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return dc.load_vendored(f.name)

    def test_load(self):
        text = "# c\n\n%s\t*\t%s\tcanonical\n%s\tvws-x\t%s\tvariant\n" % (VQ, "a" * 64, VQ, "b" * 64)
        self.assertEqual(self.load(text), {VQ: {"*": "a" * 64, "vws-x": "b" * 64}})

    def test_bad_rows_exit_2(self):
        good = "a" * 64
        bad = (
            "%s\t*\t%s\n" % (VQ, good),  # no reason
            "%s\t*\t%s\t \n" % (VQ, good),  # blank reason
            "%s\t*\tabc\tr\n" % VQ,  # short sha
            "%s\t*\t%s\tr\n" % (VQ, good.upper()),  # not lowercase
            "\t*\t%s\tr\n" % good,  # no path
            "%s\t\t%s\tr\n" % (VQ, good),  # no repo
            "%s\t*\t%s\tr\n%s\t*\t%s\tr\n" % (VQ, good, VQ, "c" * 64),  # repeated row
            "%s\tvws-x\t%s\tr\n" % (VQ, good),  # a variant without a canonical row
        )
        for text in bad:
            with self.subTest(text=text):
                err = io.StringIO()
                with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                    self.load(text)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("drift-check:", err.getvalue())


class GuardSubcommands(unittest.TestCase):
    """The subcommands a guard-agents.sh dispatches, read from its case block."""

    def test_canonical_script(self):
        subs = dc.guard_subcommands(local("scripts/guard-agents.sh"))
        for name in ("agents", "pr-base", "pr-body", "pr-checklist", "pr-issue", "rules", "judgment",
                     "identity", "verify-gate", "pr-commits", "push-watch", "english", "selftest"):
            self.assertIn(name, subs)

    def test_console_api_base_15cdb241(self):
        self.assertEqual(dc.guard_subcommands(fixture("vws-console-api-15cdb241-guard-agents.sh")),
                         {"agents", "pr-base", "pr-body", "push-watch", "selftest"})

    def test_no_dispatch_is_none(self):
        for raw in (b"", b"#!/bin/sh\necho hi\n", b"\xff\xfe"):
            self.assertIsNone(dc.guard_subcommands(raw), raw)


class GuardCalls(unittest.TestCase):
    """Only run: steps count; comments and other keys never do."""

    def test_console_api_6e550d3(self):
        calls = dc.guard_calls({RG: fixture("vws-console-api-6e550d3-repo-guard.yml")})
        self.assertEqual(sorted({sub for _, sub in calls}),
                         ["agents", "identity", "pr-base", "pr-body", "pr-commits", "verify-gate"])

    def test_comments_and_names_are_ignored(self):
        wf = (b"# scripts/guard-agents.sh as-is into another repo.\non: push\njobs:\n  a:\n    runs-on: x\n"
              b"    steps:\n      - name: scripts/guard-agents.sh nope\n        run: |\n"
              b"          # scripts/guard-agents.sh commented\n          ./scripts/guard-agents.sh pr-body\n"
              b"          bash scripts/guard-agents.sh 'agents' x\n")
        self.assertEqual(sorted(dc.guard_calls({"w.yml": wf})), [("w.yml", "agents"), ("w.yml", "pr-body")])

    def test_non_literal_subcommand_is_reported(self):
        wf = b"on: push\njobs:\n  a:\n    steps:\n      - run: scripts/guard-agents.sh \"$CMD\"\n"
        self.assertEqual(dc.guard_calls({"w.yml": wf}), [("w.yml", "$CMD")])

    def test_unparseable_or_unreadable_files_are_skipped(self):
        self.assertEqual(dc.guard_calls({"a.yml": None, "b.yml": b":\n  - [", "c.yml": b"[1]"}), [])


class Bootstrap(unittest.TestCase):
    """A workflow may call a guard subcommand only if the base-branch script already has it."""

    def test_console_api_6e550d3_on_base_15cdb241_fails(self):
        # Codex console-api-v2-6e550d3.md [P1] "Bootstrap the trusted script before enabling verify-gate".
        issues = dc.bootstrap_issues({RG: fixture("vws-console-api-6e550d3-repo-guard.yml")},
                                     fixture("vws-console-api-15cdb241-guard-agents.sh"))
        self.assertEqual(issues, [
            "repo-guard.yml calls guard-agents.sh identity, pr-commits, verify-gate, which the base-branch "
            "script does not have; land the script first"])

    def test_same_workflow_on_the_canonical_script_passes(self):
        self.assertEqual(dc.bootstrap_issues({RG: fixture("vws-console-api-6e550d3-repo-guard.yml")},
                                             local("scripts/guard-agents.sh")), [])

    def test_this_repo_workflows_pass_on_this_repo_script(self):
        d = os.path.join(ROOT, ".github", "workflows")
        files = {f".github/workflows/{n}": local(f".github/workflows/{n}") for n in sorted(os.listdir(d))}
        self.assertTrue(dc.guard_calls(files))
        self.assertEqual(dc.bootstrap_issues(files, local("scripts/guard-agents.sh")), [])

    def test_callee_calls_count_against_the_caller_base(self):
        # A caller's repo-guard.yml only uses vws-deploy's repo-guard-*.yml; their calls run on the caller's base.
        callee = {".github/workflows/repo-guard-verify-gate.yml": local(".github/workflows/repo-guard-verify-gate.yml")}
        issues = dc.bootstrap_issues({RG: b"on: pull_request\njobs: {}\n"},
                                     fixture("vws-console-api-15cdb241-guard-agents.sh"), callee)
        self.assertEqual(len(issues), 1)
        self.assertIn("vws-deploy repo-guard-verify-gate.yml calls guard-agents.sh verify-gate", issues[0])

    def test_no_calls_and_no_script_is_ok(self):
        self.assertEqual(dc.bootstrap_issues({RG: b"on: push\njobs: {}\n"}, None), [])

    def test_calls_without_a_base_script_fail_closed(self):
        issues = dc.bootstrap_issues({RG: fixture("vws-console-api-6e550d3-repo-guard.yml")}, None)
        self.assertEqual(issues, ["workflows call guard-agents.sh, but the base branch has no readable "
                                  "scripts/guard-agents.sh dispatch"])
        issues = dc.bootstrap_issues({RG: fixture("vws-console-api-6e550d3-repo-guard.yml")}, b"echo\n")
        self.assertEqual(len(issues), 1)


class PrHeads(unittest.TestCase):
    """The storage#140 finding was a PR head: class 4 is checked at every open PR head too."""

    def run_heads(self, heads, script, pending=None, api=None, at_head=None):
        saved = dc.content
        dc.content = lambda owner, repo, path, ref=None: (at_head or {}).get((path, ref))
        try:
            return dc.class4_head_issues("o", "vws-storage", heads, script, repo_pins(), pending or {}, {}, api=api)
        finally:
            dc.content = saved

    def test_storage_140_head_is_drift(self):
        sha = "919f516c9873f24d36d9160a02d759009f840b25"
        out = self.run_heads([("#140", sha, {VQ: fixture("vws-storage-140-919f516-verify-queue.yml")})],
                             local("scripts/guard-agents.sh"))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("#140 919f516c9873: verify-queue.yml differs from the vws-deploy pin"), out)

    def test_clean_head_passes_and_check_english_is_read_at_the_head(self):
        sha = "a" * 40
        heads = [("#1", sha, {VQ: local(VQ)})]
        self.assertEqual(self.run_heads(heads, local("scripts/guard-agents.sh"),
                                        at_head={(CE, sha): local_ce()}), [])
        out = self.run_heads(heads, local("scripts/guard-agents.sh"), at_head={(CE, sha): b"drifted\n"})
        self.assertEqual(len(out), 1)
        self.assertIn("check-english.sh differs", out[0])

    def test_head_callee_is_held_to_the_default_branch_script(self):
        # A caller head pinned to a vws-deploy SHA whose repo-guard-verify-gate.yml calls verify-gate, on a
        # base script without it: the console-api 15cdb241 situation in the reusable layout.
        pin = "0123456789abcdef0123456789abcdef01234567"
        vg = ".github/workflows/repo-guard-verify-gate.yml"

        def api(path):
            if path == f"repos/o/vws-deploy/contents/.github/workflows?ref={pin}":
                return [{"path": vg, "type": "file"}]
            if path == f"repos/o/vws-deploy/contents/{vg}?ref={pin}":
                return {"content": base64.b64encode(local(vg)).decode()}
            return None
        saved = dc.ref_issues
        dc.ref_issues = lambda files, home, full_copy: ([], pin)
        try:
            out = self.run_heads([("#9", "b" * 40, {VQ: local(VQ)})],
                                 fixture("vws-console-api-15cdb241-guard-agents.sh"), api=api)
        finally:
            dc.ref_issues = saved
        self.assertEqual(len(out), 1)
        self.assertIn("vws-deploy repo-guard-verify-gate.yml calls guard-agents.sh verify-gate", out[0])

    def test_unreadable_callee_fails_closed_and_is_not_cached_as_ok(self):
        # Codex round 1 [P2]: a failed listing at the pin must not become "no calls" for every repo on that pin.
        pin = "0123456789abcdef0123456789abcdef01234567"
        cache = {}
        for _ in range(2):
            issues = dc.bootstrap_for("o", {VQ: local(VQ)}, local("scripts/guard-agents.sh"), pin, cache,
                                      api=lambda path: None)
            self.assertEqual(issues, [f"cannot list the vws-deploy repo-guard-*.yml files at {pin[:12]}"])

    def test_unreadable_callee_body_fails_closed(self):
        vg = ".github/workflows/repo-guard-verify-gate.yml"
        issues = dc.bootstrap_issues({}, local("scripts/guard-agents.sh"), {vg: None})
        self.assertEqual(issues, ["cannot read vws-deploy repo-guard-verify-gate.yml"])

    def test_non_sha_pin_has_no_callee(self):
        for pin in (None, "local", "pending"):
            self.assertEqual(dc.bootstrap_for("o", {}, local("scripts/guard-agents.sh"), pin, {},
                                              api=lambda path: self.fail(path)), [])

    def test_unlistable_head_is_left_to_head_issues(self):
        self.assertEqual(self.run_heads([("#2", "c" * 40, None)], local("scripts/guard-agents.sh")), [])


if __name__ == "__main__":
    unittest.main()
