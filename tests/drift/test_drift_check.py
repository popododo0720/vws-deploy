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


class RequiredChecks(unittest.TestCase):
    def test_ruleset_contexts(self):
        rules = [
            {"type": "deletion"},
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": "pr-hygiene"}, {"context": "verify-gate"}]}},
        ]
        self.assertEqual(dc.rule_checks(rules), ["pr-hygiene", "verify-gate"])

    def test_bad_ruleset_shapes(self):
        self.assertEqual(dc.rule_checks(None), [])
        self.assertEqual(dc.rule_checks(["x", {"type": "required_status_checks", "parameters": None}]), [])
        self.assertEqual(dc.rule_checks([
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                None, {"context": ""}, {"context": "lint"}]}},
        ]), ["lint"])

    def test_classic_and_ruleset_union(self):
        rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": "vws-verify"}, {"context": "lint"}]}}]
        self.assertEqual(dc.effective_checks(["build", "lint"], rules), ["build", "lint", "vws-verify"])

    def test_each_common_check_is_required(self):
        self.assertEqual(dc.missing_checks(dc.COMMON_CHECKS, ""), [])
        self.assertEqual(dc.missing_checks(["pr-hygiene", "verify-gate"], ""), ["vws-verify"])
        self.assertEqual(dc.missing_checks(["pr-hygiene", "vws-verify", "verify-gate"], "lint test"), ["lint", "test"])


class RulesetDrift(unittest.TestCase):
    ACTIVE = {"id": 1, "name": "vws-main", "target": "branch", "enforcement": "active"}
    # GET /rulesets/{id} as a non-admin caller (github.token): no bypass_actors key at all.
    NON_ADMIN = {"id": 1, "name": "vws-main", "target": "branch", "enforcement": "active",
                 "current_user_can_bypass": "never", "rules": []}

    def test_active_ruleset_without_bypass(self):
        self.assertEqual(dc.ruleset_drift([self.ACTIVE], {1: {"bypass_actors": []}}), ([], []))

    def test_no_active_branch_ruleset(self):
        for rulesets in ([], [{**self.ACTIVE, "enforcement": "disabled"}],
                         [{**self.ACTIVE, "target": "tag"}]):
            self.assertEqual(dc.ruleset_drift(rulesets, {}), (["no active branch ruleset"], []))

    def test_other_ruleset_name(self):
        self.assertEqual(dc.ruleset_drift([{**self.ACTIVE, "name": "other"}], {1: {"bypass_actors": []}}),
                         (["no active vws-main ruleset"], []))

    def test_bypass_actor_is_drift(self):
        self.assertEqual(dc.ruleset_drift([self.ACTIVE], {1: {"bypass_actors": [{"actor_id": 5}]}}),
                         (["ruleset vws-main has 1 bypass actor(s)"], []))

    def test_unreadable_detail_is_drift(self):
        for detail in (None, [], "x"):
            self.assertEqual(dc.ruleset_drift([self.ACTIVE], {1: detail}), (["ruleset vws-main unreadable"], []))

    def test_non_admin_shape_is_unverified_not_ok(self):
        drift, unverified = dc.ruleset_drift([self.ACTIVE], {1: self.NON_ADMIN})
        self.assertEqual(drift, [])
        self.assertEqual(unverified, ["ruleset vws-main bypass actors unverified (needs admin token)"])

    def test_malformed_bypass_field_is_unverified(self):
        for value in (None, {}, "none", 0):
            _, unverified = dc.ruleset_drift([self.ACTIVE], {1: {"bypass_actors": value}})
            self.assertEqual(len(unverified), 1, value)


class MainRulesetCell(unittest.TestCase):
    """main() end to end with a stubbed gh: the non-admin ruleset detail must never print ruleset ok."""

    def run_main(self, detail, rules=None, extra_files=None):
        files = {".github/workflows/repo-guard.yml": b"x", ".github/workflows/direct-push-watch.yml": b"x",
                 ".github/workflows/verify-queue.yml": b"x",
                 "AGENTS.md": b"main is protected by the active vws-main ruleset", "QUALITY.md": b"q"}
        files.update(extra_files or {})
        files = {k: v for k, v in files.items() if v is not None}

        def gh(path):
            if path.endswith("/rulesets/1"):
                return detail
            if path.endswith("/rulesets"):
                return [{"id": 1, "name": "vws-main", "target": "branch", "enforcement": "active"}]
            if "/rules/branches/main" in path:
                return rules if rules is not None else MainRules.GOOD + [
                    {"type": "required_status_checks", "ruleset_id": 1, "parameters": {
                        "required_status_checks": [{"context": c} for c in dc.COMMON_CHECKS]}}]
            if "/labels/" in path:  # the incident label check (#454)
                return {"name": path.rsplit("/", 1)[1]}
            if path == "repos/o/r":
                return {"archived": False}
            return None

        def content(owner, repo, path):
            if path == "scripts/guard-agents.sh":
                with open(os.path.join(ROOT, "scripts", "guard-agents.sh"), "rb") as g:
                    return g.read()
            if path == ".github/ISSUE_TEMPLATE/incident.yml":
                with open(os.path.join(ROOT, path), "rb") as g:
                    return g.read()
            return files.get(path)

        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as f:
            f.write("r\tproto\t-\t\n")
        self.addCleanup(os.unlink, f.name)
        # The caller cell (#402) is not under test: r is a pending repo on the pinned full copy.
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as pend:
            pend.write("r\t%s\tfull copy\n" % FULL_SHA)
        self.addCleanup(os.unlink, pend.name)
        saved = (dc.gh, dc.content, dc.protection, dc.workflow_files, dc.sys.argv)
        dc.gh, dc.content = gh, content
        # The workflows cell reads the listing (#402); a None in extra_files removes the file from it too.
        dc.workflow_files = lambda o, r, ref=None, api=None: {
            k: v for k, v in tree().items() if (extra_files or {}).get(k, b"") is not None}
        dc.protection = lambda o, r: {"protected": True, "admins": True, "force": False, "delete": False,
                                      "checks": []}
        dc.sys.argv = ["drift_check.py", "--owner", "o", "--repos", f.name, "--caller-pending", pend.name,
                       "--no-open-prs"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = dc.main()
        finally:
            dc.gh, dc.content, dc.protection, dc.workflow_files, dc.sys.argv = saved
        row = [line for line in out.getvalue().splitlines() if line.startswith("| r |")][0]
        return rc, row.split(" | ")[2], out.getvalue(), row.split(" | ")

    def test_non_admin_detail_is_reported_unverified(self):
        rc, cell, text, _ = self.run_main(RulesetDrift.NON_ADMIN)
        self.assertEqual(rc, 0)
        self.assertEqual(cell, "ruleset vws-main bypass actors unverified (needs admin token)")
        self.assertIn("unverified: 1 repo(s)", text)

    def test_admin_detail_with_actor_is_drift(self):
        rc, cell, _, _ = self.run_main({**RulesetDrift.NON_ADMIN, "bypass_actors": [{"actor_id": 5}]})
        self.assertEqual(cell, "ruleset vws-main has 1 bypass actor(s)")
        self.assertEqual(rc, dc.DRIFT_EXIT)

    def test_admin_detail_without_actor_is_ok(self):
        _, cell, text, _ = self.run_main({**RulesetDrift.NON_ADMIN, "bypass_actors": []})
        self.assertEqual(cell, "ok")
        self.assertNotIn("unverified:", text)


    def test_missing_main_rule_is_drift(self):
        rules = [r for r in MainRules.GOOD if r["type"] != "non_fast_forward"]
        rc, cell, _, _ = self.run_main({**RulesetDrift.NON_ADMIN, "bypass_actors": []}, rules)
        self.assertEqual(cell, "vws-main has no non_fast_forward rule")
        self.assertEqual(rc, dc.DRIFT_EXIT)


    def test_missing_verify_queue_is_drift(self):
        # vws-deploy#460: a repo without verify-queue.yml never gets needs-verify, so Rule A never wakes vws-verify.
        rc, _, _, cells = self.run_main({**RulesetDrift.NON_ADMIN, "bypass_actors": []},
                                        extra_files={".github/workflows/verify-queue.yml": None})
        self.assertEqual(cells[4], "missing verify-queue.yml")
        self.assertEqual(rc, dc.DRIFT_EXIT)

    def test_every_workflow_present_is_ok(self):
        rc, _, _, cells = self.run_main({**RulesetDrift.NON_ADMIN, "bypass_actors": []})
        self.assertEqual(cells[4], "ok")
        self.assertEqual(rc, 0)

    def test_workflows_list_names_verify_queue(self):
        self.assertIn(".github/workflows/verify-queue.yml", dc.WORKFLOWS)


class MainRules(unittest.TestCase):
    """vws-main must apply to main with the rules AGENTS.md states (#455 verify FAIL)."""

    RULESETS = [RulesetDrift.ACTIVE]
    GOOD = [{"type": "pull_request", "ruleset_id": 1, "parameters": {"allowed_merge_methods": ["squash"]}},
            {"type": "non_fast_forward", "ruleset_id": 1},
            {"type": "deletion", "ruleset_id": 1}]

    def test_good_rules(self):
        self.assertEqual(dc.main_rules_drift(self.RULESETS, self.GOOD), [])

    def test_each_missing_rule_is_drift(self):
        for name in ("pull_request", "non_fast_forward", "deletion"):
            rules = [r for r in self.GOOD if r["type"] != name]
            self.assertEqual(dc.main_rules_drift(self.RULESETS, rules), [f"vws-main has no {name} rule"])

    def test_only_status_checks_left_is_drift(self):
        rules = [{"type": "required_status_checks", "ruleset_id": 1, "parameters": {}}]
        self.assertEqual(len(dc.main_rules_drift(self.RULESETS, rules)), 3)

    def test_not_squash_only_is_drift(self):
        for methods in (["squash", "merge"], ["merge"], [], None):
            rules = [{**self.GOOD[0], "parameters": {"allowed_merge_methods": methods}}] + self.GOOD[1:]
            self.assertEqual(dc.main_rules_drift(self.RULESETS, rules),
                             [f"vws-main allows merge methods {methods}, not squash only"])

    def test_vws_main_not_applying_to_main_is_drift(self):
        other = [{**r, "ruleset_id": 2} for r in self.GOOD]
        for rules in ([], None, other):
            self.assertEqual(dc.main_rules_drift(self.RULESETS, rules), ["vws-main does not apply to main"])

    def test_no_vws_main_is_left_to_ruleset_drift(self):
        self.assertEqual(dc.main_rules_drift([{**RulesetDrift.ACTIVE, "name": "other"}], []), [])


class StaleFacts(unittest.TestCase):
    def test_stale_phrases_are_pinned(self):
        self.assertEqual(dc.STALE_PHRASES, ("private, free plan", "no server-side protection",
                                            "not a required check yet", "detection, not blocking"))

    def test_each_stale_phrase_is_reported(self):
        for phrase in dc.STALE_PHRASES:
            self.assertEqual(dc.stale_phrases("before " + phrase + " after"),
                             [f"stale phrase: {phrase}"])

    def test_clean_text_passes(self):
        self.assertEqual(dc.stale_phrases("main is protected by the active vws-main ruleset"), [])

    def test_repo_agents_md_is_clean(self):
        with open(os.path.join(ROOT, "AGENTS.md"), "rb") as f:
            self.assertEqual(dc.stale_phrases(f.read().decode("utf-8")), [])


class ExitStatus(unittest.TestCase):
    def test_drift_is_not_a_crash_status(self):
        # 1 is what Python returns on a traceback; drift must not share it with a crash.
        self.assertEqual(dc.DRIFT_EXIT, 3)


class BypassAudit(unittest.TestCase):
    """--bypass-audit (vws-deploy#458): fails on any bypass actor and never passes a hidden bypass list."""

    ACTIVE = {"id": 1, "name": "vws-main", "target": "branch", "enforcement": "active"}

    def run_audit(self, detail, archived=False, rulesets=None):
        def gh(path):
            if path == "repos/o/r":
                return {"archived": archived}
            if path.endswith("/rulesets/1"):
                return detail
            if path.endswith("/rulesets"):
                return [self.ACTIVE] if rulesets is None else rulesets
            return None

        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as f:
            f.write("# header\nr\tgo\t.golangci.yml\tlint\n")
        self.addCleanup(os.unlink, f.name)
        saved = (dc.gh, dc.sys.argv)
        dc.gh = gh
        dc.sys.argv = ["drift_check.py", "--owner", "o", "--repos", f.name, "--bypass-audit"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = dc.main()
        finally:
            dc.gh, dc.sys.argv = saved
        return rc, out.getvalue()

    def test_no_actor_passes(self):
        rc, text = self.run_audit({"bypass_actors": []})
        self.assertEqual(rc, 0)
        self.assertIn("r: ok", text)
        self.assertIn("ruleset vws-main (id 1): 0 bypass actor(s)", text)

    def test_any_actor_fails_and_is_listed(self):
        actor = {"actor_type": "Integration", "actor_id": 15368, "bypass_mode": "always"}
        rc, text = self.run_audit({"bypass_actors": [actor]})
        self.assertEqual(rc, dc.DRIFT_EXIT)
        self.assertIn("r: FAIL ruleset vws-main has 1 bypass actor(s)", text)
        self.assertIn("actor_type=Integration actor_id=15368 bypass_mode=always", text)

    def test_hidden_bypass_list_fails(self):
        for detail in ({"name": "vws-main"}, {"bypass_actors": None}, {"bypass_actors": "x"}, None):
            rc, text = self.run_audit(detail)
            self.assertEqual(rc, dc.DRIFT_EXIT, detail)
            self.assertIn("r: FAIL", text)

    def test_missing_vws_main_fails(self):
        rc, text = self.run_audit({"bypass_actors": []}, rulesets=[])
        self.assertEqual(rc, dc.DRIFT_EXIT)
        self.assertIn("no active vws-main ruleset", text)

    def test_archived_repo_is_skipped(self):
        rc, text = self.run_audit(None, archived=True)
        self.assertEqual(rc, 0)
        self.assertIn("r: archived (skipped)", text)

class BypassAuditWrapper(unittest.TestCase):
    """.github/drift/audit-bypass-actors.sh with a stub gh: prints the account, never the token (#458)."""

    def test_wrapper_prints_account_not_token(self):
        import subprocess
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d))
        stub = os.path.join(d, "gh")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\ncase "$2" in\n user) echo stub-admin ;;\n repos/o/r) echo \'{"archived": true}\' ;;\n'
                    ' *) exit 1 ;;\nesac\n')
        os.chmod(stub, 0o755)
        repos = os.path.join(d, "repos.tsv")
        with open(repos, "w") as f:
            f.write("r\tgo\t-\t\n")
        env = {**os.environ, "PATH": d + os.pathsep + os.environ["PATH"], "GH_TOKEN": "secret-token-458"}
        r = subprocess.run([os.path.join(ROOT, ".github", "drift", "audit-bypass-actors.sh"), "--owner", "o",
                            "--repos", repos], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("as gh account stub-admin", r.stdout)
        self.assertIn("r: archived (skipped)", r.stdout)
        self.assertNotIn("secret-token-458", r.stdout + r.stderr)


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
VQ = ".github/workflows/verify-queue.yml"  # required since #476 (vws-deploy#460)
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
VQ_TEXT = b"name: verify-queue\non: pull_request\njobs:\n  label: {runs-on: ubuntu-latest, steps: [{run: x}]}\n"
BYPASS_SHELL = pinned().replace("permissions: {}\n", "permissions: {}\ndefaults: {run: {shell: \"bash -c 'exit 0' {0}\"}}\n", 1)
AT_MAIN = pinned().replace("@" + SHA, "@main")


def tree(rg=FULL, **more):
    """{path: bytes} of .github/workflows at one ref: repo-guard.yml (None = absent), direct-push-watch.yml,
    verify-queue.yml, more."""
    out = {DPW: DPW_TEXT, VQ: VQ_TEXT}
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

    def test_expression_in_a_job_name_is_flagged(self):
        # ${{ }} is never evaluated or parsed: a quoted }} once hid verify-gate from the check (#436).
        for name in ("\"${{ 'verify-gate' || '}}ignored' }}\"", "\"${{ 'pr-hygiene' || '}}x' }}\"",
                     "\"${{ format('verify-{0}', 'gate') }}\"", "\"${{ format('{0}-{1}', 'pr', 'hygiene') }}\"",
                     "\"${{ github.event.x || 'verify-gate' }}\"", "\"${{ inputs.n }}\"", "\"test (${{ matrix.go }})\""):
            text = "on: pull_request\njobs:\n  j:\n    name: %s\n    runs-on: ubuntu-latest\n    steps: [{run: 'true'}]\n" % name
            with self.subTest(name=name):
                out = self.issues(tree(q_yml=text))
                self.assertTrue(any("q.yml: job j name has an expression" in i for i in out), out)
                self.assertEqual(self.check_pending(tree(q_yml=text))[1], None, "no pending exemption")
        call = ("on: pull_request\njobs:\n  c:\n    name: \"${{ 'verify-gate' || '}}' }}\"\n"
                "    uses: ./.github/workflows/lib.yml\n")
        self.assertTrue(any("job c name has an expression" in i for i in self.issues(tree(q_yml=call))))

    def check_pending(self, files):
        return dc.ref_issues(files, home=False, full_copy=PENDING["vws-lib"])

    def test_ordinary_workflows_pass(self):
        ok = ("on: pull_request\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
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

    def test_a_listing_of_1000_entries_is_not_trusted(self):
        # The Contents API lists at most 1,000 entries, so a spoofing file past them would never be read (#436).
        spoof = b"on: pull_request\njobs:\n  pr-hygiene: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n"
        for n in (1000, 1001):
            entries = [{"type": "file", "path": ".github/workflows/f%04d.txt" % i} for i in range(n - 2)]
            entries += [{"type": "file", "path": DPW}, {"type": "file", "path": RG}]
            with self.subTest(n=n):
                got = dc.workflow_files("o", "r", api=self.api(entries, {DPW: DPW_TEXT, RG: FULL, ".github/workflows/z.yml": spoof}))
                self.assertIsNone(got)
                self.assertEqual(dc.ref_issues(got, home=False, full_copy=FULL_SHA), (["cannot list .github/workflows"], None))
        entries = [{"type": "file", "path": ".github/workflows/f%04d.txt" % i} for i in range(997)]
        entries += [{"type": "file", "path": DPW}, {"type": "file", "path": RG}]
        got = dc.workflow_files("o", "r", api=self.api(entries, {DPW: DPW_TEXT, RG: FULL}))
        self.assertEqual(got, {DPW: DPW_TEXT, RG: FULL}, "999 entries are a complete listing")

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
            # The ruleset and incident cells (#395, #454) are not under test: both are clean.
            if rest == "rulesets":
                return [{"id": 1, "name": "vws-main", "target": "branch", "enforcement": "active"}]
            if rest == "rulesets/1":
                return {**RulesetDrift.NON_ADMIN, "bypass_actors": []}
            if rest == "rules/branches/main":
                return MainRules.GOOD
            if rest.startswith("labels/"):
                return {"name": rest.rsplit("/", 1)[1]}
            if rest == "contents/.github/ISSUE_TEMPLATE/incident.yml":
                with open(os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "incident.yml"), "rb") as g:
                    return _b64(g.read())
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


DUAL = ("name: repo-guard\non:\n  pull_request:\n    types: [opened]\n  pull_request_target:\n    types: [opened]\n"
        "jobs:\n  pr-hygiene:\n    if: github.event_name == 'pull_request'\n    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n")


class DualEventWorkflows(unittest.TestCase):
    """vws-deploy#505: a workflow on both pull_request and pull_request_target reports each job once per
    event, so a job gated to one event leaves a skipped check run under the same name on the other.
    Rule D parked vws-conformance#32/#33 on required_skipped:pr-hygiene that way. Every repo, every file,
    pending full copies included."""

    def issues(self, files, pending=None):
        sha = hashlib.sha256(files[RG]).hexdigest() if pending is None else pending
        return dc.ref_issues(files, home=False, full_copy=sha)

    def test_pending_full_copy_with_both_events_is_flagged(self):
        issues, pin = self.issues(tree(rg=DUAL))
        self.assertIsNone(pin, "the full-copy exemption does not cover a dual-event file")
        self.assertTrue(any("repo-guard.yml runs on both pull_request and pull_request_target" in i for i in issues), issues)

    def test_every_trigger_form_is_flagged(self):
        for on in ("[pull_request, pull_request_target]", "{pull_request: null, pull_request_target: null}"):
            text = "on: %s\njobs:\n  test: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n" % on
            with self.subTest(on=on):
                out = self.issues(tree(ci_yml=text))[0]
                self.assertTrue(any("ci.yml runs on both" in i for i in out), out)

    def test_one_event_per_file_passes(self):
        target = "on: pull_request_target\njobs:\n  label: {runs-on: ubuntu-latest, steps: [{run: 'true'}]}\n"
        self.assertEqual(self.issues(tree(t_yml=target)), ([], "pending"))
        self.assertEqual(dc.dual_event_issues(tree()), [])

    def test_conformance_main_shape_is_flagged(self):
        # The trigger block of vws-conformance's repo-guard.yml on main (744911a), unchanged.
        text = ("name: repo-guard\n\non:\n  pull_request:\n    branches: [main]\n"
                "    types: [opened, edited, reopened, synchronize, labeled, unlabeled]\n"
                "  pull_request_target:\n    branches: [main]\n"
                "    types: [opened, edited, reopened, synchronize, labeled, unlabeled]\n\n"
                "jobs:\n  pr-hygiene:\n    if: github.event_name == 'pull_request'\n    runs-on: ubuntu-latest\n"
                "    steps: [{run: 'true'}]\n")
        self.assertEqual(dc.dual_event_issues(tree(rg=text)),
                         [".github/workflows/repo-guard.yml runs on both pull_request and pull_request_target: a job "
                          "gated to one event reports a skipped check under the same name on the other, and Rule D "
                          "never merges a skipped required check (vws-deploy#505)"])
